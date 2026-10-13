"""Single-job subprocess protocol. No public-API startup and no infinite polls."""

import asyncio
from contextlib import redirect_stdout
import json
import sys


def main() -> int:
    protocol_stdout = sys.stdout
    phase, code = "bootstrap", "import_failed"
    classify_worker_failure = None
    try:
        # All imports and legacy provider logging are redirected away from the
        # bounded machine protocol. Parent discards stderr rather than echo PII.
        with redirect_stdout(sys.stderr):
            from app.middleware.worker_auth import validate_worker_configuration
            from app.services.background_dispatch import (
                MAX_PROTOCOL_BYTES, classify_worker_failure, execute_once, job_enabled, validate_continuation,
            )
            phase, code = "config", "config_invalid"
            validate_worker_configuration()
            phase, code = "protocol", "input_invalid"
            if len(sys.argv) != 2 or not job_enabled(sys.argv[1]):
                raise ValueError("Unavailable background job")
            job = sys.argv[1]
            raw = sys.stdin.buffer.read(MAX_PROTOCOL_BYTES + 1)
            if len(raw) > MAX_PROTOCOL_BYTES:
                raise ValueError("Oversized worker input")
            continuation = validate_continuation(job, json.loads(raw))
            phase, code = "execute", "execution_failed"
            output = asyncio.run(execute_once(job, continuation))
            phase, code = "protocol", "output_invalid"
            result = json.dumps({"completed": True, "continuation": validate_continuation(job, output)})
            if len(result.encode()) > MAX_PROTOCOL_BYTES:
                raise ValueError("Oversized worker output")
        protocol_stdout.write(result)
        protocol_stdout.flush()
        return 0
    except BaseException as error:
        if phase == "execute" and classify_worker_failure is not None:
            code = classify_worker_failure(error, "execution_failed")
        # These values are assigned only from fixed literals above. Never inspect
        # or serialize the exception, its class, provider data or caller context.
        try:
            protocol_stdout.write(json.dumps({
                "completed": False, "failure": {"phase": phase, "code": code},
            }))
            protocol_stdout.flush()
        except BaseException:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
