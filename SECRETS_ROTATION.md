# Secrets rotation runbook

The pre-launch security audit (see `SECURITY_AUDIT.md`, finding F-02) flagged that the local `.env` file at the repo root contained live plaintext credentials. Even though `.env` is gitignored, the values lived in shell history, IDE indexes, and any backup snapshots of the working tree. They must be treated as compromised.

This document is the rotation checklist. Work through it top-to-bottom; each step rotates one credential and updates Cloud Run.

> **Order matters in places.** Where a step says "production users will be briefly affected," prefer to do it during a low-traffic window or pause the relevant service first.

---

## 0. Prep

```bash
gcloud config set project kevin-491315
gcloud auth login            # only if your token has expired
```

Identify the running revision so you have a rollback target:

```bash
gcloud run services describe kevin-api \
  --region us-central1 --format='value(status.latestReadyRevisionName)'
```

---

## 1. `API_BEARER_TOKEN` (global admin / read-all)

This is the most powerful key. Rotate first.

1. Generate a new token (≥48 random chars):
   ```bash
   python3 -c "import secrets; print('kv_prod_' + secrets.token_urlsafe(48))"
   ```
2. Update Cloud Run:
   ```bash
   gcloud run services update kevin-api \
     --region us-central1 \
     --update-env-vars=API_BEARER_TOKEN=<NEW>
   ```
3. Update local `.env` with the new value.
4. Confirm everything still works (admin tooling, scripts in `scripts/`).

---

## 2. Twilio Auth Token

1. Console → https://console.twilio.com/ → Account → API keys & tokens → "Roll Auth Token". Copy the new token.
2. Update Cloud Run:
   ```bash
   gcloud run services update kevin-api \
     --region us-central1 \
     --update-env-vars=TWILIO_AUTH_TOKEN=<NEW>
   ```
3. Webhook signature verification will pick up the new token after the next revision boot. Verify with a test call.

---

## 3. Anthropic API key

1. https://console.anthropic.com/ → Settings → API keys → revoke the leaked key, generate a new one.
2. `gcloud run services update kevin-api --region us-central1 --update-env-vars=ANTHROPIC_API_KEY=<NEW>`
3. Test: trigger a call summary and confirm.

---

## 4. Deepgram

1. https://console.deepgram.com/ → API keys → revoke the leaked key, generate a new one.
2. `gcloud run services update kevin-api --region us-central1 --update-env-vars=DEEPGRAM_API_KEY=<NEW>`
3. Make a test call; confirm transcripts come through.

---

## 5. ElevenLabs

1. https://elevenlabs.io/ → Profile → API key → regenerate.
2. `gcloud run services update kevin-api --region us-central1 --update-env-vars=ELEVENLABS_API_KEY=<NEW>`
3. Test call; confirm Kevin speaks back.

---

## 6. Gemini

1. https://aistudio.google.com/app/apikey → revoke, regenerate.
2. `gcloud run services update kevin-api --region us-central1 --update-env-vars=GEMINI_API_KEY=<NEW>`
3. Make a test call; confirm Kevin's responses.

---

## 7. Telegram bot token (if used)

1. Talk to `@BotFather` on Telegram, `/revoke`, then `/token` for a new one.
2. `gcloud run services update kevin-api --region us-central1 --update-env-vars=TELEGRAM_BOT_TOKEN=<NEW>`

---

## 8. APNs `.p8` key

This is the most disruptive — a botched rotation will silently break VoIP push (incoming calls will not ring on iOS) until the new key reaches the Cloud Run revision.

1. https://developer.apple.com/account/resources/authkeys/list → revoke the old key.
2. Create a new key with **Apple Push Notifications service** capability. Download the `.p8`.
3. Note the new key ID (looks like `XXXXXXXXXX`).
4. Update Cloud Run with both the key ID and the inline PEM (newlines pipe-separated, matching the existing format in `app/services/push_notification.py`):
   ```bash
   KEY_ID=...        # from Apple Developer
   KEY_CONTENT=$(cat AuthKey_${KEY_ID}.p8 | tr '\n' '|')
   gcloud run services update kevin-api \
     --region us-central1 \
     --update-env-vars=APNS_KEY_ID=${KEY_ID},APNS_KEY_CONTENT="${KEY_CONTENT}"
   ```
5. Verify by triggering a test call — VoIP push should ring CallKit.
6. Revoke the old key in App Store Connect once verified.

---

## 9. App Store In-App Purchase signing key (`APPSTORE_PRIVATE_KEY`)

Used to sign promotional offers. Rotation requires a small App Store Connect coordination step.

1. https://appstoreconnect.apple.com/ → Users and Access → Integrations → In-App Purchase → revoke the old key, create a new one.
2. Download the `.p8`. Note the new key ID.
3. Update Cloud Run:
   ```bash
   gcloud run services update kevin-api \
     --region us-central1 \
     --update-env-vars=APPSTORE_KEY_ID=<NEW_KEY_ID>,APPSTORE_PRIVATE_KEY="$(cat AuthKey_<NEW_KEY_ID>.p8 | tr '\n' '|')"
   ```
4. Test a paywall purchase with a Sandbox account.

---

## 10. Firebase / GCP service account keys

If `secrets/` ever contained service-account JSON keys: rotate via IAM. The repo currently uses Workload Identity Federation for CI (per `DEPLOY-SETUP.md`), so no long-lived service-account JSON should exist. Verify with:

```bash
gcloud iam service-accounts list --project kevin-491315
gcloud iam service-accounts keys list --iam-account=<sa-email>
```

Delete any user-managed keys you find unless you can identify a current need.

---

## 11. Final sweep

After all keys are rotated:

1. Delete the local `.env` and recreate it from scratch with only the values you actually need locally.
2. Install the pre-commit gitleaks hook so this doesn't recur:
   ```bash
   pip install pre-commit
   pre-commit install
   pre-commit run --all-files     # baseline scan
   ```
3. Confirm `.dockerignore` excludes `.env` and `secrets/` (already done by this commit).
4. Audit shell history for the old values:
   ```bash
   grep -E "kv_prod_|sk-ant-|api-key|AuthKey" ~/.zsh_history ~/.bash_history 2>/dev/null
   ```
   Clean any matches.
5. Note the rotation date below for the next audit:

| Key | Rotated on | By |
|---|---|---|
| `API_BEARER_TOKEN` | YYYY-MM-DD | |
| Twilio Auth Token | | |
| Anthropic | | |
| Deepgram | | |
| ElevenLabs | | |
| Gemini | | |
| Telegram bot | | |
| APNs `.p8` | | |
| App Store IAP `.p8` | | |

---

## Rollback

If a rotation breaks production:

```bash
gcloud run services update-traffic kevin-api \
  --region us-central1 \
  --to-revisions=<previous-revision>=100
```

Pin the old revision at 100% while you debug. Each `--update-env-vars` creates a new revision, so the previous one is always available for traffic split.
