"""Contract and isolation checks for the offline bakeoff coordinator."""

import ast
from pathlib import Path

from app.services.voice_call_lifecycle import CallLifecycle, SilencePhase
from app.services.voice_lifecycle import (
    VoiceEvent,
    VoiceEventKind,
    VoiceLifecycle,
    VoicePayload,
    VoiceSemanticActKind,
    VoiceSensitivity,
    VoiceSessionBinding,
    VoiceSource,
)
from app.services.voice_speech_control import (
    SemanticAct,
    SpeechAuthorization,
    SpeechControl,
    SpeechPolicy,
    SpokenPlan,
)


def _imports_target(
    tree: ast.AST,
    *,
    path: Path,
    target: str,
) -> bool:
    target_parent, _, target_leaf = target.rpartition(".")
    package = tuple(path.with_suffix("").parts[:-1])
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == target for alias in node.names):
                return True
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            keep = len(package) - node.level + 1
            if keep < 0:
                continue
            base = (*package[:keep], *(node.module or "").split("."))
            module = ".".join(part for part in base if part)
        else:
            module = node.module or ""
        if module == target:
            return True
        if module == target_parent and any(
            alias.name == target_leaf for alias in node.names
        ):
            return True
    return False


def test_coordinator_has_no_live_route_provider_or_terminal_executor_imports():
    path = Path("app/services/voice_bakeoff_coordinator.py")
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    allowed = {
        "__future__",
        "app.services.voice_call_lifecycle",
        "app.services.voice_lifecycle",
        "app.services.voice_speech_control",
    }
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert imports <= allowed
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"__import__", "eval", "exec"}
        for node in ast.walk(tree)
    )

    target = "app.services.voice_bakeoff_coordinator"
    live_modules = (
        Path("app/main.py"),
        Path("app/webhooks/media_stream.py"),
        Path("app/services/gemini_pipeline.py"),
        Path("app/services/voice_pipeline.py"),
    )
    for live_path in live_modules:
        live_tree = ast.parse(live_path.read_text(encoding="utf-8"))
        assert not _imports_target(
            live_tree,
            path=live_path,
            target=target,
        )
        assert target not in {
            node.value
            for node in ast.walk(live_tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }


def test_reverse_import_resolver_catches_parent_and_relative_forms():
    target = "app.services.voice_bakeoff_coordinator"
    live_path = Path("app/services/live_module.py")
    for source in (
        "import app.services.voice_bakeoff_coordinator",
        "from app.services import voice_bakeoff_coordinator",
        "from . import voice_bakeoff_coordinator",
        "from .voice_bakeoff_coordinator import VoiceBakeoffCoordinator",
    ):
        assert _imports_target(
            ast.parse(source),
            path=live_path,
            target=target,
        )
    assert not _imports_target(
        ast.parse("from app.services import voice_lifecycle"),
        path=live_path,
        target=target,
    )


def _binding() -> VoiceSessionBinding:
    return VoiceSessionBinding("bakeoff", "tenant_1", "call_1", "stream_1", 1)


def _coordinator():
    from app.services.voice_bakeoff_coordinator import VoiceBakeoffCoordinator

    lifecycle = VoiceLifecycle(binding=_binding())
    calls = CallLifecycle(
        binding=_binding(),
        voice_lifecycle=lifecycle,
        first_silence_ms=10,
        second_silence_ms=20,
    )
    speech = SpeechControl(
        SpeechPolicy(
            normal_word_budget=12,
            safety_word_budget=20,
            required_safety_fragments=("call emergency services",),
            terminal_fragments=("goodbye",),
        )
    )
    return VoiceBakeoffCoordinator(speech=speech, calls=calls)


def _authorization() -> SpeechAuthorization:
    return SpeechAuthorization(
        binding=_binding(),
        turn_id="turn_1",
        authorized_kinds=(
            VoiceSemanticActKind.ANSWER,
            VoiceSemanticActKind.QUESTION,
        ),
        terminal_allowed=False,
    )


def _plan(plan_id: str = "plan_1") -> SpokenPlan:
    return SpokenPlan(
        plan_id=plan_id,
        acts=(
            SemanticAct(VoiceSemanticActKind.ANSWER, "Yes, we can help."),
            SemanticAct(
                VoiceSemanticActKind.QUESTION,
                "What service do you need?",
                question_slot="service",
            ),
        ),
    )


def _event(
    kind: VoiceEventKind,
    sequence: int,
    *,
    act_id: str,
    **changes: object,
) -> VoiceEvent:
    values = {
        "schema_version": 1,
        "kind": kind,
        "source": VoiceSource.LOCAL_AUTHORITATIVE,
        "sensitivity": VoiceSensitivity.OPERATIONAL,
        "binding": _binding(),
        "sequence": sequence,
        "at_ms": sequence,
        "input_turn_id": "turn_1",
        "generation_id": "generation_1",
        "semantic_act_id": act_id,
        "semantic_act_kind": VoiceSemanticActKind.QUESTION,
        "payload": VoicePayload(),
    }
    values.update(changes)
    return VoiceEvent(**values)


def test_reservation_rolls_back_speech_when_call_lifecycle_rejects():
    coordinator = _coordinator()

    assert coordinator.reserve_plan(
        plan=_plan(),
        authorization=_authorization(),
        event_id="invalid_sequence",
        sequence=-1,
        at_ms=0,
    ) == ()
    assert coordinator.calls.phase is SilencePhase.IDLE

    reserved = coordinator.reserve_plan(
        plan=_plan(),
        authorization=_authorization(),
        event_id="reserve_1",
        sequence=1,
        at_ms=0,
    )
    assert len(reserved) == 2
    assert coordinator.calls.phase is SilencePhase.QUESTION_RESERVED


def test_semantic_confirmation_requires_exact_canonical_accepted_receipt():
    coordinator = _coordinator()
    reserved = coordinator.reserve_plan(
        plan=_plan(),
        authorization=_authorization(),
        event_id="reserve_1",
        sequence=1,
        at_ms=0,
    )
    question_id = reserved[1]
    response = _event(VoiceEventKind.RESPONSE_AUTHORIZED, 1, act_id=question_id)
    confirmation = _event(
        VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
        2,
        act_id=question_id,
    )

    assert not coordinator.semantic_confirmed(
        event=confirmation,
        event_id="raw_confirmation",
        sequence=2,
    )
    assert coordinator.calls.voice_lifecycle.ingest(response)
    assert coordinator.calls.voice_lifecycle.ingest(confirmation)
    assert coordinator.semantic_confirmed(
        event=confirmation,
        event_id="canonical_confirmation",
        sequence=2,
    )
    assert coordinator.calls.phase is SilencePhase.QUESTION_CONFIRMED
    assert not coordinator.semantic_confirmed(
        event=confirmation,
        event_id="duplicate_confirmation",
        sequence=3,
    )


def test_semantic_confirmation_rejects_canonical_wrong_kind_and_turn():
    for semantic_kind, turn_id in (
        (VoiceSemanticActKind.ANSWER, "turn_1"),
        (VoiceSemanticActKind.QUESTION, "turn_2"),
    ):
        coordinator = _coordinator()
        reserved = coordinator.reserve_plan(
            plan=_plan(),
            authorization=_authorization(),
            event_id="reserve_1",
            sequence=1,
            at_ms=0,
        )
        question_id = reserved[1]
        response = _event(
            VoiceEventKind.RESPONSE_AUTHORIZED,
            1,
            act_id=question_id,
            semantic_act_kind=semantic_kind,
            input_turn_id=turn_id,
        )
        confirmation = _event(
            VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
            2,
            act_id=question_id,
            semantic_act_kind=semantic_kind,
            input_turn_id=turn_id,
        )
        assert coordinator.calls.voice_lifecycle.ingest(response)
        assert coordinator.calls.voice_lifecycle.ingest(confirmation)
        assert not coordinator.semantic_confirmed(
            event=confirmation,
            event_id="mismatched_confirmation",
            sequence=2,
        )
        assert coordinator.calls.phase is SilencePhase.QUESTION_RESERVED


def test_semantic_confirmation_rejects_wrong_act_binding_and_advanced_receipt():
    coordinator = _coordinator()
    reserved = coordinator.reserve_plan(
        plan=_plan(),
        authorization=_authorization(),
        event_id="reserve_1",
        sequence=1,
        at_ms=0,
    )
    wrong_response = _event(
        VoiceEventKind.RESPONSE_AUTHORIZED,
        1,
        act_id="other_act",
    )
    wrong_confirmation = _event(
        VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
        2,
        act_id="other_act",
    )
    assert coordinator.calls.voice_lifecycle.ingest(wrong_response)
    assert coordinator.calls.voice_lifecycle.ingest(wrong_confirmation)
    assert not coordinator.semantic_confirmed(
        event=wrong_confirmation,
        event_id="wrong_act",
        sequence=2,
    )
    assert coordinator.calls.phase is SilencePhase.QUESTION_RESERVED


    other_binding = VoiceSessionBinding(
        "bakeoff",
        "tenant_1",
        "call_2",
        "stream_1",
        1,
    )
    cross_binding = _event(
        VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
        3,
        act_id=reserved[1],
        binding=other_binding,
    )
    assert not coordinator.calls.voice_lifecycle.ingest(cross_binding)
    assert not coordinator.semantic_confirmed(
        event=cross_binding,
        event_id="cross_binding",
        sequence=3,
    )

    coordinator = _coordinator()
    reserved = coordinator.reserve_plan(
        plan=_plan("plan_advanced"),
        authorization=_authorization(),
        event_id="reserve_advanced",
        sequence=1,
        at_ms=0,
    )
    response = _event(
        VoiceEventKind.RESPONSE_AUTHORIZED,
        1,
        act_id=reserved[1],
    )
    confirmation = _event(
        VoiceEventKind.SEMANTIC_ACT_CONFIRMED,
        2,
        act_id=reserved[1],
    )
    assert coordinator.calls.voice_lifecycle.ingest(response)
    assert coordinator.calls.voice_lifecycle.ingest(confirmation)
    assert coordinator.calls.voice_lifecycle.ingest(
        _event(
            VoiceEventKind.TTS_BOUND,
            3,
            act_id=reserved[1],
            payload=VoicePayload(text_digest="a" * 64, audio_id="audio_1"),
        )
    )
    assert not coordinator.semantic_confirmed(
        event=confirmation,
        event_id="advanced_receipt",
        sequence=2,
    )
    assert coordinator.calls.phase is SilencePhase.QUESTION_RESERVED
