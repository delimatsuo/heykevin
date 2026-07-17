"""Bounded staging diagnostics for retrospective Gemini caller turns."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
import hmac
import re
import secrets
import time
from typing import Any

from app.config import settings
from app.services.caller_turns import CallerTurnAssembler, CallerTurnEventKind
from app.services.gemini_turn_events import GeminiTurnEventAdapter
from app.utils.logging import get_logger


logger = get_logger(__name__)

_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")
_SUPPORTED_LIFECYCLE_KINDS = frozenset(
    {
        CallerTurnEventKind.CONNECTION_CLOSED,
        CallerTurnEventKind.RECONNECT_STARTED,
    }
)


def compute_caller_hmac_digest(caller_identifier: str, key: str) -> str:
    """Return the operator allowlist digest for one staging test caller."""
    if not isinstance(caller_identifier, str) or not caller_identifier.strip():
        raise ValueError("caller identifier is required")
    if not isinstance(key, str) or len(key.encode("utf-8")) < 32:
        raise ValueError("caller HMAC key must be at least 32 bytes")
    return hmac.new(
        key.encode("utf-8"),
        caller_identifier.strip().encode("utf-8"),
        sha256,
    ).hexdigest()


def _is_authorized(
    *,
    contractor_config: dict[str, Any],
    caller_identifier: str,
    now: float,
) -> bool:
    if (settings.environment or "").strip().lower() != "staging":
        return False
    if settings.receptionist_observation_shadow_enabled is not True:
        return False
    if contractor_config.get("receptionist_observation_shadow_enabled") is not True:
        return False

    expires_at = contractor_config.get("receptionist_observation_shadow_expires_at")
    max_seconds = settings.receptionist_observation_shadow_max_authorization_seconds
    if (
        isinstance(expires_at, bool)
        or not isinstance(expires_at, int)
        or isinstance(max_seconds, bool)
        or not isinstance(max_seconds, int)
        or max_seconds < 60
        or max_seconds > 86_400
        or expires_at <= now
        or expires_at - now > max_seconds
    ):
        return False

    key = settings.receptionist_observation_shadow_caller_hmac_key
    digests = contractor_config.get(
        "receptionist_observation_shadow_caller_digests"
    )
    if (
        not isinstance(key, str)
        or len(key.encode("utf-8")) < 32
        or not isinstance(digests, list)
        or not 1 <= len(digests) <= 16
        or any(
            not isinstance(digest, str)
            or _DIGEST_PATTERN.fullmatch(digest) is None
            for digest in digests
        )
    ):
        return False
    try:
        actual = compute_caller_hmac_digest(caller_identifier, key)
    except ValueError:
        return False
    return any(hmac.compare_digest(actual, digest) for digest in digests)


def build_receptionist_observation_shadow(
    *,
    contractor_config: dict[str, Any],
    caller_identifier: str,
    now: float | None = None,
) -> "ReceptionistObservationShadow | None":
    """Build one diagnostic only for an exact short-lived staging authorization."""
    observed_now = time.time() if now is None else now
    if not _is_authorized(
        contractor_config=contractor_config,
        caller_identifier=caller_identifier,
        now=observed_now,
    ):
        return None
    shadow = ReceptionistObservationShadow()
    expires_at = contractor_config["receptionist_observation_shadow_expires_at"]
    logger.info(
        "voice_event event=observation_shadow_initialized shadow=%s ttl_seconds=%s",
        shadow.shadow_id,
        max(0, int(expires_at - observed_now)),
    )
    return shadow


@dataclass(frozen=True, slots=True)
class _QueueItem:
    kind: str
    value: object
    at_ms: int
    enqueued_at: float


class ReceptionistObservationShadow:
    """Reduce diagnostic events off the live receive task without side effects."""

    def __init__(
        self,
        *,
        queue_size: int = 16,
        quiescence_ms: int = 250,
        shadow_id: str | None = None,
    ) -> None:
        if isinstance(queue_size, bool) or not isinstance(queue_size, int) or queue_size <= 0:
            raise ValueError("queue_size must be a positive integer")
        self.shadow_id = shadow_id or secrets.token_hex(4)
        self._queue: asyncio.Queue[_QueueItem] = asyncio.Queue(maxsize=queue_size)
        self._adapter = GeminiTurnEventAdapter()
        self._assembler = CallerTurnAssembler(
            active_epoch=0,
            quiescence_ms=quiescence_ms,
        )
        self._started_at = time.monotonic()
        self._next_sequence = 1
        self._epoch = 0
        self._model_output_enqueued = False
        self._model_output_active = False
        self._worker_task: asyncio.Task[None] | None = None
        self._closed = False
        self.enqueued_item_count = 0
        self.dropped_item_count = 0
        self.ignored_message_count = 0
        self.worker_error_count = 0
        self.emitted_turn_count = 0
        self.max_queue_depth = 0

    @property
    def worker_task(self) -> asyncio.Task[None] | None:
        return self._worker_task

    def _elapsed_ms(self) -> int:
        return max(0, int((time.monotonic() - self._started_at) * 1000))

    def _ensure_worker(self) -> bool:
        if self._closed:
            return False
        if self._worker_task and not self._worker_task.done():
            return True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._worker_task = loop.create_task(self._run())
        return True

    def try_enqueue_message(self, message: object) -> bool:
        """Queue a payload-minimized event projection without awaiting."""
        projected = self._project_turn_message(message)
        if projected is None:
            self.ignored_message_count += 1
            return True
        return self._try_enqueue("message", projected)

    def _project_turn_message(self, message: object) -> object | None:
        """Keep caller transcript and event markers, never model/tool payloads."""
        if not isinstance(message, dict):
            return []
        projected: dict[str, object] = {}

        for field in ("inputTranscript", "inputTranscription"):
            if field in message:
                projected[field] = self._project_transcription(message[field])

        content = message.get("serverContent")
        if content is not None and not isinstance(content, dict):
            projected["serverContent"] = None
        if isinstance(content, dict):
            projected_content: dict[str, object] = {}
            for field in ("inputTranscript", "inputTranscription"):
                if field in content:
                    projected_content[field] = self._project_transcription(
                        content[field]
                    )
            for field in ("generationComplete", "turnComplete", "interrupted"):
                if field in content:
                    projected_content[field] = content[field]
            if "modelTurn" in content:
                model_turn = content["modelTurn"]
                if not isinstance(model_turn, dict):
                    projected_content["modelTurn"] = None
                else:
                    parts = model_turn.get("parts", [])
                    if not isinstance(parts, list):
                        projected_content["modelTurn"] = {"parts": None}
                    elif parts and not self._model_output_enqueued:
                        projected_content["modelTurn"] = {"parts": [{}]}
                        self._model_output_enqueued = True

            if content.get("turnComplete") is True or content.get("interrupted") is True:
                self._model_output_enqueued = False
            if projected_content:
                projected["serverContent"] = projected_content

        if "toolCall" in message:
            tool_call = message["toolCall"]
            if not isinstance(tool_call, dict):
                projected["toolCall"] = None
            else:
                function_calls = tool_call.get("functionCalls", [])
                projected["toolCall"] = {
                    "functionCalls": (
                        ([{}] if function_calls else [])
                        if isinstance(function_calls, list)
                        else None
                    )
                }

        if "toolCallCancellation" in message:
            cancellation = message["toolCallCancellation"]
            if not isinstance(cancellation, dict):
                projected["toolCallCancellation"] = None
            else:
                ids = cancellation.get("ids", [])
                if not isinstance(ids, list) or len(ids) > 128:
                    projected_ids: object = None
                elif any(not isinstance(value, str) for value in ids):
                    projected_ids = [None]
                else:
                    projected_ids = ["redacted"] if ids else []
                projected["toolCallCancellation"] = {"ids": projected_ids}

        return projected or None

    @staticmethod
    def _project_transcription(value: object) -> object:
        if isinstance(value, dict):
            return {"text": value.get("text", "")}
        return value

    def try_enqueue_lifecycle(self, kind: CallerTurnEventKind | str) -> bool:
        """Queue one supported local lifecycle event without blocking."""
        try:
            event_kind = CallerTurnEventKind(kind)
        except (TypeError, ValueError):
            return False
        if event_kind not in _SUPPORTED_LIFECYCLE_KINDS:
            return False
        self._model_output_enqueued = False
        return self._try_enqueue("lifecycle", event_kind)

    def _try_enqueue(self, kind: str, value: object) -> bool:
        if self._closed or not self._ensure_worker():
            return False
        item = _QueueItem(
            kind=kind,
            value=value,
            at_ms=self._elapsed_ms(),
            enqueued_at=time.monotonic(),
        )
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self.dropped_item_count += 1
            logger.warning(
                "voice_event event=observation_shadow_queue_drop shadow=%s "
                "dropped=%s queue_depth=%s",
                self.shadow_id,
                self.dropped_item_count,
                self._queue.qsize(),
            )
            return False
        self.enqueued_item_count += 1
        self.max_queue_depth = max(self.max_queue_depth, self._queue.qsize())
        return True

    def abort(self) -> None:
        """Synchronously disable diagnostic work after a live-boundary failure."""
        self._closed = True
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()

    async def stop(self, *, timeout_seconds: float = 0.1) -> None:
        """Flush queued diagnostics within a short bound, then stop the worker."""
        if self._closed:
            return
        self._closed = True
        task = self._worker_task
        if task is None:
            return

        deadline = time.monotonic() + max(0.01, timeout_seconds)
        for item in (
            _QueueItem(
                kind="lifecycle",
                value=CallerTurnEventKind.PIPELINE_STOPPED,
                at_ms=self._elapsed_ms(),
                enqueued_at=time.monotonic(),
            ),
            _QueueItem(
                kind="stop",
                value=None,
                at_ms=self._elapsed_ms(),
                enqueued_at=time.monotonic(),
            ),
        ):
            remaining = max(0.001, deadline - time.monotonic())
            try:
                await asyncio.wait_for(self._queue.put(item), timeout=remaining)
            except asyncio.TimeoutError:
                break

        remaining = max(0.001, deadline - time.monotonic())
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        try:
            while True:
                timeout = self._deadline_timeout_seconds()
                try:
                    if timeout is None:
                        item = await self._queue.get()
                    else:
                        item = await asyncio.wait_for(
                            self._queue.get(),
                            timeout=max(0.001, timeout),
                        )
                except asyncio.TimeoutError:
                    await self._advance_time()
                    continue

                try:
                    if item.kind == "stop":
                        break
                    process_started_at = time.monotonic()
                    queue_lag_ms = max(
                        0,
                        int((process_started_at - item.enqueued_at) * 1000),
                    )
                    if item.kind == "message":
                        batch, turns = await asyncio.to_thread(
                            self._reduce_message,
                            item.value,
                            item.at_ms,
                        )
                        self._log_batch(
                            batch,
                            queue_lag_ms=queue_lag_ms,
                            process_started_at=process_started_at,
                        )
                    else:
                        turns = await asyncio.to_thread(
                            self._reduce_lifecycle,
                            item.value,
                            item.at_ms,
                        )
                    self._log_turns(turns)
                except Exception as error:
                    self.worker_error_count += 1
                    logger.error(
                        "voice_event event=observation_shadow_worker_error shadow=%s "
                        "exception_type=%s errors=%s",
                        self.shadow_id,
                        type(error).__name__,
                        self.worker_error_count,
                    )
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            pass
        finally:
            logger.info(
                "voice_event event=observation_shadow_stopped shadow=%s "
                "enqueued=%s ignored=%s dropped=%s errors=%s turns=%s "
                "max_queue_depth=%s",
                self.shadow_id,
                self.enqueued_item_count,
                self.ignored_message_count,
                self.dropped_item_count,
                self.worker_error_count,
                self.emitted_turn_count,
                self.max_queue_depth,
            )

    def _deadline_timeout_seconds(self) -> float | None:
        deadline_ms = self._assembler.next_deadline_ms
        if deadline_ms is None:
            return None
        return max(0.0, (deadline_ms - self._elapsed_ms()) / 1000)

    def _reduce_message(self, message: object, at_ms: int):
        batch = self._adapter.adapt_message(
            message,
            at_ms=at_ms,
            first_sequence=self._next_sequence,
            epoch=self._epoch,
        )
        self._next_sequence += max(1, len(batch.events))
        accepted_events = []
        for event in batch.events:
            if event.kind is CallerTurnEventKind.MODEL_OUTPUT_STARTED:
                if self._model_output_active:
                    continue
                self._model_output_active = True
            accepted_events.append(event)
            if event.kind in {
                CallerTurnEventKind.TURN_COMPLETE,
                CallerTurnEventKind.INTERRUPTED,
            }:
                self._model_output_active = False

        turns = []
        for event in accepted_events:
            turns.extend(self._assembler.ingest(event))
        report = batch.redacted_report_dict()
        report["event_count"] = len(accepted_events)
        report["event_types"] = dict(
            sorted(Counter(event.kind.value for event in accepted_events).items())
        )
        return report, tuple(turns)

    def _reduce_lifecycle(self, value: object, at_ms: int):
        kind = CallerTurnEventKind(value)
        self._model_output_active = False
        self._model_output_enqueued = False
        if kind is CallerTurnEventKind.RECONNECT_STARTED:
            self._epoch += 1
        event = self._adapter.adapt_lifecycle(
            kind,
            at_ms=at_ms,
            sequence=self._next_sequence,
            epoch=self._epoch,
        )
        self._next_sequence += 1
        return self._assembler.ingest(event)

    async def _advance_time(self) -> None:
        turns = await asyncio.to_thread(
            self._assembler.advance_time,
            self._elapsed_ms(),
        )
        self._log_turns(turns)

    def _log_batch(
        self,
        batch: dict[str, Any],
        *,
        queue_lag_ms: int,
        process_started_at: float,
    ) -> None:
        event_types = batch.get("event_types") or {}
        event_kinds = ",".join(
            f"{name}:{count}" for name, count in sorted(event_types.items())
        ) or "none"
        logger.info(
            "voice_event event=observation_shadow_batch shadow=%s status=%s "
            "rejection=%s event_count=%s event_kinds=%s queue_lag_ms=%s "
            "process_ms=%s",
            self.shadow_id,
            batch.get("status"),
            batch.get("rejection_code") or "none",
            batch.get("event_count", 0),
            event_kinds,
            queue_lag_ms,
            max(0, int((time.monotonic() - process_started_at) * 1000)),
        )

    def _log_turns(self, turns) -> None:
        for turn in turns:
            report = turn.redacted_report_dict()
            self.emitted_turn_count += 1
            logger.info(
                "voice_event event=observation_shadow_turn shadow=%s epoch=%s "
                "turn=%s status=%s reason=%s event_count=%s "
                "transcript_codepoints=%s transcript_utf8_bytes=%s "
                "finalize_delay_ms=%s",
                self.shadow_id,
                report["epoch"],
                report["turn_id"],
                report["status"],
                report["close_reason"],
                report["event_count"],
                report["transcript_codepoints"],
                report["transcript_utf8_bytes"],
                max(0, report["finalized_at_ms"] - report["terminal_at_ms"]),
            )
