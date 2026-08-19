import importlib.util
from pathlib import Path

import pytest


def _load_module():
    script = Path(__file__).resolve().parents[2] / "scripts" / "set_sms_compliance_status.py"
    spec = importlib.util.spec_from_file_location("set_sms_compliance_status", script)
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
        self._store.docs[self._doc_id].update(updates)


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
        "electus": {"business_name": "Electus USA"},
        "other": {"business_name": "Other Co", "sms_compliance_status": "pending"},
        "dup_a": {"business_name": "Twin Co"},
        "dup_b": {"business_name": "Twin Co"},
    }


def _factory(client):
    def _make(**kwargs):
        return client

    return _make


def test_dry_run_reports_target_without_writing(capsys):
    module = _load_module()
    client = _FakeClient(_docs())

    code = module.main(
        ["--project", "p", "--contractor-id", "electus", "--status", "approved"],
        client_factory=_factory(client),
    )

    assert code == 0
    assert client.writes == []
    out = capsys.readouterr().out
    assert "Electus USA" in out
    assert "Dry run" in out


def test_apply_writes_the_field():
    module = _load_module()
    client = _FakeClient(_docs())

    code = module.main(
        ["--project", "p", "--contractor-id", "electus", "--status", "approved", "--apply"],
        client_factory=_factory(client),
    )

    assert code == 0
    assert client.writes == [("electus", {"sms_compliance_status": "approved"})]
    assert client.docs["electus"]["sms_compliance_status"] == "approved"


def test_business_name_selector_resolves_unique_match():
    module = _load_module()
    client = _FakeClient(_docs())

    code = module.main(
        ["--project", "p", "--business-name", "Electus USA", "--status", "approved", "--apply"],
        client_factory=_factory(client),
    )

    assert code == 0
    assert client.writes == [("electus", {"sms_compliance_status": "approved"})]


def test_ambiguous_selector_refuses_to_write():
    module = _load_module()
    client = _FakeClient(_docs())

    code = module.main(
        ["--project", "p", "--business-name", "Twin Co", "--status", "approved", "--apply"],
        client_factory=_factory(client),
    )

    assert code == 1
    assert client.writes == []


def test_no_match_refuses_to_write():
    module = _load_module()
    client = _FakeClient(_docs())

    code = module.main(
        ["--project", "p", "--contractor-id", "nope", "--status", "approved", "--apply"],
        client_factory=_factory(client),
    )

    assert code == 1
    assert client.writes == []


def test_already_set_value_is_a_noop():
    module = _load_module()
    client = _FakeClient(_docs())

    code = module.main(
        ["--project", "p", "--contractor-id", "other", "--status", "pending", "--apply"],
        client_factory=_factory(client),
    )

    assert code == 0
    assert client.writes == []


def test_requires_exactly_one_selector():
    module = _load_module()
    client = _FakeClient(_docs())

    with pytest.raises(SystemExit):
        module.main(
            ["--project", "p", "--status", "approved"],
            client_factory=_factory(client),
        )

    with pytest.raises(SystemExit):
        module.main(
            [
                "--project", "p",
                "--contractor-id", "electus",
                "--business-name", "Electus USA",
                "--status", "approved",
            ],
            client_factory=_factory(client),
        )

    assert client.writes == []


def test_rejects_status_outside_the_settable_set():
    module = _load_module()
    client = _FakeClient(_docs())

    with pytest.raises(SystemExit):
        module.main(
            ["--project", "p", "--contractor-id", "electus", "--status", "missing"],
            client_factory=_factory(client),
        )

    assert client.writes == []


def test_there_is_no_bulk_update_mode():
    """A bulk mode would attest compliance for accounts nobody checked."""
    module = _load_module()

    parser_actions = module.main.__doc__ or ""
    assert "--all" not in parser_actions
    source = (
        Path(__file__).resolve().parents[2] / "scripts" / "set_sms_compliance_status.py"
    ).read_text()
    assert "--all" not in source
