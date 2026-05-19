# Admin Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the temporary `/admin` monitor with a proper operational admin dashboard for support, billing, Twilio number management, call diagnostics, and audit-safe admin actions.

**Architecture:** Keep the first version as a backend-served internal dashboard to avoid introducing a frontend build system. Split the current single static file into HTML, CSS, and JavaScript assets, add typed FastAPI admin endpoints, and keep all mutations behind command-specific handlers with durable audit events.

**Tech Stack:** FastAPI, Firestore, Firebase RTDB, Twilio Python SDK, static HTML/CSS/JavaScript, pytest.

---

## Current State

- `/admin` serves `app/static/admin.html`.
- `app/api/admin.py` exposes overview, contractor list, call stats, extend trial, and revoke.
- The existing admin contractor list was fixed separately to avoid the Firestore composite-index `500`.
- Admin auth is currently the global bearer token through `app/middleware/auth.py`.
- There is no durable audit log for admin mutations.

## MVP Scope

Build these dashboard sections first:

- Command Center: operational cards and needs-attention alerts.
- Contractors: searchable/filterable list and contractor detail view.
- Calls: recent calls and per-contractor call history.
- Twilio Numbers: live Twilio inventory reconciled against Firestore contractors.
- Subscriptions: status/tier/expiry diagnostics and safer trial extension.
- Audit Log: every admin mutation with actor, reason, before/after.

Defer these until after the MVP:

- Google/OIDC or IAP admin login with roles.
- Transcript access roles and privacy review workflow.
- Force App Store receipt repair/rebinding.
- Synthetic provider health checks.
- Full call lifecycle event ledger.
- Revenue analytics and cohort exports.

## File Structure

- Modify `app/api/admin.py`: admin read endpoints and command-specific mutation endpoints.
- Create `app/db/admin_audit.py`: durable admin audit writes and reads.
- Create `app/services/admin_numbers.py`: Twilio inventory reconciliation helpers.
- Create `app/services/admin_diagnostics.py`: contractor support diagnostics helper.
- Modify `app/static/admin.html`: shell markup only.
- Create `app/static/admin.css`: dashboard layout and states.
- Create `app/static/admin.js`: fetch helpers, rendering, filters, modals, actions.
- Modify `app/main.py`: serve split admin static assets if needed.
- Create `tests/unit/test_admin_audit.py`: audit behavior.
- Extend `tests/unit/test_admin_api.py`: admin read/mutation behavior.
- Create `tests/unit/test_admin_numbers.py`: Twilio inventory reconciliation.

---

### Task 1: Keep The Admin Contractor 500 Fixed

**Files:**
- Already modified: `app/api/admin.py`
- Already added: `tests/unit/test_admin_api.py`

- [ ] **Step 1: Confirm the regression test exists**

The test must cover Firestore rejecting the old composite query:

```python
@pytest.mark.asyncio
async def test_admin_contractors_does_not_require_created_at_composite_index(monkeypatch):
    ...
    response = await admin_api.admin_list_contractors(_admin_request())
    assert response["count"] == 2
```

- [ ] **Step 2: Run the focused admin test**

Run:

```bash
pytest tests/unit/test_admin_api.py -q
```

Expected: `1 passed`.

- [ ] **Step 3: Run all backend tests**

Run:

```bash
pytest --tb=short -q
```

Expected: all tests pass.

---

### Task 2: Add Durable Admin Audit Events

**Files:**
- Create: `app/db/admin_audit.py`
- Test: `tests/unit/test_admin_audit.py`
- Modify: `app/api/admin.py`

- [ ] **Step 1: Write failing audit tests**

Create `tests/unit/test_admin_audit.py`:

```python
from types import SimpleNamespace

import pytest

from app.db import admin_audit


class _FakeDocRef:
    def __init__(self):
        self.written = None

    def set(self, data):
        self.written = data


class _FakeCollection:
    def __init__(self):
        self.doc_ref = _FakeDocRef()

    def document(self):
        return self.doc_ref


class _FakeFirestore:
    def __init__(self):
        self.collections = {"admin_audit_events": _FakeCollection()}

    def collection(self, name):
        return self.collections[name]


def _request():
    return SimpleNamespace(
        state=SimpleNamespace(is_admin=True),
        headers={"user-agent": "pytest"},
        client=SimpleNamespace(host="127.0.0.1"),
    )


@pytest.mark.asyncio
async def test_write_admin_audit_event_persists_actor_action_reason(monkeypatch):
    fake_db = _FakeFirestore()
    monkeypatch.setattr(admin_audit, "get_firestore_client", lambda: fake_db)

    await admin_audit.write_admin_audit_event(
        request=_request(),
        action="extend_trial",
        target_type="contractor",
        target_id="contractor-1",
        reason="customer support request",
        before={"subscription_status": "expired"},
        after={"subscription_status": "trial"},
    )

    written = fake_db.collections["admin_audit_events"].doc_ref.written
    assert written["action"] == "extend_trial"
    assert written["target_id"] == "contractor-1"
    assert written["reason"] == "customer support request"
    assert written["before"] == {"subscription_status": "expired"}
    assert written["after"] == {"subscription_status": "trial"}
    assert written["actor_type"] == "global_admin_token"
    assert written["user_agent"] == "pytest"
    assert "created_at" in written
```

- [ ] **Step 2: Run the failing audit test**

Run:

```bash
pytest tests/unit/test_admin_audit.py -q
```

Expected: import failure because `app/db/admin_audit.py` does not exist yet.

- [ ] **Step 3: Implement audit writer**

Create `app/db/admin_audit.py`:

```python
"""Durable admin audit events."""

import asyncio
import hashlib
import time
from typing import Any

from app.db.firestore_client import get_firestore_client

COLLECTION = "admin_audit_events"


def _client_ip_hash(request) -> str:
    host = getattr(getattr(request, "client", None), "host", "") or ""
    if not host:
        return ""
    return hashlib.sha256(host.encode("utf-8")).hexdigest()


async def write_admin_audit_event(
    *,
    request,
    action: str,
    target_type: str,
    target_id: str,
    reason: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    db = get_firestore_client()
    headers = getattr(request, "headers", {}) or {}
    event = {
        "actor_type": "global_admin_token",
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "reason": reason,
        "before": before or {},
        "after": after or {},
        "metadata": metadata or {},
        "ip_hash": _client_ip_hash(request),
        "user_agent": headers.get("user-agent", ""),
        "created_at": time.time(),
    }

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: db.collection(COLLECTION).document().set(event),
    )
```

- [ ] **Step 4: Verify audit tests pass**

Run:

```bash
pytest tests/unit/test_admin_audit.py -q
```

Expected: `1 passed`.

---

### Task 3: Require Reasons For Admin Mutations

**Files:**
- Modify: `app/api/admin.py`
- Test: `tests/unit/test_admin_api.py`

- [ ] **Step 1: Add failing tests for required reasons**

Append to `tests/unit/test_admin_api.py`:

```python
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_extend_trial_requires_reason():
    with pytest.raises(HTTPException) as exc:
        await admin_api.admin_extend_trial(
            "contractor-1",
            admin_api.ExtendTrialRequest(days=7, reason=""),
            _admin_request(),
        )
    assert exc.value.status_code == 422
```

- [ ] **Step 2: Run the failing test**

Run:

```bash
pytest tests/unit/test_admin_api.py::test_extend_trial_requires_reason -q
```

Expected: failure because `ExtendTrialRequest` has no `reason` field.

- [ ] **Step 3: Update request model**

Modify `app/api/admin.py`:

```python
class ExtendTrialRequest(BaseModel):
    days: int = Field(..., ge=1, le=30)
    reason: str = Field(..., min_length=3, max_length=500)
```

- [ ] **Step 4: Add audit call after trial extension**

In `admin_extend_trial`, capture `before` from the contractor doc and call:

```python
await write_admin_audit_event(
    request=request,
    action="extend_trial",
    target_type="contractor",
    target_id=contractor_id,
    reason=body.reason,
    before=before,
    after={"subscription_status": "trial", "subscription_expires": new_expires},
)
```

- [ ] **Step 5: Repeat for revoke**

Create a `RevokeContractorRequest` with:

```python
class RevokeContractorRequest(BaseModel):
    reason: str = Field(..., min_length=3, max_length=500)
    mode: str = Field(default="expire_now", pattern="^(expire_now|disable_account)$")
```

Change the route signature to accept `body: RevokeContractorRequest` and audit the before/after state.

- [ ] **Step 6: Run mutation tests**

Run:

```bash
pytest tests/unit/test_admin_api.py tests/unit/test_admin_audit.py -q
```

Expected: all admin tests pass.

---

### Task 4: Add Contractor List Filters And Detail Endpoint

**Files:**
- Modify: `app/api/admin.py`
- Test: `tests/unit/test_admin_api.py`

- [ ] **Step 1: Add failing contractor filter test**

Append:

```python
@pytest.mark.asyncio
async def test_admin_contractors_filters_by_subscription_status(monkeypatch):
    fake_db = _FakeFirestore([
        _FakeDoc("trial-user", {"active": True, "subscription_status": "trial", "created_at": 2}),
        _FakeDoc("paid-user", {"active": True, "subscription_status": "active", "created_at": 1}),
    ])
    monkeypatch.setattr(admin_api, "get_firestore_client", lambda: fake_db)

    response = await admin_api.admin_list_contractors(_admin_request(), status="trial")

    assert [item["contractor_id"] for item in response["contractors"]] == ["trial-user"]
```

- [ ] **Step 2: Update endpoint signature**

Modify `admin_list_contractors`:

```python
async def admin_list_contractors(
    request: Request,
    status: Optional[str] = None,
    tier: Optional[str] = None,
    has_twilio: Optional[bool] = None,
    q: Optional[str] = None,
    limit: int = 200,
):
```

- [ ] **Step 3: Apply in-process filters after active query**

Add a helper in `app/api/admin.py`:

```python
def _matches_contractor_filters(item: dict, *, status, tier, has_twilio, q) -> bool:
    if status and item.get("subscription_status") != status:
        return False
    if tier and item.get("subscription_tier") != tier:
        return False
    if has_twilio is True and not item.get("twilio_number"):
        return False
    if has_twilio is False and item.get("twilio_number"):
        return False
    if q:
        haystack = " ".join([
            item.get("contractor_id", ""),
            item.get("business_name", ""),
            item.get("owner_name", ""),
            item.get("twilio_number", ""),
        ]).lower()
        return q.lower() in haystack
    return True
```

- [ ] **Step 4: Add detail endpoint**

Add:

```python
@router.get("/contractors/{contractor_id}")
async def admin_get_contractor_detail(contractor_id: str, request: Request):
    _require_admin(request)
    ...
```

Return contractor profile, device summary from `contractors/{id}/devices/primary`, and recent audit events. Mask secrets and never return API token hashes.

- [ ] **Step 5: Run tests**

Run:

```bash
pytest tests/unit/test_admin_api.py -q
```

Expected: all admin API tests pass.

---

### Task 5: Add Recent Calls Endpoints

**Files:**
- Modify: `app/api/admin.py`
- Test: `tests/unit/test_admin_api.py`

- [ ] **Step 1: Add endpoint contracts**

Implement:

```python
@router.get("/calls")
async def admin_list_calls(request: Request, limit: int = 50):
    ...


@router.get("/contractors/{contractor_id}/calls")
async def admin_list_contractor_calls(contractor_id: str, request: Request, limit: int = 50):
    ...
```

- [ ] **Step 2: Response fields**

Each call item should return:

```python
{
    "call_sid": call_sid,
    "contractor_id": contractor_id,
    "timestamp": timestamp,
    "caller_phone": redacted_phone,
    "caller_name": caller_name,
    "outcome": outcome,
    "route": route,
    "duration_seconds": duration_seconds,
    "has_summary": bool(summary),
    "has_transcript": bool(transcript),
}
```

- [ ] **Step 3: Do not return transcripts in list responses**

Transcript access needs a separate endpoint and role model later. For MVP, show only `has_transcript`.

- [ ] **Step 4: Run tests**

Run:

```bash
pytest tests/unit/test_admin_api.py -q
```

Expected: all admin API tests pass.

---

### Task 6: Add Twilio Number Inventory Reconciliation

**Files:**
- Create: `app/services/admin_numbers.py`
- Modify: `app/api/admin.py`
- Test: `tests/unit/test_admin_numbers.py`

- [ ] **Step 1: Write failing reconciliation test**

Create `tests/unit/test_admin_numbers.py`:

```python
from app.services import admin_numbers


def test_reconcile_numbers_flags_orphan_and_missing_numbers():
    contractors = [
        {"contractor_id": "has-number", "active": True, "twilio_number": "+15550001111"},
        {"contractor_id": "missing-number", "active": True, "twilio_number": ""},
    ]
    twilio_numbers = [
        {"phone_number": "+15550001111", "sid": "PN1", "voice_url": "https://prod/webhooks/twilio/incoming"},
        {"phone_number": "+15550002222", "sid": "PN2", "voice_url": "https://prod/webhooks/twilio/incoming"},
    ]

    result = admin_numbers.reconcile_number_inventory(contractors, twilio_numbers)

    assert result["summary"]["assigned_numbers"] == 1
    assert result["summary"]["contractors_missing_numbers"] == 1
    assert result["summary"]["orphan_numbers"] == 1
    assert result["orphan_numbers"][0]["phone_number"] == "+15550002222"
```

- [ ] **Step 2: Implement pure reconciliation helper**

Create `app/services/admin_numbers.py`:

```python
"""Admin Twilio number inventory helpers."""


def reconcile_number_inventory(contractors: list[dict], twilio_numbers: list[dict]) -> dict:
    contractor_by_number = {
        c.get("twilio_number"): c
        for c in contractors
        if c.get("active") and c.get("twilio_number")
    }
    twilio_by_number = {
        n.get("phone_number"): n
        for n in twilio_numbers
        if n.get("phone_number")
    }

    missing = [
        c for c in contractors
        if c.get("active") and not c.get("twilio_number")
    ]
    orphan = [
        n for number, n in twilio_by_number.items()
        if number not in contractor_by_number
    ]
    assigned = [
        {
            "phone_number": number,
            "contractor_id": contractor.get("contractor_id"),
            "twilio": twilio_by_number.get(number, {}),
        }
        for number, contractor in contractor_by_number.items()
        if number in twilio_by_number
    ]

    return {
        "summary": {
            "assigned_numbers": len(assigned),
            "contractors_missing_numbers": len(missing),
            "orphan_numbers": len(orphan),
        },
        "assigned_numbers": assigned,
        "contractors_missing_numbers": missing,
        "orphan_numbers": orphan,
    }
```

- [ ] **Step 3: Add read-only numbers endpoint**

Add to `app/api/admin.py`:

```python
@router.get("/numbers")
async def admin_number_inventory(request: Request):
    _require_admin(request)
    ...
```

Fetch active contractors from Firestore and Twilio incoming phone numbers from the Twilio SDK. Return the reconciliation result.

- [ ] **Step 4: Run tests**

Run:

```bash
pytest tests/unit/test_admin_numbers.py -q
```

Expected: all number tests pass.

---

### Task 7: Split The Static Admin Frontend

**Files:**
- Modify: `app/static/admin.html`
- Create: `app/static/admin.css`
- Create: `app/static/admin.js`

- [ ] **Step 1: Preserve current auth flow**

Keep the token overlay in the MVP, but move JavaScript into `admin.js` and styles into `admin.css`.

- [ ] **Step 2: Build navigation tabs**

Tabs:

- Command
- Contractors
- Calls
- Numbers
- Subscriptions
- Audit

- [ ] **Step 3: Add state object in `admin.js`**

```javascript
const state = {
  token: sessionStorage.getItem('adminToken') || '',
  contractors: [],
  calls: [],
  numbers: null,
  auditEvents: [],
  filters: {
    contractorStatus: '',
    contractorTier: '',
    hasTwilio: '',
    query: '',
  },
};
```

- [ ] **Step 4: Render empty, loading, error, and success states**

Each tab should have visible loading and error states. The contractor table must still render if one secondary panel fails.

- [ ] **Step 5: Add action modals**

Medium/high-risk actions require:

- visible before state
- reason input
- explicit confirmation button disabled until reason length is at least 3
- typed confirmation for revoke/release-number later

---

### Task 8: Add Support Diagnostics

**Files:**
- Create: `app/services/admin_diagnostics.py`
- Modify: `app/api/admin.py`
- Test: `tests/unit/test_admin_api.py`

- [ ] **Step 1: Add pure diagnostic helper**

Create:

```python
def diagnose_contractor(contractor: dict, device: dict | None, recent_calls: list[dict]) -> list[dict]:
    findings = []
    if not contractor.get("twilio_number"):
        findings.append({"severity": "critical", "code": "missing_twilio_number"})
    if contractor.get("subscription_status") == "expired":
        findings.append({"severity": "warning", "code": "subscription_expired"})
    if not device or not device.get("voip_token_present"):
        findings.append({"severity": "warning", "code": "missing_voip_token"})
    if not recent_calls:
        findings.append({"severity": "info", "code": "no_recent_calls"})
    return findings
```

- [ ] **Step 2: Include diagnostics in contractor detail**

`GET /api/admin/contractors/{id}` should include:

```python
{
    "contractor": ...,
    "device": ...,
    "recent_calls": ...,
    "diagnostics": [...]
}
```

- [ ] **Step 3: Render diagnostics on contractor detail**

In `admin.js`, show diagnostics as an ordered "needs attention" list above raw profile fields.

---

### Task 9: Add Audit Log View

**Files:**
- Modify: `app/db/admin_audit.py`
- Modify: `app/api/admin.py`
- Modify: `app/static/admin.js`
- Test: `tests/unit/test_admin_audit.py`

- [ ] **Step 1: Add `list_admin_audit_events`**

In `app/db/admin_audit.py`:

```python
async def list_admin_audit_events(limit: int = 100) -> list[dict]:
    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    docs = await loop.run_in_executor(
        None,
        lambda: list(
            db.collection(COLLECTION)
            .order_by("created_at", direction="DESCENDING")
            .limit(limit)
            .stream()
        ),
    )
    return [{"id": doc.id, **(doc.to_dict() or {})} for doc in docs]
```

- [ ] **Step 2: Add endpoint**

```python
@router.get("/audit-events")
async def admin_audit_events(request: Request, limit: int = 100):
    _require_admin(request)
    return {"events": await list_admin_audit_events(limit=limit)}
```

- [ ] **Step 3: Render audit tab**

Show timestamp, action, target, reason, and actor type. Hide before/after behind a disclosure.

---

### Task 10: Verification And Release

**Files:**
- Modify as needed from previous tasks.

- [ ] **Step 1: Run full backend tests**

Run:

```bash
pytest --tb=short -q
```

Expected: all tests pass.

- [ ] **Step 2: Run the app locally**

Run:

```bash
uvicorn app.main:app --reload --port 8080
```

Open:

```text
http://127.0.0.1:8080/admin
```

Expected: dashboard loads, token prompt appears, and each tab shows loading/error/success states cleanly.

- [ ] **Step 3: Test with production-like admin token against staging**

Use staging first. Confirm:

- contractors load
- contractor detail loads
- recent calls load
- Twilio inventory loads
- extend trial requires reason and writes audit
- revoke requires reason and writes audit

- [ ] **Step 4: Deploy backend**

Use the existing GitHub Actions production workflow after merge to `main`:

```bash
gh workflow run Deploy --ref main -f target=production
```

- [ ] **Step 5: Verify production**

Run:

```bash
curl -sf https://kevin-api-752910912062.us-central1.run.app/health
```

Then open `/admin` and verify the dashboard can load all MVP tabs.

---

## Execution Recommendation

Use subagent-driven implementation after the 500 fix is deployed:

- Agent 1: audit logging and safer mutation models.
- Agent 2: read-only admin API endpoints for contractors/calls/details.
- Agent 3: Twilio number inventory and reconciliation.
- Agent 4: static frontend split and dashboard rendering.

Merge in this order: audit first, read APIs second, Twilio inventory third, frontend last.
