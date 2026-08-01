"""Owner sole-signature CLI for bakeoff provider-approval envelopes.

Run this yourself, on your own machine, to sign an approval payload with
your own personal Ed25519 key. This script never contacts a network, never
reads a provider credential, and never itself authorizes anything — it
produces a detached signature you then attach to an approval envelope.

Usage:
    python scripts/sign_voice_bakeoff_approval.py \\
        --key ~/.config/hey-kevin/bakeoff_owner_key.pem \\
        --payload /path/to/approval_payload.json \\
        --domain "hey-kevin/bakeoff/owner-signature/v1"
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from cryptography.hazmat.primitives.asymmetric import ed25519


def load_or_create_owner_key(key_path: pathlib.Path) -> ed25519.Ed25519PrivateKey:
    if key_path.exists():
        raw = key_path.read_bytes()
        return ed25519.Ed25519PrivateKey.from_private_bytes(raw)

    key_path.parent.mkdir(parents=True, exist_ok=True)
    private_key = ed25519.Ed25519PrivateKey.generate()
    raw = private_key.private_bytes_raw()
    key_path.write_bytes(raw)
    key_path.chmod(0o600)
    return private_key


def sign_payload(
    private_key: ed25519.Ed25519PrivateKey,
    *,
    domain: bytes,
    payload: dict,
) -> bytes:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return private_key.sign(domain + canonical)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", required=True, type=pathlib.Path)
    parser.add_argument("--payload", required=True, type=pathlib.Path)
    parser.add_argument("--domain", required=True)
    args = parser.parse_args(argv)

    private_key = load_or_create_owner_key(args.key)
    payload = json.loads(args.payload.read_text())
    signature = sign_payload(private_key, domain=args.domain.encode("utf-8"), payload=payload)

    print(signature.hex())
    return 0


if __name__ == "__main__":
    sys.exit(main())
