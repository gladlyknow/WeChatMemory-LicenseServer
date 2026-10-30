from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .app import create_app
from .signing import (
    SigningKeyError,
    TicketSigner,
    inspect_private_key,
    load_public_key_manifest,
    manifest_for_signer,
    publish_public_key,
    public_record,
    verify_private_key,
    write_new_public_key_manifest,
)


DEFAULT_MANIFEST = (
    Path(__file__).resolve().parents[1] / "config" / "signing-public-keys.json"
)


def _key_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--path", type=Path, required=True)
    command.add_argument("--key-id", required=True)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="WeChatMemory license server")
    root.add_argument("--host", default="127.0.0.1")
    root.add_argument("--port", type=int, default=9100)
    root.add_argument("--threads", type=int, default=6)
    commands = root.add_subparsers(dest="command")

    # Backwards-compatible alias.  It remains fail-closed when the file exists.
    generate = commands.add_parser("generate-key")
    generate.add_argument("--output", type=Path, required=True)
    generate.add_argument("--key-id", required=True)

    signing = commands.add_parser("signing-key", help="manage local Ed25519 keys")
    signing_commands = signing.add_subparsers(dest="signing_command", required=True)
    ensure = signing_commands.add_parser(
        "ensure", help="atomically create only when the path does not exist"
    )
    _key_arguments(ensure)
    ensure.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    ensure.add_argument(
        "--publish",
        action="store_true",
        help="atomically add the public key to --manifest",
    )
    ensure.add_argument(
        "--activate",
        action="store_true",
        help="publish and explicitly make this the sole active signing key",
    )
    inspect = signing_commands.add_parser(
        "inspect", help="print public metadata for one private key"
    )
    _key_arguments(inspect)
    verify = signing_commands.add_parser(
        "verify", help="verify a private key against the tracked public manifest"
    )
    _key_arguments(verify)
    verify.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    manifest = signing_commands.add_parser(
        "manifest", help="validate and print the public manifest"
    )
    manifest.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    create_manifest = signing_commands.add_parser(
        "create-manifest", help="create a new one-key public manifest without overwriting"
    )
    _key_arguments(create_manifest)
    create_manifest.add_argument("--output", type=Path, required=True)
    publish = signing_commands.add_parser(
        "publish", help="publish an existing private key to the public manifest"
    )
    _key_arguments(publish)
    publish.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    publish.add_argument("--activate", action="store_true")
    return root


def _print(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "generate-key":
            signer = TicketSigner.generate(args.output, args.key_id)
            _print(public_record(signer))
            return 0
        if args.command == "signing-key":
            if args.signing_command == "ensure":
                should_publish = args.publish or args.activate
                if (
                    not args.path.expanduser().exists()
                    and args.manifest.expanduser().exists()
                    and any(
                        item["key_id"] == args.key_id
                        for item in load_public_key_manifest(args.manifest)["keys"]
                    )
                ):
                    raise SigningKeyError(
                        "manifest already contains this key id but the private key is "
                        "missing; restore its backup or choose a new key id"
                    )
                signer, created = TicketSigner.ensure(args.path, args.key_id)
                result = public_record(signer)
                result["created"] = created
                if should_publish:
                    manifest, published = publish_public_key(
                        args.manifest, signer, activate=args.activate
                    )
                    result["published"] = published
                    result["active"] = manifest["active_key_id"] == signer.key_id
                _print(result)
            elif args.signing_command == "inspect":
                _print(inspect_private_key(args.path, args.key_id))
            elif args.signing_command == "verify":
                _print(
                    verify_private_key(
                        args.path, args.key_id, args.manifest, require_active=True
                    )
                )
            elif args.signing_command == "manifest":
                _print(load_public_key_manifest(args.manifest))
            elif args.signing_command == "create-manifest":
                signer = TicketSigner.load(args.path, args.key_id)
                write_new_public_key_manifest(args.output, signer)
                _print(manifest_for_signer(signer))
            else:
                signer = TicketSigner.load(args.path, args.key_id)
                manifest, published = publish_public_key(
                    args.manifest, signer, activate=args.activate
                )
                manifest["published"] = published
                _print(manifest)
            return 0
    except (SigningKeyError, OSError, FileExistsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    from waitress import serve

    serve(create_app(), host=args.host, port=args.port, threads=args.threads)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
