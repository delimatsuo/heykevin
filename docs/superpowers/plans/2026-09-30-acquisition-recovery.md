# Acquisition recovery and activation acceptance

This slice combines a purchase/restore repair with default-off acquisition
measurement. The next audience experiment will emphasize Personal use, while
keeping Personal trial interest separate from paid retention and Business revenue.
Small historical aggregates do not establish a winning audience or campaign.

## Graph and completion gates

Independent design nodes: purchase/restore flow and acquisition measurement.
Independent implementation nodes use isolated worktrees and pinned
`gemini-3.7-flash-high` headless builders. The master integrates exact file changes,
counts both returned nodes, executes local tests and mutation probes, and assigns
a fresh-context reviewer the combined diff. No builder grades its own work.

Acceptance criteria:

- "Business onboarding cannot purchase an incompatible Personal product."
- "Purchase and restore both await verified entitlement and successful Business activation before advancing."
- "Personal onboarding and Settings upgrade choices remain available."
- "Default-off measurement cannot label free trials or Sandbox transactions as paid purchases."
- "Carrier-confirmed forwarding remains separate from client declarations."
- "Acquisition reports contain aggregate measurements without tokens, customer identities or call content."
- "No paid acquisition resumes before the owner-observed activation checks and release gates are satisfied."

## Personal experiment to prepare

Audience hypothesis: people who want an assistant to screen unknown callers and
summarize conversations. Keep this experiment distinct from business lead capture.
Do not infer actual use from a product name alone or treat internal paid purchases
as external customer acquisition.

Candidate exact-match keywords for a small controlled test:

- `ai call screening`
- `call screening assistant`
- `personal phone assistant`

Review actual search terms after delivery; keyword names are not proof of query
intent. Exclude unrelated general chat assistants and free caller-ID searches
when observed. Keep Search Match off for this first controlled experiment. Keep
older broad/competitor campaigns paused. Do not raise bids just to force volume.

Proposed Personal custom product page message:

> Let Kevin handle the calls you don't want to answer.

Supporting screenshot sequence: an unknown caller being screened, a readable
transcript and reason for the call, then the resulting summary and follow-up
options. Show actual current app behavior. Explain carrier forwarding and the
subscription trial before users commit. Avoid claims that every spam call will be
blocked or that forwarding is activated automatically. Confirm the personal page
has personal examples before attaching it to the experiment.

The experiment remains paused until the improved build and measurement are live
and accepted. A campaign name is not a total spending cap. At activation, enforce
the owner's existing total test allowance independently of the daily budget and
record start/end dates, spend readback, and stopping conditions. Do not create a
new recurring monitor or extend the test without the user's request.

## Five assisted Personal onboarding checks

These are owner-run usability checks with consenting volunteers, not statistical
conversion evidence. Agents must not call live customer numbers or recruit people
without explicit communication authorization.

For each participant record only anonymous case label, app version/build, carrier
family, whether onboarding completed, step of failure, and observed outcomes.
Do not retain a phone number, Apple ID, caller identity, recording or transcript.

1. Start with a new authorized account and deliberately choose Personal.
2. Confirm the selected product, displayed price and trial eligibility agree with
   the Apple purchase sheet. Check cancellation and restore recovery separately.
3. Configure the carrier-specific forwarding option. Record whether the person
   understands the distinction between tapping completion and actually forwarding.
4. Have an authorized second phone call the participant's own number and let the
   carrier forward it. A direct call to the Kevin number does not prove forwarding.
5. Confirm Kevin answers, a transcript/summary is usable and the person knows how
   to stop forwarding and manage the subscription. Only mark these observed after
   performing the test; simulator/unit tests cannot satisfy this gate.

## Release and measurement gates

Code review and tests qualify source only. Backend deployment/flag activation,
App Store privacy disclosure updates, signing/upload and public release each need
their existing owner authorization and a fresh evidence envelope. No release or
activation is implied by merging the source.

Before enabling measurement, verify the expected Apple Ads organization ID and
accurately disclose linked advertising, product-use and purchase analytics. No
new analytics vendor or identity export is introduced. Account deletion removes
the embedded measurement record; do not claim that aggregates are a permanent
accounting ledger.

Use Apple financial reports for accounting and current subscription reports for
active paid plans. An observed positive-price transaction, active entitlement,
introductory trial start, app download and successful forwarding are distinct
events. Trial cohorts must mature before judging paid conversion. Existing
historical downloads cannot be retrospectively assigned to new attribution data.
