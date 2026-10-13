"""Single-job subprocess protocol. No public-API startup and no infinite polls."""

import asyncio
from contextlib import redirect_stdout
import json
import sys


def main() -> int:
    protocol_stdout = sys.stdout
    try:
        # All imports and legacy provider logging are redirected away from the
        # bounded machine protocol. Parent discards stderr rather than echo PII.
        with redirect_stdout(sys.stderr):
            from app.middleware.worker_auth import validate_worker_configuration
            from app.services.background_dispatch import (
                MAX_PROTOCOL_BYTES, execute_once, job_enabled, validate_continuation,
            )
            validate_worker_configuration()
            if len(sys.argv) != 2 or not job_enabled(sys.argv[1]):
                raise ValueError("Unavailable background job")
            job = sys.argv[1]
            raw = sys.stdin.buffer.read(MAX_PROTOCOL_BYTES + 1)
            if len(raw) > MAX_PROTOCOL_BYTES:
                raise ValueError("Oversized worker input")
            continuation = validate_continuation(job, json.loads(raw))
            output = asyncio.run(execute_once(job, continuation))
            result = json.dumps({"completed": True, "continuation": validate_continuation(job, output)})
            if len(result.encode()) > MAX_PROTOCOL_BYTES:
                raise ValueError("Oversized worker output")
        protocol_stdout.write(result)
        protocol_stdout.flush()
        return 0
    except BaseException:
        # Exit status alone communicates failure; tokens/provider payloads never
        # become response bodies or unbounded parent-memory capture.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
