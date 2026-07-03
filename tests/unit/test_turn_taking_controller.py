from app.services.turn_taking import TurnTakingController


def test_defers_incomplete_question_fragments_from_speech_final():
    controller = TurnTakingController()
    controller.record_agent_text("What's your city or area?")

    decision = controller.decide(["do you service"], signal="speech_final")

    assert decision.should_commit is False
    assert decision.reason == "incomplete_question"


def test_defers_low_signal_function_word_even_after_location_prompt():
    controller = TurnTakingController()
    controller.record_agent_text("What's your city or area?")

    decision = controller.decide(["the"], signal="speech_final")

    assert decision.should_commit is False
    assert decision.reason == "function_word_fragment"


def test_commits_short_answers_when_the_agent_asked_for_that_slot():
    controller = TurnTakingController()
    controller.record_agent_text("What's your name?")
    name_decision = controller.decide(["Jonathan"], signal="speech_final")

    controller.record_agent_text("What's your city or area?")
    city_decision = controller.decide(["San Mateo, California."], signal="speech_final")

    controller.record_agent_text("Is the number ending in eight six six seven the best one?")
    yes_decision = controller.decide(["Yes."], signal="speech_final")

    assert name_decision.should_commit is True
    assert name_decision.reason == "expected_short_answer"
    assert city_decision.should_commit is True
    assert city_decision.reason == "expected_short_answer"
    assert yes_decision.should_commit is True
    assert yes_decision.reason == "expected_short_answer"


def test_deferred_fragments_commit_when_later_segments_complete_the_thought():
    controller = TurnTakingController()
    controller.record_agent_text("What's your city or area?")

    decision = controller.decide(["I am in", "San Mateo, California."], signal="speech_final")

    assert decision.should_commit is True
    assert decision.reason == "terminal_punctuation"
    assert decision.text == "I am in San Mateo, California."


def test_known_incomplete_fragment_does_not_allow_timeout_commit():
    controller = TurnTakingController()
    controller.record_agent_text("What's your city or area?")

    decision = controller.decide(["I am in"], signal="utterance_end")

    assert decision.should_commit is False
    assert decision.reason == "trailing_continuation_word"
    assert decision.allow_timeout_commit is False


def test_thank_you_commits_after_callback_confirmation():
    controller = TurnTakingController()
    controller.record_agent_text("Is the number ending in eight six six seven the best one?")

    decision = controller.decide(["Yes. Thank you."], signal="speech_final")

    assert decision.should_commit is True
    assert decision.reason == "expected_short_answer"
    assert decision.expected_answer == "yes_no"


def test_commits_complete_short_question_without_terminal_punctuation():
    controller = TurnTakingController()
    controller.record_agent_text("What can we help you with?")

    decision = controller.decide(["do you service San Mateo"], signal="speech_final")

    assert decision.should_commit is True
    assert decision.reason == "long_enough"
