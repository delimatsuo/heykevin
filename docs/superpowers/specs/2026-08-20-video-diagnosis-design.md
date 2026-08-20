# Video-First AI Diagnosis — Design and Builder Brief

Date: 2026-08-20
Status: approved by owner (Deli Matsuo) after brainstorming, 2026-08-20
Audience: a builder agent with no prior context on this repository
Reviewer: a separate agent reviews the deliverables against §9 before merge

## 1. Problem

After a screened call, Kevin can text the caller a link: upload a photo or
video of the problem, Gemini analyzes it, and both the caller and the owner
receive a diagnosis and price estimate. This is the product's flagship
differentiator ("AI diagnostics", `docs/pricing-competitive-strategy.md`).

Three defects keep it from working with video, which the owner considers the
primary capture mode ("a picture might not be sufficient"):

1. **Realistic videos fail analysis.** `app/api/estimates.py` accepts video up
   to `MAX_VIDEO_SIZE` (50MB), but `app/services/ai_estimate.py` sends media to
   Gemini as base64 `inline_data` in a `generateContent` call. Google's
   guidance caps inline video around 20MB of total request; base64 inflates
   payloads ~33%. A typical 30–60s phone video (30–60MB) is accepted by our
   endpoint and then fails at Gemini.
2. **The video is destroyed.** Media is buffered in memory, analyzed, and
   discarded. The contractor — a tradesperson who would want to *watch* the
   caller's leak before quoting — never sees it. Only the AI's 3-sentence
   opinion survives.
3. **Analysis runs inside the upload HTTP request.** The caller waits on the
   page. Video analysis via the Files API takes up to a minute or more; a
   locked phone screen or Cloud Run timeout loses the result.

Note: the feature is currently dark in production — `ESTIMATE_TOKEN_CREATE`
gates are off for all contractors. Enabling them is an owner action and is
**out of scope** here. This work must be correct before the owner turns it on.

## 2. Decisions already made (do not relitigate)

Owner decisions from the 2026-08-20 brainstorm:

- **D1 — Video-first, 60-second guidance.** SMS copy and the upload page lead
  with "record a short video (under a minute) showing the problem while you
  describe it." Photos remain accepted. Duration is page-side guidance only;
  the backend enforces bytes (50MB), never seconds.
- **D2 — Store the media; give the owner a watch link.** Media persists in GCS
  with 90-day retention (matching call retention). The owner's completion SMS
  includes a signed watch link.
- **D3 — Async result.** Upload returns immediately; analysis runs in the
  background; the existing result SMS delivers the outcome. The page may poll
  the existing status endpoint.
- **D4 — The caller page stays on heykevin.one (Lovable).** The builder ships
  a paste-ready Lovable prompt plus the API contract; the page itself is
  edited by the owner in Lovable, not in this repo.
- **Approach B — buffered dual-write.** Keep the audited capped-streaming
  upload (F-10). After buffering: archive to GCS, then analyze video via
  Gemini's **Files API** (upload → wait `ACTIVE` → `generateContent` with
  `file_data`). Photos keep the existing inline path. Streaming-to-GCS
  (no memory buffer) is an explicit non-goal until volume justifies it.

## 3. Repository ground rules for the builder

Read `docs/agent-operating.md` and the repo `CLAUDE.md` first; they are law.
The subset that most often bites:

- Work in a git worktree under `<clone>/.worktrees/`, cut from `origin/main`.
  Never edit the primary checkout.
- Stage explicit paths. Never `git add -A`. Never `--no-verify`. Never
  force-push `main` or `staging`.
- Run tests with the clone's venv on PATH:
  `PATH="<clone>/.venv/bin:$PATH" python -m pytest tests/unit -q`
  Do not export any name in `FORBIDDEN_ENV_NAMES`
  (see `tests/unit/test_visual_diagnosis_contracts.py`) — those tests snapshot
  `os.environ` and fail collection if one is present.
- Do not deploy anything. Do not enable any flag, gate, or Firestore field.
  Do not touch `firestore.rules`. Merging to `main` does not deploy; deploys
  and flag changes are owner-gated.
- Open a PR to `main` and **stop before merging**. A separate reviewer runs
  §9 against the deliverables.
- Baseline at spec time: **2559 unit tests passing** on `main` (`9f96143`).
  Pre-existing ruff errors exist in untouched files; your touched files must
  be clean (`ruff check <files>`).

## 4. Architecture

### 4.1 Module boundaries

- **`app/services/estimate_media.py` (new).** Owns media persistence and the
  watch link. Public surface:
  - `archive_media(token_hash, media_id, media_bytes, content_type) -> str | None` —
    writes `gs://{ESTIMATE_MEDIA_BUCKET}/{token_hash}/{media_id}.{ext}`, returns the
    object path, or `None` when `ESTIMATE_MEDIA_BUCKET` is unset (feature
    degrades: no archive, no watch link, analysis still runs). GCS client
    injectable for tests, mirroring `client_factory` in
    `scripts/set_sms_compliance_status.py`.
  - `make_watch_url(media_id) -> str` / `verify_watch_sig(media_id, expires,
    sig) -> bool` — HMAC-SHA256 over `f"estimate-media:{media_id}:{expires}"`
    using the existing vCard secret resolution (`app/services/vcard.py`
    pattern; same secret, distinct context string so signatures cannot cross
    purposes). Truncated hex digest, `hmac.compare_digest`, 90-day expiry.
  - `gcs_redirect_url(object_path) -> str` — V4 signed GET URL, 1-hour expiry,
    so GCS serves bytes (and Range requests, which mobile `<video>` requires).
- **`app/services/ai_estimate.py` (extend, keep signature).**
  `analyze_media(...)` routes internally: images → existing inline path
  (unchanged); video → Files API path: resumable upload, poll until `ACTIVE`
  (give up after 120s), `generateContent` with
  `{"file_data": {"mime_type", "file_uri"}}`. Use raw REST via `httpx`,
  consistent with the existing `generateContent` call — do not introduce a new
  SDK. **Verify the current Files API endpoints and headers against Google's
  official docs before implementing; do not code them from memory.** Add one
  line to the analysis prompt: the caller may describe the problem out loud —
  use what they say as well as what is visible.
- **`app/api/estimates.py` (modify).**
  - `upload_and_analyze` (video): after the capped buffer —
    1. Generate a fresh `media_id` (random) **per upload attempt**; archive to
       `{token_hash}/{media_id}.{ext}`. Attempts never share an object, so a
       retry can never overwrite the bytes a running analysis or an
       already-sent watch link refers to.
    2. **Claim atomically** (Firestore transaction): only `pending` or a
       terminal state may transition to `processing`; write `attempts += 1`,
       `lease_expires_at = now + 300`, plus the media fields, in the same
       transaction. A second upload while one is `processing` gets
       `409 {"status": "processing"}` and starts nothing.
    3. Schedule the inline attempt (`asyncio.create_task`) and return
       `202 {"status": "processing"}`.
    Durability does **not** rest on that task surviving: this mirrors
    `app/services/post_call_handoff.py`, where the persisted record is the
    queue and the in-process attempt is only the fast path (see worker loop
    below). If the GCS write fails, return 503 and claim nothing: with no
    archive there is no owner link and no recovery, so an honest retry beats
    a half-delivered result.
  - `upload_and_analyze` (photo): stays synchronous and inline; archived to
    its own `{token_hash}/{media_id}.{ext}` too; responds
    `200 {"status": "complete", "result": {...}}` (today's `"ok"` becomes
    `"complete"` — the feature is dark, nothing consumes the old shape).
  - Optional caller text: the upload POST accepts `?description=<url-encoded>`
    (cap 500 chars). Persist it on the estimate doc and pass it to
    `analyze_media(text_description=...)`. Never log it.
  - New `GET /api/estimates/media/{media_id}?e=<expires>&s=<sig>`: verify
    signature and expiry → 302 to `gcs_redirect_url`. 403 on bad/expired
    signature; 404 on unknown media. Public route; the signature is the auth.
    Never log the signature or the caller's phone.
- **Recovery worker (new, small).** `estimate_worker_loop` registered in
  `app/main.py` beside `post_call_worker_loop`, same shape: periodically scan
  for estimates in `processing` whose `lease_expires_at` has passed, re-claim
  (attempts += 1, fresh lease), and re-run analysis from the GCS bytes. After
  `MAX_ANALYSIS_ATTEMPTS = 3`, mark `failed` and send the failure
  notifications. Completion and notifications must be **idempotent**: a
  `notified_at` field on the doc guards the SMS pair, so a recovered re-run
  never double-texts (same pattern as `caller_notified_at` on appointment
  requests).
- **`app/services/post_call.py` (modify).** Caller-facing offer copy becomes
  video-first (D1). Keep it short; it is an SMS.
- **Owner completion SMS** (in `app/api/estimates.py` today): append
  "Watch the caller's video: <watch_url>" when an archive exists.
- **`app/config.py`**: add `estimate_media_bucket: str = ""`.
- **`pyproject.toml`**: add `google-cloud-storage` as an explicit dependency
  (currently only transitive; pin compatible with the installed 3.4.x).

### 4.2 Data

On the estimate document (Firestore `estimates` collection): add
`media_object_path`, `media_id` (random per upload attempt, not derived from
the upload token — the watch link must not leak the token that authorizes
uploads), `media_content_type`, `description` (optional caller text),
`attempts`, `lease_expires_at`, `notified_at`. Status values:
`pending → processing → complete | failed`; only the atomic claim may enter
`processing`. `media_id`/`media_object_path` always describe the attempt the
current `result` came from, so the watch link and the diagnosis can never
refer to different videos.

### 4.3 Failure handling (every path ends in a definite state)

| Failure | Outcome |
| --- | --- |
| GCS write fails (video) | 503 to uploader; status stays `pending`; no analysis |
| Files API upload/poll error, or not `ACTIVE` within 120s | status `failed`; caller SMS "we couldn't process this — call the business directly"; owner SMS still sent **with watch link** (the artifact survives analysis death) |
| `generateContent` error | same as above |
| Result/owner SMS send fails | logged; background task never raises; status still reflects analysis outcome |
| Background task crashes unexpectedly | wrap the whole task; any exception → status `failed` + the notifications above |
| Instance dies / task cancelled after 202 | the persisted claim is the queue: `estimate_worker_loop` finds the expired lease, re-claims, re-runs from GCS; after `MAX_ANALYSIS_ATTEMPTS` → `failed` + notifications. **No estimate may remain `processing` forever, even across instance restarts.** |
| Worker re-runs an attempt that already completed | idempotent: terminal status is not overwritten; `notified_at` prevents duplicate SMS |
| Second upload while one is `processing` | `409 {"status": "processing"}`; nothing scheduled, nothing overwritten |

Existing gate checks (`ESTIMATE_RESULT_SMS` on upload, `ESTIMATE_TOKEN_CREATE`
on token creation) stay exactly where and as they are.

## 5. Caller page contract (for the Lovable prompt)

Deliver `docs/lovable/2026-08-20-estimate-page-video-prompt.md` containing a
paste-ready Lovable prompt that implements:

- Video-first layout: primary button "Record a video" (`<input type="file"
  accept="video/*" capture="environment">`), guidance "up to about a minute";
  secondary "Upload a photo instead"; optional short text description.
- Flow: `POST /api/estimates/{token}/upload-url` (existing) → `POST` the file
  to the returned URL with the file's `Content-Type` header, appending
  `?description=<url-encoded text>` when the caller typed one. Handle every
  2xx by its JSON `status`: `complete` → render the result immediately (the
  synchronous photo path); `processing` → show "Got it — we'll text your
  estimate shortly" and poll `GET /api/estimates/{token}` every 5s for up to
  3 minutes; show the result if `complete`, the failure copy if `failed`, and
  stop polling either way (the SMS still delivers after the page gives up).
- Client-side size check before upload (reject > 50MB with "please record a
  shorter video").
- Error states: expired link (already handled by the page), 409 (an upload is
  already being analyzed — "we're already working on your last upload"),
  413 (too large), 429 (upload limit), 5xx (try again).

The prompt must spell out exact copy, since Lovable output tracks its input.

## 6. Infra the owner runs (not the builder)

Provide these in the PR description, ready to paste; do not run them:

```bash
gcloud storage buckets create gs://kevin-estimate-media \
  --project kevin-491315 --location us-central1 \
  --uniform-bucket-level-access
echo '{"rule":[{"action":{"type":"Delete"},"condition":{"age":90}}]}' > /tmp/lc.json
gcloud storage buckets update gs://kevin-estimate-media --lifecycle-file=/tmp/lc.json
# Cloud Run runtime SA needs objectAdmin on the bucket + token creator on itself (V4 signing):
gcloud storage buckets add-iam-policy-binding gs://kevin-estimate-media \
  --member="serviceAccount:<PRODUCTION_RUNTIME_SERVICE_ACCOUNT>" --role=roles/storage.objectAdmin
gcloud iam service-accounts add-iam-policy-binding <PRODUCTION_RUNTIME_SERVICE_ACCOUNT> \
  --member="serviceAccount:<PRODUCTION_RUNTIME_SERVICE_ACCOUNT>" --role=roles/iam.serviceAccountTokenCreator
# Then set ESTIMATE_MEDIA_BUCKET=kevin-estimate-media on the service (owner deploy path).
```

`<PRODUCTION_RUNTIME_SERVICE_ACCOUNT>` is the Cloud Run runtime service
account — its value lives in the GitHub Actions variable of the same name
(see `.github/workflows/deploy.yml`), not in this repo.

The code must work with the bucket unset (degraded mode), so nothing here
blocks merge.

## 7. Explicit non-goals

- Enabling any gate or flag for any contractor (owner action, Electus-first).
- iOS in-app video player (the watch link opens in the browser).
- Streaming uploads to GCS without buffering (Approach A).
- Server-side duration probing (ffprobe et al.).
- Multi-file guided capture.
- Changing `slot`/appointment logic or anything outside the files named here.

## 8. Testing requirements

TDD; tests live in `tests/unit/`. Required coverage, each as a named test the
reviewer can run:

**estimate_media**
1. Archive writes to the injected fake GCS client with the expected object
   path and content type; returns the path.
2. Bucket unset → `archive_media` returns `None`, no client constructed.
3. Watch URL round-trip: `make_watch_url` → `verify_watch_sig` true.
4. Tampered signature → false. Expired → false. (`hmac.compare_digest` used.)
5. `media_id` is not derived from the upload token (no substring of the token
   or its hash appears in the watch URL).

**ai_estimate video path** (mock `httpx` transport)
6. Video routes to Files API: resumable upload called, polled to `ACTIVE`,
   `generateContent` carries `file_data` and no `inline_data`.
7. Image still uses `inline_data` and never touches the Files API (regression).
8. Stuck in `PROCESSING` past the timeout → raises the failure the caller
   maps to `failed` (no infinite poll; assert bounded call count).

**upload endpoint** (fake GCS, fake analyzer, fake SMS)
9. Video upload returns 202 **before** the analysis task completes (ordering
   asserted via an analyzer that blocks on an event).
10. Video GCS failure → 503, analyzer never called, status not `processing`.
11. Background failure (analyzer raises) → status `failed`, caller failure
    SMS sent, owner SMS sent and contains the watch URL.
12. Success → status `complete`, result stored, both SMS sent, owner SMS
    contains the watch URL.
13. Gate denial still blocks the upload (regression: existing behavior).
14. Photo path unchanged: synchronous result, archived, no Files API call.

**media endpoint**
15. Valid signature → 302 with a GCS URL. Invalid → 403. Unknown id → 404.

**durability and isolation** (fake Firestore transaction, fake clock)
16. Video upload persists the claim (`processing`, `attempts=1`,
    `lease_expires_at`) **before** the 202 is returned.
17. `estimate_worker_loop` re-claims an estimate whose lease expired and
    completes it from the archived bytes; a doc already `complete` is left
    untouched and no SMS is re-sent (`notified_at` guard asserted).
18. After `MAX_ANALYSIS_ATTEMPTS` expired leases → `failed`, failure SMS to
    caller and owner sent exactly once.
19. Second upload while `processing` → 409, no new task, no object overwrite
    (first attempt's `media_id`/object path unchanged).
20. Re-upload after `failed` → fresh `media_id` and object; the stored
    result/watch pair always refers to the same attempt.
21. `?description=` reaches `analyze_media(text_description=...)`, is stored
    on the doc, and never appears in log output.

**Mutation evidence (mandatory):** for tests 4, 8, 10, 11, 17, 19 — temporarily break
the guarded behavior (accept any signature; remove the poll bound; drop the
503; swallow the task exception; delete the lease-expiry sweep; drop the 409
claim check), show the named test failing, restore, show
it passing. Include the transcript in the PR description. A safety test that
survives its mutation is a finding against the work.

## 9. Acceptance criteria (the reviewer's checklist)

Binary, in order; any failure blocks merge:

1. Full suite: `PATH="<clone>/.venv/bin:$PATH" python -m pytest tests/unit -q`
   → **0 failed**, total ≥ 2559 + the new tests. No skipped tests introduced.
2. All §8 tests exist under the names/behaviors described and pass.
3. Mutation evidence present in the PR for tests 4, 8, 10, 11, 17, 19, and the
   reviewer can reproduce at least two of them independently.
4. `ruff check` clean on every touched file; `git diff --check` clean.
5. Invariant table in the PR: each §4.3 row mapped to the test that proves it.
6. 202-before-analysis holds (test 9), i.e. no Gemini call inside the upload
   request for video.
7. Degraded mode: with `ESTIMATE_MEDIA_BUCKET` unset the suite still passes
   and photo analysis still works (tests 2, 14).
8. No new logging of phone numbers, tokens, signatures, or media bytes
   (reviewer greps the diff; `redact_phone` used where phones appear).
9. Watch URL leaks nothing: test 5 plus reviewer inspection.
10. Deliverables all present: code PR; Lovable prompt doc (§5); infra
    commands in the PR body (§6); updated `pyproject.toml`; no changes
    outside the files this spec names (plus their tests) without a stated
    reason in the PR.
11. Commits are focused with explanatory messages (repo style: why-first);
    no `git add -A` artifacts (no stray files in the diff).
12. Builder did not merge, deploy, or enable anything.

Post-ship product metric (owner-tracked, not a merge gate): estimate
completion rate — `complete / tokens created` — currently 0 of 9 all-time.
The audit script `scripts/phase0_account_audit.py` reports the aggregate.

## 10. Review protocol

The reviewer (separate session) will: re-run §9.1 verbatim; re-run two
mutation checks from §8 without looking at the builder's transcript first;
read the full diff; verify the invariant table honestly maps to tests (not
merely names them); and check the Lovable prompt against §5's contract. The
builder should write the PR description with that audit in mind: claims
backed by commands and their tails, not prose.
