# Guarded SMS sender enrollment and recovery

Prepared October 6, 2026. Source base:
`16c45688e0b36a58b30d4e691e1debcb4ac4ff18`.
This implements prevention of the previously repaired US sender-pool gap.
The October 3 repair remains complete; this slice performs no live repair.

## Acceptance contract

> Enroll or confirm only the single freshly verified US number already assigned
> to the requesting active account, in the explicitly pinned, verified Kevin
> Messaging Service campaign. Default off. Preserve the number, voice routing,
> preferences and opt-outs on every failure; never purchase a recovery number,
> move a sender, change a campaign or send/replay a message. Confirm membership
> by an independent provider read, and leave activation and handset receipt
> as separate acceptance steps.

The eligible new assignment and an authenticated later provision-number request
are the only triggers. No scheduler, bulk enrollment, startup sweep or retry
loop is introduced. Existing provisioning purchase/admission behavior is kept.
Existing assignments retain their early return and never reach number search.

## Work graph and ownership

Master pins this contract and owns all Git operations. One headless builder,
`gemini-3.7-flash-high`, implements the bounded code in a separate worktree.
In parallel the master refreshes the authorized support case and prepares the
Brazil pilot package. The outputs are counted and reduced in code. A fresh
staff reviewer examines the integrated artifacts and test evidence. Builder
statements do not establish verification. No hosted execution or activation
follows without its corresponding admission.

## Runtime contract

- Add `sms_sender_enrollment_enabled: bool = False`,
  `sms_sender_enrollment_campaign_sid: str = ""`, and
  `sms_sender_enrollment_campaign_sha256: str = ""` to Settings. Gate checks
  happen before constructing any client. No workflow or live configuration edit.
- Activation is production-only with explicit `firestore_project_id` equal to
  `kevin-491315`, and equal, syntactically valid `twilio_account_sid` and
  `production_twilio_account_sid`. The existing Messaging Service SID must be
  valid. A valid campaign SID and lowercase SHA-256 pin are mandatory.
- The actual Firestore client project and Twilio client account must match.
  Fetch the exact service and campaign. Verify service/account identities,
  `us_app_to_person_registered is True`, `use_inbound_webhook_on_number is True`,
  campaign/account/service identities, `campaign_status == "VERIFIED"`, and
  `us_app_to_person_usecase == "LOW_VOLUME"`.
- Campaign scope digest is SHA-256 of UTF-8 JSON containing exactly
  `description`, `message_flow`, and `us_app_to_person_usecase`, with sorted
  keys, separators `(',', ':')`, and `ensure_ascii=True`. Both text fields must
  be nonempty strings. A changed digest fails closed. Re-read before creating.
  This algorithm is new; do not reuse a differently computed historical digest.
- Firestore assignment query is an equality filter on the exact canonical
  E.164 number, projected fields only, `limit(2)`, timeout 2 seconds, retry=None.
  Exactly one existing document with the requested ID must match. Fields are
  `twilio_number`, `active`, `country_code`, `provisioned_country_code`,
  `number_provider`, `number_type`, `number_capabilities`, `owner_sms_enabled`,
  `owner_sms_opted_out`, `deletion_requested_at`, `deactivated_at`,
  `deleted_app_detected_at`, and `number_released_at`.
- Assignment must be active (`is True`), `country_code` and
  `provisioned_country_code` US, provider twilio, type local, actual boolean
  stored Voice/SMS capabilities true, SMS enabled (`is True`) and opted-out
  (`is False`). Every non-null lifecycle hold blocks enrollment. Missing or
  malformed consent/metadata is unknown and skips; no data is repaired here.
  Validate the sender's actual phone region as US, not merely the +1 prefix.
- Query Twilio IncomingPhoneNumbers by that number with `limit=2`. Require one
  resource, matching E.164/account, a valid PN SID, origin twilio, type local and
  actual boolean Voice/SMS capabilities true. The intended callbacks must match the
  configured Kevin production URL, with no voice/SMS application or trunk SID.
  Freeze its voice/status/SMS callbacks, fallback URLs/methods and app/trunk bindings.
- Fetch membership by exact service/PN SID. Only TwilioRestException HTTP 404
  with code 20404 means absent. All other errors are uncertainty. A present
  membership must match PN/account/service/number, US and Voice/SMS capabilities
  in the SDK's array representation, distinct from the IncomingPhoneNumber dict.
  Matching membership is a no-write success; malformed membership blocks.
- Before the single create, independently re-read the projected tenant query,
  owned number, service and campaign and recheck every invariant, including the
  PN SID and frozen routing. Then use only
  `services(service_sid).phone_numbers.create(phone_number_sid=pn_sid)`.
  Do not update/delete/move numbers or service/campaign configuration. Twilio
  [21712](https://www.twilio.com/docs/api/errors/21712) rejects membership in
  another service; report conflict and never remove that membership.
- After success or an uncertain create exception, attempt one exact membership
  readback. A create response alone is insufficient. A matching readback plus
  unchanged ownership/routing and assignment gives membership evidence only.
  Never retry a create within the invocation. Duplicate/already-member responses
  can resolve through the same readback. Other-service conflict is terminal.
- Use TwilioHttpClient(timeout=2, max_retries=0), a 15-second monotonic budget
  checked before each external operation, and no unbounded enumeration.
  Do not use wait_for to abandon an executor that could later mutate. Signal
  cancellation to the executor and check it before every operation, especially
  the create. A request already in flight may complete within its transport
  timeout; no subsequent create/retry is permitted. Do not claim a strict total
  wall-clock deadline for DNS/transport beyond what the SDK guarantees.
- `ensure_sms_sender_membership(contractor_id, number)` returns a small typed
  result with status/reason enums or allowlisted strings and tri-state mutation
  evidence (unknown after an ambiguous create); no raw IDs, numbers,
  exception text or provider payloads in logs/results. It catches enrollment
  faults, while cancellation is propagated after signaling the worker. No
  Firestore writes or sensitive payload logging. Provisioning wrappers also
  preserve their number if the helper unexpectedly raises.
- Wire after successful assignment persistence in DB provisioning, before the
  DB existing-number return, and before the API existing-number return. No
  client-facing readiness claim or iOS behavior change. Log an allowlisted
  operational reason so the read-only audit can diagnose an unclosed gap.
  Suppress the SDK transport's request/payload logs with a private silent logger.

## Verification and publication

Focused synthetic tests must verify gate-off zero I/O; invalid settings/client
bindings; unknown/disabled/opted-out preferences; lifecycle holds; malformed and
duplicate assignments; CA/BR/GB rejection; foreign/duplicate/missing ownership;
wrong service/campaign/scope/routing; absent versus uncertain membership;
idempotent already-member; one successful addition and independent readback;
concurrent duplicate; other-service rejection; create timeout/failed readback;
budget/cancellation before write; and every fresh-read drift before create.
Assert no number purchase, release, update, send, campaign mutation or Firestore
write from the helper. Exercise all three integration triggers and failure
preservation. Use synthetic valid US numbers only and never authenticate clients.

Master runs focused pytest with `KEVIN_DISABLE_DOTENV=1` and the clone .venv,
fatal Ruff for changed Python, compilation, whitespace checks, and deliberate
guard mutations. A fresh-context review must consume the diff and observed test
evidence. All seven Python shards, quality and stable `Test` are required for a
published PR. Bind exact candidate and cost before any hosted trigger. No paid
Actions allocation is carried from September into October.

## Release boundary

Activation requires a separate owner-authorized deploy/flag envelope binding
serving source, account/service/campaign scope digest, finite registration
scope, rollback, and observed readback. Review the substantial gap between the
last observed serving source and current main. New-source merge is not release.
The feature is scoped to the qualified US campaign and does not authorize any
Brazil registration or SMS route. Preference changes can suppress sends without
removing membership; enrollment never changes an opt-out. An assignment or
consent race after the final prewrite read remains possible across providers;
existing send-time preference gates remain authoritative and no message is sent
by this operation. Operator review handles uncertain or drifted outcomes.

References: [membership API](https://www.twilio.com/docs/messaging/api/phonenumber-resource),
[service routing properties](https://www.twilio.com/docs/messaging/api/service-resource),
[campaign resource](https://www.twilio.com/docs/messaging/api/usapptoperson-resource).
