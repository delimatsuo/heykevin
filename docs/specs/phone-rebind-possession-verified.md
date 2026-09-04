# Spec — Possession-verified owner-phone rebind

**Status:** design, awaiting owner decision. No code exists for this flow.
**Owner gate:** the feature flag, any production enablement, and the SMS spend it implies are Deli's calls (`docs/current-roadmap.md` §6.B).
**Roadmap item:** §6.A, "Possession-Verified Phone Rebind Flow".
**Security context:** `SECURITY_AUDIT.md` F-04.

Today a Hey Kevin customer cannot change the phone number their account is
bound to. `owner_phone` and `owner_phone_e164` sit in `PROTECTED_FIELDS`
(`app/db/contractors.py`), so the generic `PATCH /api/contractors/{id}` silently
drops them, and no other endpoint writes them after creation. That was the right
emergency fix for F-04 and it must stay. This spec describes the one narrow,
possession-verified path that is allowed to write those two fields, and nothing
else changes.

---

## 1. Problem and threat model

F-04 in `SECURITY_AUDIT.md` is an identity-binding finding: `owner_phone` and
`apple_user_id` determine who gets an account back later, so anything that can
write them can take an account. Two write paths existed. `api_update_contractor`
was closed by adding all three identity fields to `PROTECTED_FIELDS`.
`api_create_contractor` was closed by requiring the existing record's
`apple_user_id` to equal the verified caller's before a phone match returns a
token, and by failing closed (409) on legacy records that have no
`apple_user_id`. Both fixes are in the tree today and are covered by
`tests/unit/test_contractor_protected_fields.py` and
`tests/unit/test_account_dedupe.py`.

A rebind flow reopens a write path to exactly those fields, so it must carry its
own controls. The threats it has to survive:

| # | Threat | Why it applies here |
|---|---|---|
| T-1 | **F-04 hijack** — a caller writes a victim's number onto their own account, or their own number onto a victim's | The flow's entire purpose is to write `owner_phone` / `owner_phone_e164` |
| T-2 | **SIM swap / number recycling** — the attacker controls the *target* number, legitimately or not | Possession of a number is the weakest of the three factors; carriers recycle mobile numbers and Twilio recycles released numbers after an aging period (verify the exact period against Twilio docs before implementation) |
| T-3 | **OTP brute force** — guessing a 6-digit code | 10^6 space; unbounded attempts break it in minutes |
| T-4 | **Existence enumeration** — using error codes to learn whether a phone number has a Hey Kevin account | Distinct 409/404 responses leak the customer list one number at a time |
| T-5 | **Race with account creation** — a rebind and a signup claim the same number concurrently | `get_contractor_by_owner_phone` is a read; nothing today makes claim-then-write atomic |
| T-6 | **Stale forward** — the old phone keeps forwarding to the Kevin number after the rebind | Kevin would keep screening the previous number's callers, and the account's forwarding state would silently describe a phone the user no longer uses |
| T-7 | **Token-only attacker** — someone holds the account's API token (stolen device, leaked Keychain) but neither the phone nor the Apple account | `require_contractor_access` alone would let them rebind the account to a number they control and lock the real owner out of restore |
| T-8 | **SMS pumping / toll fraud** — driving Kevin to send SMS to attacker-chosen destinations | The verification SMS is the only owner-facing message the product sends to a number the account has *not* proven it owns |
| T-9 | **Rebind as a number-release trigger** — the rebind erases forwarding evidence and the sweep then releases a still-forwarded Twilio number | `is_safe_to_release_number` and `is_safe_to_release_lapsed_number` read `forwarding_last_seen_at`, which this flow clears |

T-2 deserves a sentence of framing. Possession of the new number is *never* the
authorization to change the account — the authorization is the authenticated
session plus a fresh Apple identity. Possession only proves the destination is
real and reachable, so we do not bind an account to a number the user mistyped
or does not hold. Read the rest of the spec with that ordering in mind.

---

## 2. Goals and non-goals

**Goals.** Let a customer who still controls their account move it to a new
phone number, without support involvement, without weakening F-04, and without
leaving their old carrier forward pointed at the Kevin number.

**Non-goals.**

- **Changing the Kevin (Twilio) number.** That is `twilio_number`, also
  protected, and it has its own provisioning and release paths
  (`release_twilio_number`, `POST /api/contractors/{id}/provision-number`). A
  rebind never touches it. The customer keeps the same Kevin number, which is
  the point — their callers' address book stays valid.
- **Changing the Apple identity.** `apple_user_id` stays protected and
  unwritable. A customer who loses their Apple account has a different problem
  with a different (unbuilt) answer.
- **Admin or support-driven rebind.** *How support does this today: not at all.*
  There is no admin endpoint that writes `owner_phone` — `app/api/admin.py`
  exposes only reads plus `extend-trial` and `revoke` — and the
  `PROTECTED_FIELDS` filter in `api_update_contractor` is unconditional, so even
  an admin bearer token cannot PATCH the field. The only mechanism that exists
  is a manual Firestore document edit, which is an owner-gated live mutation
  (`docs/current-roadmap.md` §6.B). v1 does not change that; see §9. That stays
  true after this spec ships only because the rebind endpoints deliberately
  decline the admin bypass built into the two helpers they otherwise reuse:
  `require_contractor_access` (`app/middleware/auth.py`) and
  `_enforce_apple_identity` (`app/api/contractors.py`) both begin `if
  getattr(request.state, "is_admin", False): return`. Reusing
  `require_contractor_access` as-is is harmless — it only gates which
  `contractor_id` a caller may address. `_enforce_apple_identity`'s bypass is
  *not* reused (§3 Step 1); if it were, an admin bearer token could rebind any
  account with no Apple proof at all, which is exactly the write path this
  bullet says does not exist.
- **Multi-number accounts / aliases.** One account, one owner phone.
- **Backfilling the 22 production records with no `owner_phone`.** Setting a
  phone for the first time uses the same endpoints and the same checks, but no
  bulk job is in scope.

---

## 3. Flow

Three calls: start, verify, and an explicit cancel.

**Step 0 — authentication.** Both mutating endpoints sit on the existing
`router` in `app/api/contractors.py`, which already carries
`dependencies=[Depends(verify_api_token)]`, and each calls
`require_contractor_access(request, contractor_id)` first, exactly like
`api_update_contractor` and `api_release_number` do. Putting them on this router
matters practically: `app/main.py` is byte-hash-pinned by
`tests/unit/test_voice_bakeoff_turn_composition.py` and
`tests/unit/test_voice_bakeoff_session_driver.py`, so a new module with its own
router would force a reviewed re-pin for no benefit.

`require_contractor_access` itself is unconditional for admin — it begins
`if getattr(request.state, "is_admin", False): return`, so a global bearer
token has access to every `contractor_id`, exactly as it does for
`api_update_contractor`. That is fine at this step: Step 0 only decides *which*
`contractor_id` a caller may address, not *whether* the write proceeds. The
proceed-or-refuse decision belongs entirely to Step 1, which is why Step 1 must
not inherit `_enforce_apple_identity`'s own admin bypass — see below.

**Step 1 — fresh Apple identity (recommended, see §9).** `start` additionally
requires an `apple_identity_token`. **This is not a call to
`_enforce_apple_identity`.** That helper begins with the same short-circuit as
`require_contractor_access` — `if getattr(request.state, "is_admin", False):
return` — which is correct on the unauthenticated bootstrap endpoints it was
written for, where "admin caller, skip Apple verification" is the intended
behaviour. Reused verbatim on the rebind endpoints, that one line would let any
holder of `API_BEARER_TOKEN` call `start` with no `apple_identity_token` at all
and complete a rebind on any account — exactly the confused-deputy shape F-04
describes, only with a support badge on it. So the rebind endpoints run their
own identity check: either a rebind-specific function, or the same
`verify_apple_identity_token` plumbing `_enforce_apple_identity` calls, with the
admin branch removed. Either way, an admin bearer token presenting no valid
Apple identity token for the account's stored `apple_user_id` is refused with
the same generic `401 Apple authentication required` as any other caller —
admin status buys access to the *route* (Step 0), never a waiver on the
*identity proof* (Step 1). Whichever shape is implemented, it still calls
`verify_apple_identity_token` and maps every failure to that same generic 401.
The expected `apple_user_id` is **read from the contractor document, not from
the request body** — the caller does not get to say who they are; they get to
prove they are who the account already says.

The reason to require it: an API token proves *possession of a session*, while
F-04's whole model binds account identity to the verified Apple ID. A rebind is
an identity-binding write, so it should require the identity factor that F-04
trusts, not merely the one that F-04 already found insufficient. This closes
T-7 and, by declining the admin bypass above, also closes the admin-confused-
deputy path that reusing `_enforce_apple_identity` verbatim would have reopened.
An account with no `apple_user_id` on file (legacy records) cannot satisfy this
and is refused — the same fail-closed choice `api_create_contractor` already
makes for legacy records.

Apple identity tokens are short-lived, and `verify_apple_identity_token` already
rejects expired ones with 60s leeway. It does **not** currently enforce a
freshness ceiling on `iat`. The rebind endpoint should require the token to have
been issued recently (recommend 10 minutes) so an old token captured from an
earlier sign-in cannot be replayed. *Verify Apple's documented identity-token
lifetime and `iat` semantics against Apple docs before implementation* rather
than assuming the window.

**Step 2 — normalize.** Resolve the account country the way the rest of the code
does: the contractor's stored `country_code` when it is in `SUPPORTED_COUNTRIES`,
else `detect_country_from_phone`, else `US` — i.e. `_resolve_country_code`'s
precedence. Then `normalize_phone(new_phone, default_region=None)` first and
`normalize_phone(new_phone, default_region=country)` as fallback, the identical
two-step `create_contractor` and `api_create_contractor` use. No canonical form
means `400 Invalid owner phone number`, reusing the create endpoint's exact
message. Reject a target whose region is outside `SUPPORTED_COUNTRIES`: it
bounds the destinations Kevin will text (T-8) and matches the countries the
product actually supports. *Twilio geo-permissions must also allow the
destination; verify against Twilio docs before implementation* — an SMS to a
permitted-by-us but blocked-by-Twilio country would fail silently, since
`send_sms` returns `False` rather than raising.

If the canonical target equals the account's current `owner_phone_e164`, return
`200` with `{"status": "no changes"}` and send nothing. It is not an error, and
it must not consume SMS budget — but it still burns one `phone_rebind_start_limit`
slot (§6), the same as any other `start` call, before the no-op check even runs.
Exempting the no-op from the start limit would let a caller spam `start` with
the account's own current number to learn how many real attempts remain without
spending any of them — a free rate-limit-state oracle. It does not touch
`phone_rebind_target_limit`, since no SMS is sent to any destination.

**Step 3 — collision check.** Call `get_contractor_by_owner_phone(canonical,
country_code=country)`, the same lookup `api_create_contractor` uses, so a
number that would collide at signup collides here too. Three outcomes:

- `PhoneDedupeAmbiguityError` → `409` with the rebind's own single collision
  message (§5).
- A different active contractor owns it → `409` with that same single
  collision message.
- `None`, or a match whose `contractor_id` is this account → proceed.

On the message and enumeration (T-4): `api_create_contractor` actually returns
three distinct 409 bodies for what a caller experiences as one outcome —
"An account already exists for this phone number. Please contact support to
recover your account." for the ambiguity case and for a legacy match with no
`apple_user_id`, but "An account already exists for this phone number under a
different Apple ID." for an active match whose `apple_user_id` differs from the
caller's, which is the realistic collision shape. That three-body split is
itself a small existence oracle: the response text tells a caller whether a
collision is a legacy record or an Apple-ID mismatch, not merely that one
exists. The rebind endpoint does not replicate that split. It returns exactly
one message for every collision shape it can produce — ambiguity error or an
active-contractor match — so a rebind caller learns nothing about *why* a
number collided, only *that* it did. This is a deliberate unification, not a
byte-for-byte match with `api_create_contractor`'s text (see §5): closing the
oracle on this path is worthwhile on its own even though
`api_create_contractor`'s three-body split is unchanged; if Deli later wants
that split closed too, it is a separate, smaller fix to the create endpoint,
not a reason to hold this one back.

Note the lookup queries only `active == True` documents, so a deactivated
account that once held the number does not block the rebind. That is the
behaviour we want (a churned customer should not squat a recycled number
forever) and it should be stated in the tests so nobody "fixes" it later.

**Step 4 — challenge.** Generate a 6-digit code with `secrets` (never `random`),
store only its salted hash (§4), and send it with `send_sms(target, body,
from_number=contractor["twilio_number"])` — from the account's own Kevin number,
which is what every other owner-facing send does
(`app/webhooks/twilio_incoming.py`, `app/services/post_call.py`,
`app/services/number_release.py`). Fall back to `settings.twilio_phone_number`
only if the account has no Kevin number yet; `_message_create_kwargs` already
handles an empty `from_number`.

No `ActionKey` is passed. Owner-facing SMS is ungated throughout the codebase,
and every action in `GATE_POLICIES` that requires `sms_compliance_status` is
caller-facing. A one-time code the account holder just requested about their own
account is the most transactional message the product sends. A2P registration
was approved 2026-08-19.

**The code goes to the NEW number, not the old one.** The old number may be
lost, stolen, disconnected, or already reassigned — those are the actual reasons
people rebind, and requiring the old number would make the flow useless in every
case it exists for. The old number's role is notification, not authorization
(step 7).

`start` returns `202` and never reveals whether the SMS was actually delivered.

**Step 5 — verify.** The client posts the code. Compare with
`hmac.compare_digest` against the stored hash (constant-time; T-3). Reject if the
challenge is missing, expired (10 minutes), or has reached 5 attempts — the
challenge document is deleted on the 5th failure rather than left to be
retried, so an attacker must re-`start` (and pay a rate-limit slot) to get five
more guesses at a *new* code. Every failure increments `attempts` even when the
comparison itself is cheap.

**Step 6 — atomic write.** On a correct code, a single Firestore transaction on
`contractors/{contractor_id}`:

1. Reads the account document. Aborts if it is missing or `active` is not true.
2. Compare-and-set: aborts unless `owner_phone_e164` still equals the value
   recorded on the challenge at `start`. This is what makes two concurrent
   rebinds, or a rebind racing a support edit, resolve to exactly one winner.
3. **Re-runs the collision check** for the canonical target. The write must not
   depend on a check made minutes earlier (T-5).
4. Writes `owner_phone`, `owner_phone_e164` (both the canonical E.164 string —
   `create_contractor` writes them identically, and the dual field exists for
   legacy-record compatibility), `owner_phone_rebound_at`, and clears
   `forwarding_last_seen_at`.
5. Appends an audit entry (§4).
6. Deletes the challenge document.

An honest caveat for the implementer: step 3's re-check is a *query*, and
whether the Firestore Python client can run it inside the transaction — and
whether the multi-query legacy-candidate scan inside
`get_contractor_by_owner_phone` is viable there at all — *must be verified
against the google-cloud-firestore docs before implementation*. If a
transactional query on the canonical `owner_phone_e164` equality is available,
use it for step 3 and accept that the bounded legacy-format scan runs only in
step 2's pre-check. If it is not, the transaction still gives a real CAS on this
account, the pre-check runs immediately before the transaction, and the residual
race with a *concurrent signup* is detected rather than prevented: see §6 for the
duplicate-detection metric. Do not paper over this — write down which of the two
shapes was implemented.

`update_contractor` is a plain `.update()` and cannot express any of this, so the
rebind write is its own function in `app/db/contractors.py` and does not route
through the generic updater.

**Step 7 — hand-off.** After the transaction commits, fire two best-effort side
effects (neither may fail the request; the rebind is already durable):

- SMS the **old** number, from the same Kevin number: *"Your Kevin account phone
  number was changed. If this wasn't you, contact support."* This is the
  out-of-band alarm for T-1 and T-7 — the only signal a real owner gets if
  someone with their token and Apple session moved their account. Skip it if the
  old value was empty. If the old number's carrier has already recycled it, this
  message reaches a stranger, which is a reason to keep it terse and to include
  no account identifiers, no business name, and no Kevin number.
- Nothing else. In particular, do **not** attempt to change the carrier
  forwarding — the product has no ability to do so.

**Step 8 — forwarding hand-off in the response.** `verify` returns `200` with
the new canonical number and *two* code sets from `FORWARDING_CODES`: the
**disable** codes for the old number's country, so the app can walk the user
through turning the forward off on the phone they are leaving, and the
**enable** codes for the new number's country. Serve them through the same
resolution `get_forwarding_instructions` uses, including `FALLBACK_MESSAGE`, and
carry the `recommended` key so iOS keeps preferring `forward_unanswered`. US/CA
rows carry only the legacy `disable`; the granular NANP cancel codes remain
undecided (`docs/current-roadmap.md` §6.A), so this flow must not invent them —
it serves what the table holds and says nothing more. The Verizon distinction is
already the client's `forwarding_carrier_family` behaviour and is unchanged.

Until the user actually disables the old forward, calls from the old number keep
arriving. That is a carrier-side fact we cannot observe or fix; §7 covers what
it means for the release sweeps.

---

## 4. Data model

**New collection `phone_rebind_challenges`, document id = `contractor_id`.**
One live challenge per account by construction; a second `start` overwrites the
first, which invalidates the earlier code.

| Field | Type | Notes |
|---|---|---|
| `target_e164` | string | Canonical E.164 target |
| `code_hash` | string | SHA-256 of `salt + code`. The code itself is never stored |
| `salt` | string | Per-challenge random, from `secrets` |
| `expires_at` | float | Unix seconds; `created_at` + 600 |
| `attempts` | int | Incremented on every verify; challenge deleted at 5 |
| `created_at` | float | Unix seconds |
| `sent_from` | string | The Kevin number the SMS was sent from, for support triage |
| `bound_owner_phone_e164` | string | The account's value at `start`; the CAS precondition in step 6 |

A Firestore TTL policy on `expires_at` is the cheapest sweep (the same shape
`check_and_increment` already supports via `document_ttl_seconds`); TTL policy
creation is an owner-gated infrastructure change, so the code must also treat an
expired-but-present document as absent and delete it on sight.

**Contractor document.** One new field, `owner_phone_rebound_at` (float, unix
seconds), and it **joins `PROTECTED_FIELDS`** — it is server-owned evidence
about an identity change, and a client that could forge or clear it could hide a
hijack and defeat the release hold in §7. `owner_phone`, `owner_phone_e164` and
`apple_user_id` stay in `PROTECTED_FIELDS` unchanged. `ContractorUpdate` keeps
`owner_phone` declared — it is stripped by the filter, and the existing test
`test_patch_contractor_strips_owner_phone_and_identity_fields` proves it.

**Audit.** Append to a bounded list `owner_phone_rebind_history` on the
contractor document (cap 10, oldest dropped): `{at, from_hash, to_hash, source}`
where the hashes are `phone_hash(e164)`, not the numbers. Hashing keeps the
history useful for "did this account rebind away from the number in the support
ticket?" without storing a second copy of the customer's phone numbers. This
field is server-written and joins `PROTECTED_FIELDS` too.

---

## 5. API

All three live under the authenticated contractor router.

| Method | Path | Body | Success |
|---|---|---|---|
| `POST` | `/api/contractors/{contractor_id}/phone-rebind/start` | `new_phone` (≤20), `apple_identity_token` (≤8192) | `202` `{"status":"pending","expires_in":600,"target_last4":"0123"}` |
| `POST` | `/api/contractors/{contractor_id}/phone-rebind/verify` | `code` (6 chars) | `200` `{"status":"ok","owner_phone_e164":"+14165550123","country_code":"CA","forwarding":{"disable_old":{…},"enable_new":{…},"fallback_message":"…"}}` |
| `DELETE` | `/api/contractors/{contractor_id}/phone-rebind` | — | `204` (idempotent: also `204` when no challenge exists) |

`start` echoes only the last four digits of the target so the UI can render
"code sent to ···0123" without the response becoming a phone-number oracle.

### Errors

| Status | Detail (exact) | When | Threat |
|---|---|---|---|
| 400 | `Invalid owner phone number` | Target does not normalize to E.164 | — |
| 400 | `This country is not supported` | Target region outside `SUPPORTED_COUNTRIES` | T-8 |
| 400 | `Invalid code` | Code is not 6 digits (shape check, before any lookup) | T-3 |
| 401 | `Apple authentication required` | Missing/invalid/stale `apple_identity_token`, or the account has no `apple_user_id` | T-7 |
| 403 | `Access denied` | `require_contractor_access` mismatch | T-1 |
| 404 | `Contractor not found` | No such account, or inactive | — |
| 404 | `No verification in progress` | `verify` with no challenge, or an expired one | T-3 |
| 409 | `An account already exists for this phone number. Please contact support to recover your account.` | Target owned by another active account, **or** `PhoneDedupeAmbiguityError` | T-1, T-4 |
| 409 | `Your phone number changed. Please start again.` | CAS precondition failed in the transaction | T-5 |
| 422 | *(FastAPI validation)* | Field length/type violations | — |
| 429 | `Too many attempts. Please try again later.` | Any rate limit; body carries `retry_after_seconds` | T-3, T-8 |
| 503 | `Could not send the verification code. Please try again.` | `send_sms` returned `False` | — |

The `An account already exists...` 409 above deliberately covers both of its
causes — an active contractor already owning the target, or
`PhoneDedupeAmbiguityError` — with one message, so a rebind caller cannot tell
those two shapes apart from response text (T-4). It is **not** the same string
as any of `api_create_contractor`'s three 409 bodies (two of which read
"...Please contact support to recover your account.", the third "...under a
different Apple ID."; see §3 Step 3): the rebind intentionally unifies what the
create endpoint splits into two, rather than reusing either of the create
endpoint's strings. A wrong code returns `404 No verification in progress`
**only** after the challenge is destroyed on the fifth failure; a wrong code with
attempts remaining returns `400 Invalid code` with the remaining count omitted.

**Invariant to state in the code and prove in tests:** these three endpoints are
the only writers of `owner_phone` and `owner_phone_e164` after
`create_contractor`. Nothing in this spec removes a field from
`PROTECTED_FIELDS` or widens what `api_update_contractor` accepts.

---

## 6. Abuse controls and privacy

**Rate limits**, all through `check_and_increment` in `app/db/rate_limits.py`,
which is Firestore-backed (so it survives Cloud Run autoscaling) and *fails
closed* on Firestore errors. Two new settings on `Settings`, shaped exactly like
`pin_rate_limit` / `pin_rate_window_seconds`:

| Setting | Default | Scope key | Purpose |
|---|---|---|---|
| `phone_rebind_start_limit` / `phone_rebind_start_window_seconds` | `3` / `3600` | `contractor_id` | Caps SMS spend and collision probing per account (T-4, T-8) |
| `phone_rebind_target_limit` / `phone_rebind_target_window_seconds` | `3` / `86400` | `phone_hash(target_e164)` | Caps messages to any one destination across all accounts (T-8) |
| `phone_rebind_verify_limit` / `phone_rebind_verify_window_seconds` | `10` / `3600` | `contractor_id` | Backstop above the per-challenge 5-attempt cap (T-3) |

Use `phone_hash` for the target key so no raw phone number becomes a Firestore
document id. Each `start` burns one slot from both start limits *before* the SMS
goes out. Cancelling a challenge does not refund a slot. The no-op path (§3
Step 2, target equals the account's current number) is not exempt from
`phone_rebind_start_limit` either: it still burns a per-account start slot even
though it sends no SMS and never touches `phone_rebind_target_limit`. Without
that, the no-op would be a free way to probe how much of the account's start
budget is left.

**Lockout behaviour.** Per-challenge: 5 wrong codes destroys the challenge.
Per-account: exceeding `phone_rebind_start_limit` returns `429` with
`retry_after_seconds` for the rest of the rolling window; there is no permanent
lock and no support-unlock path, because a permanent lock is a denial-of-service
weapon against a customer whose only recovery channel is the number they are
trying to change.

**Privacy and logging.** No log line contains a code, a salt, or a full phone
number. Phones go through `redact_phone` (last four only); contractor ids
through the same 8-char truncation `record_gate_decision` uses. Log the *events*
— `phone_rebind_start`, `phone_rebind_verify_failed`, `phone_rebind_committed`,
`phone_rebind_collision`, `phone_rebind_rate_limited` — with an outcome and a
reason, never a payload. `httpx` and `twilio` loggers are already pinned to
WARNING in `setup_logging`, which is what keeps the Twilio request line (and its
`To` parameter) out of the logs; that pinning is load-bearing for this feature.

**Metrics to add** (counters, derived from those log events): starts, sends
failed, verifies succeeded, verifies failed, collisions, rate-limit hits, CAS
aborts, and — most importantly — a **duplicate-owner-phone detector**: a periodic
count of active contractor documents sharing an `owner_phone_e164`. That is the
observable that catches a lost T-5 race, and it should be zero forever.

**Admin overview** (`admin_overview` in `app/api/admin.py`) should gain
`rebinds_last_7d` and `duplicate_owner_phone_count`. Neither exposes a phone
number. The per-contractor admin read can show `owner_phone_rebound_at` and the
hashed history, which is enough for support to answer "when did this account
move?" without support gaining the ability to move it.

These counters are not only the T-5 race detector described above. They are
also the detection backstop for §3 Step 1's decline of `_enforce_apple_identity`'s
`is_admin` bypass: if that control were ever weakened or regressed — a future
edit that reused the helper verbatim, say — there is today no legitimate path
that produces an admin-triggered rebind, so any rebind volume or pattern that
does not trace back to a customer's own session is the signal that catches it,
not just a means to spot a lost concurrency race.

---

## 7. Interactions with existing systems

**Number-release guards (T-9).** `is_safe_to_release_number` and
`is_safe_to_release_lapsed_number` both refuse to release a Twilio number while
`forwarding_last_seen_at` is inside the quiet window, and both treat *absence*
of that stamp as "no evidence either way" rather than as a block. Clearing the
stamp on rebind therefore **removes a hold**, which is the opposite of what a
mid-migration account needs. Two consequences the implementation must honour:

1. `last_inbound_call_at` is **never** cleared by a rebind. It is stamped by
   `_record_inbound_call_evidence` on every inbound call regardless of route, so
   it — not `forwarding_last_seen_at` — is what keeps a still-forwarded number
   held while the old forward is live. It describes traffic to the Kevin number
   and has nothing to do with which owner phone is bound.
2. Both guards gain a **rebind hold**: a number whose account has an
   `owner_phone_rebound_at` newer than the guard's own window is not safe to
   release. An account that just moved phones is definitionally mid-migration.
   This is a small, testable addition to `app/services/subscription.py` and it is
   part of this feature, not a follow-up.

Clearing `forwarding_last_seen_at` is still correct for its primary purpose: it
is carrier-derived proof that *the bound owner phone* forwards to Kevin, and
after a rebind it proves nothing about the new phone. Leaving it would make the
app tell the user forwarding is set up when it is not. Write it as an explicit
`None` rather than deleting the key — the guards' `"key" in contractor` checks
treat both identically, and an explicit null reads better in the console.

**Forwarding evidence re-accrual.** `_record_forwarding_evidence` only stamps
when `forwarding_confirms_owner(ForwardedFrom, owner_phone)` matched, so after a
rebind the stamp naturally re-accrues from the *new* number's forward — and only
from it. Nothing extra is needed; the throttle is hourly.

**Client-asserted forwarding intent.** `forwarding_self_reported_at`,
`forwarding_skipped_at` and `forwarding_carrier_family` are deliberately *not*
protected. A rebind should clear the first two through the ordinary PATCH path
from the client (they are client-side assertions by definition), and the spec
does not move them into the server-owned transaction.

**A2P / SMS compliance.** Transactional one-time codes to the account holder are
the permitted shape, and A2P registration was approved 2026-08-19. The rebind
sends are owner-facing and ungated, consistent with every other owner send. *If
Twilio's messaging policy for OTP content or the Messaging Service configuration
matters to delivery, verify against Twilio docs before implementation.*

**International.** Targets are E.164 only; the region is resolved from the
account country (`country_code`, then `detect_country_from_phone`, then `US`) and
must land in `SUPPORTED_COUNTRIES`. A rebind does **not** change `country_code` —
that is the Settings country control (Task 7,
`ios/Kevin/Services/SettingsCountry.swift`). If the new number's region differs
from the account country, `verify` should say so in its response so the app can
prompt the user to update the country, since the forwarding codes key on it.

**Subscription and Twilio.** Untouched. Rebinding does not alter
`subscription_*`, does not re-provision or release a number, and does not
invalidate the API token (`invalidate_contractor_tokens` is for deactivation).

---

## 8. Rollout

**Flag.** `phone_rebind_enabled: bool = False` on `Settings`
(`PHONE_REBIND_ENABLED`), default off, same shape as `purge_enabled` and
`lapsed_number_release_enabled`. With the flag off, all three endpoints return
`404` — not `403` — so a disabled feature is indistinguishable from an
unimplemented one. Enabling it in production is an owner action: an env change
plus a deploy.

**iOS.** A "Change phone number" row in Settings' Account & Plan section
(`ios/Kevin/Views/SettingsView.swift`), rendered **only** when the backend
reports the flag on — no client-side flag, no row that 404s. There is no
phone-edit UI today (confirmed: `SettingsView.swift` has no owner-phone field;
`owner_phone` appears in the iOS tree only in `OnboardingView.swift` and
`APIClient.createContractor`). The screen ends on the forwarding hand-off from
§3 step 8, reusing `ForwardingInstructions.swift`.

**Owner gates.** Flag enablement, the SMS spend the flow implies, any production
deploy, and any decision to let support force a rebind are Deli's. No agent
enables this in production.

**Test plan.** Unit tests per endpoint and guard, plus one integration test
driving start → verify → commit against a fake Twilio client (the pattern in
`tests/unit/test_phase0_sms_gates.py` and
`tests/unit/test_account_deletion.py`). Bind the fakes to the real
`client.messages.create` signature — a permissive fake certifies bugs.

Negative tests, one per threat in §1:

| Threat | Negative test |
|---|---|
| T-1 | PATCH with `owner_phone` still drops it (extend `test_contractor_protected_fields.py`); a rebind targeting a number owned by another active account returns the generic 409 and writes nothing; a `start` for a `contractor_id` the token does not own returns 403 |
| T-2 | A `verify` succeeds only for the code sent to the target; a code delivered to the *old* number is never accepted (no such code is ever generated); see T-7 below — those tests are what actually prove possession of the target number alone, with no valid session or Apple proof, is insufficient to write anything |
| T-3 | Five wrong codes destroy the challenge and the sixth returns 404; comparison uses `hmac.compare_digest`; a 7-digit or alphabetic code is rejected on shape before any Firestore read |
| T-4 | The collision 409 (an active contractor owns the target) and the ambiguity 409 (`PhoneDedupeAmbiguityError`) are byte-identical to *each other*; the rebind returns this same single message for every collision shape, and it is **not** asserted equal to any of `api_create_contractor`'s three 409 strings — the rebind intentionally has its own message (§3 Step 3); `start` never returns the target's full number |
| T-5 | With the account's `owner_phone_e164` mutated between start and verify, the transaction aborts with the CAS 409 and no field is written; a collision introduced between start and verify is caught by the in-transaction re-check |
| T-6 | A successful rebind clears `forwarding_last_seen_at`, leaves `last_inbound_call_at` untouched, and returns both the old country's disable codes and the new country's enable codes |
| T-7 | `start` without `apple_identity_token`, with a token whose `sub` differs from the stored `apple_user_id`, with a stale `iat`, and on an account with no `apple_user_id`, all return the generic 401; a request authenticated with the global admin bearer token and no `apple_identity_token` also returns the generic 401 and writes nothing — this is the test that proves the rebind endpoints do not inherit `_enforce_apple_identity`'s `is_admin` bypass |
| T-8 | The per-target limit blocks a fourth start for the same number across different accounts; an unsupported-region target is rejected before any send; a duplicate `start` overwrites the challenge without a second free SMS beyond the limit |
| T-9 | `is_safe_to_release_number` and `is_safe_to_release_lapsed_number` both return `False` for an account with a recent `owner_phone_rebound_at`, including when `forwarding_last_seen_at` is absent |

Plus: flag-off returns 404 on all three endpoints and writes nothing; a
`send_sms` that returns `False` leaves no challenge document behind; a rebind to
the account's current number is a no-op that sends no SMS.

---

## 9. Open decisions for Deli

1. **Require a fresh Apple identity token at `start`? — Recommend yes.** It is
   the factor F-04's model actually trusts, it is the only control that stops a
   stolen-token attacker (T-7), and iOS can re-present it with a Sign in with
   Apple re-authorization. The cost is that accounts with no `apple_user_id`
   cannot rebind — which is the same fail-closed line `api_create_contractor`
   already draws for legacy records, and those users can still sign in with
   Apple to bind one first.
2. **Can support force a rebind? — Recommend no in v1.** Support cannot do it
   today (no admin write path exists), so saying no changes nothing and keeps
   the possession requirement absolute: an admin-forced rebind is exactly the
   confused-deputy F-04 describes, only with a nicer UI. This "no" is enforced
   in code, not merely stated: §3 Step 1 declines `_enforce_apple_identity`'s
   `is_admin` bypass, so shipping this feature does not quietly reopen the
   admin path this decision forecloses. Revisit if real support volume proves
   the self-serve flow leaves people stranded; the honest v1 answer for a
   stranded customer is an owner-performed Firestore edit under the existing
   live-mutation gate.
3. **Keep the old number as an alias for 7 days? — Recommend no.** An alias
   means two active numbers resolve to one account, which reintroduces exactly
   the dedupe ambiguity `PhoneDedupeAmbiguityError` exists to catch, and it
   points the ambiguity at a number the user may no longer control. The real
   problem an alias would solve — the old carrier forward still being live — is
   solved by the disable codes in the response and by the release hold in §7.
4. **Add a `owner_phone_claims/{phone_hash}` uniqueness collection? —
   Recommend no in v1.** A claim document is the only construction that makes
   phone uniqueness genuinely atomic across signup and rebind, but it needs a
   collision-aware backfill of every existing account and a change to
   `create_contractor`, which is a much larger blast radius than this feature.
   v1 uses CAS plus the duplicate-detection metric in §6, and the claim
   collection becomes the right answer if that metric ever fires.

---

## Symbols referenced

Every symbol below was grepped in this worktree and exists.

`app/db/contractors.py`: `PROTECTED_FIELDS`, `get_contractor_by_owner_phone`,
`PhoneDedupeAmbiguityError`, `DOC_QUERY_CAP`, `COLLECTION`, `create_contractor`,
`update_contractor`, `get_contractor`, `get_contractor_by_apple_user_id`,
`release_twilio_number`, `deactivate_contractor`, `SUPPORTED_COUNTRIES`,
`detect_country_from_phone`.

`app/api/contractors.py`: `router`, `api_create_contractor`,
`api_update_contractor`, `api_get_contractor`, `api_release_number`,
`ContractorCreate`, `ContractorUpdate`, `_enforce_apple_identity`,
`_resolve_country_code`, `_require_admin`.

`app/middleware/auth.py`: `verify_api_token`, `require_contractor_access`,
`generate_contractor_token`, `invalidate_contractor_tokens`.

`app/services/apple_auth.py`: `verify_apple_identity_token`, `AppleAuthError`,
`AppleIdentity`, `DEFAULT_AUDIENCE`.

`app/utils/phone.py`: `normalize_phone`, `phone_hash`,
`forwarding_confirms_owner`.

`app/services/sms.py`: `send_sms`, `_message_create_kwargs`.

`app/api/forwarding.py`: `FORWARDING_CODES`, `FALLBACK_MESSAGE`,
`get_forwarding_instructions`, `check_dial_in_pin_attempt`, `DIAL_IN_PIN_SCOPE`.

`app/db/rate_limits.py`: `check_and_increment`, `RateLimitResult`,
`document_ttl_seconds`.

`app/config.py`: `Settings`, `pin_rate_limit`, `pin_rate_window_seconds`,
`purge_enabled`, `lapsed_number_release_enabled`, `twilio_phone_number`.

`app/webhooks/twilio_incoming.py`: `_record_forwarding_evidence`,
`_record_inbound_call_evidence`.

`app/services/subscription.py`: `is_safe_to_release_number`,
`is_safe_to_release_lapsed_number`, `_inbound_stamp_blocks`,
`NUMBER_RELEASE_QUIET_DAYS`, `LAPSED_NUMBER_RELEASE_DAYS`.

`app/services/number_release.py` (module). `app/api/admin.py`: `admin_overview`.

`app/services/gated_actions.py`: `ActionKey`, `GATE_POLICIES`.
`app/services/side_effect_audit.py`: `record_gate_decision`.

`app/utils/logging.py`: `redact_phone`, `get_logger`, `setup_logging`.

Contractor fields: `owner_phone`, `owner_phone_e164`, `apple_user_id`,
`country_code`, `twilio_number`, `active`, `forwarding_last_seen_at`,
`last_inbound_call_at`, `forwarding_self_reported_at`, `forwarding_skipped_at`,
`forwarding_carrier_family`, `sms_compliance_status`, `subscription_status`.
New: `owner_phone_rebound_at`, `owner_phone_rebind_history`.

Tests: `tests/unit/test_contractor_protected_fields.py`
(`test_identity_binding_fields_in_protected_fields`,
`test_patch_contractor_strips_owner_phone_and_identity_fields`),
`tests/unit/test_account_dedupe.py`, `tests/unit/test_forwarding_evidence.py`,
`tests/unit/test_number_release_safety.py`,
`tests/unit/test_lapsed_number_release.py`,
`tests/unit/test_forwarding_instructions.py`, `tests/unit/test_phone.py`,
`tests/unit/test_phase0_sms_gates.py`, `tests/unit/test_account_deletion.py`,
`tests/unit/test_voice_bakeoff_turn_composition.py`,
`tests/unit/test_voice_bakeoff_session_driver.py`.

iOS: `ios/Kevin/Views/SettingsView.swift`,
`ios/Kevin/Views/OnboardingView.swift`,
`ios/Kevin/Services/ForwardingInstructions.swift`,
`ios/Kevin/Services/SettingsCountry.swift`,
`ios/Kevin/Services/APIClient.swift` (`createContractor`).
