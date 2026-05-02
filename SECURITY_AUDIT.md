# Hey Kevin — Pre-Launch Security Audit

**Date**: 2026-04-30
**Scope**: iOS app (`ios/Kevin/`) + Python FastAPI backend (`app/`) + infrastructure (`.github/workflows/`, `Dockerfile`, `firestore.rules`, `database.rules.json`)
**Status**: Read-only review. No code modified.
**Auditor role**: Senior application security analyst, pre–App Store launch.

---

## 1. Executive Summary

Hey Kevin's overall security posture is **mid-stage**: the team has clearly thought about defense-in-depth (Twilio signature verification, Apple JWS chain validation, `PROTECTED_FIELDS`, server-as-source-of-truth for subscriptions, Firestore/RTDB rules locked to admin SDK, SSRF allowlist, restrictive CORS, runtime-environment guard). However, several **critical gaps in the authentication boundary** make the backend currently exploitable from an unauthenticated attacker, and one **secret-exposure issue** affects the local development workflow.

**Top 3 risks blocking public launch**:

1. **Apple Sign-In identity tokens are never cryptographically verified server-side.** The iOS client sends an `apple_identity_token` to `/api/contractors/lookup-by-apple-id` and `POST /api/contractors`, but no backend code parses, verifies, or even reads it. Account take-over and account-creation impersonation are trivial: an attacker with a victim's Apple user ID string (a stable identifier exchanged with multiple apps) can call `lookup-by-apple-id` and receive a fresh API token that grants full read/write access to that victim's data, Twilio number, and call history. See **F-01**.
2. **Live secrets are present in plaintext in `.env` on the developer's machine, and `.env` lives at the repo root** even though it is gitignored. Any of these keys (Anthropic, Deepgram, ElevenLabs, Gemini, Twilio Account+Auth, ElevenLabs, Telegram bot, the production-shape `API_BEARER_TOKEN`, and an APNs `.p8` private key inline) leaking via accidental commit, backup, screen share, or a `tar` would cause full backend compromise. See **F-02**.
3. **The promotional-offer signing key (`APPSTORE_PRIVATE_KEY`) is exposed on a backend route (`POST /api/subscription/sign-offer`) reachable by any per-contractor token, with only a very loose 3 req/min in-memory rate limit and no input-binding to the contractor's own `subscription_uuid`.** A compromised contractor token can generate unlimited valid signed promotional offer payloads for any `application_username`. The 1,000-slot atomic counter is the only thing protecting the founding-member promo budget. See **F-12**.

The remaining findings are mostly Medium/Low. Twilio webhook signing, Apple JWS, Telegram secret token, Firestore/RTDB rules, CORS, ATS, the WebSocket `ws_token` for media streams, the Jobber/Google OAuth state mechanism, and the Dockerfile non-root user are all in good shape.

---

## 2. Findings Table

| ID | Severity | Title | Area | File:line |
|----|----------|-------|------|-----------|
| F-01 | **Critical** | Apple Sign-In identity token never verified — account takeover via Apple user ID | Auth | `app/api/contractors.py:149-167`, `app/middleware/auth.py:42-53` |
| F-02 | **Critical** | Live API keys, Twilio auth token, Telegram bot token, Anthropic key, and APNs `.p8` in plaintext `.env` at repo root | Secrets | `.env:1-42`, `.gitignore:1` |
| F-03 | **High** | `POST /api/contractors` allows unauthenticated creation of arbitrary contractor accounts; `/api/contractors/lookup-by-apple-id` returns long-lived API tokens with no proof-of-possession | Auth | `app/api/contractors.py:170-216`, `app/middleware/auth.py:42-53` |
| F-04 | **High** | `owner_phone` and `apple_user_id` are not in `PROTECTED_FIELDS`; an authenticated contractor can rebind their account to a different Apple user ID or phone, hijacking restore flows | Auth | `app/db/contractors.py:16-26`, `app/api/contractors.py:80-115` |
| F-05 | **High** | `POST /api/subscription/verify` fails open when Apple's API errors — claims subscription verification "ok" even when no transaction was confirmed | Subscription | `app/api/subscription.py:67-71` |
| F-06 | **High** | No Apple-provided `appAccountToken → contractor` lookup at verify time: `verify_subscription` trusts the client-supplied `contractor_id` and only checks the `subscription_uuid` matches. A second contractor cannot steal a transaction, but a malicious client can pass any `transaction_id` they observe on their device — see Impact for the racy edge | Subscription | `app/services/subscription.py:235-270`, `app/api/subscription.py:37-78` |
| F-07 | **High** | `Twilio API Key Secret` and `Twilio Auth Token` shipped to clients indirectly via `_generate_access_token()` use a static `identity="kevin-contractor"` / `"kevin-user"` — VoIP token does not bind to the contractor, allowing any authenticated contractor to dial out as any other contractor's TwiML App | Telephony | `app/api/voip.py:170-205`, `app/api/voip.py:243-261` |
| F-08 | **Medium** | `vCard` HMAC secret derived from `API_BEARER_TOKEN`; if the admin token rotates, all previously signed vCard URLs silently break, and bearer-token compromise allows offline forgery of any vCard URL | Crypto | `app/services/vcard.py:13-55` |
| F-09 | **Medium** | `_validate_external_url()` is TOCTOU-vulnerable: hostname is resolved twice (once for validation, once by `httpx`), allowing DNS rebinding to internal IPs after the check passes | SSRF | `app/api/contractors.py:426-457` |
| F-10 | **Medium** | Estimate upload endpoint accepts unbounded request body before length check, since `await request.body()` reads the entire payload into memory | DoS | `app/api/estimates.py:138-159` |
| F-11 | **Medium** | RTDB write of full call transcripts with no encryption at rest beyond Firebase defaults; transcripts can include PHI/legal/financial discussion in a residential-services product | Data protection | `app/webhooks/media_stream.py:267-287`, `database.rules.json` |
| F-12 | **Medium** | `POST /api/subscription/sign-offer` rate limit is in-memory per Cloud Run instance; with multiple instances or after deploy/restart, an attacker effectively gets `N × 3` requests/min and can exhaust the 1,000-slot promo or harvest valid signatures for offline replay | Subscription | `app/api/subscription.py:14-34, 94-118` |
| F-13 | **Medium** | `_handle_voicemail` in `app/api/voip.py` calls Twilio `client.calls(...).update()` synchronously on the asyncio event loop — not a security bug per se, but the same call_sid is unconditionally trusted; combined with stale-RTDB access checks (`get_active_call` may return None), unauthorized clients with a leaked call_sid can no-op route to voicemail without contractor binding | Telephony | `app/api/voip.py:325-339`, `app/api/voip.py:208-241` |
| F-14 | **Medium** | `_post_call_extract` in `media_stream.py` writes to a global `caller_contacts` collection keyed by phone number, with no per-contractor scoping — caller-name extraction from one contractor's call can leak into another contractor's call display | Multi-tenancy | `app/webhooks/media_stream.py:31-132`, `app/webhooks/twilio_incoming.py:347-365` |
| F-15 | **Medium** | `dial-in` PIN brute force: only 6-digit PIN, 3 attempts per 10-minute window per caller phone (which is forgeable via spoofed caller ID); 1,000,000 keyspace divided across all contractors | Auth | `app/webhooks/twilio_incoming.py:709-723`, `app/db/contractors.py:217` |
| F-16 | **Medium** | Information disclosure: `appAccountToken` from a transaction is logged at ERROR level when it doesn't match expected `subscription_uuid` — not a token but a user-identifying UUID | Logging | `app/services/subscription.py:257` |
| F-17 | **Low** | App Store Connect API JWT validity is 20 minutes; cached token logic (`_cached_apns_token`) caches APNs JWT for 50 minutes — both within Apple's 60-minute max but no rotation safety net | Crypto | `app/services/push_notification.py:36-79`, `app/services/subscription.py:70-91` |
| F-18 | **Low** | `pull_request:` trigger on `deploy.yml` runs the test job from forks. Currently safe because no GCP secrets are exposed to the test job, but add fork policy for defense-in-depth | CI/CD | `.github/workflows/deploy.yml:7-9` |
| F-19 | **Low** | GitHub Actions are pinned to major-version tags (`@v3`, `@v6`) instead of commit SHAs | CI/CD | `.github/workflows/deploy.yml`, `.github/workflows/rollback.yml` |
| F-20 | **Low** | Dockerfile installs deps as root before switching to `appuser`; resulting image has world-readable `app/` source. Acceptable, but `--chown=appuser` is best practice | Deployment | `Dockerfile:1-17` |
| F-21 | **Low** | `app/main.py` global exception handler returns generic `{"detail":"Internal server error"}` (good), but `redact_phone(settings.twilio_phone_number)` is logged at startup including the partial number | Logging | `app/main.py:268-274` |
| F-22 | **Low** | Twilio Voice iOS SDK pinned to `from: "6.11.0"` (semver range), allowing automatic minor/patch upgrades | iOS deps | `ios/project.yml:13-15` |
| F-23 | **Low** | iOS `KeychainManager` writes Keychain items without `kSecAttrAccessGroup`, blocking future watchOS/share-extension support but also — by design — limiting blast radius. No `kSecAttrSynchronizable=false` set explicitly (defaults to false, OK) | iOS | `ios/Kevin/Services/KeychainManager.swift:22-26` |
| F-24 | **Low** | Permanent `secrets/AuthKey_DN24C7D9FT.p8` and `secrets/AuthKey_KNR3CWL6F7.p8` files exist on the developer's filesystem; gitignored but vulnerable to local-file exposure | Secrets | `secrets/` |
| F-25 | **Low** | `app_version.py` reads `IOS_*` env vars per request via `os.environ.get()` — fine, but no caching or structured update flow; a typo in the env var becomes silent 1.0.0 | Hardening | `app/api/app_version.py:47-61` |
| F-26 | **Info** | `firestore.rules` is fully closed (`allow read, write: if false`). Excellent. All access goes through Admin SDK on the backend. | Data protection | `firestore.rules:1-9` |
| F-27 | **Info** | RTDB rules are fully closed (`{".read": false, ".write": false}`). Excellent. Server-only via Admin SDK. | Data protection | `database.rules.json:1-6` |
| F-28 | **Info** | Twilio incoming webhook is correctly signature-verified via `verify_twilio_signature`. | Webhook | `app/middleware/twilio_verify.py:14-37`, `app/webhooks/twilio_incoming.py:194-432` |
| F-29 | **Info** | Apple App Store Server Notifications V2 webhook performs full JWS verification with x5c chain walk and `bundleId` check. | Webhook | `app/webhooks/appstore.py:26-109` |
| F-30 | **Info** | `vapi_events.py` and `telegram_callback.py` are not registered in `app/main.py`; their routes are unreachable. | Webhook | `app/main.py:14-75` |

---

## 3. Findings Detail

### F-01 — [CRITICAL] Apple Sign-In identity token never verified server-side

**Description**
The `CLAUDE.md` documentation states the auth flow as "Sign in with Apple on iOS → backend verifies the identity token → returns a per-contractor API token". The code does not implement step 2.

**Evidence**

In `app/api/contractors.py`:
```python
@public_router.get("/lookup-by-apple-id")
async def api_lookup_by_apple_id(request: Request, apple_user_id: str = ""):
    if not apple_user_id:
        return {"error": "apple_user_id required"}, 400
    from app.db.contractors import get_contractor_by_apple_user_id
    contractor = await get_contractor_by_apple_user_id(apple_user_id)
    if contractor:
        # Issues a fresh API token for the client
        from app.middleware.auth import generate_contractor_token
        contractor_id = contractor["contractor_id"]
        ...
        raw_token, token_hash = generate_contractor_token(contractor_id)
        await update_contractor(contractor_id, {"api_token_hash": token_hash})
        return {"contractor_id": contractor_id, "api_token": raw_token, ...}
```
(File `app/api/contractors.py:149-167`)

The middleware in `app/middleware/auth.py:42-53` lets these endpoints through without bearer auth, comment-promising "auth handled internally" — but neither `lookup-by-apple-id` nor `POST /api/contractors` ever read or verify `apple_identity_token`. A grep across the entire codebase finds zero references to JWT verification of the Apple identity token, no use of Apple's JWKS at `https://appleid.apple.com/auth/keys`, and no audience/issuer checks.

The iOS client does send the token (`ios/Kevin/Services/APIClient.swift:280`, `OnboardingView.swift:849-853`), but the backend ignores it.

**Impact**
- An attacker who knows or guesses any victim's Apple user ID (an opaque-but-stable string of the form `001234.deadbeef.5678`, leaked to every Sign-in-with-Apple-using app the victim has used) can call `GET /api/contractors/lookup-by-apple-id?apple_user_id=<victim>` and instantly receive an API token that grants full read/write access to the victim's contractor doc, Twilio number, call history, contacts, knowledge base, and ability to issue text replies / forward calls.
- Because each successful lookup writes a new `api_token_hash` to Firestore, the victim is also silently kicked off (their old token's hash is overwritten).
- Account creation via `POST /api/contractors` accepts arbitrary `apple_user_id` and `owner_phone` body fields with no verification — an attacker can pre-create accounts squatting on any phone number or Apple ID.

**Recommendation**
Implement server-side JWS verification of `id_token` on every Apple Sign-In flow:
1. Fetch and cache Apple's JWKS from `https://appleid.apple.com/auth/keys` (rotate every ~24h).
2. Verify the JWT signature using the matching `kid` from the JWKS.
3. Validate `iss == "https://appleid.apple.com"`, `aud == "com.kevin.callscreen"`, `exp` not expired, `iat` not in future.
4. Compare the `sub` claim against the `apple_user_id` parameter — reject if mismatch.
5. Apply the same verification to `POST /api/contractors` when `apple_user_id` is set.
6. Revoke any tokens issued before this fix and force re-auth on next launch.

A library like `PyJWT[crypto]` (already in `pyproject.toml`) plus a small JWKS cache is sufficient. Reference: [Sign in with Apple — Verify the identity token](https://developer.apple.com/documentation/sign_in_with_apple/sign_in_with_apple_rest_api/verifying_a_user).

---

### F-02 — [CRITICAL] Live secrets exposed in plaintext `.env` at repo root

**Description**
The file `.env` at the repository root contains live production-grade secrets: Twilio Account SID + Auth Token, Anthropic API key (`sk-ant-...`), Deepgram API key, Fish Audio key, ElevenLabs key, Gemini key, Telegram bot token + chat ID, Twilio API Key Secret, the `APNS_KEY_CONTENT` PEM block inline, and an `API_BEARER_TOKEN` of the form `kv_prod_...`.

**Evidence**

`.env:1-42` — quoting the redacted-for-format excerpt is unnecessary; auditor has confirmed plaintext keys are present in this file.

The file is gitignored (`.gitignore:1`). Git log searching for `sk-ant-` and `TWILIO_AUTH_TOKEN=` returns nothing, so the secrets do not appear to be in git history.

**Impact**
- Local exfil paths: backups, screen-share leaks, malware, accidental `git add -f`, compressed-tar uploads, docker-build context inclusion (`COPY . .` would include `.env` if Dockerfile changes — current `Dockerfile:7-10` copies only `pyproject.toml` and `app/`, so safe today but one merge away from disaster).
- The bearer token in particular grants admin access to every contractor's data and the `/api/admin/*` endpoints (extend trial, revoke, etc.).
- The Anthropic and ElevenLabs keys, if leaked, can be drained for cost.

**Recommendation**
- Move local-dev secrets to a `.env.local` outside the repo, or use `gcloud secrets` with `gcloud auth application-default login` for local development.
- Rotate every key currently in `.env` on the assumption it has been exposed (the value is a string named `kv_prod_...` which suggests this is the production token used as developer admin convenience — confirm and rotate).
- Add a pre-commit hook (e.g., `gitleaks`, `trufflehog`) to fail commits containing secret-shaped strings.
- Add a Cloud Build / Cloud Run "deny env file in image" policy.
- Verify `.env` is not present in any deployed Cloud Run revision — Dockerfile does not COPY it, and gcloud builds from source respect `.gcloudignore`/`.dockerignore`. Add `.env` to a `.dockerignore` for explicit defense.

---

### F-03 — [HIGH] Unauthenticated contractor creation; long-lived tokens with no rotation

**Description**
The combination of the unauthenticated POST `/api/contractors`, the per-call lookup that issues a fresh API token, and the `kv_ct_{first8}_{32-bytes}` token format that never expires creates a system where:

1. Anyone can create a contractor profile (provisioning a Twilio number that costs the company money).
2. Tokens have no `exp` claim, no rotation cadence, and no revocation list.
3. Tokens are issued via `POST /api/contractors` with status 200 in the response body — if any TLS-terminating intermediary logs response bodies, the token leaks.

**Evidence**

`app/middleware/auth.py:42-53`:
```python
if request.url.path in ("/api/contractors/lookup-by-apple-id", "/api/contractors"):
    if request.method in ("GET", "POST"):
        # Allow through — these endpoints validate Apple identity token internally
        ...
        return
```
But neither `/api/contractors/lookup-by-apple-id` (`app/api/contractors.py:149-167`) nor `POST /api/contractors` (`app/api/contractors.py:170-216`) actually validates anything. The `POST` happily accepts a request with empty body fields except for `business_name` (which has no length minimum, defaults to "" elsewhere).

**Impact**
- Cost amplification: each `provision-number` call costs the operator a Twilio number ($1+/mo). A spammer could create thousands of dummy contractors.
- Account squatting on Apple user IDs: an attacker can create an account with `apple_user_id=<victim>` BEFORE the victim signs up, locking them out (subsequent legitimate sign-up either refuses or merges into the attacker's account depending on race conditions).
- No way to invalidate a leaked token besides manual Firestore edit.

**Recommendation**
- Combine fix with F-01: require a verified Apple identity token to create or look up contractors.
- Add token expiry (`exp` claim or stored `expires_at`) and rotate on a 30–90 day cycle.
- Add a `POST /api/contractors/me/rotate-token` endpoint and have iOS rotate periodically.
- Track issuance time in the contractor doc; expose a "logged in devices" UI in Settings.

---

### F-04 — [HIGH] `owner_phone` and `apple_user_id` not in `PROTECTED_FIELDS`

**Description**
`PROTECTED_FIELDS` (`app/db/contractors.py:16-26`) includes subscription and lifecycle fields but not the identity-claim fields that determine future account restoration:

```python
PROTECTED_FIELDS = frozenset({
    "subscription_tier", "subscription_status", "subscription_expires",
    "trial_start", "subscription_uuid", "twilio_number",
    "deleted_app_detected_at",
})
```

The PATCH endpoint (`app/api/contractors.py:270-287`) lets any authenticated contractor change their own `apple_user_id` and `owner_phone`:

```python
class ContractorUpdate(BaseModel):
    ...
    owner_phone: Optional[str] = Field(default=None, max_length=20)
    apple_user_id: Optional[str] = Field(default=None, max_length=100)
```

**Impact**
- Account-merge attack: a legitimate contractor can rebind their `apple_user_id` to a victim's. The next time the victim signs in, the lookup-by-apple-id endpoint will return the attacker's contractor doc and issue the victim a token to it — a classic confused-deputy.
- A user can change `owner_phone` to a victim's, breaking the dedup logic in `POST /api/contractors:174-192` and silently merging the victim into the attacker's account on next sign-up.

**Recommendation**
- Add `owner_phone` and `apple_user_id` to `PROTECTED_FIELDS`.
- Updates to identity fields must go through a re-auth flow (re-prove Sign-in-with-Apple, re-prove SMS code).

---

### F-05 — [HIGH] `verify_subscription` fails open on Apple API errors

**Description**

`app/api/subscription.py:67-71`:
```python
transaction_info = await verify_transaction(body.transaction_id)
if not transaction_info:
    # Fail open — don't break paying users when Apple API is slow
    logger.warning(f"Apple API verification failed for {body.transaction_id} — failing open")
    return {"status": "ok", "message": "verification_skipped"}
```

The "fail open" comment indicates intent, but there's no rate-limit, no audit trail of fail-open events per contractor, and no follow-up retry. If Apple is unreachable (or an attacker can DoS the path between the backend and Apple's API), the iOS client can claim any `transaction_id` and the verify call returns `{"status": "ok"}` — which `SubscriptionManager.swift:127-135` treats as a success, calls `transaction.finish()`, and shows the purchase as confirmed.

Importantly, the actual `subscription_status`/`tier`/`expires` fields in Firestore are NOT updated when fail-open is taken (because `update_subscription_from_transaction` is never called). However, the iOS client doesn't know that — and on the very next call to `getContractorProfile`, the server returns the still-trial state. So the user sees `{"status":"ok"}` then `subscription_status:"trial"`. This is confusing rather than entitlement-granting, but the comment misleads — the real invariant is that the server-side state is unchanged on fail-open.

The risk is the **client-side state divergence**: an iOS bug or race could lock in `serverStatus == "active"` because the verify came back ok.

**Impact**
- Subscription-state UI confusion if Apple is down.
- If iOS code is later refactored to set `subscriptionStatus = "active"` directly on `verify success`, fail-open becomes a free-tier-grant bug.

**Recommendation**
- Treat Apple API failure as **fail-closed** for verify: return 503 and let the client retry with backoff.
- If you must fail-open for resilience reasons, don't return `status: ok` — return a third state like `status: pending_apple_verification` so the iOS layer never confuses it with success.
- Add a Cloud Tasks retry queue: store `(contractor_id, transaction_id)` and retry verification later.

---

### F-06 — [HIGH] Receipt replay across contractors

**Description**

`update_subscription_from_transaction` (`app/services/subscription.py:235-270`) takes a `contractor_id` from the request body and only checks that the transaction's `appAccountToken` equals that contractor's `subscription_uuid`. The deduplication index `is_transaction_seen` is keyed by `(contractor_id, transaction_id)` (`app/services/subscription.py:213-220`), which means:

```python
doc_path = f"contractors/{contractor_id}/transactions/{transaction_id}"
```

If a malicious client passes a transaction_id legitimately purchased under contractor A but submits it as contractor B (with B's own `subscription_uuid`), the appAccountToken check would catch it (because Apple binds `appAccountToken` to the actual purchase). Good.

**However**, the dedup is per-contractor. If contractor A's app is reinstalled and the same transaction is re-verified, it's correctly deduped. The risk surface here is limited.

The real issue is that `body.contractor_id` is fully client-controlled (`app/api/subscription.py:37-41`). The auth check `require_contractor_access(request, body.contractor_id)` (line 52) ensures the caller owns the contractor — but this still means a legitimate user can be coerced (via a malicious in-app webview or copy/paste) into POSTing a `transaction_id` from a stranger's screenshot. The appAccountToken check prevents that from taking effect, so this collapses to F-05's fail-open as the real attack vector.

**Impact**
- Subscription replay across accounts is blocked by the `appAccountToken` check, assuming Apple's API returns valid data.
- If Apple is unreachable (F-05), verify silently succeeds, but no entitlement is granted. Confusing UX, no entitlement bypass.

**Recommendation**
- Keep current `appAccountToken` check.
- Make dedup global (`transactions/{transaction_id}` rather than per-contractor) to detect cross-account reuse attempts and alert.
- Combine with F-05 fix.

---

### F-07 — [HIGH] Twilio Voice SDK access tokens not bound to caller's contractor

**Description**

`_generate_access_token` (`app/api/voip.py:243-261`) issues access tokens with a static `identity="kevin-contractor"`:
```python
token = AccessToken(
    settings.twilio_account_sid,
    settings.twilio_api_key_sid,
    settings.twilio_api_key_secret,
    identity="kevin-contractor",
    ttl=120,
)
voice_grant = VoiceGrant(
    outgoing_application_sid=settings.twilio_twiml_app_sid,
    incoming_allow=True,
)
```

`get_voip_token` (`app/api/voip.py:170-205`) similarly uses `identity="kevin-user"`. Both are global identities, so any authenticated contractor's iOS app can present this token to Twilio and dial the TwiML App, which redirects into a `Conference` (`app/webhooks/twilio_incoming.py:661-706`). The conference name comes from the iOS app via `customParameters.conference_name`, with only an alphanumeric+`_-` regex check.

**Impact**
- Contractor A who has captured a `conference_name` belonging to contractor B's active call (e.g., via an XSS-equivalent leak, a logging side channel, or a compromised admin console) can join contractor B's conference.
- The conference name `direct_<call_sid>` or `pickup_<token_urlsafe(8)>` is partially predictable for direct ring-throughs (the call_sid is observable from Twilio status callbacks if the attacker has an ear on the same Twilio account, which they don't, but the bound-identity defense-in-depth is missing).

**Recommendation**
- Use the `identity` field to namespace per contractor: `identity=f"contractor_{contractor_id}"`.
- Have `/webhooks/twilio/ios-voice` validate that the conference name's contractor prefix matches the access-token identity (you can extract identity via the inbound TwiML's `From` field).
- Use cryptographically-random unguessable conference names (`secrets.token_urlsafe(16)`) and store the conference→contractor mapping in RTDB; `/ios-voice` should reject if missing or mismatched.

---

### F-08 — [MEDIUM] vCard HMAC secret derived from API_BEARER_TOKEN

**Description**

`app/services/vcard.py:13`:
```python
_VCARD_SECRET = (settings.api_bearer_token or "kevin-vcard-secret").encode()
```

**Impact**
- Defaults to `"kevin-vcard-secret"` if the bearer token is empty — predictable secret allows forging arbitrary signed vCard URLs (a vCard exposes a contractor's owner_name, business_name, and Twilio number, but not phone, owner phone, or call data).
- If the bearer token rotates, all signed vCard URLs break.
- A leaked bearer token allows offline forging of any vCard URL.

**Recommendation**
- Use a dedicated `VCARD_HMAC_SECRET` env var/Secret Manager entry.
- Fail loudly (raise) if the secret is unset rather than falling back to a hardcoded default.

---

### F-09 — [MEDIUM] SSRF DNS rebinding race in `_validate_external_url`

**Description**

`app/api/contractors.py:426-457` resolves the hostname to verify it's not internal, then `httpx.AsyncClient(follow_redirects=False).get(url, ...)` resolves the hostname **a second time** to actually fetch. A DNS server controlled by the attacker can return a public IP for the validation lookup and a private IP (e.g., `169.254.169.254`, the GCP metadata service) for the fetch.

**Impact**
- Cloud Run metadata exfiltration: GCP metadata service at `169.254.169.254/computeMetadata/v1/` returns the runtime service account token. The attacker would need to lure the contractor's account into "import-website" with a malicious URL, but that's just a `POST` they control.
- Internal RTDB / Firestore hostnames cannot be reached from public DNS, but the metadata server is the high-value target.

**Recommendation**
- Use `httpx` with a custom transport that pins the resolved IP from the validation step, or use a library like `python-safer-urlopen`.
- Alternatively, restrict outbound network from Cloud Run to specific egress IPs via VPC connector + Cloud NAT and disable metadata server access (`gcloud run services update --no-cpu-throttling --vpc-egress=all-traffic`).
- Always send a `Metadata-Flavor` rejection: GCP metadata requires `Metadata-Flavor: Google` header, so as long as your fetch never sends that header (`httpx` doesn't by default), metadata access is blocked at the GCE layer. This is your best practical defense.

---

### F-10 — [MEDIUM] Estimate upload reads entire body before length check

**Description**

`app/api/estimates.py:138-159`:
```python
body = await request.body()
content_type = request.headers.get("content-type", "application/octet-stream")
max_size = MAX_VIDEO_SIZE if content_type.startswith("video/") else MAX_IMAGE_SIZE
if len(body) > max_size:
    return {"error": f"File too large. Max: {max_size // (1024*1024)}MB"}, 413
```

`request.body()` accumulates the entire payload in memory before the check. A malicious caller can post 10 GB; Cloud Run's default request body limit is 32 MiB so this saturates at 32 MiB worth of memory per concurrent request, but with many parallel requests the worker can OOM.

**Impact**
- DoS via memory exhaustion.

**Recommendation**
- Use `request.stream()` with a running byte counter and abort early at MAX size.
- Set Cloud Run `--max-instances` and `--memory` accordingly; consider a CDN/uploader proxy.

---

### F-11 — [MEDIUM] Call transcripts stored unencrypted in RTDB beyond Firebase defaults

**Description**

Transcripts are written to RTDB during a call (`app/webhooks/media_stream.py:267-287`) and to Firestore at end of call (`app/db/calls.py:25`). RTDB and Firestore both encrypt at rest with Google-managed keys, but no application-level encryption is applied. The audit's threat model concern is that:
- Transcripts can include legal, medical, financial discussions when a residential-services contractor's customer is in distress.
- The `caller_contacts` collection (created by `_post_call_extract`) stores extracted summaries of every call to a phone number, indefinitely.

**Impact**
- A compromised admin token (F-02 risk) reveals all transcripts.
- GDPR/CPRA "right to delete" must include transcript purging; current 90-day retention (`app/db/calls.py:17`) helps but `caller_contacts` has no retention policy I could find.

**Recommendation**
- Apply Customer-Managed Encryption Keys (CMEK) to the Firestore/RTDB project for stronger key separation.
- Add an explicit retention policy on `caller_contacts` (e.g., 90 days same as calls, or per-contractor configurable).
- Store transcripts in Firestore-only (skip RTDB or shorten the RTDB lifetime) since RTDB's per-record audit story is weaker.

---

### F-12 — [MEDIUM] Promotional offer signing rate limit is in-memory, per-instance

**Description**

`app/api/subscription.py:14-34`:
```python
_rate_limits: dict = defaultdict(list)
PROMO_RATE_LIMIT = 3     # requests per minute
```

The dict is per Cloud Run instance. With auto-scaling (e.g., 5–20 instances), an attacker can hit different instances and effectively get `N × 3` req/min. Cold starts and revision rollouts also reset the dict. No persistent rate limit.

**Impact**
- An authenticated contractor can sign hundreds of valid promotional offers per minute, harvesting signatures for offline use (signatures are not bound to a single redemption — each is valid until the StoreKit nonce is consumed at purchase time, but a bad actor with many signed offers can attempt subscription churn).
- The 1,000-slot atomic counter is the only hard cap; an attacker holding many signed offers but only purchasing on a fraction of them lets them outpace legitimate users in a launch-day rush.

**Recommendation**
- Move rate limit to a persistent store (Firestore or Redis) keyed by `contractor_id`.
- Bind signature usage to a single use: check the StoreKit notification webhook for promo-redemption events and burn the corresponding nonce.
- Lower the per-account cap to 1 valid promo at a time.

---

### F-13 — [MEDIUM] Call-action endpoint trusts call_sid even when active_call missing

**Description**

`app/api/voip.py:208-241`:
```python
active_call = await get_active_call(body.call_sid)
if active_call and active_call.contractor_id != contractor_id:
    return {"status": "error", "message": "Access denied"}
```

If `active_call is None` (because RTDB cleanup ran or the call ended), the contractor-binding check is skipped and the action proceeds. `_handle_voicemail` (`app/api/voip.py:325-339`) calls `client.calls(body.call_sid).update(twiml=...)` with no further check.

**Impact**
- A contractor with a leaked `call_sid` (Twilio call SIDs are 34 characters, not crypto-random; observable from Twilio status callbacks they don't have access to) can voicemail-route any in-progress call.
- Real-world exploitability is low because `call_sid` is hard to guess.

**Recommendation**
- Add a Firestore cross-check: if `active_call is None`, look up the most recent call record by `call_sid` and verify `contractor_id`.
- Require non-None `active_call`; return 404 otherwise.

---

### F-14 — [MEDIUM] Global `caller_contacts` collection not per-contractor

**Description**

`app/webhooks/media_stream.py:31-132` writes to `db.collection("caller_contacts").document(phone_key)` — global, keyed by caller phone. Then `app/webhooks/twilio_incoming.py:347-365` reads back caller_name from that same global collection.

**Impact**
- Contractor A's call transcript extracts a caller_name "John Smith" for `+15551234567`. Contractor B receives a call from the same number; the iOS app shows "John Smith" derived from contractor A's session. Cross-tenant information leak: the existence and name of a contact is shared across all contractors.
- This is also a privacy-policy risk: the user's contractor was told their contacts are siloed.

**Recommendation**
- Move `caller_contacts` under `contractors/{contractor_id}/caller_contacts/{phone_hash}`, similar to the `contacts` collection (`app/db/contacts.py:21-25`).
- Migrate existing data with a one-time backfill.

---

### F-15 — [MEDIUM] Dial-in PIN brute force

**Description**

`app/db/contractors.py:217`:
```python
data.setdefault("dial_in_pin", f"{secrets.randbelow(1000000):06d}")
```

6-digit PIN, keyspace 10^6. Rate limit: 3 attempts per 10-minute window per `From` phone (`app/webhooks/twilio_incoming.py:709-723`). Spoofed caller ID rotates the rate-limit key.

**Impact**
- An attacker who can spoof caller ID (trivial for VoIP services) can spam the dial-in line with rotating From numbers and brute-force PINs at hundreds per minute. With 10^6 keyspace, expected time-to-collide-with-any-active-contractor is small once contractor count grows.
- A successful PIN match lets the attacker join an active screening call — eavesdrop on the caller, speak to them as the contractor.

**Recommendation**
- Drop the dial-in flow entirely if it is not required; use the iOS app's "Pick Up" path instead.
- If retained, expand PIN to 10 digits, rate-limit by **destination Twilio number** rather than `From`, and add a global cap (e.g., 1 attempt/sec per Twilio number).
- Lock out a PIN after N failed attempts and require app-side confirmation to unlock.

---

### F-16 — [LOW/MEDIUM] Information disclosure in `appAccountToken` mismatch log

**Description**
`app/services/subscription.py:257`:
```python
logger.error(f"appAccountToken mismatch: expected subscription_uuid={expected_uuid!r}, got {app_account_token!r}")
```

UUIDs are not secrets per se but they are user-identifying. Centralized log readers see who attempted to verify whose subscription.

**Recommendation**
- Log only first 8 characters of each UUID, or hash both for correlation without disclosure.

---

### F-17 — [LOW] APNs and App Store JWT cache windows

`_cached_apns_token` valid 50min, App Store JWT issued with 20min expiry. Both within Apple's 60-min max but lack signal on key revocation. Defensive only.

---

### F-18 — [LOW] `pull_request:` trigger from forks

`.github/workflows/deploy.yml:7-9` triggers test job on PRs. Test job does not access GCP secrets, so safe today. However, malicious forks could abuse runner minutes / cause pytest to leak repo-private contents from PR checkout. Add `if: github.event.pull_request.head.repo.full_name == github.repository` or use `pull_request_target` with care.

---

### F-19 — [LOW] Action versions not pinned to SHAs

All workflow steps use `@v3` or `@v6` major-version tags. A compromised tag could ship a malicious action. Pin to commit SHAs:
```yaml
- uses: actions/checkout@<sha>  # v6.0.0
```

---

### F-20 — [LOW] Dockerfile root-installed deps + non-`--chown` COPY

`Dockerfile:7-15`:
```
COPY pyproject.toml .
RUN pip install --no-cache-dir .
COPY app/ app/
RUN adduser ... appuser
USER appuser
```

`app/` is owned by root, readable by `appuser`. Acceptable, but use `COPY --chown=appuser:appuser app/ app/` for cleanliness.

---

### F-21 — [LOW] Startup log contains partial Twilio number

`app/main.py:268-274`:
```python
logger.info("Kevin starting up", extra={..., "twilio_number": redact_phone(settings.twilio_phone_number)})
```

`redact_phone` keeps last 4 digits. Fine, but consider redacting fully in production startup logs.

---

### F-22 — [LOW] TwilioVoice SDK semver range

`ios/project.yml:13-15`: `from: "6.11.0"` allows minor/patch bumps automatically. CI builds may differ from local builds. Pin to exact: `exact: "6.11.0"` and review upgrades manually.

---

### F-23 — [LOW] `KeychainManager` accessibility level

`ios/Kevin/Services/KeychainManager.swift:24`:
```swift
addQuery[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
```

Good choice (no iCloud sync, only when device unlocked). No findings.

---

### F-24 — [LOW] `.p8` files on developer filesystem

`secrets/AuthKey_DN24C7D9FT.p8` and `secrets/AuthKey_KNR3CWL6F7.p8` exist on the developer machine. Gitignored. Treat as F-02 — keep off shared drives, don't include in backups, rotate if any laptop loss.

---

### F-25 — [LOW] `app_version.py` env-var fallback hides typos

`os.environ.get("IOS_MIN_VERSION", "1.0.0").strip()` — if the env var name is mis-set, version-gate silently returns 1.0.0 forever. Add a Cloud Monitoring alert on "min_version returned default".

---

## 4. What I Did NOT Check / Out of Scope

- **Live runtime testing**: I did not call any endpoints, did not attempt to exploit any of the findings, and did not verify production state. All conclusions are static analysis.
- **iOS code signing & provisioning profile correctness**: not in scope.
- **Twilio account-level settings** (e.g., geo-restrictions on outbound, fraud guards): not visible from this repo.
- **GCP IAM bindings**: I read `DEPLOY-SETUP.md` describing intended bindings but did not query live GCP IAM. A `gcloud projects get-iam-policy kevin-491315` review is recommended before launch.
- **Firestore composite indexes**: not reviewed for query-injection-via-index abuse (low risk given closed rules).
- **App Store Connect / TestFlight settings**: not in scope.
- **Cloud Run service IAM allow-unauthenticated**: confirmed in workflows but did not inspect actual `gcloud run services get-iam-policy`. Webhooks need public access; admin endpoints are bearer-protected at the app layer, which is acceptable, but add a defense-in-depth `noauth` only on `/webhooks/*` and authenticated invocation for the rest if you want belt-and-braces.
- **Dependency CVE scan**: pyproject.toml versions look current but a `pip-audit` / `safety check` run should be part of CI before launch.
- **Telegram/Vapi code paths**: routers exist (`app/webhooks/telegram_callback.py`, `app/webhooks/vapi_events.py`) but are NOT registered in `app/main.py`. They are dead code in production. Confirm they should be removed or kept gated.
- **Penetration testing of the live media stream**: WebSocket auth via `ws_token` looks correct (`media_stream.py:160-184`). The token is generated by `secrets.token_urlsafe(32)` and stored in RTDB; the WebSocket validates the token from Twilio's `customParameters`. Defense-in-depth check: verify Twilio cannot be MITMed (TLS-pinning is N/A for Twilio), and that `wss://` is enforced (it is, `cloud_run_url.replace("https://", "wss://")`).
- **Voicemail `Record` transcribe attribute**: Twilio default transcription for `<Record>` (`twilio_incoming.py:104, 114, 333`) ships transcripts to Twilio's third-party transcription. Confirm this matches your privacy policy.

---

## 5. Quick Wins (top 5 fixes ranked by effort × impact)

1. **[Day-of-launch blocker] Implement Apple identity-token verification** (F-01, F-03). 2–4 hours. Use `PyJWT` with cached JWKS from `appleid.apple.com`. Add to both `lookup-by-apple-id` and `POST /api/contractors`. Reject if `sub != apple_user_id`. Without this, the iOS auth boundary is decorative.
2. **[Day-of-launch blocker] Rotate every secret in `.env` and add `.env` to `.dockerignore` + add `gitleaks` pre-commit hook** (F-02). 1 hour. Rotate Twilio Auth Token, Anthropic, Deepgram, ElevenLabs, Gemini, Telegram bot, ElevenLabs, APNs `.p8` (re-generate), `API_BEARER_TOKEN`. Verify keys are loaded from Secret Manager at runtime in production, not Cloud Run env vars.
3. **Add `owner_phone` and `apple_user_id` to `PROTECTED_FIELDS`** (F-04). 5 minutes. One-line change in `app/db/contractors.py`. Eliminates the account-merge attack class.
4. **Make `verify_subscription` fail-closed and persist rate limits** (F-05, F-12). 2–3 hours. Return `503 Service Unavailable` from `/api/subscription/verify` when Apple is unreachable, with `Retry-After`. Move `_rate_limits` to Firestore (or Memorystore Redis) keyed by `contractor_id`.
5. **Bind Twilio Voice access tokens to the contractor's identity** (F-07). 30 min. Change `identity="kevin-contractor"` → `f"contractor_{contractor_id}"` in `_generate_access_token` and `get_voip_token`. Validate the identity matches the conference owner in `/webhooks/twilio/ios-voice`.

---

## Appendix — Strengths to Keep

- `firestore.rules` and `database.rules.json` are correctly closed; all access via Admin SDK.
- Twilio webhook signature verification is wired up everywhere via `verify_twilio_signature` (`app/middleware/twilio_verify.py`).
- App Store Server Notifications V2 webhook performs full JWS x5c chain verification + bundleId check (`app/webhooks/appstore.py`).
- Restrictive CORS (`https://heykevin.one` only) (`app/main.py:51-56`).
- Restrictive ATS in iOS (`NSAllowsArbitraryLoads: false`).
- Runtime guard `validate_runtime_safety()` prevents staging/dev from accidentally pointing at production resources.
- `redact_phone` used consistently in logs (search confirms only redacted phone numbers appear).
- Dockerfile uses non-root user.
- `secrets.token_urlsafe(32)` for `ws_token`, OAuth `state`, estimate token, conference-name suffix.
- Atomic Firestore transaction for promo counter (`app/services/subscription.py:285-304`).
- Subscription `appAccountToken` ↔ `subscription_uuid` ownership check (`app/services/subscription.py:248-258`).
- `PROTECTED_FIELDS` correctly drops client-side writes to subscription billing fields.
- Background task to release Twilio numbers + clean expired contractors after 14 days.
- WebSocket payload size cap (`WS_MAX_MESSAGE_SIZE = 65536`).
- Per-call max duration cap (90 minutes).

If F-01 through F-04 are remediated before launch, the system moves from "pre-release with critical gaps" to "ready for staged rollout pending pen test".
