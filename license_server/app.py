from __future__ import annotations

import os
import secrets
import time
from collections import deque
from ipaddress import ip_address
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from flask import Flask, Response, jsonify, request

from . import __version__
from .database import Database
from .service import LicenseError, LicenseManager, LicenseSettings, require_text
from .signing import TicketSigner, manifest_for_signer


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < minimum or value > maximum:
        raise RuntimeError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return value


class SlidingWindowLimiter:
    """A bounded, process-local sliding-window limiter.

    Both the per-key and process-wide windows are protected by the same lock so
    a burst handled by multiple Waitress threads cannot exceed either limit.
    Unknown keys are rejected when the bounded key table is full; this avoids
    turning spoofed client addresses into an unbounded memory allocation.
    """

    def __init__(
        self,
        limit: int = 20,
        window_seconds: int = 60,
        *,
        global_limit: int = 1000,
        max_keys: int = 4096,
        clock: Callable[[], float] = time.monotonic,
    ):
        if limit <= 0 or window_seconds <= 0 or global_limit <= 0 or max_keys <= 0:
            raise ValueError("rate limiter limits must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self.global_limit = global_limit
        self.max_keys = max_keys
        self.values: dict[str, deque[float]] = {}
        self.global_values: deque[float] = deque()
        self.lock = Lock()
        self._clock = clock
        self._sweep_interval = min(float(window_seconds), 10.0)
        self._next_sweep = clock() + self._sweep_interval

    @staticmethod
    def _expire(values: deque[float], cutoff: float) -> None:
        while values and values[0] <= cutoff:
            values.popleft()

    def _sweep(self, cutoff: float, now: float) -> None:
        for key, values in list(self.values.items()):
            self._expire(values, cutoff)
            if not values:
                del self.values[key]
        self._next_sweep = now + self._sweep_interval

    def allow(self, key: str) -> bool:
        now = self._clock()
        cutoff = now - self.window_seconds
        with self.lock:
            self._expire(self.global_values, cutoff)
            if now >= self._next_sweep:
                self._sweep(cutoff, now)
            if len(self.global_values) >= self.global_limit:
                return False

            values = self.values.get(key)
            if values is not None:
                self._expire(values, cutoff)
                if not values:
                    del self.values[key]
                    values = None
            if values is None:
                if len(self.values) >= self.max_keys:
                    return False
                values = deque()
                self.values[key] = values
            if len(values) >= self.limit:
                return False
            values.append(now)
            self.global_values.append(now)
            return True


def create_app(
    *, database_path: Path | None = None, signer: TicketSigner | None = None
) -> Flask:
    database_path = database_path or Path(
        os.environ.get("DATABASE_PATH", "/var/lib/wechat-memory-license/license.sqlite3")
    )
    if signer is None:
        key_path = os.environ.get("SIGNING_PRIVATE_KEY_PATH", "")
        key_id = os.environ.get("SIGNING_KEY_ID", "")
        manifest_path = os.environ.get("SIGNING_PUBLIC_KEYS_PATH", "")
        if not key_path or not key_id or not manifest_path:
            raise RuntimeError(
                "SIGNING_PRIVATE_KEY_PATH, SIGNING_KEY_ID and "
                "SIGNING_PUBLIC_KEYS_PATH are required"
            )
        signer, public_manifest = TicketSigner.load_verified(
            Path(key_path), key_id, Path(manifest_path)
        )
    else:
        public_manifest = manifest_for_signer(signer)
    database = Database(database_path)
    refresh_days = _env_int(
        "REFRESH_TOKEN_DAYS", 365, minimum=1, maximum=3650
    )
    ticket_hours = _env_int(
        "LICENSE_TICKET_HOURS", 24, minimum=1, maximum=720
    )
    per_source_limit = _env_int(
        "RATE_LIMIT_PER_MINUTE", 20, minimum=1, maximum=10000
    )
    global_limit = _env_int(
        "RATE_LIMIT_GLOBAL_PER_MINUTE", 1000, minimum=1, maximum=1000000
    )
    max_rate_limit_keys = _env_int(
        "RATE_LIMIT_MAX_KEYS", 4096, minimum=1, maximum=100000
    )
    if global_limit < per_source_limit:
        raise RuntimeError(
            "RATE_LIMIT_GLOBAL_PER_MINUTE must be greater than or equal to "
            "RATE_LIMIT_PER_MINUTE"
        )

    manager = LicenseManager(
        database,
        signer,
        LicenseSettings(
            issuer=os.environ.get("LICENSE_ISSUER", "wechat-memory-license"),
            audience=os.environ.get("LICENSE_AUDIENCE", "wechat-memory-client"),
            refresh_days=refresh_days,
            ticket_hours=ticket_hours,
        ),
    )
    limiter = SlidingWindowLimiter(
        per_source_limit,
        60,
        global_limit=global_limit,
        max_keys=max_rate_limit_keys,
    )
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
    app.extensions["license_database"] = database
    app.extensions["license_manager"] = manager
    app.extensions["rate_limiter"] = limiter

    @app.before_request
    def protect_api() -> Response | None:
        if request.path in {"/healthz", "/readyz/deep"}:
            return None
        forwarded = request.headers.get("X-Forwarded-For", "")
        remote = forwarded.split(",", 1)[0].strip() if forwarded else request.remote_addr or "unknown"
        if not limiter.allow(remote[:128]):
            return jsonify({"error": "rate_limited"}), 429
        if request.method == "POST" and not request.is_json:
            return jsonify({"error": "json_required"}), 415
        return None

    @app.after_request
    def headers(response: Response) -> Response:
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'none'"
        response.headers.pop("Server", None)
        return response

    @app.errorhandler(LicenseError)
    def license_error(error: LicenseError):
        return jsonify({"error": error.public_code}), error.status

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify({"error": "request_too_large"}), 413

    @app.get("/healthz")
    def health() -> Response:
        return jsonify({"status": "ok"})

    @app.get("/readyz")
    def ready() -> Response:
        is_ready = database.ready()
        return jsonify({"status": "ready" if is_ready else "not_ready"}), (
            200 if is_ready else 503
        )

    @app.get("/readyz/deep")
    def deep_ready() -> Response:
        # The production reverse proxy always supplies X-Forwarded-For.  Keep
        # the potentially expensive integrity check available to local deploy
        # and operations scripts without exposing it through the public proxy.
        remote = request.remote_addr or ""
        try:
            direct_loopback = ip_address(remote).is_loopback
        except ValueError:
            direct_loopback = False
        if request.headers.get("X-Forwarded-For") or not direct_loopback:
            return jsonify({"error": "not_found"}), 404
        is_ready = database.deep_ready()
        return jsonify(
            {"status": "ready" if is_ready else "not_ready", "check": "deep"}
        ), (200 if is_ready else 503)

    @app.get("/v1/time")
    def server_time() -> Response:
        from .service import iso, utc_now

        return jsonify({"server_time": iso(utc_now())})

    @app.get("/v1/keys")
    def keys() -> Response:
        return jsonify(public_manifest)

    @app.post("/v1/activate")
    def activate() -> Response:
        return jsonify(manager.activate(_json()))

    @app.post("/v1/licenses/refresh")
    def refresh() -> Response:
        return jsonify(manager.refresh(_json()))

    @app.get("/v1/licenses/status")
    def status() -> Response:
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            raise LicenseError("refresh_failed", status=401)
        return jsonify(manager.status(authorization[7:].strip()))

    @app.post("/v1/devices/deactivate")
    def deactivate() -> Response:
        return jsonify(manager.deactivate(_json()))

    return app


def _json() -> dict[str, Any]:
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise LicenseError("invalid_request")
    return value
