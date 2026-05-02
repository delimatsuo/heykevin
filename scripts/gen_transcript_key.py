#!/usr/bin/env python3
"""Generate a fresh AES-256-GCM key for transcript encryption (F-11).

Usage:
    python scripts/gen_transcript_key.py

The script prints a base64-encoded 32-byte key to stdout. Set the value as
the `TRANSCRIPT_ENCRYPTION_KEY` environment variable on Cloud Run (or in
Secret Manager). Once set, new call transcripts written by the backend
are encrypted with AES-256-GCM before being stored in Firestore. Reads
remain backwards compatible with legacy plaintext records.

Rotating the key requires re-encrypting historical records — out of scope
for this generator. Save the existing key before generating a new one if
preserving access to old transcripts matters.
"""

import base64
import os
import sys


def main() -> int:
    key = os.urandom(32)
    sys.stdout.write(base64.b64encode(key).decode("ascii"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
