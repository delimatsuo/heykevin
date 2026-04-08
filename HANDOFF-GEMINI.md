# Gemini Live Pipeline — Coding Handoff

## What to Build

Add a Gemini Live API voice pipeline as a switchable alternative to the current Deepgram+Claude+ElevenLabs pipeline. This reduces AI costs by 42% and latency by 2-3x.

## Files to Read First

1. **Implementation Plan** (follow this step by step): `docs/superpowers/plans/2026-04-07-gemini-live-pipeline.md`
2. **Design Spec** (architecture context): `docs/superpowers/specs/2026-04-07-gemini-live-pipeline-design.md`
3. **Current voice pipeline** (reference — do NOT modify): `app/services/voice_pipeline.py`
4. **Media stream bridge** (modify for pipeline selection): `app/webhooks/media_stream.py`
5. **Config** (gemini_api_key at line 39): `app/config.py`
6. **Old Gemini prototype** (reference only, will be deleted): `app/services/gemini_agent.py`

## Execution Method

Use `superpowers:subagent-driven-development` to implement the plan task-by-task.

## Key Details

- Gemini model: `gemini-2.5-flash-live-preview-native-audio`
- Audio conversion needed: mulaw 8kHz (Twilio) ↔ PCM 16kHz/24kHz (Gemini)
- Python 3.12 on Cloud Run — `audioop` is available
- Test contractor ID: `COgOeaSL4lbmuSvD7sOu`
- Deploy command: `gcloud run deploy kevin-api --source . --project kevin-491315 --region us-central1 --allow-unauthenticated`

## Environment

- gcloud auth may need refresh: `gcloud auth login --account=deli@ellaexecutivesearch.com`
- Working directory: `/Volumes/Extreme Pro/myprojects/Kevin`
