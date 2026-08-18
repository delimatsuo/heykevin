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
