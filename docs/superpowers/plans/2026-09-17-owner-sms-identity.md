# Owner SMS identity — September 17, 2026

The owner could not tell that post-call summary texts came from Kevin and
approved adding an explicit identity to the existing messages.

## Success criteria

- Every personal and business owner post-call summary starts with the exact
  first line `Hey Kevin: Call summary`.
- The identity is added after optional translation, so translation cannot
  remove or translate the brand. Translation failure or an empty translation
  retains the original useful call details beneath the identity.
- Personal summaries say `Call from`, not `Missed call from`. Unknown business
  call classifications use the neutral `CALL` label rather than `MISSED CALL`.
- Preserve the caller facts, phone numbers, business urgency, unconfirmed
  appointment warnings, exact owner/sending-number routing and delivery results.
- Caller-facing confirmations, vCards, automatic replies and SMS transport
  remain unchanged. No real SMS is sent for verification.

## Method and verification

Base: `908855aa9a7b65d982f72d407a7a03a3e37c423a`, tree
`029585762174be66b0ef396604ed38a1b8d1bdb0`.

The master pins the copy and contract. A read-only inventory checks recipient
boundaries; the required headless `gemini-3.7-flash-high` builder implements the
bounded change. The master executes focused tests and mutation probes, then a
fresh reviewer checks the actual diff. Required exact-head CI and configured
Codex review must complete before merge.

## Twilio naming

A phone number's FriendlyName describes its Twilio resource; it is not an SMS
display name. CNAM is for voice caller ID and does not display for SMS.
Alphanumeric SMS sender IDs are unavailable in the United States and Canada.
Therefore this change identifies Kevin inside the message body and does not
rename or reprovision numbers. Saving the sending number as a Kevin contact is
a separate user action; no contact-management UI is added here.

Sources checked September 17, 2026:
- [IncomingPhoneNumber](https://www.twilio.com/docs/phone-numbers/api/incomingphonenumber-resource)
- [SMS sender identification](https://www.twilio.com/docs/messaging/guides/sending-international-sms-guide)
- [Alphanumeric Sender IDs](https://www.twilio.com/docs/numbers-and-senders/alphanumeric-senders)

## Activation boundary

This is backend source work. It requires no new iOS build. Merge does not deploy;
staging and production deployment remain owner-gated under
`docs/agent-operating.md`. A production SMS/phone observation is not claimed by
mocked tests. The added header can increase the segment count of a message near
an SMS length boundary; existing send frequency and sender selection are preserved.

## Local evidence

- 73 focused tests passed: `test_post_call_sms_identity.py` (13 new cases),
  `test_post_call_delivery_results.py`, `test_phase0_post_call_gates.py`,
  `test_appointment_requests.py`, `test_address_validation.py`, and
  `test_post_call_logging_privacy.py`, all under `tests/unit/`.
- The master ran pytest with `KEVIN_DISABLE_DOTENV=1` and a temporary local
  plugin blocking provider networking and real Firestore construction. An
  earlier neighboring-test run stalled on an unmocked database path and was
  stopped; the bounded run with database construction blocked completed in
  0.69 seconds. No real SMS was sent.
- Four in-memory mutations were caught: removing the personal header failed
  5 tests; removing the business header failed 8; removing either empty-response
  fallback failed 2 tests for that mode. Repository files were not mutated.
- Fatal Ruff selectors and `git diff --check` passed. Master audit corrected
  four test-fixture isolation/gate defects and two old first-line assumptions.
  Production behavior remains the bounded SMS-formatting change above.

Routing: `gemini-3.7-flash-high` handled implementation and fixture repair;
`gemini-3.7-flash-low` handled the pinned mechanical line-position assertions and
comment correction. Aggregate reported usage across three headless runs:
467144 input tokens, 53279 output tokens, 520423 total tokens; two repair rounds.
CI and independent review are recorded on the associated pull request.
