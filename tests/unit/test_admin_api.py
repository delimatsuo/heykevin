"""Admin API regression tests."""

from types import SimpleNamespace

import pytest

from app.api import admin as admin_api


class _FakeDoc:
    def __init__(self, doc_id: str, data: dict):
        self.id = doc_id
        self._data = data

    def to_dict(self):
        return dict(self._data)


class _FakeQuery:
    def __init__(self, docs: list[_FakeDoc]):
        self._docs = docs

    def where(self, field_path, op_string=None, value=None, **_kwargs):
        if field_path == "active" and op_string == "==" and value is True:
            return _FakeQuery([doc for doc in self._docs if doc.to_dict().get("active") is True])
        return self

    def order_by(self, *_args, **_kwargs):
        raise RuntimeError("Firestore missing composite index")

    def limit(self, value: int):
        return _FakeQuery(self._docs[:value])

    def stream(self):
        return list(self._docs)


class _FakeCollection:
    def __init__(self, docs: list[_FakeDoc]):
        self._docs = docs

    def where(self, *args, **kwargs):
        return _FakeQuery(self._docs).where(*args, **kwargs)


class _FakeFirestore:
    def __init__(self, docs: list[_FakeDoc]):
        self._docs = docs

    def collection(self, name: str):
        assert name == "contractors"
        return _FakeCollection(self._docs)


def _admin_request():
    return SimpleNamespace(state=SimpleNamespace(is_admin=True))


@pytest.mark.asyncio
async def test_admin_contractors_does_not_require_created_at_composite_index(monkeypatch):
    fake_db = _FakeFirestore([
        _FakeDoc("old-active", {
            "active": True,
            "owner_name": "Old Active",
            "created_at": 100,
            "subscription_status": "trial",
        }),
        _FakeDoc("inactive", {
            "active": False,
            "owner_name": "Inactive",
            "created_at": 300,
        }),
        _FakeDoc("new-active", {
            "active": True,
            "owner_name": "New Active",
            "created_at": 200,
            "subscription_status": "active",
        }),
    ])
    monkeypatch.setattr(admin_api, "get_firestore_client", lambda: fake_db)

    response = await admin_api.admin_list_contractors(_admin_request())

    assert response["count"] == 2
    assert [item["contractor_id"] for item in response["contractors"]] == [
        "new-active",
        "old-active",
    ]
