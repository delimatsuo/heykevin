"""Owner sole-signature CLI for bakeoff provider-approval envelopes.

Run this yourself, on your own machine, to sign an approval payload with
your own personal Ed25519 key. This script never contacts a network, never
reads a provider credential, and never itself authorizes anything — it
produces a detached signature you then attach to an approval envelope.

Usage:
    python scripts/sign_voice_bakeoff_approval.py \\
        --key ~/.config/hey-kevin/bakeoff_owner_key.pem \\
        --payload /path/to/approval_payload.json \\
        --domain-name approval
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import stat
import sys

from cryptography.hazmat.primitives.asymmetric import ed25519

from app.services.voice_bakeoff_security_contracts import APPROVAL_DOMAIN

# Symbolic names for the domain-separation constants a sole-owner signer
# may sign under, mapped to the real byte strings in
# app/services/voice_bakeoff_security_contracts.py.
#
# Free-text --domain was removed on purpose: the real domain constants are
# NUL-terminated (e.g. b"hey-kevin/voice-bakeoff/approval/v1\x00"), and a NUL
# byte cannot survive as a process argv element, so free text could never
# reproduce the exact domain OfflineApprovalVerifier.verify() checks against.
#
# Only "approval" is exposed: the module's other domains (provenance,
# trust-snapshot-root, preauth-grant, preauth-ack, control-proof,
# custody-lock-attestation) belong to internal system authorities, not
# the external owner role this CLI signs for.
_DOMAIN_NAME_TO_BYTES: dict[str, bytes] = {
    "approval": APPROVAL_DOMAIN,
}


def load_owner_key(
    key_path: pathlib.Path, *, create: bool
) -> ed25519.Ed25519PrivateKey:
    if key_path.exists():
        _require_owner_only_permissions(key_path)
        raw = key_path.read_bytes()
        return ed25519.Ed25519PrivateKey.from_private_bytes(raw)

    if not create:
        raise FileNotFoundError(
            f"owner key not found at {key_path}; pass --create-key to mint a "
            "new keypair. A missing key file must fail loudly — a mistyped "
            "--key path must not silently sign under a fresh identity."
        )

    key_path.parent.mkdir(parents=True, exist_ok=True)
    private_key = ed25519.Ed25519PrivateKey.generate()
    raw = private_key.private_bytes_raw()
    # Bake the restrictive mode into the creation syscall itself so the file
    # never exists, even momentarily, at the default umask-widened mode.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
    return private_key


def _require_owner_only_permissions(key_path: pathlib.Path) -> None:
    mode = stat.S_IMODE(key_path.stat().st_mode)
    if mode != 0o600:
        raise PermissionError(
            f"refusing to load owner key at {key_path}: expected mode 0o600, "
            f"found {oct(mode)} — private key permissions have been loosened"
        )


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
    parser.add_argument(
        "--domain-name",
        required=True,
        choices=sorted(_DOMAIN_NAME_TO_BYTES),
        help=(
            "Symbolic name of the domain-separation constant to sign under; "
            "maps to the real bytes in "
            "app/services/voice_bakeoff_security_contracts.py. Free-text "
            "domains are not accepted — the real domain constants embed a "
            "NUL byte, which cannot be passed as a process argv argument."
        ),
    )
    parser.add_argument(
        "--create-key",
        action="store_true",
        help=(
            "Explicitly allow minting a new Ed25519 keypair at --key when no "
            "file exists there. Without this flag a missing key file is an "
            "error, so a typo'd path cannot create a fresh signing identity."
        ),
    )
    args = parser.parse_args(argv)

    try:
        private_key = load_owner_key(args.key, create=args.create_key)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    payload = json.loads(args.payload.read_text())
    domain = _DOMAIN_NAME_TO_BYTES[args.domain_name]
    signature = sign_payload(private_key, domain=domain, payload=payload)

    print(signature.hex())
    return 0


if __name__ == "__main__":
    sys.exit(main())
