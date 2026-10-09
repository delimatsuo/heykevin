# Authenticated Jobber disconnect envelope — pinned pure foundation

Base `8cc4a01e85e5c3e0c741037722c18aa7aba04735`, tree
`514fabb54905c68de0dfd556b97e304eea3252e7`.
Master owns architecture, Git and verification; builder is headless
`agy gemini-3.7-flash-high` for bounded Python implementation.

Implement only `app/services/jobber_webhook_events.py` (new) and
`tests/unit/test_jobber_webhook_events.py` (new). No edits to this brief or
other files. No Git mutation, test execution, dotenv/credential read, network,
provider/cloud/configuration, routes, flags, logging, persistence or deployment.

Expose frozen `JobberDisconnectEnvelope` and pure
`authenticate_jobber_disconnect_envelope(raw_body, signature, client_secret,
expected_app_id) -> JobberDisconnectEnvelope | None`. Inputs may be malformed
objects. First require a valid nonempty configured expected_app_id, then use
the existing exact-raw-byte HMAC primitive before JSON parsing. No signature
verification changes. No parse or returned data on authentication failure.

Modern JSON contract is data.webHookEvent with exact string topic
APP_DISCONNECT, appId equal to configured expected_app_id, nonempty opaque
accountId, and occurredAt. IDs are exact strings 1..1024 Unicode code points,
without any whitespace or Unicode control category Cc. Do not decode IDs or
impose uniqueness/tenant relationships. itemId is optional, null or an ID of
the same type; it is metadata only because the provider has not documented
APP_DISCONNECT-specific meaning. Extra fields are ignored, never retained.

Use strict UTF-8 and JSON. Reject duplicate object keys at any depth, nonfinite
JSON constants, invalid structures/types, BOM/malformed UTF-8, too-deep input,
and every malformed event by returning None without exceptions/logging.
Body bounding is the existing 1..65536 byte HMAC limit; document that a future
HTTP caller must enforce the limit while streaming, before buffering.

occurredAt must be an exact bounded RFC3339 string: 4-digit year, date, T,
hour:minute:second, optional 1..9 fractional digits, and Z or +/-HH:MM offset.
Use a real datetime parser to reject impossible dates/times; reject absent
timezone and legacy misspelling occuredAt. Store the original validated text
as `occurred_at` (do not silently lose timestamp precision). No timestamp age,
freshness, clock-skew, event order, local generation, or grant decision.

Return only app_id, account_id, item_id, occurred_at and
`delivery_fingerprint` = lowercase SHA256 of authenticated exact raw bytes.
Never retain raw body, client secret, signature, unknown fields, tokens or codes.
Fingerprint identifies identical raw deliveries only; it is NOT a provider
event ID or a semantic deduplication proof. Different serialization can yield
different fingerprints. Success authenticates the envelope only, NOT grant
applicability, durable acceptance, safe revocation, or Marketplace readiness.
Module remains unmounted with no datastore/provider effects.

Tests: valid exact body + real HMAC; changed body/signature/secret; expected
app mismatch; absent/invalid configured app; wrong topic/schema/types/IDs;
duplicate keys at multiple depths; malformed UTF-8/JSON/BOM/nonfinite/deep;
size boundaries; valid fractional/offset timestamps; impossible/local/legacy
dates; optional/null itemId; ignored unknown fields; same exact bytes same
fingerprint versus same event differing serialization different fingerprint;
authenticate-before-parse guard. Master will kill HMAC/app/topic guard mutants.

Primary source:
https://developer.getjobber.com/docs/using_jobbers_api/setting_up_webhooks/
Provider event ID, retry horizon, ordering and grant correlation remain
unresolved. Do not invent a receiver, storage queue or registration mutation.
