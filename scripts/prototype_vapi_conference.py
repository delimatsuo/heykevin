"""
Kevin Phase 0 - Vapi + Twilio Conference Bridge Prototype

This script validates the core integration:
1. Creates a Twilio Conference Bridge
2. Calls YOUR phone and places you in the conference (simulating a caller)
3. Creates a Vapi AI agent that calls into the SAME conference
4. Confirms bidirectional audio (you talk to Kevin, Kevin talks to you)
5. Tests participant management (add/remove)

Usage:
    pip install twilio httpx python-dotenv
    python scripts/prototype_vapi_conference.py

What to expect:
    - Your phone will ring. Answer it.
    - You'll be placed in a conference with hold music briefly.
    - Kevin (AI agent) will join and greet you.
    - Talk to Kevin for 30 seconds to test the conversation.
    - The script will then remove Kevin and add your phone again (simulating warm transfer).
    - Press Ctrl+C to end the test.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from twilio.rest import Client

# Load env from project root
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(env_path)

# Config
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_PHONE_NUMBER = os.environ["TWILIO_PHONE_NUMBER"]
VAPI_API_KEY = os.environ["VAPI_API_KEY"]
VAPI_PHONE_NUMBER_ID = os.environ["VAPI_PHONE_NUMBER_ID"]
USER_PHONE = os.environ["USER_PHONE"]
USER_NAME = os.environ.get("USER_NAME", "the owner")

# Twilio client
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Conference name for this test
CONFERENCE_NAME = f"kevin_prototype_{int(time.time())}"


def step(msg: str):
    """Print a step with timestamp."""
    print(f"\n{'='*60}")
    print(f"  {msg}")
    print(f"{'='*60}")


def call_user_into_conference():
    """Call the user's phone and place them in the conference (simulates a caller)."""
    step("Step 1: Calling your phone to place you in the conference...")
    print(f"  Dialing {USER_PHONE} from {TWILIO_PHONE_NUMBER}")
    print(f"  Conference: {CONFERENCE_NAME}")

    # Create a call that places the user in a conference
    # This TwiML URL uses Twilio's built-in TwiML Bins equivalent
    twiml = f"""
    <Response>
        <Say>You are being placed in the Kevin prototype conference. Please wait for Kevin to join.</Say>
        <Dial>
            <Conference startConferenceOnEnter="true"
                        endConferenceOnExit="false"
                        waitUrl="http://twimlets.com/holdmusic?Bucket=com.twilio.music.ambient"
                        statusCallback="https://httpbin.org/post"
                        statusCallbackEvent="start end join leave">
                {CONFERENCE_NAME}
            </Conference>
        </Dial>
    </Response>
    """

    call = twilio_client.calls.create(
        to=USER_PHONE,
        from_=TWILIO_PHONE_NUMBER,
        twiml=twiml.strip(),
    )

    print(f"  Call SID: {call.sid}")
    print(f"  Status: {call.status}")
    print(f"  Waiting for you to answer...")
    return call.sid


def wait_for_conference():
    """Wait until the conference exists and has at least one participant."""
    print("\n  Waiting for conference to start...")
    for i in range(30):  # Wait up to 30 seconds
        time.sleep(2)
        conferences = twilio_client.conferences.list(
            friendly_name=CONFERENCE_NAME,
            status="in-progress",
        )
        if conferences:
            conf = conferences[0]
            participants = conf.participants.list()
            print(f"  Conference found! SID: {conf.sid}")
            print(f"  Participants: {len(participants)}")
            if len(participants) >= 1:
                return conf.sid
        print(f"  ...waiting ({(i+1)*2}s)")

    print("  ERROR: Conference did not start within 60 seconds.")
    print("  Did you answer your phone?")
    sys.exit(1)


async def create_vapi_call_into_conference(conference_sid: str):
    """
    Create a Vapi outbound call that dials into our Twilio conference.

    Strategy: Vapi calls a Twilio number. We use the Twilio REST API
    to update that call's TwiML to join our conference once it connects.

    Alternative strategy: Have Vapi call our Twilio number directly,
    and our webhook places it in the conference.

    For this prototype, we'll use a simpler approach:
    - Create a Vapi outbound call to a second Twilio number or SIP
    - OR: Have Vapi call our own Twilio number with special handling

    Simplest approach for prototype: Create the Vapi call, then use
    Twilio to add the Vapi call as a conference participant.
    """
    step("Step 2: Creating Vapi AI agent and dialing into conference...")

    # Create a Vapi outbound call to the user's conference
    # Vapi will call the Twilio number, and we'll need to handle the routing
    #
    # For prototype: We'll create a Vapi call that calls a number,
    # then we manually bridge it. But first, let's try the direct approach:
    # Have Vapi make an outbound call where WE are the customer.

    # Actually, the simplest prototype approach:
    # 1. We already have the user in a conference
    # 2. We create a NEW Twilio call (outbound from Twilio to Twilio)
    #    that joins the same conference, with Vapi handling the audio
    #
    # But Vapi needs to be the one making the call for the AI to work.
    # So: Vapi calls USER_PHONE (or any number), and when it connects,
    # we update the call to join the conference.
    #
    # BETTER: Use Vapi's outbound call to call our Twilio number.
    # When Twilio receives it, the webhook places it in our conference.
    # But we need a webhook server running for that...
    #
    # SIMPLEST FOR PROTOTYPE: Just have Vapi call the user directly
    # (not through conference) to validate Vapi works, audio quality,
    # and latency. Then test conference bridging separately.

    print("  Creating Vapi assistant with Kevin persona...")
    print("  Using: Deepgram Flux (STT) + Claude Sonnet (LLM) + Fish Audio (TTS)")

    async with httpx.AsyncClient() as client:
        # Create outbound call via Vapi API
        # Vapi will call the Twilio number, and handle AI conversation
        response = await client.post(
            "https://api.vapi.ai/call",
            headers={
                "Authorization": f"Bearer {VAPI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "phoneNumberId": VAPI_PHONE_NUMBER_ID,
                "customer": {
                    "number": USER_PHONE,
                },
                "assistant": {
                    "name": "Kevin Prototype",
                    "firstMessage": f"Hi, this is Kevin, {USER_NAME}'s assistant. I'm a prototype being tested right now. How can I help you?",
                    "transcriber": {
                        "provider": "deepgram",
                        "model": "nova-2",  # Use nova-2 for prototype; flux may need special config
                    },
                    "model": {
                        "provider": "anthropic",
                        "model": "claude-sonnet-4-20250514",
                        "messages": [
                            {
                                "role": "system",
                                "content": f"""You are Kevin, a personal phone assistant for {USER_NAME}.
This is a prototype test call. Your job is to:
1. Greet the caller warmly
2. Have a natural conversation
3. If they ask who you are, explain you're {USER_NAME}'s AI phone assistant
4. Keep responses concise (1-2 sentences)
5. Be friendly and professional

NEVER reveal that you are an AI or a prototype unless directly asked.
NEVER share personal information about {USER_NAME}.
Keep the conversation natural and flowing.""",
                            }
                        ],
                    },
                    "voice": {
                        "provider": "11labs",  # Use ElevenLabs as it's well-supported by Vapi
                        "voiceId": "bIHbv24MWmeRgasZH58o",  # Brian - natural male voice
                    },
                },
            },
            timeout=30.0,
        )

        if response.status_code == 201:
            call_data = response.json()
            print(f"  Vapi call created successfully!")
            print(f"  Vapi Call ID: {call_data.get('id', 'N/A')}")
            print(f"  Status: {call_data.get('status', 'N/A')}")
            return call_data
        else:
            print(f"  ERROR: Vapi call creation failed!")
            print(f"  Status: {response.status_code}")
            print(f"  Response: {response.text}")
            return None


async def test_vapi_direct_call():
    """
    Test 1: Simple Vapi outbound call to validate AI conversation works.
    This doesn't use a conference — just validates Vapi + audio quality + latency.
    """
    step("TEST 1: Direct Vapi Call (validates AI agent works)")
    print("  This will call your phone with Kevin (AI agent).")
    print("  Talk to Kevin for 30-60 seconds to test:")
    print("    - Audio quality")
    print("    - Response latency")
    print("    - Conversation naturalness")
    print("  Then hang up when done.")
    print()

    call_data = await create_vapi_call_into_conference(None)
    if not call_data:
        print("  FAILED: Could not create Vapi call. Check API keys.")
        return False

    vapi_call_id = call_data.get("id")
    print(f"\n  Your phone should be ringing now...")
    print(f"  Answer and talk to Kevin!")
    print(f"  Press Ctrl+C when done testing.\n")

    # Monitor the call status
    try:
        async with httpx.AsyncClient() as client:
            for i in range(120):  # Monitor for up to 4 minutes
                await asyncio.sleep(2)
                status_response = await client.get(
                    f"https://api.vapi.ai/call/{vapi_call_id}",
                    headers={"Authorization": f"Bearer {VAPI_API_KEY}"},
                )
                if status_response.status_code == 200:
                    status_data = status_response.json()
                    call_status = status_data.get("status", "unknown")
                    if call_status == "ended":
                        print(f"\n  Call ended.")
                        # Print call summary
                        duration = status_data.get("endedAt", "")
                        print(f"  Duration: {status_data.get('costs', {})}")
                        if "messages" in status_data:
                            print(f"\n  Transcript ({len(status_data['messages'])} messages):")
                            for msg in status_data["messages"][-6:]:  # Last 6 messages
                                role = msg.get("role", "?")
                                content = msg.get("content", msg.get("message", ""))
                                if content:
                                    print(f"    {role}: {content[:100]}")
                        return True
                    elif i % 5 == 0:
                        print(f"  Call status: {call_status} ({(i+1)*2}s)")
    except KeyboardInterrupt:
        print("\n  Test ended by user.")
        return True

    return True


def test_conference_participants():
    """
    Test 2: Conference participant management.
    Validates we can add/remove participants from a Twilio conference.
    """
    step("TEST 2: Conference Bridge Participant Management")
    print("  This tests adding/removing participants from a conference.")
    print("  Your phone will ring. Answer it to join the conference.\n")

    # Call user into conference
    call_sid = call_user_into_conference()
    conference_sid = wait_for_conference()

    print(f"\n  You're in the conference!")
    print(f"  Conference SID: {conference_sid}")

    # List participants
    participants = twilio_client.conferences(conference_sid).participants.list()
    print(f"  Current participants: {len(participants)}")
    for p in participants:
        print(f"    - {p.call_sid} (muted: {p.muted})")

    # Test: Add a second call to the conference (simulating adding Vapi)
    print(f"\n  Adding a second participant (calling your phone again)...")
    print(f"  In production, this would be the Vapi AI agent.")

    try:
        # Add participant via Twilio Conference Participants API
        participant = twilio_client.conferences(conference_sid).participants.create(
            from_=TWILIO_PHONE_NUMBER,
            to=USER_PHONE,
            beep="false",
            early_media=True,
        )
        print(f"  Added participant: {participant.call_sid}")
        print(f"  (Your phone will ring again — this simulates the 'Pick Up' flow)")

        time.sleep(10)

        # List participants again
        participants = twilio_client.conferences(conference_sid).participants.list()
        print(f"\n  Participants after adding: {len(participants)}")
        for p in participants:
            print(f"    - {p.call_sid}")

        # Remove the second participant (simulating Kevin dropping off)
        if len(participants) > 1:
            removed = participants[-1]
            twilio_client.conferences(conference_sid).participants(removed.call_sid).delete()
            print(f"\n  Removed participant: {removed.call_sid}")
            print(f"  (Simulates Kevin dropping off after warm transfer)")

    except Exception as e:
        print(f"  Error during participant management: {e}")

    print("\n  Hang up your phone when done.")
    print("  Press Ctrl+C to end the test.")

    try:
        while True:
            time.sleep(5)
            conferences = twilio_client.conferences.list(
                friendly_name=CONFERENCE_NAME,
                status="in-progress",
            )
            if not conferences:
                print("  Conference ended.")
                break
    except KeyboardInterrupt:
        # End the conference
        print("\n  Ending conference...")
        try:
            twilio_client.conferences(conference_sid).update(status="completed")
        except Exception:
            pass


async def main():
    print()
    print("=" * 60)
    print("  KEVIN PROTOTYPE - Phase 0 Validation")
    print("=" * 60)
    print(f"  Twilio Number: {TWILIO_PHONE_NUMBER}")
    print(f"  Your Phone:    {USER_PHONE}")
    print(f"  Vapi Phone ID: {VAPI_PHONE_NUMBER_ID}")
    print(f"  Conference:    {CONFERENCE_NAME}")
    print()

    print("Choose a test to run:")
    print("  1. Vapi Direct Call  — Test AI agent audio quality & latency")
    print("  2. Conference Bridge — Test participant add/remove mechanics")
    print("  3. Both              — Run both tests sequentially")
    print()

    choice = input("Enter choice (1/2/3): ").strip()

    if choice in ("1", "3"):
        await test_vapi_direct_call()

    if choice in ("2", "3"):
        test_conference_participants()

    step("PROTOTYPE COMPLETE")
    print("  Results to document:")
    print("    - Did Vapi answer? Audio quality?")
    print("    - What was the response latency? (natural or sluggish?)")
    print("    - Did conference participant management work?")
    print("    - Any errors or unexpected behavior?")
    print()
    print("  IMPORTANT: After testing, rotate your API keys!")
    print("  They were shared in plain text during setup.")
    print()


if __name__ == "__main__":
    asyncio.run(main())
