"""
Kevin v2 Phase 0 — Twilio + Gemini Live API Audio Bridge Prototype

Deployed on Cloud Run. Handles:
1. Incoming call webhook → <Connect><Stream> to our WebSocket
2. WebSocket receives Twilio audio, bridges to Gemini Live API
3. Gemini audio streams back to Twilio
4. Caller talks to Kevin (Gemini) directly
"""

# This is now integrated into the main app — see app/webhooks/media_stream.py
# This file is kept as documentation of the prototype approach.
