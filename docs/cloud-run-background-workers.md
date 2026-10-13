# Staging background-worker cost qualification

Date: 2026-10-13. Project: `kevin-491315`, region: `us-central1`.

This candidate extracts maintenance and recovery from the call API into a
private HTTP worker. It is staging-only and defaults to the existing embedded
loops. Source validation is not deployment or call qualification. Production
must retain its existing minimum instance and instance-based billing settings.

## Why this is a staged change

The October 13 cost audit measured R$538 of Cloud Run cost in the previous
30 days, with production and staging each accumulating about 2.59 million
billable instance-seconds. Each service keeps one 1-vCPU/512-MiB instance warm.
Staging had 111 requests in that interval. Its roughly half share of those
instance-seconds suggests a worthwhile saving opportunity; R$269 is an
approximate allocation, not a measured service invoice or realized saving.

Request-based CPU cannot simply be enabled on the call API: ring timeouts,
redirects, transcript persistence, and post-call work can continue after a
response or WebSocket closes. Cloud Run may terminate idle instances at any
time and provides only a short shutdown window. Separating the polling loops
does not itself close the window before a transcript and outbox entry are
durable. See [Cloud Run billing settings](https://docs.cloud.google.com/run/docs/configuring/billing-settings)
and the [container contract](https://docs.cloud.google.com/run/docs/container-contract).

## Source contract

- `BACKGROUND_WORKER_MODE=embedded` remains the default. `external` is
  rejected outside staging, with a production-resource override, or unless
  `BACKGROUND_WORKER_CUTOVER_GENERATION` is a 16–128 character non-secret
  identifier. It also rejects enabled `PURGE_ENABLED`,
  `SERVICE_REQUEST_RECOVERY_ENABLED`, or
  `JOBBER_DISCONNECT_RECOVERY_ENABLED`: those loops have not been given this
  external-worker contract and must never be silently stopped by a cutover.
- The API starts its existing nine loops only in embedded mode. Maintenance
  tasks are retained, cancelled, and awaited on shutdown. Call routes,
  subscription policy, feature defaults, and Firestore client rules are intact.
- Before any external cutover, an updated staging API must run in embedded mode
  and write protocol version 1 to
  `private_background_worker_control/cutover`. The only permitted state path
  is `embedded → external_requested → external_active → external_draining →
  embedded`. A missing protocol baseline is not a valid starting point.
- A controlled, non-HTTP operator action may create `external_requested` only
  after independently observing old embedded instances drained and a blocked
  Scheduler request reach the private worker. It records the exact generation
  and a 60-second to 24-hour lease. The API acknowledges that record
  transactionally before it stops embedded loops; the worker transactionally
  refuses to claim business work until the matching `external_active` record
  exists. An incomplete, mismatched, or expired handoff fences external claims
  and restores embedded processing only after every ledger record is
  `completed`. A `running`, malformed, or `needs_attention` record holds the
  API in draining state until an operator reconciles it. If the control
  datastore is unavailable, the API reports
  `cutover_unavailable` and starts no loops rather than risk duplicate work.
- `app.worker_main:app` recognizes the ledger's fixed job names but executes
  only the six jobs below. Requests for `data_purge`, `service_requests`, or
  `jobber_disconnect` return `disabled` before a datastore claim or business
  I/O. It imports no public API startup. Cloud Run IAM and a Google-signed OIDC
  token with the exact audience and invoker email protect task requests.
- Each admitted request awaits a separate process with a 240-second wall
  budget. Cancellation, timeout, malformed output, and shutdown terminate
  the owned process group, escalating after two seconds and reaping the
  direct child. Descendants cannot evade cleanup by closing stdout.
- The private Firestore collection `private_background_jobs` has at most
  nine documents. Server snapshot time, a random claim nonce, calendar-slot
  admission, and transactional completion prevent concurrent execution of
  the same scheduled job. Every ledger RPC has a two-second timeout and no
  retry. Empty server replies do not fall back to local clock admission.
- A failed/uncertain job becomes `needs_attention`; an old `running` entry
  is also held. Neither is automatically replayed. Existing per-item
  post-call and estimate claims remain authoritative. A quarantined
  post-call item does not stop the global scan.
- Pending/in-progress post-call cursors and estimate cursors are persisted
  across requests. Cold starts cannot repeatedly starve later documents.
- External orphan cleanup reads at most 25 RTDB keys plus its current boundary;
  lapsed trials read at most 25 Firestore documents; and expired-contractor
  cleanup reads and acts on at most five documents per request. Call retention
  retains its four 500-record-batch cap. No external job materializes an
  unbounded contractor or active-call collection.

## Proposed pilot resources

This is the reviewable configuration, not evidence that it exists.

| Setting | Value |
| --- | --- |
| Cloud Run service | `kevin-background-staging` |
| Project / region | `kevin-491315` / `us-central1` |
| Image | Immutable digest built from the exact admitted candidate SHA |
| Entrypoint | `python -m uvicorn app.worker_main:app --host 0.0.0.0 --port 8080` |
| Runtime identity | Existing `kevin-api-staging-runtime` service account |
| Invoker identity | Dedicated new `kevin-background-scheduler` service account, no key |
| Worker CPU / memory | 1 vCPU / 512 MiB, request-based billing |
| Worker scaling / concurrency | Minimum 0, maximum 2, concurrency 1 |
| Cloud Run / Scheduler timeout | 300 seconds / 300 seconds |
| Scheduler retry count | 0; later cron deliveries use the durable ledger |
| Audience | Exact worker service origin returned by Cloud Run, without task path |
| Data | Firestore `kevin-staging-491315`, staging RTDB only |
| Worker mode | `ENVIRONMENT=staging`, `BACKGROUND_WORKER_MODE=external`, exact generation |
| Cutover control | `private_background_worker_control/cutover`, protocol version 1, finite lease |
| Public invokers | None; no `allUsers` or `allAuthenticatedUsers` binding |
| API during migration | Staging minimum 1, instance-based CPU |

The runtime must receive the existing staging secret references and required
configuration through the protected deployment mechanism, without exporting
secret values to the checkout or logs. Bind the worker audience and the exact
scheduler service-account email in the two `BACKGROUND_WORKER_SCHEDULER_*`
settings. Verify staging data and telephony bindings before running any job.
Do not enable optional business features as part of this cost change.

Initial UTC schedules (six jobs):

| Endpoint suffix | Cron | Bound / retained behavior |
| --- | --- | --- |
| `post_calls` | `* * * * *` | One pending item and 25 in-progress records; durable cursors |
| `estimates` | `* * * * *` | Scan at most 25; analyze at most one |
| `orphan_calls` | `*/5 * * * *` | At most 25 RTDB keys plus boundary; recheck stale state before deletion |
| `call_retention` | `7 */6 * * *` | At most four existing 500-record cleanup batches |
| `lapsed_trials` | `17 */6 * * *` | At most 25 documents; transactionally recheck before expiry |
| `expired_contractors` | `27 */6 * * *` | At most five documents/actions; existing release safety guards |

`data_purge`, `service_requests`, and `jobber_disconnect` do not have external
schedules. Immediately before cutover, read back `PURGE_ENABLED`,
`SERVICE_REQUEST_RECOVERY_ENABLED`, and `JOBBER_DISCONNECT_RECOVERY_ENABLED`
and require all three to be false (unset uses the source default false). The
runtime rejects external mode if any is true. A later migration needs a
separate reviewed contract and owner-authorized schedule/resource envelope.
The new ledger does not authorize Twilio number release or messages
independently of the existing service and owner rules.

## Cost envelope and limits

At published us-central1 rates, an always-running 1-vCPU/512-MiB instance is
about US$49.25 per 30 days before free allowance and currency conversion.
For the proposed six schedules, there are about 95,400 requests per 30 days.
At a **hypothetical** mean billed execution of 1, 3, or 5 seconds, worker CPU
and memory would be about US$2.41, US$7.23, or US$12.04, plus request fees
(about US$0.04 before allowance). These are scenarios, not measured results;
include process startup, OIDC verification, datastore time, and billed idle
retention when measuring actuals. See [Cloud Run pricing](https://cloud.google.com/run/pricing).

Six Scheduler jobs cost at most US$0.60 per 31 days before the shared three-job
free allowance. Paused jobs are still charged. Nine jobs would cost US$0.90
before allowance. See [Scheduler pricing](https://cloud.google.com/scheduler/pricing).

Proposed qualification envelope: one image build and at most 24 hours of
worker/staging qualification, **US$5 maximum incremental GCP cost**. Keep
production unchanged. A monitor outside GitHub Actions must read actual
execution seconds/request counts and billing during the pilot. At 1 vCPU,
512 MiB and maximum two worker instances, 24 hours of continuous worker
compute is about US$4.36 before request/build/other charges; therefore maximum
instances alone does not enforce the US$5 total. Check every 30 minutes,
stop scheduling at US$4 projected incremental usage, and preserve headroom
for in-flight work. A budget alert is not a hard cap. Do not start if build,
other usage, or billing lag prevents this envelope from being respected.

Keeping the API minimum at one during qualification temporarily adds worker
cost. No Cloud Run saving can be reported until an approved scale reduction
has been qualified and read back, followed by measured billing.

## Activation sequence

1. Bind the exact candidate SHA, required CI result, approved spend, runtime
   identity, immutable image digest, and current staging rollback revision.
   Deploy that candidate first with `BACKGROUND_WORKER_MODE=embedded`; read
   back the protocol-version-1 `embedded` control document and the healthy API.
   This baseline is required before external preparation.
2. Deploy the worker privately with the exact generation but no active
   cutover. Inspect actual IAM, configuration, route surface, and unauthorized
   responses. Create the six schedules paused, grant the dedicated scheduler
   identity `roles/run.invoker` on this worker only, and preserve the
   Google-managed Scheduler service-agent role required for OIDC delivery. See
   [Scheduler HTTP authentication](https://docs.cloud.google.com/scheduler/docs/http-target-auth).
3. With the API still embedded and the three omitted-worker flags confirmed
   false, use one paused/controlled Scheduler delivery to verify the exact OIDC
   identity reaches the worker. Its pre-cutover `409 cutover_blocked` response
   is expected and performs no business I/O. Record the delivery outcome before
   calling the controlled preparation action; do not treat an HTTP `200` or a
   health endpoint as proof that Scheduler is configured.
4. Ensure no staging call is active and drain every old embedded staging
   instance. Record old revision/tag IDs, remove traffic allocations and tags,
   then wait for outstanding requests to settle and old revisions to reach zero
   instances. Fresh per-revision metrics may lag; absence of metrics is not
   proof of zero. Revision minima can keep tagged revisions warm even at zero
   traffic, as described in [minimum-instance behavior](https://docs.cloud.google.com/run/docs/configuring/min-instances).
   Legacy loops do not participate in the ledger. If termination cannot be
   verified, stop rather than preparing a cutover or deleting revisions.
5. Only after steps 1–4 are independently read back, prepare the finite lease
   with the exact generation and explicit drain/Scheduler confirmations. A
   schedule now receives `200 cutover_pending`, still without business I/O.
   Deploy or restart the staging API in external mode. It must report
   `worker_mode=external` after transactionally acknowledging the same record;
   otherwise it remains/waits in embedded recovery. Resume only the approved
   six schedules.
6. Verify duplicate delivery across two instances, cancellation during
   admission and completion, lease expiry/fallback, and uncertain completion
   acknowledgement. Check ledger statuses without exposing call records or
   secrets. Measure duration, peak memory, cold start, failures, overlap,
   oldest pending item, and completion latency. Stop if oldest ready
   post-call/estimate exceeds five minutes for two checks, fails to drain after
   a test burst, memory reaches 80%, or any job is held.
7. Before reducing staging minimum instances, the owner qualifies actual
   cold-start calls, two simultaneous calls, long calls, ring/pickup/redirect,
   hangup and transcript persistence, summary/SMS once, and restart/shutdown
   around post-call handoff using dedicated test identities. Agents do not call
   existing customers. Keep minimum one if the pre-durable-handoff window
   remains unresolved.
8. **Separate later phase:** the initial 24-hour/US$5 envelope ends after
   qualification and does not authorize another day of operation. Only after
   qualification and a further explicit time/cost envelope for scale reduction,
   reduce both service-level and revision-level staging minima to zero,
   preserving instance-based API CPU. Read the effective service and revision
   settings back, repeat cold-start checks, and compare at least 24 hours of
   measured instance seconds and request latency. Do not change production in
   this pilot.

## Holds and rollback

Any authentication discrepancy, production data binding, missing transcript,
duplicate side effect, growing backlog, resource limit, or budget concern
stops scheduling. Pause and read back all six schedules; allow already
admitted requests to settle within their bounded lifetime. Read running and
held ledger records before any replay. Never delete the ledger or clear
`needs_attention` merely to make the job run again.

An operator reconciles provider outcome and the existing per-item claim for
every uncertain execution. After confirming no process is executing and
whether a side effect happened, the operator may authorize a nonce-matched
ledger transition to completed with the preserved cursor and explicit next
due time. No automated replay or repair endpoint is included in this slice.

For API rollback, restore minimum one first; pause schedules and let the API
move the control record to `external_draining`. The worker refuses new claims,
and embedded loops start only after every ledger record is completed. A held
`needs_attention` record requires explicit operator reconciliation; it is
never replayed automatically by fallback loops. Never delete or manually clear
the control record to bypass that fence. Retain the candidate image and rollback
configuration through qualification; do not expand the earlier
one-time container cleanup approval to these images or other resources.

## Evidence

See `docs/audits/2026-10-13-background-worker-validation.json` for the
historical root-run local checks, reviewed source hash, mutation probes,
provider observations, and remaining activation work. It predates the durable
cutover and pagination hardening described above. CI, deployment, real-call
safety, and realized billing are distinct outcomes.

## October 13 pilot result

The owner approved US$1 maximum for CI/AI and US$5 incremental GCP for the
candidate `ec6b8c1246c8f27a690c39876981dc5acf04c6ef`. Its single
[staging workflow](https://github.com/delimatsuo/heykevin/actions/runs/38421770589)
passed 9,286 Python tests and deployed successfully. All six jobs completed an
initial authenticated pass, and duplicate requests overlapped across two
worker instances without a second claim in the same slot.

**The pilot did not qualify sustained external operation.** A later estimates
request returned HTTP 503 and became `needs_attention` at 16:05:17 UTC. The
external monitor paused all six schedules at 16:05:47. Deployed diagnostics
could not distinguish a child failure from a completion-write failure, so the
technical cause remains unresolved. Do not replay or clear this claim.

Authoritative Firestore snapshots found the entire estimates collection empty
at admission, at the hold transition, and after draining. There was no observed
staging write traffic, active call, or recovery backlog. After confirming
execution had stopped, the six temporary Scheduler jobs were deleted and the
pilot invoker binding was removed. Independent readback at 16:15 UTC confirmed
staging revision `kevin-api-staging-00191-jim`, the same candidate image,
embedded mode, both minima one, and always-allocated CPU. Production remained
`kevin-api-00277-neb`. The held ledger, historical quarantines, images and
revisions were retained.

The [pilot receipt](audits/2026-10-13-background-worker-pilot.json) and
[monitor observations](audits/2026-10-13-background-worker-pilot-observations.jsonl)
record execution, reconciliation, costs and cleanup. The earlier validation
file is a pre-pilot snapshot. No Cloud Run saving or real-call qualification
has been demonstrated. Before another pilot, add bounded failure-phase/code
diagnostics without provider messages or personal data, and bind a verified
staging iPhone build, push environment and dedicated test account.

## Current hardening and call setup

The local follow-up keeps fixed failure phases and codes for worker bootstrap,
configuration, admission, subprocess execution, completion and cleanup. It
does not log exception messages, provider payloads, caller identifiers or
tracebacks. Child output remains bounded at 4 KiB and stderr is discarded.
It also adds the durable generation/lease handoff, a verified embedded
baseline, external claim fencing, bounded Firestore/RTDB pages, and a runtime
refusal for unmigrated optional loops. It has not run in hosted CI or staging.
It does not resolve the historical estimates failure or authorize another
pilot. See the [diagnostic validation receipt](audits/2026-10-13-background-worker-diagnostics.json)
for the earlier diagnostics-specific source review and deliberate
guard-removal checks.

The following setup facts were checked on October 13:

| Item | Status |
| --- | --- |
| Staging API | Restored to embedded workers, warm, healthy |
| Staging Twilio number | Existing number ending 0993 routes to staging and one staging contractor; dedicated test ownership still needs confirmation |
| Installed iPhone app | Connected developer build 1.3.4 (45); its backend and signed entitlement are not exposed in device metadata |
| App backend | Baked into `BackendURL`; there is no runtime environment switch or label |
| TestFlight | Does not establish the backend; version/build alone is insufficient and no matching physical-device archive is locally available |
| Push environment | Local candidate gives Debug and Staging a development entitlement matching sandbox APNs; Release retains production |
| Staging source validation | XcodeGen, resolved Debug/Staging settings, and unsigned generic-iPhone Staging and Release builds passed |

The connected app is a developer build, but device metadata does not expose
its `BackendURL` or signed APNs entitlement. The local candidate now gives
Debug and `Kevin Staging` a separate development entitlement and keeps Release
on the production entitlement. It also fixes a compile-only screenshot-fixture
path that blocked release-like Staging builds; screenshot scenarios remain
disabled outside Debug. XcodeGen generation, settings resolution, and unsigned
generic-iPhone Staging and Release builds passed.

The configurations share the same bundle identifier and Keychain namespace, so
any later installation must account for the existing app and credentials. No
app was signed or installed, and no real call, push change, SMS or number
purchase was initiated in this follow-up.

Before another worker pilot, bind its exact candidate and a new one-build
CI/GCP envelope, confirm the device and dedicated test account, reconcile the
held estimates claim using fresh aggregate counts and its exact nonce and
update time, and complete the new control-record sequence above. Do not clear
or replay the claim automatically. Production stays unchanged and staging
stays warm throughout call qualification.
