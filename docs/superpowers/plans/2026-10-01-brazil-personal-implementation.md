# Brazil Personal — implementation contract

Owner authorized implementation October 1, 2026, after accepting the
[Brazil-first qualification plan](2026-10-01-brazil-personal-first.md).
Country order remains Brazil Personal, Canada, then UK.

## Source scope and acceptance

The plan's provider-independent requirements form one reviewable candidate.
Provider selection, number registration, carrier qualification and
country activation remain separate acceptance work; the source must not invent
those outcomes.

The accepted plan requires:

> Add explicit Brazil/+55/DDD entry, normalized and validated numbers, and a clear
> unsupported-country response.

New account creation and number provisioning must validate the actual phone
region and current admission policy before trial or provider effects. US and
Canada remain available; Brazil and UK are in qualification. Verified existing
accounts remain recoverable regardless of the new-signup policy.

> Bind the account's country, provider, number type and capabilities together;
> changing a profile country must not reinterpret an already assigned number.

Server-owned assignment metadata drives the iPhone's setup. Legacy numbers keep
their service; absent evidence means unknown capabilities. Conflicting country
changes are rejected. Unqualified non-NANP templates must not produce activation
or cancellation dial actions, including paywall cancellation.

> Complete pt-BR signup, setup, paywall, notifications and errors; qualify
> greetings, conversation, transcript and summary with the actual voice engine.

This candidate completes scoped source copy, deterministic Personal greetings,
language instructions and summary wiring. Tests can prove copy, routing and
request construction; natural speech and live delivery require physical/provider
acceptance. StoreKit metadata supplies displayed subscription currency and
price. No Brazil price or unlimited-use offer is invented.

## Interfaces pinned before implementation

Public `GET /api/markets` returns a `markets` array of `country_code` and
`status`, plus `qualification_order: [BR, CA, GB]`. Status is `available` for
US/CA, `qualification_required` for BR/GB, and `unsupported` for the remaining
recognized legacy countries. It performs no customer/provider lookup.

Admission errors use an HTTP error `detail` object with `code`, `country_code`
and a fallback `message`: `country_not_available` and `country_locked_to_number`
are 409; `invalid_owner_phone` and `country_phone_mismatch` are 400.

Contractor/provision/settings reads expose optional `service_binding` with
`country_code`, `provider`, `number_type`, and boolean `capabilities`. A legacy
assigned number may have known country and unknown remaining metadata; an
unparseable number has unknown country. Reads do not silently migrate accounts.

The existing `user_language` profile field carries the owner's language.
`pt` and `pt-BR` select Brazilian Portuguese Personal copy; caller language can
still change during a call. English machine keys and enum values remain stable.

## Work graph and evidence

Two independent discovery nodes pinned backend and iPhone contracts at
`cde08a48d0d67cba387510ba85cfd8ac2921e9d6`; both returned and were reduced in code.
The master then pinned three file-disjoint implementation nodes in separate
worktrees: backend admission, iPhone foundations, and Personal language handling.
Headless `agy` uses `gemini-3.7-flash-high` for these non-mechanical changes.
Builders cannot mutate Git, access providers or grade their own output.

The master integrates all three outputs, executes focused behavioral and
mutation checks, builds/tests the iPhone through the guarded Xcode wrapper, and
obtains independent fresh-context review of the combined diff. Hosted checks
run only on a coherent candidate after exact-source and cost preflight. The
release record will bind actual results and any remaining defects.

## External qualification

The Twilio console required sign-in in both available browsers at the initial
read. After sign-in, the owner noted that two Twilio accounts may exist. The
visible account ended in **45a9**; its two numbers referenced other applications.
A read of the repository's production environment variable
`PRODUCTION_TWILIO_ACCOUNT_SID` identified the expected Kevin account as ending
in **0494**. These accounts do not match. No provider inventory decision may use
the former account. Direct Cloud Run configuration readback could not complete
because Google authentication required renewal; a deployment variable is not
substituted for current runtime evidence. A later authenticated Google Cloud
console read resolved that limitation: production revision `kevin-api-00277-neb`
serves 100% of traffic and both its `TWILIO_ACCOUNT_SID` and
`PRODUCTION_TWILIO_ACCOUNT_SID` end in **0494**, with deploy SHA
`b62eea38d9ab5656fb8d809fb872f05d9c81147d`. Kevin is using the expected account.
The Twilio console's account list exposes only **45a9** under the current login;
access to **0494** is still needed for inventory and eligibility qualification.

The owner was asked to switch to the Kevin account and to identify a Brazilian
carrier/plan/DDD for qualification while implementation continued. No number,
carrier instruction or provider integration is selected by this source contract
alone. Account-specific eligibility, inventory and Kevin webhook verification
remain required before a provider decision.
