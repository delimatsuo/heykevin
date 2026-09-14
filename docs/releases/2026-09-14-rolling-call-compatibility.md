# Notification release compatibility — September 14, 2026

The production review found that the staged N1 candidate could lose Take a
message commands when an existing call's WebSocket stayed on the older backend.
This repair adds compatibility in both directions. It does not add a product
feature or approve the native frontend redesign.

The repair starts from `ef9adafda38967fefcf974604e02b22912fcca66`. The publishing
PR identifies its final source HEAD, exact-HEAD CI and merge. The earlier staging
revision `kevin-api-staging-00167-qiq` / `2b56fdaec3eafddbd42f4ebb0516e60db812161d`
does not contain this repair and is superseded as a production candidate.

## Result and verification

New decisions remain in the durable active-call record. A committed message
request can publish one legacy command; an uncertain publication is not repeated.
Old command deletion is not treated as acknowledgment. New consumers adopt old
commands only for their authenticated stream or pinned direct-ring conference.
Delivery, acknowledgment, conditional cleanup and fallback redirects recheck the
current owner, operation, claim, live state and applicable stream/conference.

- Full offline backend suite: **7,869 passed, one skipped**, 30 warnings,
  125.10 seconds. Network connections and application-default credential lookup
  were blocked, and `KEVIN_DISABLE_DOTENV=1` prevented dotenv access.
- Final focused suite after fixture dependency declarations: **113 passed**,
  two warnings, 0.81 seconds. The declarations do not change the extracted
  legacy function bodies. Fatal Ruff checks, compilation and whitespace passed.
- The seven legacy function fixtures match production commit
  `407bf0bc7b6604f33f0113e28c3a2ba82e48b7dc` by AST (only function names differ);
  all seven recorded source-function hashes were independently verified.
- Causal tests initially exposed missing generation/claim fences and a stale
  direct-ring redirect. All were repaired. Tests actually replace records during
  transaction retries, commit a command before raising a lost-response error,
  consume it through the old voice function, and attempt a duplicate request.
- Baseline race probes: **14 passed**. All eight in-memory mutations were caught:
  committed projection nonce (two failures), single publication attempt (two),
  stream generation (one), conference generation (one), adoption stream binding
  (one), conditional cleanup (one), delivery/acknowledgment claim nonce (two),
  and the redirect transaction's conference fence (one). Repository files were
  unchanged by mutation execution.
- Independent Codex source review (`gpt-6-astra`, high) approved with no remaining
  findings, conditioned on the full suite and mutation evidence above.
- No iOS source changed in this repair. Earlier simulator evidence remains
  separate from installed-iPhone and provider acceptance.

Reviewed source SHA-256 values:

| File | SHA-256 |
|---|---|
| `app/services/owner_call_actions.py` | `054b4aa6467e031294254d5e77939cd039d1b88583274526310834328afcd7ae` |
| `app/services/legacy_call_commands.py` | `075edc2460ac884a911c4b053a0b18cb4248b189dcc0a338dd5785604cd3677d` |
| `app/webhooks/twilio_incoming.py` | `c6f846594e834cdde66d07654822e1c0622d3f680df1fe9942581e40822c1732` |
| `app/webhooks/media_stream.py` | `e34b315dee73cbffe6cb7fe36b862c58a96be5319414f7e9b125d7d88c84fb72` |

The media-stream change binds the already-authenticated stream token to its
consumer. Both existing bakeoff integrity pins were updated to that exact hash;
other integrity pins and activation/import prohibitions remain unchanged.

## One-deployment plan and recovery

The final staff release review approved one normal candidate deployment, subject
to exact-HEAD CI, refreshed staging and owner production approval. Deploying a
notification-suppressed baseline first would improve recovery latency, but is
not required to repair the newly introduced delivery regression. No maintenance
mode, new feature flag, ingress restriction or interrupted call is proposed.

1. Merge the reviewed repair after exact-HEAD CI. Stage the resulting main SHA
   through the existing workflow with its explicit `candidate_sha` and verify
   identity, traffic, staging isolation and anonymous serving.
2. Rebind remote main, queue, runtime configuration, required checks and cost.
   Dispatch production once from main without `candidate_sha`. Only Deli may
   approve its GitHub production environment. The workflow promotes before its
   health check and has no automatic rollback.
3. Verify production health's exact SHA, revision and serving traffic. Do not
   claim provider delivery or installed-iPhone acceptance from these checks.
4. During the rolling overlap, legacy 407 accept and direct-ring workers retain
   their pre-existing races. The adapter fixes command delivery, not those old
   writers. Global operation-ownership guarantees apply only after incompatible
   workers have ceased. No fixed sleep or idle request snapshot proves that.
5. If new notification behavior needs containment, use the reviewed
   [forward-recovery patch](patches/2026-09-14-notification-containment.patch).
   It suppresses additional urgent alerts and screening-summary updates while
   retaining initial banners, normal screening, known-contact rings and the
   repaired action/command protocol. This requires a new source candidate,
   matching containment test expectations, exact-HEAD CI, build and separately
   owner-approved deployment. It is not an already deployed rollback revision.
   Additional recovery cost and elapsed time must be reviewed if it is needed.

The patch applies cleanly to the reviewed source. Offline execution of the
patched entry points verifies suppression before storage/extraction/push, and
runs the shared 113 action/compatibility checks against those patched modules:
**115 passed**, four warnings, 6.54 seconds.
This bounded recovery verification does not assert that the regular notification
emission tests should pass after intentionally disabling their behavior.

Patch SHA-256:
`1ca3548eafa87bc5fe5cb13fc16c0f85ff1b5e5200b5d636e426b32d48cfb136`.
The patch is stored as an unapplied artifact; normal candidate notifications
remain enabled. It contains no runtime flag, credentials or provider changes.

Do not route ordinary recovery back to `kevin-api-00269-42l` / `407bf0bc...` while
current operations remain live. That version ignores durable operation ownership.
The containment patch addresses notification behavior only; a defect in shared
call arbitration requires a targeted forward fix. Recovery cannot retroactively
stop tasks already executing on a previous revision.

## Hosted validation and scope

Before publication, the repository and personal billing owner were verified as
`delimatsuo`; `delimatsuo/heykevin` is public. Main requires strict `Test` from
GitHub Actions (app 15368), with no extra rulesets. Feature-branch pushes have no
deploy trigger; a PR runs seven Python shards, quality and Test on standard
`ubuntu-latest` runners. The Python jobs have 15-minute limits and Test has five;
PR-scoped concurrency cancels obsolete runs. The previous PR used 385 runner
seconds; expected ordinary validation is approximately 6–10 runner-minutes,
with $0 chargeable public-repository Actions usage. No paid allocation is assumed.
Cloud Build/runtime charges are separate. No workflow, runner or billing settings
were changed. Both deployment jobs are skipped on PRs; merging main does not deploy.

Source routing: parent/master Codex `gpt-6-astra` (high); one pinned headless
`agy gemini-3.7-flash-high` builder pass, followed by parent causal corrections
and independent reviews. Builder usage was 385,403 input, 95,332 output,
480,735 total tokens, with zero builder retries. The source review and final
release review were independent; the latter explicitly narrowed its initial
proposal after distinguishing legacy behavior from the new regression.

The current PRD remains the scope authority. Callback/text actions, broader
frontend refactoring, provider changes and additional product work remain deferred.
