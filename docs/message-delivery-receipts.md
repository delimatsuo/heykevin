# Message Delivery Receipt Operations

## Scope

Post-call owner notifications, caller confirmations, caller vCards, and caller
auto-replies register an opaque receipt before Twilio is called. Other SMS/MMS
sends preserve their existing behavior and do not enter this queue.

Twilio does not emit a callback for a Message resource's initial status. Kevin
therefore persists the returned Message SID and initial status, then applies
subsequent callbacks. Twilio may deliver callbacks out of order, so transitions
are monotonic and duplicate-safe. These behaviors follow Twilio's current
[outbound status](https://www.twilio.com/docs/messaging/guides/outbound-message-status-in-status-callbacks),
[tracking](https://www.twilio.com/docs/messaging/guides/track-outbound-message-status),
and [logging](https://www.twilio.com/docs/messaging/guides/outbound-message-logging)
guidance.

## State Model

| Receipt status | Meaning |
|---|---|
| `pending` | Twilio accepted the Message resource, but no terminal receipt exists. |
| `delivered` | Twilio reported `delivered` or `read`. |
| `failed` | Submission failed, Twilio reported failure, provider identity was missing during reconciliation, or terminal evidence conflicted. |
| `acknowledged` | A global administrator resolved a failed receipt without retrying it. |

Only the first valid provider Message SID is bound to a receipt. A callback for
a different SID is rejected. Terminal conflicts fail closed into the operator
queue. Acknowledgement never creates, updates, or resends a Twilio Message.

## Security And Privacy

- Callback URLs contain only a random receipt ID, never a call ID, phone number,
  message body, contractor ID, or provider token.
- Every callback is validated with Twilio's SDK and the complete signed form.
- The handler reads only `MessageSid`, `MessageStatus`, and `ErrorCode`; added
  provider fields remain signature-covered but are ignored.
- Provider Message SIDs remain internal Firestore fields. Logs and admin
  responses expose only bounded receipt/call labels, effect, status, numeric
  error code, and timestamps.
- Receipt registration fails closed before the external send. A persistence
  failure after Twilio accepted a message does not trigger a second send.

## Reconciliation

The tracked post-call worker queries `pending` receipts by a persisted
`next_reconcile_at` due time. The first check is scheduled 12 hours after
registration, matching Twilio's recommendation to poll when no terminal
callback was received; later checks advance by one hour. Due-time ordering
prevents recently checked receipts from starving a larger backlog. The worker
transactionally leases each receipt before fetching it, which moves it out of
the due queue and prevents competing instances from polling the same receipt.
Leases expire after 60 seconds. Provider HTTP I/O uses an 8-second client
timeout and a 10-second async deadline. The worker fetches the existing Message
resource by SID and feeds the result through the same transition function as
callbacks. Reconciliation has no Message create path.

The receipt collection is the source of truth. Effect-scoped fields on call
records are a versioned operational projection. Every receipt state update sets
a durable projection-pending flag. A Firestore transaction then reads the latest
receipt, updates the call record, and clears that flag atomically, so an older
callback cannot overwrite a newer terminal projection. A separate repair pass
handles projection failures without calling Twilio. Callback requests receive a
retriable response when projection fails, while the durable receipt remains
available to operators.

## Worker Runtime

The reconciliation loop runs outside request handling. Cloud Run must therefore
use instance-based billing so background CPU remains available, and it must keep
at least one minimum instance. Google documents both the
[background-activity CPU requirement](https://docs.cloud.google.com/run/docs/tips/general)
and [minimum-instance behavior](https://docs.cloud.google.com/run/docs/configuring/min-instances).

`scripts/verify_cloud_run_worker_runtime.sh` checks both properties before every
staging or production deployment. It is read-only and fails before a revision
or traffic changes instead of modifying service configuration or accepting an
unreliable worker.

The `Deploy` workflow also exposes `staging-audit` and `staging-prepare` manual
operations from the enterprise integration branch. Both use only the staging
WIF identity and immutable staging resource names. Preparation creates a
staging-only configuration revision with background CPU and deploy health
checks, sets a service-level minimum of one, creates missing indexes and TTL
configuration in the isolated staging Firestore project, and reconciles four
payload-free log alert policies. It preserves the normal latest-revision traffic
mode, repairs a stale split to 100% latest when necessary, and verifies the
public staging health contract. It performs every required read before its
first mutation and is idempotent after partial failure. Audit is read-only.

Alert policies preserve an existing managed route. On first setup, the workflow
uses the sole enabled notification channel when exactly one exists; otherwise
set the staging environment variable `STAGING_ALERT_NOTIFICATION_CHANNELS` to a
comma-separated list of enabled channel resource names. Missing, disabled, or
ambiguous routes fail before infrastructure changes.

### Staging IAM Prerequisites

The staging WIF service account must receive only these additional roles:

- `roles/datastore.indexAdmin` on the isolated `kevin-staging-491315` project
- `roles/monitoring.alertPolicyEditor` on the runtime `kevin-491315` project
- `roles/monitoring.notificationChannelViewer` on the runtime
  `kevin-491315` project
- `projects/kevin-491315/roles/kevinStagingLogAlertPolicyEditor` on the
  runtime project, with exactly these permissions:
  `logging.notificationRules.create`, `logging.notificationRules.delete`,
  `logging.notificationRules.get`, `logging.notificationRules.list`, and
  `logging.notificationRules.update`

Log-matched alert policies require both Monitoring alert-policy permissions and
Logging notification-rule permissions. Keep the latter in the project-scoped
custom role above; do not substitute the broader `roles/logging.configWriter`
role.

Do not grant `Owner`, `Editor`, Datastore owner, Monitoring admin, production
Firestore access, or any user-managed credential. After changing IAM, allow for
policy propagation and run `staging-audit`. Run `staging-prepare` only after the
audit completes successfully. The preparation job validates the expected WIF
provider project and exact staging impersonation identity before requesting an
OIDC token.

## Admin Operations

- `GET /api/admin/message-delivery-receipts?status=failed`
- `GET /api/admin/message-delivery-receipts?status=pending`
- `POST /api/admin/message-delivery-receipts/{receipt_id}/acknowledge`

These routes require the global admin credential. Responses are payload-free,
resolutions use a closed enum, acknowledgements emit an admin audit event, and
the queue returns the oldest records first so clearing failures advances the
backlog.

## Rollback

A code rollback must not delete the receipt collection, composite indexes, or
TTL policy. A revision from before this route existed can return `404` to
in-flight callbacks, but it cannot cause a resend because receipts are created
before the provider call and reconciliation has no create path. Roll forward to
a route-compatible revision, verify its exact deploy SHA, and run fetch-only
reconciliation for due receipts. Treat an extended rollback with pending
receipts as an operational incident rather than clearing or recreating them.

## Environment Gates

Before staging canaries:

1. In the isolated staging Firestore project, create the
   `message_delivery_receipts(status, created_at)` and
   `message_delivery_receipts(status, next_reconcile_at)` indexes plus the
   `message_delivery_receipts(call_projection_pending,
   call_projection_next_at)` repair index
   declared in `firestore.indexes.json`. Do not modify production indexes until
   production release approval.
2. Enable Firestore TTL on `expires_at` for the
   `message_delivery_receipts` collection group.
3. Create Cloud Logging alerts for `message_delivery event=terminal_failure`,
   `receipt_storage_error`, `reconciliation_fetch_failed`,
   `reconciliation_pending`, `call_projection_failed`,
   `projection_list_failed`, and
   `post_call_handoff event=worker_component_error`.
4. Configure background CPU and at least one minimum instance; verify the
   deployment's `Verify background worker runtime` step passes.
5. Verify `/health` reports the exact candidate SHA and query only that Cloud
   Run revision during canaries.

Production remains blocked until staging proves submitted, sent, delivered,
undelivered, duplicate, out-of-order, invalid-signature, storage-failure, and
12-hour reconciliation paths, competing-worker lease behavior, bounded provider
timeouts, and durable projection repair without payload leakage or duplicate
sends.
