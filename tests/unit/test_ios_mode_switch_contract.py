"""Source contract for the existing-number iOS mode-switch path."""

from pathlib import Path


ONBOARDING_SOURCE = Path("ios/Kevin/Views/OnboardingView.swift")


def test_existing_number_fast_path_persists_mode_before_updating_local_state():
    source = ONBOARDING_SOURCE.read_text()
    provision = source.split("private func provision(mode: String) async {", 1)[1]
    provision = provision.split(
        "private func prepareBusinessDraftProfile() async -> Bool", 1
    )[0]
    fast_path = provision.split("// Fast-path:", 1)[1]
    fast_path = fast_path.split("if contractorId.isEmpty {", 1)[0]

    persistence = 'body: ["mode": mode]'
    local_update = "appState.mode = mode"

    assert persistence in fast_path
    assert local_update in fast_path
    assert fast_path.index(persistence) < fast_path.index(local_update)
