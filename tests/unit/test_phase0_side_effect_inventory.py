from app.services.side_effect_inventory import SIDE_EFFECT_SURFACES, surfaces_by_path


REQUIRED_PATHS = {
    "app/services/post_call.py",
    "app/services/voice_pipeline.py",
    "app/services/gemini_pipeline.py",
    "app/services/sms.py",
    "app/api/calls.py",
    "app/services/appointment_confirm.py",
    "app/api/voip.py",
    "app/webhooks/telegram_callback.py",
    "app/webhooks/twilio_incoming.py",
    "app/webhooks/media_stream.py",
    "app/db/jobs.py",
    "app/services/job_card.py",
    "app/services/calendar.py",
    "app/services/jobber.py",
    "app/api/integrations.py",
    "app/api/estimates.py",
    "app/api/contractors.py",
    "app/services/conference.py",
    "app/services/warm_transfer.py",
    "app/services/vcard.py",
    "app/api/vcard.py",
    "app/services/push_notification.py",
}


def test_inventory_covers_required_phase0_paths():
    paths = {surface.path for surface in SIDE_EFFECT_SURFACES}
    assert REQUIRED_PATHS <= paths


def test_every_surface_has_gate_and_evidence():
    for surface in SIDE_EFFECT_SURFACES:
        assert surface.path
        assert surface.current_behavior
        assert surface.required_gate
        assert surface.required_evidence
        assert surface.risk in {"user_contact", "external_write", "twilio_mutation", "sensitive_read", "irreversible"}


def test_surfaces_by_path_groups_all_entries():
    grouped = surfaces_by_path()
    assert set(grouped) == {surface.path for surface in SIDE_EFFECT_SURFACES}
    assert sum(len(surfaces) for surfaces in grouped.values()) == len(SIDE_EFFECT_SURFACES)
    for surface in SIDE_EFFECT_SURFACES:
        assert surface in grouped[surface.path]
    assert any("caller SMS" in s.current_behavior for s in grouped["app/services/post_call.py"])


def test_voip_inventory_distinguishes_core_call_controls_from_text_reply_gate():
    surface = surfaces_by_path()["app/api/voip.py"][0]

    assert "accept, decline, and voicemail remain ownership-only" in surface.required_gate
    assert "text_reply requires the caller-text backend gate" in surface.required_gate
    assert "disabled-gate tests for text_reply" in surface.required_evidence
    assert "disabled-gate tests for accept" not in surface.required_evidence


def test_jobber_inventory_pins_post_call_request_semantics_and_no_voice_tool():
    grouped = surfaces_by_path()
    post_call_surface = grouped["app/services/post_call.py"][0]
    voice_surface = grouped["app/services/voice_pipeline.py"][0]

    assert "create a Jobber client when lookup misses" in post_call_surface.current_behavior
    assert "creates a Request and attempts to add a note" in post_call_surface.current_behavior
    assert "never calls create_job or create_quote" in post_call_surface.current_behavior
    assert "jobber_lead_capture_enabled and claim idempotency" in post_call_surface.required_gate
    assert "duplicate-prevention claim tests" in post_call_surface.required_evidence

    assert "expose no Jobber write tool" in voice_surface.current_behavior
    assert "Jobber exposes no write tool" in voice_surface.required_gate
    assert "Jobber write tool attempts are rejected as unknown tools" in voice_surface.required_evidence
