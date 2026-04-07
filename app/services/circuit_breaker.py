"""Circuit breaker — detects error spikes and switches to safe mode.

When error rate exceeds threshold, all calls route to voicemail instead of
direct forwarding. This prevents induced-failure attacks from bypassing screening.
"""

import time
from collections import deque

from app.utils.logging import get_logger

logger = get_logger(__name__)

# Track errors in a sliding window
_error_timestamps: deque = deque()
_WINDOW_SECONDS = 60
_ERROR_THRESHOLD = 5  # errors within window to trigger circuit breaker
_circuit_open_until = 0.0  # timestamp when circuit breaker resets
_COOLDOWN_SECONDS = 120  # how long circuit stays open


def record_error():
    """Record an error occurrence."""
    now = time.time()
    _error_timestamps.append(now)
    _cleanup_old_errors(now)

    if len(_error_timestamps) >= _ERROR_THRESHOLD:
        _open_circuit(now)


def is_circuit_open() -> bool:
    """Check if the circuit breaker is currently tripped."""
    if time.time() < _circuit_open_until:
        return True
    return False


def _open_circuit(now: float):
    """Trip the circuit breaker."""
    global _circuit_open_until
    _circuit_open_until = now + _COOLDOWN_SECONDS
    logger.error(
        f"CIRCUIT BREAKER OPEN — {len(_error_timestamps)} errors in {_WINDOW_SECONDS}s. "
        f"All calls routing to voicemail for {_COOLDOWN_SECONDS}s."
    )


def _cleanup_old_errors(now: float):
    """Remove errors outside the sliding window."""
    cutoff = now - _WINDOW_SECONDS
    while _error_timestamps and _error_timestamps[0] < cutoff:
        _error_timestamps.popleft()
