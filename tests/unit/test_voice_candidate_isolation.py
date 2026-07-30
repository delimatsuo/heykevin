"""AST isolation checks for the offline-only candidate adapter package."""

import ast
from pathlib import Path

import pytest

from app.services.voice_candidates import (
    AdapterRejectReason,
    CandidateLimits,
    EventContext,
)
from app.services.voice_candidates.chained_streaming import (
    ChainedSignal,
    ChainedSignalKind,
    ChainedStreamingAdapter,
)
from app.services.voice_candidates.conversation_relay import (
    ConversationRelayAdapter,
    RelaySignal,
    RelaySignalKind,
)
from app.services.voice_candidates.manual_native import (
    ManualNativeAdapter,
    ManualNativeSignal,
    ManualNativeSignalKind,
)
from app.services.voice_candidates.native_gemini import (
    NativeGeminiAdapter,
    NativeMode,
    NativeSignal,
    NativeSignalKind,
)
from app.services.voice_lifecycle import (
    VoiceEventKind,
    VoiceLifecycle,
    VoicePayload,
    VoiceSemanticActKind,
    VoiceSessionBinding,
    VoiceSource,
)

_CANDIDATE_PATHS = tuple(
    Path("app/services/voice_candidates") / name
    for name in (
        "__init__.py",
        "native_gemini.py",
        "chained_streaming.py",
        "conversation_relay.py",
        "manual_native.py",
    )
)
_FORBIDDEN = (
    "google",
    "twilio",
    "deepgram",
    "elevenlabs",
    "socket",
    "requests",
    "httpx",
    "websockets",
    "subprocess",
    "app.main",
    "app.webhooks",
    "app.services.gemini_pipeline",
    "app.services.voice_pipeline",
)


def _binding(epoch: int = 1) -> VoiceSessionBinding:
    return VoiceSessionBinding("bakeoff", "tenant_1", "call_1", "stream_1", epoch)


def _context(
    sequence: int,
    *,
    turn: int = 1,
) -> EventContext:
    return EventContext(
        binding=_binding(),
        sequence=sequence,
        at_ms=sequence,
        input_turn_id=f"turn_{turn}",
        generation_id=f"generation_{turn}",
        semantic_act_id=f"act_{turn}",
        semantic_act_kind=VoiceSemanticActKind.ANSWER,
    )


def _adapter(arm: str, *, request_count: int = 10):
    limits = CandidateLimits(128, 1_000, 10_000, 10_000, 100, request_count)
    if arm == "A":
        return NativeGeminiAdapter(
            binding=_binding(),
            mode=NativeMode.MANUAL_GATED,
            limits=limits,
        )
    if arm == "B1":
        return ChainedStreamingAdapter(binding=_binding(), limits=limits)
    if arm == "B2":
        return ConversationRelayAdapter(binding=_binding(), limits=limits)
    if arm == "C":
        return ManualNativeAdapter(
            binding=_binding(),
            limits=limits,
            generation_timeout_ms=100,
        )
    raise AssertionError("unknown arm")


def _prepare_turn(adapter, arm: str, *, turn: int, sequence: int) -> None:
    context = _context(sequence, turn=turn)
    if arm == "B1":
        assert adapter.handle(
            ChainedSignal(
                ChainedSignalKind.INPUT_FINAL,
                context,
                payload=VoicePayload(text_digest="a" * 64),
            )
        ).accepted
    elif arm == "B2":
        assert adapter.handle(
            RelaySignal(
                RelaySignalKind.PROMPT_FINAL,
                context,
                payload=VoicePayload(text_digest="a" * 64),
            )
        ).accepted
    elif arm == "C":
        assert adapter.handle(
            ManualNativeSignal(
                ManualNativeSignalKind.ACTIVITY_STARTED,
                context,
            )
        ).accepted
        assert adapter.handle(
            ManualNativeSignal(
                ManualNativeSignalKind.ACTIVITY_ENDED,
                _context(sequence + 1, turn=turn),
            )
        ).accepted
        assert adapter.handle(
            ManualNativeSignal(
                ManualNativeSignalKind.INPUT_FINAL,
                _context(sequence + 2, turn=turn),
                payload=VoicePayload(text_digest="a" * 64),
            )
        ).accepted


def _permit(adapter, arm: str, *, turn: int, sequence: int):
    _prepare_turn(adapter, arm, turn=turn, sequence=sequence)
    context = _context(sequence + 3, turn=turn)
    lifecycle = VoiceLifecycle(binding=_binding())
    permit = context.event(
        VoiceEventKind.RESPONSE_AUTHORIZED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
    )
    assert lifecycle.ingest(permit)
    return permit, lifecycle


def _session_signal(
    arm: str,
    kind: str,
    *,
    sequence: int,
    turn: int = 1,
):
    context = _context(sequence, turn=turn)
    if arm == "A":
        return NativeSignal(NativeSignalKind(kind), context)
    if arm == "B1":
        return ChainedSignal(ChainedSignalKind(kind), context)
    if arm == "B2":
        return RelaySignal(RelaySignalKind(kind), context)
    if arm == "C":
        return ManualNativeSignal(ManualNativeSignalKind(kind), context)
    raise AssertionError("unknown arm")


def _generation_signal(arm: str, *, sequence: int):
    context = _context(sequence)
    if arm == "A":
        return NativeSignal(NativeSignalKind.GENERATION_STARTED, context)
    if arm == "B1":
        return ChainedSignal(ChainedSignalKind.GENERATION_STARTED, context)
    if arm == "B2":
        return RelaySignal(RelaySignalKind.GENERATION_STARTED, context)
    if arm == "C":
        return ManualNativeSignal(
            ManualNativeSignalKind.GENERATION_STARTED,
            context,
        )
    raise AssertionError("unknown arm")


def _assert_disconnect_cleared_transient_state(adapter, arm: str) -> None:
    if arm == "A":
        assert adapter._generation_state == {}
        assert adapter._audio_ids == {}
        assert adapter._audio_bindings == {}
        assert adapter._playout_records == {}
    elif arm == "B1":
        assert adapter._final_turns == set()
        assert adapter._generation_state == {}
        assert adapter._tts_bindings == {}
        assert adapter._playout_bindings == {}
    elif arm == "B2":
        assert adapter._final_turns == set()
        assert adapter._generation_state == {}
    elif arm == "C":
        assert adapter._activity_open == set()
        assert adapter._completed_activity_turns == set()
        assert adapter._final_turns == set()
        assert adapter._begin_deadlines == {}
        assert adapter._completion_deadlines == {}
        assert adapter._pending_timeouts == {}
        assert adapter._audio_seen == set()
        assert adapter._audio_ids == {}
        assert adapter._tts_bindings == {}
        assert adapter._playout_bindings == {}
    else:
        raise AssertionError("unknown arm")


def _literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _imported_modules(source: str, *, module_name: str) -> set[str]:
    tree = ast.parse(source)
    package = module_name.split(".")[:-1]
    imported: set[str] = set()
    importlib_names = {"importlib"}
    import_module_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    importlib_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "importlib":
            for alias in node.names:
                if alias.name == "import_module":
                    import_module_names.add(alias.asname or alias.name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = len(package) - (node.level - 1)
                base_parts = package[: max(keep, 0)]
                if node.module:
                    base_parts.extend(node.module.split("."))
                base = ".".join(base_parts)
            else:
                base = node.module or ""
            if base:
                imported.add(base)
            for alias in node.names:
                if alias.name != "*":
                    imported.add(".".join(part for part in (base, alias.name) if part))
        elif isinstance(node, ast.Call) and node.args:
            target = _literal_string(node.args[0])
            if target is None:
                continue
            if (
                isinstance(node.func, ast.Name)
                and (
                    node.func.id == "__import__"
                    or node.func.id in import_module_names
                )
            ) or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in importlib_names
            ):
                imported.add(target)
    return imported


def _is_forbidden(module: str) -> bool:
    return any(
        module == forbidden or module.startswith(f"{forbidden}.")
        for forbidden in _FORBIDDEN
    )


def test_ast_import_detector_covers_from_relative_and_dynamic_forms():
    modules = _imported_modules(
        """
import google.genai
from google import genai
from ...webhooks import media_stream
import importlib as il
from importlib import import_module as load_module
il.import_module("app.services.voice_pipeline")
load_module("deepgram")
__import__("twilio.rest")
""",
        module_name="app.services.voice_candidates.fixture",
    )
    assert {
        "google.genai",
        "google",
        "app.webhooks",
        "app.webhooks.media_stream",
        "app.services.voice_pipeline",
        "deepgram",
        "twilio.rest",
    } <= modules
    assert all(
        _is_forbidden(module)
        for module in (
            "google.genai",
            "app.webhooks.media_stream",
            "app.services.voice_pipeline",
            "deepgram",
            "twilio.rest",
        )
    )


def test_candidate_package_has_no_provider_network_or_live_route_imports():
    violations: dict[str, list[str]] = {}
    for path in _CANDIDATE_PATHS:
        module_name = ".".join(path.with_suffix("").parts)
        modules = _imported_modules(
            path.read_text(encoding="utf-8"),
            module_name=module_name,
        )
        forbidden = sorted(module for module in modules if _is_forbidden(module))
        if forbidden:
            violations[str(path)] = forbidden
    assert violations == {}


@pytest.mark.parametrize("arm", ("A", "B1", "B2", "C"))
def test_all_arms_bound_pre_generation_permits_by_request_cap(arm: str):
    adapter = _adapter(arm, request_count=2)
    accepted = 0
    for turn in range(1, adapter.limits.request_count + 1):
        permit, lifecycle = _permit(
            adapter,
            arm,
            turn=turn,
            sequence=turn * 10,
        )
        accepted += adapter.accept_permit(permit, lifecycle=lifecycle)

    extra_context = _context(100, turn=100)
    extra_lifecycle = VoiceLifecycle(binding=_binding())
    extra_permit = extra_context.event(
        VoiceEventKind.RESPONSE_AUTHORIZED,
        source=VoiceSource.LOCAL_AUTHORITATIVE,
    )
    assert extra_lifecycle.ingest(extra_permit)
    assert not adapter.accept_permit(
        extra_permit,
        lifecycle=extra_lifecycle,
    )
    assert accepted == adapter.limits.request_count
    assert adapter.retained_permit_count == adapter.limits.request_count
    assert adapter.request_count == 0


@pytest.mark.parametrize("arm", ("B1", "B2", "C"))
def test_provider_input_and_usage_state_are_bounded_before_permit(arm: str):
    adapter = _adapter(arm, request_count=2)
    accepted = 0
    for turn in range(1, 501):
        context = _context(turn * 10, turn=turn)
        if arm == "B1":
            result = adapter.handle(
                ChainedSignal(
                    ChainedSignalKind.INPUT_FINAL,
                    context,
                    payload=VoicePayload(text_digest="a" * 64),
                )
            )
        elif arm == "B2":
            result = adapter.handle(
                RelaySignal(
                    RelaySignalKind.PROMPT_FINAL,
                    context,
                    payload=VoicePayload(text_digest="a" * 64),
                )
            )
        else:
            result = adapter.handle(
                ManualNativeSignal(
                    ManualNativeSignalKind.ACTIVITY_STARTED,
                    context,
                )
            )
            if result.accepted:
                assert adapter.handle(
                    ManualNativeSignal(
                        ManualNativeSignalKind.ACTIVITY_ENDED,
                        _context(turn * 10 + 1, turn=turn),
                    )
                ).accepted
                result = adapter.handle(
                    ManualNativeSignal(
                        ManualNativeSignalKind.INPUT_FINAL,
                        _context(turn * 10 + 2, turn=turn),
                        payload=VoicePayload(text_digest="a" * 64),
                    )
                )
        if result.accepted:
            accepted += 1
        else:
            assert result.reason is AdapterRejectReason.LIMIT_EXCEEDED

    assert accepted == adapter.limits.request_count
    assert adapter.retained_input_turn_count == adapter.limits.request_count
    assert len(adapter._usage) == adapter.limits.request_count
    assert len(adapter._final_turns) == adapter.limits.request_count
    if arm == "C":
        assert (
            len(adapter._completed_activity_turns)
            == adapter.limits.request_count
        )

    for turn in range(501, 1_001):
        assert adapter.handle(
            _session_signal(
                arm,
                "session_disconnected",
                sequence=turn * 10,
                turn=turn,
            )
        ).accepted
    assert len(adapter._usage) == adapter.limits.request_count


@pytest.mark.parametrize("arm", ("A", "B1", "B2", "C"))
def test_disconnect_immediately_revokes_output_and_old_permit(arm: str):
    adapter = _adapter(arm)
    permit, lifecycle = _permit(
        adapter,
        arm,
        turn=1,
        sequence=10,
    )
    assert adapter.accept_permit(permit, lifecycle=lifecycle)
    assert adapter.handle(_generation_signal(arm, sequence=14)).accepted
    assert adapter.handle(
        _session_signal(
            arm,
            "session_disconnected",
            sequence=20,
        )
    ).accepted
    assert adapter.permit_admission_closed
    assert adapter.retained_permit_count == 1
    assert not adapter.accept_permit(permit, lifecycle=lifecycle)
    _assert_disconnect_cleared_transient_state(adapter, arm)

    rejected = adapter.handle(_generation_signal(arm, sequence=21))
    assert not rejected.accepted
    assert rejected.reason in {
        AdapterRejectReason.PERMIT_REQUIRED,
        AdapterRejectReason.STALE_EPOCH,
    }

    assert adapter.handle(
        _session_signal(
            arm,
            "session_reestablished",
            sequence=22,
        )
    ).accepted
    assert adapter.permit_admission_closed
    assert adapter.retained_permit_count == 0
    assert not adapter.accept_permit(permit, lifecycle=lifecycle)
    assert (
        adapter.handle(_generation_signal(arm, sequence=23)).reason
        is AdapterRejectReason.STALE_EPOCH
    )

    fresh = _adapter(arm)
    fresh_permit, fresh_lifecycle = _permit(
        fresh,
        arm,
        turn=1,
        sequence=30,
    )
    assert fresh.accept_permit(
        fresh_permit,
        lifecycle=fresh_lifecycle,
    )
