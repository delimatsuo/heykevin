"""Admin Twilio number inventory helpers."""


def reconcile_number_inventory(contractors: list[dict], twilio_numbers: list[dict]) -> dict:
    contractor_by_number = {
        c.get("twilio_number"): c
        for c in contractors
        if c.get("active") and c.get("twilio_number")
    }
    twilio_by_number = {
        n.get("phone_number"): n
        for n in twilio_numbers
        if n.get("phone_number")
    }

    missing = [
        c for c in contractors
        if c.get("active") and not c.get("twilio_number")
    ]
    orphan = [
        n for number, n in twilio_by_number.items()
        if number not in contractor_by_number
    ]
    assigned = [
        {
            "phone_number": number,
            "contractor_id": contractor.get("contractor_id"),
            "twilio": twilio_by_number.get(number, {}),
        }
        for number, contractor in contractor_by_number.items()
        if number in twilio_by_number
    ]

    return {
        "summary": {
            "assigned_numbers": len(assigned),
            "contractors_missing_numbers": len(missing),
            "orphan_numbers": len(orphan),
        },
        "assigned_numbers": assigned,
        "contractors_missing_numbers": missing,
        "orphan_numbers": orphan,
    }


def fetch_twilio_incoming_numbers(limit: int = 1000) -> list[dict]:
    from twilio.rest import Client

    from app.config import settings

    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    numbers = client.incoming_phone_numbers.list(limit=limit)
    return [
        {
            "phone_number": str(number.phone_number),
            "sid": number.sid,
            "friendly_name": getattr(number, "friendly_name", ""),
            "voice_url": getattr(number, "voice_url", ""),
            "sms_url": getattr(number, "sms_url", ""),
        }
        for number in numbers
    ]
