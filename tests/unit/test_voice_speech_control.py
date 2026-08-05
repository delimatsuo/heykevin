"""Offline contract tests for provider-neutral speech control."""

import hashlib
import json
from pathlib import Path

import pytest

from app.services.voice_lifecycle import VoiceSessionBinding
from app.services.voice_speech_control import (
    CancellationReason,
    FailureClass,
    ReplayMode,
    SemanticAct,
    SemanticActKind,
    SpeechAuthorization,
    SpeechControl,
    SpeechPolicy,
    SpokenPlan,
)


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


def _replay_authorization(
    *,
    turn_id: str,
    kind: SemanticActKind,
    locale: str = "en",
) -> SpeechAuthorization:
    return SpeechAuthorization(
        binding=_authorization().binding,
        turn_id=turn_id,
        authorized_kinds=(kind,),
        terminal_allowed=False,
        locale=locale,
    )


def _observe_exact_act(
    control: SpeechControl,
    *,
    plan_id: str,
    turn_id: str,
    kind: SemanticActKind,
    text: str,
    question_slot: str | None = None,
):
    authorization = SpeechAuthorization(
        binding=_authorization().binding,
        turn_id=turn_id,
        authorized_kinds=(kind,),
        terminal_allowed=False,
    )
    reserved = control.reserve(
        SpokenPlan(
            plan_id=plan_id,
            acts=(
                SemanticAct(
                    kind,
                    text,
                    question_slot=question_slot,
                ),
            ),
        ),
        authorization,
    )[0]
    assert control.authorize_text(reserved.act_id, reserved.text)
    assert control.bind_tts(
        reserved.act_id,
        audio_id=f"audio_{plan_id}",
    )
    assert control.bind_playout(
        reserved.act_id,
        playout_id=f"playout_{plan_id}",
    )
    assert control.record_caller_playback_observed(
        reserved.act_id,
        playout_id=f"playout_{plan_id}",
    )
    return reserved


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
    question_only = control.reserve(
        SpokenPlan(plan_id="plan_question", acts=(SemanticAct(SemanticActKind.QUESTION, "What do you need?", question_slot="service"),)),
        _authorization(),
    )
    assert len(question_only) == 1
    assert question_only[0].kind is SemanticActKind.QUESTION
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


def test_repeat_and_slower_reserve_exact_latest_observed_act_once_per_request():
    control = SpeechControl(_policy())
    original = _observe_exact_act(
        control,
        plan_id="original",
        turn_id="turn_original",
        kind=SemanticActKind.QUESTION,
        text="What service do you need?",
        question_slot="service",
    )
    source = control.latest_replay_source(original.binding)
    assert source is not None
    assert source.act_id == original.act_id
    assert source.text == original.text
    assert source.text_digest == hashlib.sha256(
        original.text.encode("utf-8")
    ).hexdigest()
    assert source.question_slot == "service"

    exact_authorization = _replay_authorization(
        turn_id="turn_repeat",
        kind=source.kind,
    )
    exact = control.reserve_replay(
        source=source,
        request_id="request_repeat",
        mode=ReplayMode.EXACT,
        authorization=exact_authorization,
    )
    assert len(exact) == 1
    assert exact[0].text == original.text
    assert exact[0].act_id != original.act_id
    assert control.authorized_text_digest(exact[0].act_id) is None
    binding = control.replay_binding(exact[0].act_id)
    assert binding is not None
    assert binding.source_act_id == original.act_id
    assert binding.request_id == "request_repeat"
    assert binding.mode is ReplayMode.EXACT
    assert binding.text_digest == source.text_digest
    assert control.authorize_text(exact[0].act_id, exact[0].text)
    assert (
        control.authorized_text_digest(exact[0].act_id)
        == source.text_digest
    )
    assert control.reserve_replay(
        source=source,
        request_id="request_repeat",
        mode=ReplayMode.EXACT,
        authorization=exact_authorization,
    ) == ()

    slower = control.reserve_replay(
        source=source,
        request_id="request_slower",
        mode=ReplayMode.SLOWER,
        authorization=_replay_authorization(
            turn_id="turn_slower",
            kind=source.kind,
        ),
    )
    assert len(slower) == 1
    assert slower[0].text == exact[0].text
    slower_binding = control.replay_binding(slower[0].act_id)
    assert slower_binding is not None
    assert slower_binding.mode is ReplayMode.SLOWER
    assert slower_binding.text_digest == source.text_digest


def test_replay_rejects_unobserved_stale_and_mismatched_sources_but_rolls_back_pristine():
    control = SpeechControl(_policy())
    unobserved = control.reserve(
        SpokenPlan(
            plan_id="unobserved",
            acts=(
                SemanticAct(
                    SemanticActKind.ANSWER,
                    "I understand.",
                ),
            ),
        ),
        _authorization(),
    )[0]
    assert control.authorize_text(unobserved.act_id, unobserved.text)
    assert control.latest_replay_source(unobserved.binding) is None

    first = _observe_exact_act(
        control,
        plan_id="first_observed",
        turn_id="turn_first",
        kind=SemanticActKind.QUESTION,
        text="What service do you need?",
        question_slot="service",
    )
    stale_source = control.latest_replay_source(first.binding)
    assert stale_source is not None
    second = _observe_exact_act(
        control,
        plan_id="second_observed",
        turn_id="turn_second",
        kind=SemanticActKind.QUESTION,
        text="When do you need help?",
        question_slot="urgency",
    )
    latest = control.latest_replay_source(second.binding)
    assert latest is not None
    assert latest.act_id == second.act_id
    assert control.reserve_replay(
        source=stale_source,
        request_id="request_stale",
        mode=ReplayMode.EXACT,
        authorization=_replay_authorization(
            turn_id="turn_stale",
            kind=stale_source.kind,
        ),
    ) == ()
    assert control.reserve_replay(
        source=latest,
        request_id="request_wrong_kind",
        mode=ReplayMode.EXACT,
        authorization=_replay_authorization(
            turn_id="turn_wrong_kind",
            kind=SemanticActKind.ANSWER,
        ),
    ) == ()
    pristine = control.reserve_replay(
        source=latest,
        request_id="request_retry",
        mode=ReplayMode.EXACT,
        authorization=_replay_authorization(
            turn_id="turn_retry",
            kind=latest.kind,
        ),
    )
    assert len(pristine) == 1
    assert control.rollback_reservation(pristine)
    with pytest.raises(ValueError, match="already reserved"):
        control.reserve(
            SpokenPlan(
                plan_id="slot_still_reserved",
                acts=(
                    SemanticAct(
                        SemanticActKind.QUESTION,
                        "When do you need help?",
                        question_slot="urgency",
                    ),
                ),
            ),
            SpeechAuthorization(
                binding=latest.binding,
                turn_id="turn_slot_check",
                authorized_kinds=(
                    SemanticActKind.QUESTION,
                ),
                terminal_allowed=False,
            ),
        )
    assert control.reserve_replay(
        source=latest,
        request_id="request_retry",
        mode=ReplayMode.EXACT,
        authorization=_replay_authorization(
            turn_id="turn_retry",
            kind=latest.kind,
        ),
    ) == pristine


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


def test_hard_terminalization_removes_live_speech_and_question_authority():
    control = SpeechControl(_policy())
    question = control.reserve(
        SpokenPlan(
            plan_id="hard_terminal",
            acts=(
                SemanticAct(
                    SemanticActKind.QUESTION,
                    "What service do you need?",
                    question_slot="service",
                ),
            ),
        ),
        _authorization(),
    )[0]
    assert control.authorize_text(question.act_id, question.text)
    assert control.is_live(question.act_id)

    assert control.hard_terminalize(question.act_id)
    assert control.hard_terminalize(question.act_id)
    assert control.is_cancelled(question.act_id)
    assert not control.is_live(question.act_id)


def test_binding_hard_terminalization_retires_only_matching_speech():
    control = SpeechControl(_policy())
    matching = control.reserve(
        SpokenPlan(
            plan_id="matching",
            acts=(
                SemanticAct(
                    SemanticActKind.QUESTION,
                    "What service do you need?",
                    question_slot="service",
                ),
            ),
        ),
        _authorization(),
    )[0]
    foreign_authorization = SpeechAuthorization(
        binding=VoiceSessionBinding(
            environment="bakeoff",
            contractor_binding="tenant_1",
            call_binding="call_2",
            stream_binding="stream_2",
            epoch=1,
        ),
        turn_id="turn_2",
        authorized_kinds=(SemanticActKind.ANSWER,),
        terminal_allowed=False,
    )
    foreign = control.reserve(
        SpokenPlan(
            plan_id="foreign",
            acts=(
                SemanticAct(
                    SemanticActKind.ANSWER,
                    "I understand.",
                ),
            ),
        ),
        foreign_authorization,
    )[0]
    assert control.authorize_text(matching.act_id, matching.text)
    assert control.authorize_text(foreign.act_id, foreign.text)
    assert control.act_ids_for_binding(
        matching.binding
    ) == (matching.act_id,)
    assert control.act_ids_for_binding(
        foreign.binding
    ) == (foreign.act_id,)

    assert control.hard_terminalize_binding(
        matching.binding
    )
    assert not control.is_live(matching.act_id)
    assert control.is_live(foreign.act_id)


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
    assert control.reserve_repair(original_act_id=act.act_id, failure=FailureClass.RECOVERABLE, plan=repair_plan, authorization=_authorization(), confirmed_fact_ids=("service_1",)) is None
    assert control.reserve_repair(original_act_id=repair.repair_act_id, failure=FailureClass.RECOVERABLE, plan=repair_plan, authorization=_authorization(), confirmed_fact_ids=()) is None
    assert control.reserve_repair(original_act_id=act.act_id, failure=FailureClass.SECURITY, plan=repair_plan, authorization=_authorization(), confirmed_fact_ids=()) is None


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
