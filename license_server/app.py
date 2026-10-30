from __future__ import annotations

import os
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path
from threading import Lock
from typing import Any

from flask import Flask, Response, jsonify, request

from . import __version__
from .database import Database
from .service import LicenseError, LicenseManager, LicenseSettings, require_text
from .signing import TicketSigner, manifest_for_signer


class SlidingWindowLimiter:
    def __init__(self, limit: int = 20, window_seconds: int = 60):
        self.limit = limit
        self.window_seconds = window_seconds
        self.values: dict[str, deque[float]] = defaultdict(deque)
        self.lock = Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            values = self.values[key]
            while values and values[0] <= now - self.window_seconds:
                values.popleft()
            if len(values) >= self.limit:
                return False
            values.append(now)
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
    manager = LicenseManager(
        database,
        signer,
        LicenseSettings(
            issuer=os.environ.get("LICENSE_ISSUER", "wechat-memory-license"),
            audience=os.environ.get("LICENSE_AUDIENCE", "wechat-memory-client"),
            refresh_days=int(os.environ.get("REFRESH_TOKEN_DAYS", "30")),
            ticket_hours=int(os.environ.get("LICENSE_TICKET_HOURS", "24")),
        ),
    )
    limiter = SlidingWindowLimiter(
        int(os.environ.get("RATE_LIMIT_PER_MINUTE", "20")), 60
    )
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
    app.extensions["license_database"] = database
    app.extensions["license_manager"] = manager

    @app.before_request
    def protect_api() -> Response | None:
        if request.path in {"/healthz", "/readyz", "/v1/time", "/v1/keys"}:
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
        return jsonify({"status": "ready" if database.ready() else "not_ready"}), (
            200 if database.ready() else 503
        )

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
