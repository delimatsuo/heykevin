"""RTDB's REST surface for firebase-admin's real Reference and Query.

Tests build `firebase_admin.db.Reference(path=..., client=FakeRtdbClient(...))`, so
firebase-admin's own transaction, set_if_unchanged and query code runs, including
its refusal to write None. ETags change on every write; a PUT or DELETE carrying a
stale if-match fails with 412. As on the real server, one without if-match is
applied unconditionally (requests drops a None header).
"""

import copy
import hashlib
import json
from types import SimpleNamespace


class FakeRtdbClient:
    def __init__(self, records, root="active_calls"):
        self.root = root
        self.records = copy.deepcopy(records)
        self.record_reads = []
        self.send_etag = True
        # Hooks run once, e.g. a live call refreshing its record mid-operation.
        self.before_read = None
        self.before_write = None

    def _key(self, url):
        return url.removeprefix(f"/{self.root}").removesuffix(".json").strip("/")

    def _etag(self, key):
        value = self.records if not key else self.records.get(key)
        return '"' + hashlib.sha1(json.dumps(value, sort_keys=True).encode()).hexdigest() + '"'

    def _precondition(self, key, headers):
        from firebase_admin.exceptions import FailedPreconditionError
        if self.before_write:
            hook, self.before_write = self.before_write, None
            hook(self.records)
        expected = (headers or {}).get("if-match")
        if expected is not None and expected != self._etag(key):
            response = SimpleNamespace(headers={"ETag": self._etag(key)},
                                       json=lambda: copy.deepcopy(self.records.get(key)))
            raise FailedPreconditionError("412 Precondition Failed", http_response=response)

    def body(self, method, url, **kwargs):  # get() and ordered-by-key pages
        assert method == "get"
        key = self._key(url)
        return copy.deepcopy(self.records if not key else self.records.get(key))

    def headers_and_body(self, method, url, headers=None, **kwargs):  # get(etag=True)
        assert method == "get" and headers == {"X-Firebase-ETag": "true"}
        if self.before_read:
            hook, self.before_read = self.before_read, None
            hook(self.records)
        key = self._key(url)
        self.record_reads.append(key)
        response_headers = {"ETag": self._etag(key)} if self.send_etag else {}
        return response_headers, copy.deepcopy(self.records.get(key))

    def headers(self, method, url, json=None, headers=None, **kwargs):  # set_if_unchanged
        assert method == "put"
        key = self._key(url)
        self._precondition(key, headers)
        self.records[key] = copy.deepcopy(json)
        return {"ETag": self._etag(key)}

    def request(self, method, url, headers=None, **kwargs):  # conditional DELETE
        assert method == "delete", method
        key = self._key(url)
        self._precondition(key, headers)
        self.records.pop(key, None)


def use_fake_rtdb(monkeypatch, **roots):
    """Route firebase_admin.db.reference to one FakeRtdbClient per root path.

    use_fake_rtdb(monkeypatch, active_calls={...}, call_commands={...}) returns the
    clients by root name.
    """
    from firebase_admin import db as rtdb

    from app.db import cache
    from app.services import legacy_call_commands, owner_call_actions

    fakes = {root: FakeRtdbClient(records, root=root) for root, records in roots.items()}
    for module in (cache, owner_call_actions, legacy_call_commands):
        monkeypatch.setattr(module, "_init_firebase", lambda: None)

    def reference(path):
        root = path.strip("/").split("/")[0]
        return rtdb.Reference(path=path, client=fakes[root])

    monkeypatch.setattr(rtdb, "reference", reference)
    return fakes
