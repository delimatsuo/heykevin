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
  rejected outside staging and with a production-resource override.
- The API starts its existing nine loops only in embedded mode. Maintenance
  tasks are retained, cancelled, and awaited on shutdown. Call routes,
  subscription policy, feature defaults, and Firestore client rules are intact.
- `app.worker_main:app` exposes health and nine allowlisted job endpoints.
  It imports no public API startup. Cloud Run IAM and a Google-signed OIDC
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
| Worker mode | `ENVIRONMENT=staging`, `BACKGROUND_WORKER_MODE=external` |
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
| `orphan_calls` | `*/5 * * * *` | Recheck stale call state transactionally before deletion |
| `call_retention` | `7 */6 * * *` | At most four existing 500-record cleanup batches |
| `lapsed_trials` | `17 */6 * * *` | Transactionally recheck subscription before expiry |
| `expired_contractors` | `27 */6 * * *` | Existing deleted-app and lapsed-number safety guards |

`data_purge`, `service_requests`, and `jobber_disconnect` remain disabled when
their existing feature flags are off; do not create their schedules then.
Immediately before cutover, read back `PURGE_ENABLED`,
`SERVICE_REQUEST_RECOVERY_ENABLED`, and `JOBBER_DISCONNECT_RECOVERY_ENABLED`
and require all three to be false (unset uses the source default false).
If any is true, stop and revise the authorized schedule/resource envelope;
do not silently stop an existing recovery worker or change its flag.
If separately authorized later, their intervals are six hours, one minute,
and one minute respectively. The new ledger does not authorize Twilio number
release or messages independently of the existing service and owner rules.

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

1. Bind exact candidate SHA, required CI result, approved spend, runtime
   identity, immutable image digest, and the current staging rollback revision.
   As observed on October 13, staging runs source `5a1236d7d0aaa187b5bf1d951c8765d83d9d8117`;
   this candidate includes intervening main-branch changes, so qualify the
   complete candidate, not just this diff.
2. Enable Scheduler only within the approved project. Deploy the worker
   privately, with no scheduler invoker grant yet. Inspect its actual IAM,
   config, route surface, and unauthorized responses. Create all six schedules
   and explicitly pause/read back each one before granting invoker permission.
   Do not assume the create command supports a `--paused` option. Grant the
   scheduler identity `roles/run.invoker` on this worker only; preserve the
   Google-managed Scheduler service-agent role required for OIDC delivery.
   See [Scheduler HTTP authentication](https://docs.cloud.google.com/scheduler/docs/http-target-auth).
3. Keep the staging API minimum at one and CPU always allocated. Require the
   three omitted-worker flags to be false as described above. Ensure no staging
   call is active, deploy staging API with external mode, and stop **every**
   old embedded staging instance before schedules resume. Record the exact old
   revision/tag IDs; remove their traffic allocations and traffic tags, then
   wait for outstanding requests to settle and old revisions to reach zero
   instances. Verify fresh per-revision metrics and account for ingestion lag;
   missing metrics are not proof of zero. Revision minima keep tagged revisions
   warm even at zero traffic, as described in
   [minimum-instance behavior](https://docs.cloud.google.com/run/docs/configuring/min-instances).
   Legacy loops do not participate in the new ledger, and moving traffic alone
   does not prove they stopped. This pilot does not delete old revisions. If
   their termination cannot be verified, stop rather than activating schedules
   or deleting revisions. Retain the rollback revision, image and configuration.
4. Verify worker OIDC audience/email, successful real scheduled delivery,
   duplicate delivery across two instances, cancellation during admission and
   completion, and uncertain completion acknowledgement. Check each ledger
   status without exposing call records or secrets. Confirm inactive jobs
   execute zero business I/O. Resume only the approved six jobs.
5. Measure duration, peak memory, cold-start time, request failures, scheduling
   overlap, oldest pending item, and completion latency. The bounded recovery
   throughput is lower than embedded mode (post-calls formerly ten per pass
   every 30 seconds, estimates every 30 seconds, Jobber every ten seconds).
   Stop if oldest ready post-call/estimate exceeds five minutes for two checks,
   fails to drain after a test burst, memory reaches 80%, or any job is held.
   A one-item scan per minute is not a general high-volume capacity guarantee.
6. Before reducing staging minimum instances, the owner qualifies actual
   cold-start calls, two simultaneous calls, long calls, ring/pickup/redirect,
   hangup and transcript persistence, summary/SMS once, and restart/shutdown
   around post-call handoff using dedicated test identities. Agents do not
   call existing customers. Investigate the pre-durable-handoff window;
   a successful normal call is insufficient evidence for arbitrary instance
   termination. Keep minimum one if this remains unresolved.
7. **Separate later phase:** the initial 24-hour/US$5 envelope ends after
   qualification and does not authorize another day of operation. Only after
   qualification and a further explicit time/cost envelope for scale reduction,
   reduce **both** service-level and revision-level staging minima to zero,
   preserving instance-based API CPU. October 13 readback found both minima
   set to one. Read the effective service and revision settings back, repeat
   cold-start checks, and compare at least 24 hours of measured instance
   seconds and request latency. Do not change production in this pilot.

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

For API rollback, restore minimum one first; pause/drain external workers and
reconcile holds before starting any embedded revision. Redeploy the retained
rollback image with staging bindings and embedded mode. Never run old
embedded and new external sweeps concurrently. Retain the candidate image
and rollback configuration through qualification; do not expand the earlier
one-time container cleanup approval to these images or other resources.

## Evidence

See `docs/audits/2026-10-13-background-worker-validation.json` for root-run
local checks, the independently reviewed source hash, mutation probes,
provider observations, and remaining activation work. CI, deployment,
real-call safety, and realized billing are distinct outcomes.
