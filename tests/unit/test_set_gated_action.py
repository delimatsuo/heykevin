"""Enabling a gated action is an operator act with real blast radius.

`check_gated_action` reads `gated_actions[<action>]` and
`automation_approvals[<action>]`; both are in PROTECTED_FIELDS so no client
can set them, and nothing else in the repo writes them. Turning one on sends
side effects to real callers, so the script is single-target, dry-run by
default, and must never disturb sibling actions in the same map.
"""

import importlib.util
from pathlib import Path

import pytest


def _load_module():
    script = Path(__file__).resolve().parents[2] / "scripts" / "set_gated_action.py"
    spec = importlib.util.spec_from_file_location("set_gated_action", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeDoc:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    def to_dict(self):
        return dict(self._data)


class _FakeDocRef:
    def __init__(self, store, doc_id):
        self._store = store
        self._doc_id = doc_id

    def update(self, updates):
        self._store.writes.append((self._doc_id, dict(updates)))


class _FakeCollection:
    def __init__(self, store):
        self._store = store

    def stream(self, **kwargs):
        return [_FakeDoc(doc_id, data) for doc_id, data in self._store.docs.items()]

    def document(self, doc_id):
        return _FakeDocRef(self._store, doc_id)


class _FakeClient:
    def __init__(self, docs):
        self.docs = docs
        self.writes = []

    def collection(self, name):
        assert name == "contractors"
        return _FakeCollection(self)


def _docs():
    return {
        "electus": {
            "business_name": "Electus USA",
            "services": [{"name": "Toilet replacement", "price_min": 175, "price_max": 650}],
            # A sibling action already enabled: the write must not clobber it.
            "gated_actions": {"google_create_event": True},
        },
        "noservices": {"business_name": "No Services Co", "services": []},
        "dup_a": {"business_name": "Twin Co"},
        "dup_b": {"business_name": "Twin Co"},
    }


def _factory(client):
    def _make(**kwargs):
        return client

    return _make


def _args(*extra):
    return ["--project", "p", "--action", "estimate_token_create", *extra]


def test_dry_run_reports_without_writing(capsys):
    module = _load_module()
    client = _FakeClient(_docs())

    code = module.main(
        _args("--contractor-id", "electus"), client_factory=_factory(client)
    )

    assert code == 0
    assert client.writes == []
    out = capsys.readouterr().out
    assert "Electus USA" in out
    assert "Dry run" in out


def test_apply_sets_the_flag():
    module = _load_module()
    client = _FakeClient(_docs())

    code = module.main(
        _args("--contractor-id", "electus", "--apply"), client_factory=_factory(client)
    )

    assert code == 0
    _, updates = client.writes[0]
    assert updates["gated_actions.estimate_token_create"] is True


def test_automation_approval_is_opt_in():
    """requires_owner_confirmation actions need it; others must not get it silently."""
    module = _load_module()

    without = _FakeClient(_docs())
    module.main(_args("--contractor-id", "electus", "--apply"), client_factory=_factory(without))
    assert "automation_approvals.estimate_token_create" not in without.writes[0][1]

    with_approval = _FakeClient(_docs())
    module.main(
        _args("--contractor-id", "electus", "--apply", "--approve-automation"),
        client_factory=_factory(with_approval),
    )
    assert with_approval.writes[0][1]["automation_approvals.estimate_token_create"] is True


def test_write_uses_dotted_paths_so_sibling_actions_survive():
    """A whole-map write would silently disable every other enabled action."""
    module = _load_module()
    client = _FakeClient(_docs())

    module.main(
        _args("--contractor-id", "electus", "--apply", "--approve-automation"),
        client_factory=_factory(client),
    )

    _, updates = client.writes[0]
    # Dotted field paths only — never a bare "gated_actions" map replacement.
    assert "gated_actions" not in updates
    assert "automation_approvals" not in updates
    assert all(
        key.startswith(("gated_actions.", "automation_approvals.")) or key.startswith("gated_actions_")
        for key in updates
    )


def test_disable_sets_false_rather_than_deleting():
    module = _load_module()
    client = _FakeClient(_docs())

    module.main(
        _args("--contractor-id", "electus", "--apply", "--disable"),
        client_factory=_factory(client),
    )

    _, updates = client.writes[0]
    assert updates["gated_actions.estimate_token_create"] is False


def test_provenance_recorded():
    module = _load_module()
    client = _FakeClient(_docs())

    module.main(
        _args("--contractor-id", "electus", "--apply", "--note", "electus-first-test"),
        client_factory=_factory(client),
    )

    _, updates = client.writes[0]
    assert updates["gated_actions_source"] == "cli:electus-first-test"
    assert updates["gated_actions_updated_at"] > 0


def test_empty_services_warns_because_the_offer_is_skipped(capsys):
    """Enabling the gate alone produces nothing when services is empty."""
    module = _load_module()
    client = _FakeClient(_docs())

    module.main(
        _args("--contractor-id", "noservices"), client_factory=_factory(client)
    )

    assert "no services configured" in capsys.readouterr().err


def test_ambiguous_selector_refuses_to_write():
    module = _load_module()
    client = _FakeClient(_docs())

    code = module.main(
        _args("--business-name", "Twin Co", "--apply"), client_factory=_factory(client)
    )

    assert code == 1
    assert client.writes == []


def test_no_match_refuses_to_write():
    module = _load_module()
    client = _FakeClient(_docs())

    code = module.main(_args("--contractor-id", "nope", "--apply"), client_factory=_factory(client))

    assert code == 1
    assert client.writes == []


def test_requires_exactly_one_selector():
    module = _load_module()
    client = _FakeClient(_docs())

    with pytest.raises(SystemExit):
        module.main(_args(), client_factory=_factory(client))

    with pytest.raises(SystemExit):
        module.main(
            _args("--contractor-id", "electus", "--business-name", "Electus USA"),
            client_factory=_factory(client),
        )

    assert client.writes == []


def test_unknown_action_is_rejected():
    module = _load_module()
    client = _FakeClient(_docs())

    with pytest.raises(SystemExit):
        module.main(
            ["--project", "p", "--action", "not_a_real_action", "--contractor-id", "electus"],
            client_factory=_factory(client),
        )

    assert client.writes == []


def test_there_is_no_bulk_mode():
    module = _load_module()
    source = (
        Path(__file__).resolve().parents[2] / "scripts" / "set_gated_action.py"
    ).read_text()
    assert "--all" not in source
    assert module is not None
