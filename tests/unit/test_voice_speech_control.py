"""Offline contract tests for provider-neutral speech control."""

import pytest
import json
from pathlib import Path

from app.services.voice_speech_control import (
    CancellationReason,
    FailureClass,
    SemanticAct,
    SemanticActKind,
    SpeechAuthorization,
    SpeechControl,
    SpeechPolicy,
    SpokenPlan,
)
from app.services.voice_lifecycle import VoiceSessionBinding


def _policy() -> SpeechPolicy:
    return SpeechPolicy(
        normal_word_budget=12,
        safety_word_budget=20,
        required_safety_fragments=("call emergency services",),
        terminal_fragments=("goodbye",),
    )


def _authorization(*, terminal_allowed: bool = False) -> SpeechAuthorization:
    return SpeechAuthorization(
        binding=VoiceSessionBinding(
            environment="bakeoff",
            contractor_binding="tenant_1",
            call_binding="call_1",
            stream_binding="stream_1",
            epoch=1,
        ),
        turn_id="turn_1",
        authorized_kinds=(SemanticActKind.ANSWER, SemanticActKind.QUESTION, SemanticActKind.SAFETY, SemanticActKind.REPAIR),
        terminal_allowed=terminal_allowed,
    )


def test_answer_precedes_one_reserved_question_and_binds_exact_audio_identity():
    control = SpeechControl(_policy())
    plan = SpokenPlan(
        plan_id="plan_1",
        acts=(
            SemanticAct(SemanticActKind.ANSWER, "Yes, we can help."),
            SemanticAct(SemanticActKind.QUESTION, "What service do you need?", question_slot="service"),
        ),
    )

    reserved = control.reserve(plan, _authorization())
    assert [item.kind for item in reserved] == [SemanticActKind.ANSWER, SemanticActKind.QUESTION]
    assert reserved[1].reservation_id is not None
    assert control.authorize_text(reserved[0].act_id, reserved[0].text)
    assert control.bind_tts(reserved[0].act_id, audio_id="audio_1")
    assert control.audio_binding(reserved[0].act_id).audio_id == "audio_1"
    assert control.bind_playout(reserved[0].act_id, playout_id="playout_1")
    assert control.playout_binding(reserved[0].act_id).audio_id == "audio_1"
    assert not control.bind_tts(reserved[0].act_id, audio_id="audio_2")


def test_rejects_extra_questions_terminal_language_and_unauthorized_acts():
    control = SpeechControl(_policy())
    with pytest.raises(ValueError, match="one question"):
        SpokenPlan(
            plan_id="plan_1",
            acts=(
                SemanticAct(SemanticActKind.QUESTION, "What do you need?", question_slot="service"),
                SemanticAct(SemanticActKind.QUESTION, "When do you need it?", question_slot="urgency"),
            ),
        )
    with pytest.raises(ValueError, match="preceding direct answer"):
        control.reserve(
            SpokenPlan(plan_id="plan_question", acts=(SemanticAct(SemanticActKind.QUESTION, "What do you need?", question_slot="service"),)),
            _authorization(),
        )
    terminal = SpokenPlan(plan_id="plan_2", acts=(SemanticAct(SemanticActKind.CLOSING, "Goodbye."),))
    with pytest.raises(ValueError, match="terminal"):
        control.reserve(terminal, _authorization())
    with pytest.raises(ValueError, match="terminal wording"):
        control.reserve(SpokenPlan(plan_id="plan_words", acts=(SemanticAct(SemanticActKind.ANSWER, "Goodbye."),)), _authorization())

    plan = SpokenPlan(plan_id="plan_3", acts=(SemanticAct(SemanticActKind.OPT_OUT, "I will not contact you again."),))
    with pytest.raises(ValueError, match="not authorized"):
        control.reserve(plan, _authorization())


def test_safety_requires_complete_validation_before_audio_and_uses_safety_budget():
    control = SpeechControl(_policy())
    plan = SpokenPlan(
        plan_id="plan_1",
        acts=(SemanticAct(SemanticActKind.SAFETY, "If there is immediate danger, call emergency services now."),),
    )
    reserved = control.reserve(plan, _authorization())[0]
    assert not control.bind_tts(reserved.act_id, audio_id="audio_1")
    assert control.authorize_text(reserved.act_id, reserved.text)
    assert control.bind_tts(reserved.act_id, audio_id="audio_1")

    too_long = " ".join(["safe"] * 13)
    normal = SpokenPlan(plan_id="plan_2", acts=(SemanticAct(SemanticActKind.ANSWER, too_long),))
    with pytest.raises(ValueError, match="budget"):
        control.reserve(normal, _authorization())


def test_lower_risk_segments_are_bounded_and_exact_before_tts():
    control = SpeechControl(_policy())
    act = control.reserve(
        SpokenPlan(plan_id="segments", acts=(SemanticAct(SemanticActKind.ANSWER, "Yes, we can help."),)),
        _authorization(),
    )[0]
    assert control.authorize_text(act.act_id, act.text)
    assert control.accept_segment(act.act_id, "Yes, ", final=False)
    assert not control.accept_segment(act.act_id, "wrong", final=True)
    assert control.accept_segment(act.act_id, "we can help.", final=True)


def test_caller_activity_releases_question_reservation_for_a_new_plan():
    control = SpeechControl(_policy())
    first = SpokenPlan(
        plan_id="first",
        acts=(SemanticAct(SemanticActKind.ANSWER, "Yes."), SemanticAct(SemanticActKind.QUESTION, "What service do you need?", question_slot="service")),
    )
    question = control.reserve(first, _authorization())[1]
    assert control.cancel(question.act_id, reason=CancellationReason.CALLER_ACTIVITY)
    second = SpokenPlan(
        plan_id="second",
        acts=(SemanticAct(SemanticActKind.ANSWER, "I understand."), SemanticAct(SemanticActKind.QUESTION, "What service do you need?", question_slot="service")),
    )
    assert control.reserve(second, _authorization())[1].reservation_id is not None


def test_pristine_batch_rollback_releases_every_reservation_atomically():
    control = SpeechControl(_policy())
    plan = SpokenPlan(
        plan_id="rollback",
        acts=(
            SemanticAct(SemanticActKind.ANSWER, "Yes."),
            SemanticAct(
                SemanticActKind.QUESTION,
                "What service do you need?",
                question_slot="service",
            ),
        ),
    )
    reserved = control.reserve(plan, _authorization())
    assert control.rollback_reservation(reserved)
    assert control.reserve(plan, _authorization()) == reserved


def test_reservation_rollback_refuses_to_erase_advanced_evidence():
    control = SpeechControl(_policy())
    reserved = control.reserve(
        SpokenPlan(
            plan_id="advanced",
            acts=(SemanticAct(SemanticActKind.ANSWER, "Yes."),),
        ),
        _authorization(),
    )
    assert control.authorize_text(reserved[0].act_id, reserved[0].text)
    assert not control.rollback_reservation(reserved)


def test_reservation_rollback_rejects_partial_or_duplicated_batches():
    control = SpeechControl(_policy())
    reserved = control.reserve(
        SpokenPlan(
            plan_id="batch",
            acts=(
                SemanticAct(SemanticActKind.ANSWER, "Yes."),
                SemanticAct(
                    SemanticActKind.QUESTION,
                    "What service do you need?",
                    question_slot="service",
                ),
            ),
        ),
        _authorization(),
    )
    assert not control.rollback_reservation(reserved[:1])
    assert not control.rollback_reservation((reserved[0], reserved[0]))
    assert control.rollback_reservation(reserved)


def test_cancellation_stale_epoch_and_single_recoverable_repair_preserve_facts():
    control = SpeechControl(_policy())
    plan = SpokenPlan(
        plan_id="plan_1",
        acts=(
            SemanticAct(SemanticActKind.ANSWER, "Yes, we can help."),
            SemanticAct(SemanticActKind.QUESTION, "What service do you need?", question_slot="service"),
        ),
    )
    act = control.reserve(plan, _authorization())[1]
    assert control.cancel(act.act_id, reason=CancellationReason.CALLER_ACTIVITY)
    assert control.is_cancelled(act.act_id)
    assert not control.authorize_text(act.act_id, act.text)

    repair_plan = SpokenPlan(plan_id="repair_1", acts=(SemanticAct(SemanticActKind.REPAIR, "I am sorry. Please give me a moment."),))
    repair = control.reserve_repair(
        original_act_id=act.act_id,
        failure=FailureClass.RECOVERABLE,
        plan=repair_plan,
        authorization=_authorization(),
        confirmed_fact_ids=("service_1",),
    )
    assert repair is not None
    assert repair.original_act_id == act.act_id
    assert repair.repair_act_id != act.act_id
    assert control.authorize_text(repair.repair_act_id, repair_plan.acts[0].text)
    assert control.bind_tts(repair.repair_act_id, audio_id="audio_repair")
    assert control.bind_playout(repair.repair_act_id, playout_id="playout_repair")
    assert control.reserve_repair(
        original_act_id=act.act_id, failure=FailureClass.RECOVERABLE, plan=repair_plan, authorization=_authorization(), confirmed_fact_ids=("service_1",)
    ) is None
    assert control.reserve_repair(
        original_act_id=repair.repair_act_id, failure=FailureClass.RECOVERABLE, plan=repair_plan, authorization=_authorization(), confirmed_fact_ids=()
    ) is None
    assert control.reserve_repair(
        original_act_id=act.act_id, failure=FailureClass.SECURITY, plan=repair_plan, authorization=_authorization(), confirmed_fact_ids=()
    ) is None


def test_fixture_is_development_only_and_uses_valid_typed_question_slots():
    fixture = json.loads(Path("tests/fixtures/voice_architecture_bakeoff/spoken_plans.json").read_text(encoding="utf-8"))
    assert fixture["development_only"] is True
    for plan in fixture["plans"]:
        acts = tuple(
            SemanticAct(
                SemanticActKind(item["kind"]),
                item["text"],
                question_slot=item.get("question_slot"),
            )
            for item in plan["acts"]
        )
        SpokenPlan(plan_id=plan["plan_id"], acts=acts)


def test_speech_and_lifecycle_use_the_same_closed_semantic_vocabulary():
    from app.services.voice_lifecycle import VoiceSemanticActKind

    assert SemanticActKind is VoiceSemanticActKind
