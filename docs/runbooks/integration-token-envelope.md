# Runbook: Integration Token Encryption Envelope

## Overview
This runbook defines the key generation, deployment order, key rotation, multi-instance coordination, and rollback constraints for the versioned, context-bound AES-256-GCM integration token envelope in Hey Kevin.

---

## 1. Key Generation & Secret Provisioning

### Generating a Key
Each key version must be an exact 32-byte cryptographically secure random key, standard base64-encoded:

```bash
python3 -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode('ascii'))"
```

### Formatting the Environment Variables
- `INTEGRATION_TOKEN_ENCRYPTION_KEYS`: A canonical JSON object mapping positive decimal version strings to base64-encoded 32-byte keys.
- `INTEGRATION_TOKEN_ACTIVE_KEY_VERSION`: The canonical positive integer version used for all new writes (must be a canonical string with no leading zeroes or whitespace padding).
- `INTEGRATION_TOKEN_ENCRYPTED_WRITES_ENABLED`: The boolean activation flag (`false` by default).

Example for initial deployment:
```json
INTEGRATION_TOKEN_ENCRYPTION_KEYS='{"1":"<32-byte-base64-key-v1>"}'
INTEGRATION_TOKEN_ACTIVE_KEY_VERSION="1"
INTEGRATION_TOKEN_ENCRYPTED_WRITES_ENABLED="false"
```

---

## 2. Pair-Valid Provider Boundary & Durable Monotonic Envelope Floor

### Pair-Valid Provider Boundary (`resolve_usable_token_pair`)
For a contractor's integration credentials to be usable by any subsystem (Gemini voice pipeline, receptionist context, job creation, or background token refreshes), BOTH `access_token` and `refresh_token` must be present, non-empty, and valid at the same time:
- **Allowed Representations**: EITHER both are exact valid plaintext strings, OR both are authenticated AES-256-GCM envelope dictionaries bound to the specific contractor and provider.
- **Fail-Closed Resolution**: Any absent, one-sided, mixed (`str` + `dict`), malformed, unknown-key, or tampered pair returns `(None, None)`.
- **Zero HTTP Calls on Invalid Boundary**: Downstream clients make zero provider HTTP requests when token pair resolution fails.

### Durable Monotonic Envelope Floor (`token_envelope_required`)
Each provider tracks a server-owned, protected boolean field (`jobber_token_envelope_required`, `google_calendar_token_envelope_required`):
- **Monotonic Floor**: Once an encrypted envelope has ever been durably stored for a contractor and provider, the floor field is set to `true` and remains `true` monotonically across disconnects and reconnects.
- **Downgrade Rejection**: If `envelope_required` is `true`, any attempt to write or refresh plaintext credentials fails closed under CAS (`IntegrationTokenEnvelopeError`) with zero mutations.
- **Envelope Reconnect Requirement**: When `envelope_required` is `true`, reconnects require encrypted envelope writes even if the global configuration flag `INTEGRATION_TOKEN_ENCRYPTED_WRITES_ENABLED` is currently `false`.

### Disconnect Lifecycle & Envelope Floor Preservation
Provider disconnection (`disconnect_provider_cas`) executes atomically under CAS:
- **Representation-Independent Deletion**: Credentials, claims, and refresh timestamps are tombstoned using `firestore.DELETE_FIELD`, advancing the generation integer and recording a durable audit event. Disconnect is never blocked by malformed, unknown-key, mixed, or corrupt credentials.
- **Conservative Floor Preservation & Normalization**: If `token_envelope_required` was exact `true`, if either raw stored credential was an envelope dictionary (even if corrupted, tampered, or missing a decryption key), OR if `token_envelope_required` was present with any non-boolean malformed value (e.g. `1`, `'true'`, `None`, `list`, `dict`), `token_envelope_required` is conservatively normalized to exact boolean `true` in the same atomic update. This guarantees cleanup cannot strand the account in an un-reconnectable state or permit a plaintext downgrade after ambiguous history. Exact `false` with no envelope credentials remains `false`/unenforced.
- **Non-Blocking Revocation**: Best-effort provider revocation errors or decryption failures never block credential deletion or disconnect CAS commit.

---

## 3. Rollout Sequence (Single Compatibility Release + Configuration Activation)

To guarantee zero downtime and prevent decryption failures or token downgrades during deployment, releases must follow this exact sequence:

> [!IMPORTANT]
> **Owner-Gated Release Process**
> Source unit tests prove in-memory and simulated CAS/crypto contracts, but DO NOT prove Cloud Run deployment, secret provisioning, live provider authorization, or traffic routing. Secret creation, configuration updates, deployment, and live provider qualification are strictly owner gates.

### Step 1: Merge Compatibility Source Release
- **Code Merged**: One compatibility source PR containing:
  - Envelope-aware pair-valid readers (`resolve_usable_token_pair`, `resolve_usable_token`, `has_usable_token`) across all consumers
  - Envelope-aware and legacy-string-aware Jobber and Google Calendar read/refresh paths
  - Transactional CAS mutations (`persist_refreshed_tokens_cas`, `connect_provider_cas`, `disconnect_provider_cas`)
  - Multi-instance cross-process leases (`acquire_refresh_claim_cas`, `release_refresh_claim_cas`) with per-attempt retry clocks
  - Durable monotonic envelope floor enforcement across all write and disconnect paths
  - Closed-schema lifecycle audit trail
  - Centralized monotonic write-format policy (`determine_write_format`)
  - Default-off activation flag (`INTEGRATION_TOKEN_ENCRYPTED_WRITES_ENABLED=false`)

### Step 2: Provision Secrets & Deploy Compatibility Revision (Rollback Floor)
- Owner provisions `INTEGRATION_TOKEN_ENCRYPTION_KEYS` and `INTEGRATION_TOKEN_ACTIVE_KEY_VERSION` in Cloud Run Secret Manager / environment for staging, then production.
- Deploy the exact compatibility source SHA with `INTEGRATION_TOKEN_ENCRYPTED_WRITES_ENABLED=false`.
- **Permanent Rollback Floor Established**: Verify and record the deployed Cloud Run revision ID, source commit SHA, active key versions, reader decryptability, and monotonic write-policy capability.
- In this state, existing plaintext records remain plaintext, while any existing envelope records are preserved as envelopes.

### Step 3: Enable Encrypted Writes (Configuration Revision)
- Enable encrypted writes by deploying a configuration-only revision setting `INTEGRATION_TOKEN_ENCRYPTED_WRITES_ENABLED=true` with the **SAME** source commit SHA and keyring, staging first before production.
- All new connections and token refreshes now persist as v1 AES-256-GCM encrypted envelopes and establish the durable `token_envelope_required` floor.
- **Mixed-Revision Traffic Behavior**: During traffic migration between Step 2 and Step 3 instances:
  - Flag-off instances (Step 2) preserve existing envelopes and never downgrade them to plaintext (enforced by `determine_write_format` and `token_envelope_required`).
  - Never-encrypted records touched by Step 2 remain plaintext until refreshed by a Step 3 instance.
  - No records are corrupted, broken, or downgraded.

---

## 4. Key Rotation & Retention Procedure

When rotating to a new key version (e.g. from version `1` to version `2`):

1. **Generate New Key**: Generate a fresh 32-byte key for version `2`.
2. **Append to Keyring (Retain All Historical Keys)**:
   ```json
   INTEGRATION_TOKEN_ENCRYPTION_KEYS='{"1":"<key-v1>","2":"<key-v2>"}'
   ```
3. **Switch Active Key Version**:
   ```
   INTEGRATION_TOKEN_ACTIVE_KEY_VERSION="2"
   ```
4. **Deploy Environment Update**:
   - Reads with `key_version: 1` decrypt transparently using key `1`.
   - All new OAuth callbacks and token refreshes write `key_version: 2`.
   - Historical keys must be retained in the keyring JSON until all existing contractor documents have been refreshed or migrated.

---

## 5. Rollback Constraints & Forbidden Rollback Rules

> [!CAUTION]
> **CRITICAL ROLLBACK CONSTRAINT**
> Once Step 3 (Encrypted Writes Enabled) is live and any contractor document has had an encrypted token envelope written to Firestore:
> - You MAY roll back to Step 2 (the verified Keys-Present, Flag-Off Compatibility Revision) at any time. Step 2 instances can read envelopes and will maintain envelope representation under CAS without downgrading.
> - You must **NEVER** roll back to any pre-compatibility codebase or keyless revision. Pre-compatibility revisions cannot parse envelope dicts and will treat them as missing or invalid credentials.
> - All referenced historical keys MUST be retained in the keyring during rollback.

---

## 6. Jobber Refresh Token Rotation & Multi-Instance Coordination

### Jobber Single-Use Token Rotation Contract
According to Jobber's API documentation ([Jobber Refresh Token Rotation](https://developer.getjobber.com/docs/building_your_app/refresh_token_rotation/)), Jobber issues a single-use refresh token. When exchanged, the previous refresh token is immediately invalidated on Jobber's authorization server and replaced by a new `(access_token, refresh_token)` pair.

### Cross-Process Coordination (Durable Lease in Firestore)
Because in-process locks only protect threads within a single Cloud Run container, cross-process and multi-instance coordination is enforced via a durable Firestore lease claim (`acquire_refresh_claim_cas`) **before** making the HTTP call to the provider:
1. **Acquisition & Retry-Time Clock**: The primary instance transactionally acquires a 60-second lease bound to the contractor ID, provider, observed generation, and exact raw credentials. On transaction retries, the lease expiry is freshly recomputed from `time.time()`.
2. **Contenders**: Any concurrent instance observing an active lease makes **zero HTTP calls** to the provider. The contender inspects the durable document; if the winning instance advanced the generation and committed, the contender reloads the winner's fresh tokens.
3. **Commit & Mandatory Lease Invariant**: Token persistence via `persist_refreshed_tokens_cas` strictly enforces that an active, unexpired, matching lease claim is present on the contractor document at commit time, comparing held expiry against fresh `time.time()` on every transaction attempt. Unclaimed or expired refresh attempts fail closed without writing tokens.
4. **Release**: Upon successful commit, lease fields are atomically deleted in the CAS transaction. On HTTP or validation failure, the lease claim is released via `release_refresh_claim_cas`.

### Legacy Contractor Compatibility Rule (Missing Connected Flag)
Baseline production records stored valid `access_token` and `refresh_token` credentials without an explicit `provider_connected` boolean or `provider_generation` integer.
- **Refresh Compatibility**: Both `acquire_refresh_claim_cas` and `persist_refreshed_tokens_cas` accept an absent `provider_connected` field as a legacy connected state when both stored credentials form a valid durable pair under `determine_write_format` and exact observed CAS.
- **First Refresh Transition**: A successful legacy refresh automatically writes `provider_connected = true` and advances the absent generation 0 to 1.
- **Explicit Disconnected Refresh Rejection**: An explicit `provider_connected = false` or non-boolean value fails closed and is rejected under CAS.

### Irreducible Crash Risk & Manual Reauthorization
In distributed systems, no mechanism can provide 100% "exactly-once" delivery across separate network domains (e.g. if the provider executes the token rotation and the server crashes/restarts before the response can be durably written to Firestore). In this rare irreducible edge case, the old refresh token is invalid at Jobber and the new one was lost in transit before durable commit.
- **Impact**: The contractor's next refresh request returns 400/401 (`invalid_grant`) from Jobber, and subsequent Jobber API calls fail.
- **Recovery Procedure (Manual Reauthorization)**: Automatic crash-loss re-auth prompting is not implemented in backend workers. Resolution requires manual contractor reauthorization via the standard OAuth connect flow (`GET /api/integrations/jobber/connect` or the Settings tab in the iOS app), which completes a new OAuth exchange, overwrites credentials via `connect_provider_cas`, and advances the generation.
