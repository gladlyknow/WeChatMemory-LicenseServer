from __future__ import annotations

import argparse
import json
from pathlib import Path

from .app import create_app
from .signing import TicketSigner


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="WeChatMemory license server")
    root.add_argument("--host", default="127.0.0.1")
    root.add_argument("--port", type=int, default=9100)
    root.add_argument("--threads", type=int, default=6)
    commands = root.add_subparsers(dest="command")
    generate = commands.add_parser("generate-key")
    generate.add_argument("--output", type=Path, required=True)
    generate.add_argument("--key-id", required=True)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "generate-key":
        signer = TicketSigner.generate(args.output, args.key_id)
        print(
            json.dumps(
                {"key_id": signer.key_id, "public_key": signer.public_key_b64()},
                separators=(",", ":"),
            )
        )
        return 0
    from waitress import serve

    serve(create_app(), host=args.host, port=args.port, threads=args.threads)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
