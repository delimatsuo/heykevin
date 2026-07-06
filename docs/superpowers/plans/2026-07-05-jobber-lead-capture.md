# Jobber Lead Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace unsafe Jobber job/booking behavior with a v1 lead-capture flow that creates Jobber Clients, Requests, and Request notes after qualified service-request calls.

**Architecture:** Jobber v1 is post-call only. Live-call Jobber tools keep caller lookup but do not expose availability or booking. Post-call processing gates remote writes behind a protected per-contractor flag, claims local sync idempotently by local job record, then runs `lookup client -> create client if needed -> create request -> create request note`.

**Tech Stack:** Python 3.12, FastAPI service modules, Firestore-backed local job records, Jobber GraphQL API version `2025-04-16`, pytest.

---

## File Structure

- Modify `app/services/jobber.py`: Jobber GraphQL version header, phone-search lookup, Client/Request/Request-note mutations, user-error handling, lead-note formatting helpers.
- Modify `app/services/post_call.py`: replace Jobber Job creation with gated Request lead capture.
- Modify `app/services/voice_pipeline.py`: keep Jobber `check_customer`; remove Jobber availability and booking tools.
- Modify `app/services/gemini_pipeline.py`: mirror the Jobber tool gating for Gemini Live.
- Modify `app/db/jobs.py`: add a transactional Jobber sync claim helper for local idempotency.
- Modify `app/api/contractors.py` and `app/db/contractors.py`: protect `jobber_lead_capture_enabled` from client writes.
- Modify `tests/unit/test_jobber.py`: cover Jobber API request shape and mutation helpers.
- Create `tests/unit/test_jobber_tool_gating.py`: prove Jobber booking tools are not exposed.
- Create `tests/unit/test_jobber_post_call.py`: prove post-call lead capture is gated and idempotent.

---

### Task 1: Disable Jobber Booking Tools

**Files:**
- Modify: `app/services/voice_pipeline.py`
- Modify: `app/services/gemini_pipeline.py`
- Create: `tests/unit/test_jobber_tool_gating.py`

- [ ] **Step 1: Write failing tests for Jobber tool exposure**

Create `tests/unit/test_jobber_tool_gating.py`:

```python
"""Jobber v1 exposes caller lookup only, not scheduling or booking."""

from app.services.gemini_pipeline import GeminiPipeline
from app.services.voice_pipeline import VoicePipeline


def test_voice_pipeline_jobber_tools_do_not_expose_booking():
    names = {tool["name"] for tool in VoicePipeline.JOBBER_TOOLS}

    assert names == {"check_customer"}
    assert "check_availability" not in names
    assert "book_appointment" not in names


def test_gemini_pipeline_jobber_tools_do_not_expose_booking():
    pipeline = GeminiPipeline.__new__(GeminiPipeline)
    pipeline._contractor_config = {"jobber_access_token": "jobber-token"}

    tools = pipeline._build_gemini_tools()
    declarations = tools[0]["function_declarations"]
    names = {declaration["name"] for declaration in declarations}

    assert names == {"check_customer"}
    assert "check_availability" not in names
    assert "book_appointment" not in names
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
TWILIO_ACCOUNT_SID=test TWILIO_AUTH_TOKEN=test TWILIO_PHONE_NUMBER=+15555550100 TELEGRAM_BOT_TOKEN=test USER_PHONE=+15555550101 /Volumes/Extreme\ Pro/myprojects/Kevin/.venv/bin/python -m pytest tests/unit/test_jobber_tool_gating.py -q
```

Expected: FAIL because `check_availability` and `book_appointment` are still exposed.

- [ ] **Step 3: Remove Jobber booking tools from `VoicePipeline.JOBBER_TOOLS`**

In `app/services/voice_pipeline.py`, replace the `JOBBER_TOOLS` list with:

```python
    JOBBER_TOOLS = [
        {
            "name": "check_customer",
            "description": "Look up the caller in the business's customer database by phone number. Returns customer name and address if found.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "phone": {
                        "type": "string",
                        "description": "The caller's phone number in E.164 format (e.g. +14155551234)",
                    }
                },
                "required": ["phone"],
            },
        },
    ]
```

- [ ] **Step 4: Remove Jobber booking execution paths from `VoicePipeline._execute_tool`**

In the Jobber branch of `_execute_tool`, replace:

```python
        from app.services.jobber import lookup_customer, get_available_slots, create_job
```

with:

```python
        from app.services.jobber import lookup_customer
```

Then remove the `elif tool_name == "check_availability"` and `elif tool_name == "book_appointment"` blocks from the Jobber branch. The final Jobber branch should return the existing `check_customer` result, then fall through to:

```python
            else:
                return json.dumps({"error": f"Unknown tool: {tool_name}"})
```

- [ ] **Step 5: Remove Jobber booking declarations from Gemini**

In `app/services/gemini_pipeline.py`, replace the `if has_jobber:` declaration block with:

```python
        if has_jobber:
            declarations.append(
                {
                    "name": "check_customer",
                    "description": "Look up the caller in the business's customer database by phone number.",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "phone": {"type": "STRING", "description": "Phone number in E.164 format"}
                        },
                        "required": ["phone"],
                    },
                }
            )
```

Leave the Google Calendar declarations unchanged.

- [ ] **Step 6: Run tests to verify tool gating passes**

Run:

```bash
TWILIO_ACCOUNT_SID=test TWILIO_AUTH_TOKEN=test TWILIO_PHONE_NUMBER=+15555550100 TELEGRAM_BOT_TOKEN=test USER_PHONE=+15555550101 /Volumes/Extreme\ Pro/myprojects/Kevin/.venv/bin/python -m pytest tests/unit/test_jobber_tool_gating.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/services/voice_pipeline.py app/services/gemini_pipeline.py tests/unit/test_jobber_tool_gating.py
git commit -m "fix: disable jobber booking tools"
```

---

### Task 2: Pin Jobber GraphQL Version and Handle Mutation Errors

**Files:**
- Modify: `app/services/jobber.py`
- Modify: `tests/unit/test_jobber.py`

- [ ] **Step 1: Write failing tests for version header and user errors**

Append these tests to `tests/unit/test_jobber.py` before `_noop_async`:

```python
@pytest.mark.asyncio
async def test_graphql_request_sends_jobber_version_header(monkeypatch):
    calls = []
    responses = [_FakeResponse(200, {"data": {"viewer": {"id": "viewer-1"}}})]
    monkeypatch.setattr(jobber.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))

    data = await jobber._graphql_request("jobber-token", "query { viewer { id } }")

    assert data == {"viewer": {"id": "viewer-1"}}
    assert calls[0][1]["headers"]["X-JOBBER-GRAPHQL-VERSION"] == "2025-04-16"


def test_extract_mutation_payload_rejects_user_errors(caplog):
    payload = {
        "requestCreate": {
            "request": None,
            "userErrors": [{"message": "Client is required", "path": ["clientId"]}],
        }
    }

    with caplog.at_level(logging.WARNING):
        result = jobber._extract_mutation_object(payload, "requestCreate", "request")

    assert result is None
    assert "Jobber mutation returned user errors" in caplog.text
    assert "Client is required" not in caplog.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
TWILIO_ACCOUNT_SID=test TWILIO_AUTH_TOKEN=test TWILIO_PHONE_NUMBER=+15555550100 TELEGRAM_BOT_TOKEN=test USER_PHONE=+15555550101 /Volumes/Extreme\ Pro/myprojects/Kevin/.venv/bin/python -m pytest tests/unit/test_jobber.py -q
```

Expected: FAIL because the version header and `_extract_mutation_object` helper do not exist.

- [ ] **Step 3: Add GraphQL version constant and header**

In `app/services/jobber.py`, add below `JOBBER_TOKEN_URL`:

```python
JOBBER_GRAPHQL_VERSION = "2025-04-16"
```

Then add this header in `_graphql_request`:

```python
                    "X-JOBBER-GRAPHQL-VERSION": JOBBER_GRAPHQL_VERSION,
```

- [ ] **Step 4: Add sanitized mutation object extraction**

Add this helper above `lookup_customer` in `app/services/jobber.py`:

```python
def _extract_mutation_object(data: Optional[dict], mutation_name: str, object_name: str) -> Optional[dict]:
    """Return a mutation object only when Jobber accepted the mutation."""
    payload = (data or {}).get(mutation_name) or {}
    user_errors = payload.get("userErrors") or []
    if user_errors:
        logger.warning(
            "Jobber mutation returned user errors: mutation=%s error_count=%s",
            mutation_name,
            len(user_errors),
        )
        return None
    obj = payload.get(object_name)
    if not obj:
        logger.warning("Jobber mutation returned no object: mutation=%s object=%s", mutation_name, object_name)
        return None
    return obj
```

- [ ] **Step 5: Run tests to verify version and error handling pass**

Run:

```bash
TWILIO_ACCOUNT_SID=test TWILIO_AUTH_TOKEN=test TWILIO_PHONE_NUMBER=+15555550100 TELEGRAM_BOT_TOKEN=test USER_PHONE=+15555550101 /Volumes/Extreme\ Pro/myprojects/Kevin/.venv/bin/python -m pytest tests/unit/test_jobber.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/jobber.py tests/unit/test_jobber.py
git commit -m "fix: pin jobber graphql version"
```

---

### Task 3: Add Jobber Client, Request, and Note Helpers

**Files:**
- Modify: `app/services/jobber.py`
- Modify: `tests/unit/test_jobber.py`

- [ ] **Step 1: Write failing tests for live-schema request helpers**

Append these tests to `tests/unit/test_jobber.py` before `_noop_async`:

```python
@pytest.mark.asyncio
async def test_lookup_customer_searches_phone_fields(monkeypatch):
    calls = []
    responses = [
        _FakeResponse(
            200,
            {
                "data": {
                    "clients": {
                        "nodes": [
                            {
                                "id": "client-1",
                                "name": "Jane Private",
                                "firstName": "Jane",
                                "lastName": "Private",
                                "phones": [{"number": "+15551234567"}],
                                "emails": [],
                                "billingAddress": None,
                                "clientProperties": {"nodes": []},
                            }
                        ]
                    }
                }
            },
        )
    ]
    monkeypatch.setattr(jobber.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))

    customer = await jobber.lookup_customer("jobber-token", "+15551234567")

    assert customer["id"] == "client-1"
    assert calls[0][1]["variables"] == {"phone": "+15551234567"}
    assert "searchFields: [PHONES]" in calls[0][1]["json"]["query"]


@pytest.mark.asyncio
async def test_create_client_builds_jobber_client_payload(monkeypatch):
    calls = []
    responses = [
        _FakeResponse(
            200,
            {
                "data": {
                    "clientCreate": {
                        "client": {
                            "id": "client-1",
                            "name": "Jane Private",
                            "clientProperties": {"nodes": [{"id": "property-1"}]},
                        },
                        "userErrors": [],
                    }
                }
            },
        )
    ]
    monkeypatch.setattr(jobber.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))

    result = await jobber.create_client(
        "jobber-token",
        {
            "caller_name": "Jane Private",
            "caller_phone": "+15551234567",
            "address": "123 Main Street, Denver CO",
        },
    )

    assert result == {"id": "client-1", "name": "Jane Private", "property_id": "property-1"}
    input_payload = calls[0][1]["json"]["variables"]["input"]
    assert input_payload["firstName"] == "Jane"
    assert input_payload["lastName"] == "Private"
    assert input_payload["phones"] == [{"number": "+15551234567", "primary": True, "smsAllowed": True}]
    assert input_payload["sourceAttribution"] == {"sourceText": "Hey Kevin"}
    assert input_payload["properties"] == [{"address": {"street1": "123 Main Street, Denver CO"}}]


@pytest.mark.asyncio
async def test_create_request_and_note(monkeypatch):
    calls = []
    responses = [
        _FakeResponse(
            200,
            {
                "data": {
                    "requestCreate": {
                        "request": {"id": "request-1", "title": "Leaking sink", "jobberWebUri": "https://example.test/request"},
                        "userErrors": [],
                    }
                }
            },
        ),
        _FakeResponse(
            200,
            {
                "data": {
                    "requestCreateNote": {
                        "request": {"id": "request-1"},
                        "requestNote": {"id": "note-1"},
                        "userErrors": [],
                    }
                }
            },
        ),
    ]
    monkeypatch.setattr(jobber.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))

    request = await jobber.create_request(
        "jobber-token",
        {
            "client_id": "client-1",
            "property_id": "property-1",
            "title": "Leaking sink",
        },
    )
    note_id = await jobber.create_request_note("jobber-token", "request-1", "Call summary")

    assert request == {"id": "request-1", "title": "Leaking sink", "jobberWebUri": "https://example.test/request"}
    assert note_id == "note-1"
    assert calls[0][1]["json"]["variables"]["input"] == {
        "clientId": "client-1",
        "propertyId": "property-1",
        "title": "Leaking sink",
    }
    assert calls[1][1]["json"]["variables"] == {
        "requestId": "request-1",
        "input": {"message": "Call summary", "pinned": False},
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
TWILIO_ACCOUNT_SID=test TWILIO_AUTH_TOKEN=test TWILIO_PHONE_NUMBER=+15555550100 TELEGRAM_BOT_TOKEN=test USER_PHONE=+15555550101 /Volumes/Extreme\ Pro/myprojects/Kevin/.venv/bin/python -m pytest tests/unit/test_jobber.py -q
```

Expected: FAIL because `create_client`, `create_request`, and `create_request_note` do not exist, and `lookup_customer` still uses the old phone filter.

- [ ] **Step 3: Update phone lookup to live schema**

Replace `lookup_customer` with:

```python
async def lookup_customer(auth: str | dict, phone: str) -> Optional[dict]:
    """Look up a Jobber client by phone number."""
    if not phone:
        return None
    query = """
    query LookupClient($phone: String!) {
        clients(searchTerm: $phone, searchFields: [PHONES], first: 1) {
            nodes {
                id
                name
                firstName
                lastName
                phones { number }
                emails { address }
                billingAddress { street1 street2 city province postalCode }
                clientProperties(first: 1) { nodes { id } }
            }
        }
    }
    """
    data = await _graphql_request_with_refresh(auth, query, {"phone": phone})
    if data and data.get("clients", {}).get("nodes"):
        return data["clients"]["nodes"][0]
    return None
```

- [ ] **Step 4: Add client payload helpers**

Add these helpers above `lookup_customer`:

```python
def _split_name(full_name: str) -> tuple[str, str]:
    parts = (full_name or "").strip().split()
    if not parts:
        return "Unknown", "Caller"
    if len(parts) == 1:
        return parts[0], "Caller"
    return parts[0], " ".join(parts[1:])


def _first_property_id(client: dict) -> str:
    nodes = ((client or {}).get("clientProperties") or {}).get("nodes") or []
    if nodes:
        return nodes[0].get("id", "")
    return ""


def _build_client_create_input(job_data: dict) -> dict:
    first_name, last_name = _split_name(job_data.get("caller_name", ""))
    payload: dict = {
        "firstName": first_name,
        "lastName": last_name,
        "sourceAttribution": {"sourceText": "Hey Kevin"},
    }
    phone = job_data.get("caller_phone", "")
    if phone:
        payload["phones"] = [{"number": phone, "primary": True, "smsAllowed": True}]
    address = (job_data.get("address") or "").strip()
    if address:
        payload["properties"] = [{"address": {"street1": address}}]
    return payload
```

- [ ] **Step 5: Add Client, Request, and Request-note mutations**

Add these functions below `lookup_customer`:

```python
async def create_client(auth: str | dict, job_data: dict) -> Optional[dict]:
    """Create a Jobber client for an unknown caller."""
    query = """
    mutation CreateClient($input: ClientCreateInput!) {
        clientCreate(input: $input) {
            client {
                id
                name
                clientProperties(first: 1) { nodes { id } }
            }
            userErrors { message path }
        }
    }
    """
    data = await _graphql_request_with_refresh(auth, query, {"input": _build_client_create_input(job_data)})
    client = _extract_mutation_object(data, "clientCreate", "client")
    if not client:
        return None
    return {
        "id": client.get("id", ""),
        "name": client.get("name", ""),
        "property_id": _first_property_id(client),
    }


async def create_request(auth: str | dict, request_data: dict) -> Optional[dict]:
    """Create a Jobber Request for a captured lead."""
    query = """
    mutation CreateRequest($input: RequestCreateInput!) {
        requestCreate(input: $input) {
            request { id title jobberWebUri }
            userErrors { message path }
        }
    }
    """
    input_data = {
        "clientId": request_data["client_id"],
        "title": request_data.get("title", "Phone inquiry from Hey Kevin")[:100],
    }
    if request_data.get("property_id"):
        input_data["propertyId"] = request_data["property_id"]
    data = await _graphql_request_with_refresh(auth, query, {"input": input_data})
    return _extract_mutation_object(data, "requestCreate", "request")


async def create_request_note(auth: str | dict, request_id: str, message: str) -> Optional[str]:
    """Attach Kevin call details to a Jobber Request."""
    query = """
    mutation CreateRequestNote($requestId: EncodedId!, $input: RequestCreateNoteInput!) {
        requestCreateNote(requestId: $requestId, input: $input) {
            request { id }
            requestNote { id }
            userErrors { message path }
        }
    }
    """
    data = await _graphql_request_with_refresh(
        auth,
        query,
        {"requestId": request_id, "input": {"message": message[:5000], "pinned": False}},
    )
    note = _extract_mutation_object(data, "requestCreateNote", "requestNote")
    if not note:
        return None
    return note.get("id", "")
```

- [ ] **Step 6: Run Jobber unit tests**

Run:

```bash
TWILIO_ACCOUNT_SID=test TWILIO_AUTH_TOKEN=test TWILIO_PHONE_NUMBER=+15555550100 TELEGRAM_BOT_TOKEN=test USER_PHONE=+15555550101 /Volumes/Extreme\ Pro/myprojects/Kevin/.venv/bin/python -m pytest tests/unit/test_jobber.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/services/jobber.py tests/unit/test_jobber.py
git commit -m "feat: add jobber lead request helpers"
```

---

### Task 4: Add Local Idempotency Claim for Jobber Sync

**Files:**
- Modify: `app/db/jobs.py`
- Create: `tests/unit/test_jobber_post_call.py`

- [ ] **Step 1: Write failing test for sync claim helper**

Create `tests/unit/test_jobber_post_call.py`:

```python
"""Post-call Jobber lead capture behavior."""

import time

import pytest

from app.db import jobs


class _FakeSnapshot:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data or {})


class _FakeDoc:
    def __init__(self, data):
        self.data = data
        self.updates = []

    def get(self, transaction=None):
        return _FakeSnapshot(self.data)


class _FakeCollection:
    def __init__(self, doc):
        self.doc = doc

    def document(self, job_id):
        assert job_id == "job-1"
        return self.doc


class _FakeTransaction:
    def __init__(self):
        self.updates = []

    def update(self, ref, updates):
        self.updates.append((ref, updates))
        ref.updates.append(updates)


class _FakeDb:
    def __init__(self, doc):
        self.doc = doc
        self.tx = _FakeTransaction()

    def collection(self, name):
        assert name == jobs.COLLECTION
        return _FakeCollection(self.doc)

    def transaction(self):
        return self.tx


@pytest.mark.asyncio
async def test_claim_jobber_sync_skips_existing_request(monkeypatch):
    fake_doc = _FakeDoc({"jobber_request_id": "request-1"})
    fake_db = _FakeDb(fake_doc)
    monkeypatch.setattr(jobs, "get_firestore_client", lambda: fake_db)
    monkeypatch.setattr(jobs.firestore_module, "transactional", lambda fn: fn)

    claimed = await jobs.claim_jobber_sync("job-1")

    assert claimed is False
    assert fake_doc.updates == []


@pytest.mark.asyncio
async def test_claim_jobber_sync_marks_in_progress(monkeypatch):
    fake_doc = _FakeDoc({"call_sid": "CA123"})
    fake_db = _FakeDb(fake_doc)
    monkeypatch.setattr(jobs, "get_firestore_client", lambda: fake_db)
    monkeypatch.setattr(jobs.firestore_module, "transactional", lambda fn: fn)
    monkeypatch.setattr(time, "time", lambda: 12345.0)

    claimed = await jobs.claim_jobber_sync("job-1")

    assert claimed is True
    assert fake_doc.updates == [
        {"jobber_sync_status": "in_progress", "jobber_sync_started_at": 12345.0}
    ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
TWILIO_ACCOUNT_SID=test TWILIO_AUTH_TOKEN=test TWILIO_PHONE_NUMBER=+15555550100 TELEGRAM_BOT_TOKEN=test USER_PHONE=+15555550101 /Volumes/Extreme\ Pro/myprojects/Kevin/.venv/bin/python -m pytest tests/unit/test_jobber_post_call.py -q
```

Expected: FAIL because `claim_jobber_sync` does not exist.

- [ ] **Step 3: Add transactional sync claim helper**

Append this function to `app/db/jobs.py` after `update_job`:

```python
async def claim_jobber_sync(job_id: str) -> bool:
    """Claim one Jobber sync attempt for a local job record."""
    db = get_firestore_client()
    loop = asyncio.get_event_loop()

    def _claim() -> bool:
        transaction = db.transaction()
        doc_ref = db.collection(COLLECTION).document(job_id)

        @firestore_module.transactional
        def _transactional_claim(tx) -> bool:
            snapshot = doc_ref.get(transaction=tx)
            if not snapshot.exists:
                return False
            data = snapshot.to_dict() or {}
            if data.get("jobber_request_id"):
                return False
            if data.get("jobber_sync_status") == "in_progress":
                return False
            tx.update(doc_ref, {
                "jobber_sync_status": "in_progress",
                "jobber_sync_started_at": time.time(),
            })
            return True

        return _transactional_claim(transaction)

    return await loop.run_in_executor(None, _claim)
```

- [ ] **Step 4: Run idempotency tests**

Run:

```bash
TWILIO_ACCOUNT_SID=test TWILIO_AUTH_TOKEN=test TWILIO_PHONE_NUMBER=+15555550100 TELEGRAM_BOT_TOKEN=test USER_PHONE=+15555550101 /Volumes/Extreme\ Pro/myprojects/Kevin/.venv/bin/python -m pytest tests/unit/test_jobber_post_call.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/db/jobs.py tests/unit/test_jobber_post_call.py
git commit -m "feat: add jobber sync idempotency claim"
```

---

### Task 5: Replace Post-Call Job Creation with Lead Capture

**Files:**
- Modify: `app/services/post_call.py`
- Modify: `tests/unit/test_jobber_post_call.py`

- [ ] **Step 1: Write failing tests for gated post-call lead capture**

Append to `tests/unit/test_jobber_post_call.py`:

```python
from app.services import post_call


@pytest.mark.asyncio
async def test_capture_jobber_lead_skips_when_feature_flag_disabled(monkeypatch):
    called = False

    async def fake_claim(_job_id):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(post_call, "_jobber_lead_capture_enabled", lambda _contractor: False)
    monkeypatch.setattr(jobs, "claim_jobber_sync", fake_claim)

    await post_call._capture_jobber_lead(
        {"jobber_access_token": "token", "jobber_lead_capture_enabled": False},
        {"call_sid": "CA123", "call_type": "service_request", "caller_phone": "+15551234567"},
        "job-1",
    )

    assert called is False


@pytest.mark.asyncio
async def test_capture_jobber_lead_creates_request_for_unknown_client(monkeypatch):
    updates = []

    async def fake_claim(job_id):
        assert job_id == "job-1"
        return True

    async def fake_update_job(job_id, update):
        assert job_id == "job-1"
        updates.append(update)

    async def fake_save_call(call_sid, update):
        assert call_sid == "CA123"
        updates.append({"call_update": update})

    async def fake_lookup(_contractor, phone):
        assert phone == "+15551234567"
        return None

    async def fake_create_client(_contractor, job_data):
        assert job_data["caller_name"] == "Jane Private"
        return {"id": "client-1", "name": "Jane Private", "property_id": "property-1"}

    async def fake_create_request(_contractor, request_data):
        assert request_data["client_id"] == "client-1"
        assert request_data["property_id"] == "property-1"
        return {"id": "request-1", "title": "Leaking sink", "jobberWebUri": "https://example.test/request"}

    async def fake_create_request_note(_contractor, request_id, message):
        assert request_id == "request-1"
        assert "Call SID: CA123" in message
        assert "Leaking sink" in message
        return "note-1"

    monkeypatch.setattr(jobs, "claim_jobber_sync", fake_claim)
    monkeypatch.setattr(jobs, "update_job", fake_update_job)
    monkeypatch.setattr(post_call.call_db, "save_call", fake_save_call)
    monkeypatch.setattr(post_call.jobber_service, "lookup_customer", fake_lookup)
    monkeypatch.setattr(post_call.jobber_service, "create_client", fake_create_client)
    monkeypatch.setattr(post_call.jobber_service, "create_request", fake_create_request)
    monkeypatch.setattr(post_call.jobber_service, "create_request_note", fake_create_request_note)

    await post_call._capture_jobber_lead(
        {"jobber_access_token": "token", "jobber_lead_capture_enabled": True},
        {
            "call_sid": "CA123",
            "call_type": "service_request",
            "caller_name": "Jane Private",
            "caller_phone": "+15551234567",
            "issue_description": "Leaking sink",
            "urgency": "routine",
            "address": "123 Main Street",
            "callback_number": "+15557654321",
            "transcript": "Caller needs a leaking sink repaired.",
        },
        "job-1",
    )

    assert any(update.get("jobber_sync_status") == "succeeded" for update in updates)
    assert any(update.get("jobber_request_id") == "request-1" for update in updates)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
TWILIO_ACCOUNT_SID=test TWILIO_AUTH_TOKEN=test TWILIO_PHONE_NUMBER=+15555550100 TELEGRAM_BOT_TOKEN=test USER_PHONE=+15555550101 /Volumes/Extreme\ Pro/myprojects/Kevin/.venv/bin/python -m pytest tests/unit/test_jobber_post_call.py -q
```

Expected: FAIL because `_capture_jobber_lead` and `jobber_service` do not exist.

- [ ] **Step 3: Import Jobber service module and local job helpers**

Near the top of `app/services/post_call.py`, add:

```python
from app.db import calls as call_db
from app.db import jobs as job_db
from app.services import jobber as jobber_service
```

- [ ] **Step 4: Replace post-call scheduling hook**

Replace:

```python
    # 2b. Auto-create job in Jobber for service requests
    if contractor.get("jobber_access_token") and job_data.get("call_type") == "service_request":
        asyncio.create_task(_create_jobber_job(contractor, job_data))
```

with:

```python
    # 2b. Capture qualified leads in Jobber as Requests. No scheduling in v1.
    if contractor.get("jobber_access_token") and job_data.get("call_type") == "service_request":
        asyncio.create_task(_capture_jobber_lead(contractor, job_data, job_id))
```

- [ ] **Step 5: Replace `_create_jobber_job` with lead capture**

Replace `_create_jobber_job` with:

```python
def _jobber_lead_capture_enabled(contractor: dict) -> bool:
    return contractor.get("jobber_lead_capture_enabled") is True


def _jobber_request_title(job_data: dict) -> str:
    issue = (job_data.get("issue_description") or "Phone inquiry").strip()
    call_sid = job_data.get("call_sid", "")
    suffix = f" - Hey Kevin {call_sid[:8]}" if call_sid else " - Hey Kevin"
    return (issue[: max(1, 100 - len(suffix))] + suffix)[:100]


def _format_jobber_lead_note(job_data: dict) -> str:
    lines = [
        "Captured by Hey Kevin",
        f"Call SID: {job_data.get('call_sid', '')}",
        f"Caller: {job_data.get('caller_name', '') or 'Unknown caller'}",
        f"Phone: {job_data.get('caller_phone', '')}",
        f"Callback: {job_data.get('callback_number', '') or job_data.get('caller_phone', '')}",
        f"Address: {job_data.get('address', '')}",
        f"Urgency: {job_data.get('urgency', 'none')}",
        f"Issue: {job_data.get('issue_description', '')}",
        f"Message: {job_data.get('message', '')}",
        "",
        "Transcript:",
        (job_data.get("transcript", "") or "")[:3000],
    ]
    return "\n".join(line for line in lines if line is not None)


async def _capture_jobber_lead(contractor: dict, job_data: dict, job_id: str):
    """Best-effort: capture a service request in Jobber without scheduling work."""
    if not _jobber_lead_capture_enabled(contractor):
        logger.info("Jobber lead capture skipped: feature flag disabled")
        return

    try:
        claimed = await job_db.claim_jobber_sync(job_id)
        if not claimed:
            logger.info("Jobber lead capture skipped: sync already claimed or complete")
            return

        caller_phone = job_data.get("caller_phone", "")
        client = None
        if caller_phone:
            client = await asyncio.wait_for(
                jobber_service.lookup_customer(contractor, caller_phone),
                timeout=5.0,
            )
        if client:
            client_id = client.get("id", "")
            property_id = jobber_service._first_property_id(client)
        else:
            created_client = await asyncio.wait_for(
                jobber_service.create_client(contractor, job_data),
                timeout=5.0,
            )
            if not created_client:
                raise RuntimeError("Jobber client creation returned no client")
            client_id = created_client.get("id", "")
            property_id = created_client.get("property_id", "")

        request = await asyncio.wait_for(
            jobber_service.create_request(
                contractor,
                {
                    "client_id": client_id,
                    "property_id": property_id,
                    "title": _jobber_request_title(job_data),
                },
            ),
            timeout=5.0,
        )
        if not request:
            raise RuntimeError("Jobber request creation returned no request")

        note_id = await asyncio.wait_for(
            jobber_service.create_request_note(
                contractor,
                request["id"],
                _format_jobber_lead_note(job_data),
            ),
            timeout=5.0,
        )

        updates = {
            "jobber_sync_status": "succeeded",
            "jobber_client_id": client_id,
            "jobber_request_id": request["id"],
            "jobber_request_url": request.get("jobberWebUri", ""),
            "jobber_request_note_id": note_id or "",
            "jobber_synced_at": time.time(),
        }
        await job_db.update_job(job_id, updates)
        if job_data.get("call_sid"):
            await call_db.save_call(job_data["call_sid"], updates)
        logger.info("Jobber lead captured: request=%s job_id=%s", request["id"], job_id)

    except asyncio.TimeoutError:
        await job_db.update_job(job_id, {
            "jobber_sync_status": "failed",
            "jobber_sync_error": "timeout",
            "jobber_sync_failed_at": time.time(),
        })
        logger.warning("Jobber lead capture timed out")
    except Exception as e:
        await job_db.update_job(job_id, {
            "jobber_sync_status": "failed",
            "jobber_sync_error": type(e).__name__,
            "jobber_sync_failed_at": time.time(),
        })
        logger.warning("Jobber lead capture failed: exception_type=%s", type(e).__name__)
```

- [ ] **Step 6: Run post-call tests**

Run:

```bash
TWILIO_ACCOUNT_SID=test TWILIO_AUTH_TOKEN=test TWILIO_PHONE_NUMBER=+15555550100 TELEGRAM_BOT_TOKEN=test USER_PHONE=+15555550101 /Volumes/Extreme\ Pro/myprojects/Kevin/.venv/bin/python -m pytest tests/unit/test_jobber_post_call.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/services/post_call.py tests/unit/test_jobber_post_call.py
git commit -m "feat: capture jobber leads as requests"
```

---

### Task 6: Protect the Lead-Capture Kill Switch

**Files:**
- Modify: `app/api/contractors.py`
- Modify: `app/db/contractors.py`
- Modify: `tests/unit/test_twilio_provisioning.py`

- [ ] **Step 1: Add failing protected-field assertion**

Append this test to `tests/unit/test_twilio_provisioning.py`:

```python
def test_jobber_lead_capture_flag_is_server_protected():
    assert "jobber_lead_capture_enabled" in contractors_db.PROTECTED_FIELDS
```

- [ ] **Step 2: Run protected-field test**

Run:

```bash
TWILIO_ACCOUNT_SID=test TWILIO_AUTH_TOKEN=test TWILIO_PHONE_NUMBER=+15555550100 TELEGRAM_BOT_TOKEN=test USER_PHONE=+15555550101 /Volumes/Extreme\ Pro/myprojects/Kevin/.venv/bin/python -m pytest tests/unit/test_twilio_provisioning.py::test_jobber_lead_capture_flag_is_server_protected -q
```

Expected: FAIL until the field is protected.

- [ ] **Step 3: Add the feature flag to protected fields**

Add this field to both `PROTECTED_FIELDS` sets:

```python
    "jobber_lead_capture_enabled",
```

Files:
- `app/api/contractors.py`
- `app/db/contractors.py`

- [ ] **Step 4: Run protected-field test**

Run:

```bash
TWILIO_ACCOUNT_SID=test TWILIO_AUTH_TOKEN=test TWILIO_PHONE_NUMBER=+15555550100 TELEGRAM_BOT_TOKEN=test USER_PHONE=+15555550101 /Volumes/Extreme\ Pro/myprojects/Kevin/.venv/bin/python -m pytest tests/unit/test_twilio_provisioning.py::test_jobber_lead_capture_flag_is_server_protected -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/api/contractors.py app/db/contractors.py tests/unit/test_twilio_provisioning.py
git commit -m "fix: protect jobber lead capture flag"
```

---

### Task 7: Run Full Verification and Live Write Test

**Files:**
- No required source changes unless verification finds a defect.

- [ ] **Step 1: Run focused unit tests**

Run:

```bash
TWILIO_ACCOUNT_SID=test TWILIO_AUTH_TOKEN=test TWILIO_PHONE_NUMBER=+15555550100 TELEGRAM_BOT_TOKEN=test USER_PHONE=+15555550101 /Volumes/Extreme\ Pro/myprojects/Kevin/.venv/bin/python -m pytest tests/unit/test_jobber.py tests/unit/test_jobber_tool_gating.py tests/unit/test_jobber_post_call.py -q
```

Expected: PASS.

- [ ] **Step 2: Run broader backend tests**

Run:

```bash
TWILIO_ACCOUNT_SID=test TWILIO_AUTH_TOKEN=test TWILIO_PHONE_NUMBER=+15555550100 TELEGRAM_BOT_TOKEN=test USER_PHONE=+15555550101 /Volumes/Extreme\ Pro/myprojects/Kevin/.venv/bin/python -m pytest tests/unit -q
```

Expected: PASS or documented pre-existing failures unrelated to Jobber. If unrelated failures appear, rerun the focused tests and capture the unrelated failing test names.

- [ ] **Step 3: Run lint or syntax validation**

Run:

```bash
/Volumes/Extreme\ Pro/myprojects/Kevin/.venv/bin/python -m py_compile app/services/jobber.py app/services/post_call.py app/services/voice_pipeline.py app/services/gemini_pipeline.py app/db/jobs.py
```

Expected: no output and exit code 0.

- [ ] **Step 4: Perform live writable Jobber test in a test account**

Use the local OAuth credentials in the original checkout, not committed secrets. Enable `jobber_lead_capture_enabled` only on a throwaway contractor record. Run one post-call style invocation with:

```python
job_data = {
    "call_sid": "CA_JOBBER_TEST_001",
    "call_type": "service_request",
    "caller_name": "Hey Kevin Test",
    "caller_phone": "+15555550123",
    "address": "123 Test Street",
    "issue_description": "Test lead capture request",
    "urgency": "routine",
    "message": "This is a test lead created by Hey Kevin.",
    "callback_number": "+15555550123",
    "transcript": "Caller requested a test service. This is not a real customer.",
}
```

Expected in Jobber:
- One Client named `Hey Kevin Test`.
- One Request attached to that client.
- One Request note containing `Call SID: CA_JOBBER_TEST_001`.
- No Job created.
- No Visit created.
- No Assessment created.
- No appointment scheduled.

- [ ] **Step 5: Retry duplicate live write**

Run the same post-call invocation again with the same local `job_id` and `call_sid`.

Expected:
- No second Jobber Request is created.
- Local job record keeps the original `jobber_request_id`.

- [ ] **Step 6: Commit verification fixes if needed**

If Step 1 through Step 5 require source fixes, commit them:

```bash
git add app tests
git commit -m "test: verify jobber lead capture"
```

If no source fixes are needed, do not create an empty commit.

---

## Self-Review

- Spec coverage: The plan disables unsafe booking tools, pins the live Jobber API version, implements Client/Request/Request-note lead capture, adds local idempotency, gates rollout behind a protected feature flag, and defines unit plus live-write verification.
- Placeholder scan: No placeholder tokens or unspecified implementation steps remain.
- Type consistency: The plan uses live-schema names from the OAuth probe: `ClientCreateInput`, `RequestCreateInput`, `RequestCreateNoteInput`, `clientCreate`, `requestCreate`, `requestCreateNote`, `clients(searchTerm, searchFields: [PHONES])`, and GraphQL version `2025-04-16`.
