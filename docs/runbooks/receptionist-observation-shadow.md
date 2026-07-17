# Receptionist Observation Shadow Pilot

## Status And Boundaries

This runbook is for synthetic calls made by the maintainer to the isolated staging
service. It does not authorize a deployment, flag change, customer call, semantic
extraction, controller decision, merge, production action, or changes to PR #76.

Keep these decisions separate:

1. code and exact-SHA review;
2. staging deployment;
3. staging service flag and secret mount;
4. contractor/test-caller authorization;
5. each structured call window;
6. rollback or cleanup;
7. any later controller work.

## Local Dry Run

The default operator action is `plan`. It performs no network or Firestore write.
It reads the synthetic caller identifier without echoing it and reads the HMAC key
from an environment variable. Its output is payload-free and excludes the caller,
key, digest, and contractor ID.

```bash
umask 077
KEY_FILE="$(mktemp)"
openssl rand -hex 32 | tr -d '\n' > "$KEY_FILE"
export RECEPTIONIST_OBSERVATION_SHADOW_CALLER_HMAC_KEY="$(<"$KEY_FILE")"

.venv/bin/python scripts/manage_receptionist_observation_shadow.py plan \
  --project kevin-staging-491315 \
  --health-url https://kevin-api-staging-l63rergg7a-uc.a.run.app/health \
  --expected-sha <exact-remote-branch-head-sha> \
  --contractor-id <synthetic-staging-contractor-id> \
  --ttl-seconds 600
```

At the prompt, enter only the maintainer's staging test caller identifier. Do not
use a customer number or paste the value into a command argument.

## Predeployment Record

Before a separately authorized deployment, record:

```bash
CANDIDATE_SHA="$(git rev-parse origin/codex/voice-pilot-shadow)"
ROLLBACK_REVISION="$(gcloud run services describe kevin-api-staging \
  --project kevin-491315 \
  --region us-central1 \
  --format='value(status.latestReadyRevisionName)')"
```

The candidate must equal the current remote branch head, PR head, reviewed SHA, and
later `/health.deploy_sha`. The rollback revision must be nonempty. Store only the
opaque contractor label emitted by the operator tool; do not put the contractor ID
or caller digest in the evidence artifact.

## Exact-SHA Staging Deployment

Run only after explicit staging-deployment authorization:

```bash
gh workflow run deploy.yml \
  --ref main \
  -f target=staging \
  -f candidate_sha="$CANDIDATE_SHA"
```

The deploy workflow checks the exact remote branch head, runs tests, deploys with
the observation flag forced off, and verifies `/health.deploy_sha`. A successful
deploy does not authorize flag enablement.

## Shadow-Off Window

With `RECEPTIONIST_OBSERVATION_SHADOW_ENABLED=false`, run the pre-registered
synthetic call script. Capture payload-free voice metrics for:

- first outbound audio;
- response first audio;
- interruption clear;
- audio gaps and delivery errors;
- reconnects;
- event-loop lag if available;
- exact revision and deploy SHA.

Do not capture or export transcript text, phone values, raw Gemini messages, audio,
tool arguments, prompts, or exception bodies.

## Service Enablement

Run only after separate flag-enablement authorization. Create or update a dedicated
staging secret; never reuse a live provider, admin, OAuth, or production secret.

```bash
SECRET_NAME=kevin-staging-observation-shadow-hmac
gcloud secrets describe "$SECRET_NAME" --project kevin-491315 >/dev/null 2>&1 || \
  gcloud secrets create "$SECRET_NAME" \
    --project kevin-491315 \
    --replication-policy=automatic
gcloud secrets versions add "$SECRET_NAME" \
  --project kevin-491315 \
  --data-file="$KEY_FILE"

gcloud run services update kevin-api-staging \
  --project kevin-491315 \
  --region us-central1 \
  --update-secrets \
    RECEPTIONIST_OBSERVATION_SHADOW_CALLER_HMAC_KEY="$SECRET_NAME":latest \
  --update-env-vars RECEPTIONIST_OBSERVATION_SHADOW_ENABLED=true
```

Verify the new revision still reports `CANDIDATE_SHA`. Then apply the short-lived,
exact-SHA contractor authorization:

```bash
.venv/bin/python scripts/manage_receptionist_observation_shadow.py enable \
  --project kevin-staging-491315 \
  --health-url https://kevin-api-staging-l63rergg7a-uc.a.run.app/health \
  --expected-sha "$CANDIDATE_SHA" \
  --contractor-id <synthetic-staging-contractor-id> \
  --ttl-seconds 600 \
  --confirm enable-staging-observation-shadow
```

The enable command checks staging identity and exact health SHA before creating a
Firestore client or writing protected fields. Authorization expires automatically
and becomes invalid after any deploy SHA change.

## Shadow-On Window

Use the same synthetic scripts and call counts as the shadow-off window. The pilot
requires two separate windows overall, each with at least:

- 10 calls;
- 30 response turns;
- 5 deliberate interruptions;
- language and code-switch examples from the pre-registered fixture set;
- one clean hangup and one reconnect exercise.

Required observation results:

- zero queue drops;
- zero worker or enqueue errors;
- zero teardown failures;
- zero payload leaks;
- no prompt, websocket, audio, tool, persistence, or post-call mutation;
- first-audio p95 at most 2.5 seconds;
- response-first-audio p95 at most 1.5 seconds;
- interruption-clear p95 at most 250 milliseconds;
- no material shadow-on regression against the shadow-off window;
- retrospective results described only as diagnostic, never provider-final.

Any failed gate blocks semantic extraction and controller work.

## Disable And Cleanup

Contractor disable does not depend on service health:

```bash
.venv/bin/python scripts/manage_receptionist_observation_shadow.py disable \
  --project kevin-staging-491315 \
  --contractor-id <synthetic-staging-contractor-id> \
  --confirm disable-staging-observation-shadow

gcloud run services update kevin-api-staging \
  --project kevin-491315 \
  --region us-central1 \
  --update-env-vars RECEPTIONIST_OBSERVATION_SHADOW_ENABLED=false \
  --remove-secrets RECEPTIONIST_OBSERVATION_SHADOW_CALLER_HMAC_KEY

rm -f "$KEY_FILE"
unset RECEPTIONIST_OBSERVATION_SHADOW_CALLER_HMAC_KEY
```

If caller experience or operational isolation regresses, disable first and invoke
the protected rollback workflow with the recorded rollback revision. Confirm the
restored revision and `/health.deploy_sha` before any retry.

## Production Check

Before closing the pilot, confirm production reports the expected production SHA,
has `RECEPTIONIST_OBSERVATION_SHADOW_ENABLED=false`, and has no observation HMAC
secret mapping. Do not print the full environment or secret values.
