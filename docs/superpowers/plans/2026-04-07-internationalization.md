# Kevin AI Internationalization — Implementation Plan (Revised)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Kevin AI work in 9 countries (US, CA, BR, GB, DE, FR, IT, ES, PT) with local phone numbers, per-language Gemini voices, regional dial-in numbers, and call forwarding instructions.

**Architecture:** Add `country_code` and business address fields to the contractor model. Make phone provisioning country-aware with Twilio regulatory bundles for EU/BR. Replace single dial-in number with a per-country mapping. Replace 2-voice selection with a language-to-voice dict. Add a forwarding instructions API endpoint.

**Tech Stack:** Twilio (number provisioning, regulatory bundles), `phonenumbers` (country detection), Gemini Live API (per-language voices), FastAPI, Firestore.

**Spec:** `docs/superpowers/specs/2026-04-07-internationalization-design.md`
**Review:** `docs/superpowers/plans/2026-04-07-internationalization-REVIEW.md`

**Key files in current codebase:**
- `app/config.py` — settings, `dial_in_number` at line 53
- `app/db/contractors.py` — contractor CRUD, `provision_twilio_number` at line 149
- `app/api/contractors.py` — contractor API, `ContractorCreate` at line 44, `provision-number` at line 176
- `app/api/voip.py` — VoIP API
- `app/api/settings.py` — settings API
- `app/services/gemini_pipeline.py` — voice selection at lines 22-24, 110-112
- `app/services/warm_transfer.py` — uses `settings.dial_in_number` at line 69
- `app/services/state_machine.py` — `ActiveCall` class (has `contractor_id`, NOT `contractor_config`)
- `app/utils/phone.py` — `normalize_phone()` already accepts `default_region`

---

### Task 1: Add Country Fields to Contractor Model

**Files:**
- Modify: `app/db/contractors.py`
- Modify: `app/api/contractors.py`

- [ ] **Step 1: Add supported countries constant and country detection helper**

Add at the top of `app/db/contractors.py` (after imports):

```python
# Supported countries for Kevin AI
SUPPORTED_COUNTRIES = {"US", "CA", "BR", "GB", "DE", "FR", "IT", "ES", "PT"}

# Countries that require Twilio regulatory bundles for number provisioning
REGULATORY_COUNTRIES = {"DE", "FR", "IT", "ES", "PT", "BR"}

# Country code to full name mapping
COUNTRY_NAMES = {
    "US": "United States",
    "CA": "Canada",
    "BR": "Brazil",
    "GB": "United Kingdom",
    "DE": "Germany",
    "FR": "France",
    "IT": "Italy",
    "ES": "Spain",
    "PT": "Portugal",
}


def detect_country_from_phone(phone: str) -> str:
    """Detect ISO 3166-1 alpha-2 country code from a phone number. Defaults to 'US'."""
    import phonenumbers
    try:
        parsed = phonenumbers.parse(phone, None)
        region = phonenumbers.region_code_for_number(parsed)
        if region and region in SUPPORTED_COUNTRIES:
            return region
    except phonenumbers.NumberParseException:
        pass
    return "US"
```

- [ ] **Step 2: Add country defaults to create_contractor**

In `app/db/contractors.py`, in the `create_contractor` function, add defaults after the existing `data.setdefault("voice_engine", "elevenlabs")` line:

```python
    data.setdefault("voice_engine", "elevenlabs")
    data.setdefault("country_code", "US")
    data.setdefault("business_address", "")
    data.setdefault("business_city", "")
    data.setdefault("business_country_name", "")
```

- [ ] **Step 3: Add country fields to ContractorCreate model with validation**

In `app/api/contractors.py`, add these fields to the `ContractorCreate` class and add a validator:

```python
from pydantic import BaseModel, Field, field_validator
```

Add fields to `ContractorCreate`:

```python
    # International fields
    country_code: str = Field(default="", max_length=2)
    business_address: str = Field(default="", max_length=500)
    business_city: str = Field(default="", max_length=100)
    business_country_name: str = Field(default="", max_length=100)

    @field_validator("country_code")
    @classmethod
    def validate_country_code(cls, v):
        if v and v.upper() not in {"US", "CA", "BR", "GB", "DE", "FR", "IT", "ES", "PT", ""}:
            raise ValueError(f"Unsupported country code: {v}")
        return v.upper() if v else v
```

- [ ] **Step 4: Auto-detect country in api_create_contractor**

In `app/api/contractors.py`, in the `api_create_contractor` function, add country detection after `data = body.dict()`:

```python
    data = body.dict()
    # Auto-detect country from phone if not explicitly provided
    if not data.get("country_code") and data.get("owner_phone"):
        from app.db.contractors import detect_country_from_phone
        data["country_code"] = detect_country_from_phone(data["owner_phone"])
```

- [ ] **Step 5: Add country_code to ContractorUpdate model with same validator**

In `app/api/contractors.py`, add to the `ContractorUpdate` class:

```python
    country_code: Optional[str] = Field(default=None, max_length=2)
    business_address: Optional[str] = Field(default=None, max_length=500)
    business_city: Optional[str] = Field(default=None, max_length=100)
    business_country_name: Optional[str] = Field(default=None, max_length=100)

    @field_validator("country_code")
    @classmethod
    def validate_country_code(cls, v):
        if v is not None and v and v.upper() not in {"US", "CA", "BR", "GB", "DE", "FR", "IT", "ES", "PT"}:
            raise ValueError(f"Unsupported country code: {v}")
        return v.upper() if v else v
```

- [ ] **Step 6: Verify syntax**

```bash
cd "/Volumes/Extreme Pro/myprojects/Kevin"
python3 -m py_compile app/db/contractors.py
python3 -m py_compile app/api/contractors.py
echo "Compile OK"
```

- [ ] **Step 7: Commit**

```bash
git add app/db/contractors.py app/api/contractors.py
git commit -m "feat: add country_code and business address fields to contractor model"
```

---

### Task 2: Country-Aware Number Provisioning (Async)

**Files:**
- Modify: `app/db/contractors.py` (rewrite `provision_twilio_number`, add regulatory helper)
- Modify: `app/api/contractors.py` (update provision endpoint)

**Review fixes addressed:** CRITICAL-2 (async provisioning), CRITICAL-3 (sms_enabled), CRITICAL-4 (regulation_sid lookup), IMPORTANT-4 (bundle_sid in search), IMPORTANT-5 (error message sanitization).

- [ ] **Step 1: Add the regulatory bundle helper**

Add this function in `app/db/contractors.py` (before `provision_twilio_number`):

```python
async def _create_regulatory_bundle(client, loop, country_code: str, business_name: str, address: str, city: str) -> str:
    """Create a Twilio regulatory bundle for EU/BR number provisioning.

    Returns the bundle SID. Raises if the bundle cannot be created or approved.
    """
    country_name = COUNTRY_NAMES.get(country_code, "")

    # Look up the regulation SID for this country + number type
    regulations = await loop.run_in_executor(
        None,
        lambda: client.numbers.v2.regulatory_compliance.regulations.list(
            iso_country=country_code, number_type="local", limit=1
        )
    )
    if not regulations:
        raise Exception(f"No Twilio regulations found for {country_name} local numbers")
    regulation_sid = regulations[0].sid

    # Create an address in Twilio
    twilio_address = await loop.run_in_executor(
        None,
        lambda: client.addresses.create(
            friendly_name=f"{business_name} - {city}",
            street=address,
            city=city,
            region="",
            postal_code="",
            iso_country=country_code,
            customer_name=business_name,
        )
    )

    # Create a regulatory bundle
    bundle = await loop.run_in_executor(
        None,
        lambda: client.numbers.v2.regulatory_compliance.bundles.create(
            friendly_name=f"{business_name} - {country_name} number",
            regulation_sid=regulation_sid,
            iso_country=country_code,
            number_type="local",
        )
    )

    # Attach the address as a supporting document
    await loop.run_in_executor(
        None,
        lambda: client.numbers.v2.regulatory_compliance.bundles(bundle.sid)
        .item_assignments.create(object_sid=twilio_address.sid)
    )

    # Submit the bundle for review
    await loop.run_in_executor(
        None,
        lambda: client.numbers.v2.regulatory_compliance.bundles(bundle.sid)
        .update(status="pending-review")
    )

    # Poll for approval (usually instant, max 30 seconds)
    # If not approved in 30s, return the bundle SID anyway — caller handles pending state
    for _ in range(15):
        await asyncio.sleep(2)
        updated = await loop.run_in_executor(
            None,
            lambda: client.numbers.v2.regulatory_compliance.bundles(bundle.sid).fetch()
        )
        if updated.status == "twilio-approved":
            logger.info(f"Regulatory bundle approved: {bundle.sid} ({country_code})")
            return bundle.sid
        if updated.status == "provisionally-approved":
            logger.info(f"Regulatory bundle provisionally approved: {bundle.sid}")
            return bundle.sid
        if updated.status == "twilio-rejected":
            raise Exception(f"Regulatory bundle rejected for {country_name}. Please verify your business address.")

    # Bundle still pending after 30s — store it for async completion
    logger.info(f"Regulatory bundle pending: {bundle.sid} ({country_code})")
    return bundle.sid  # Try to provision anyway — Twilio may accept provisionally
```

- [ ] **Step 2: Rewrite provision_twilio_number for country support**

Replace the entire `provision_twilio_number` function in `app/db/contractors.py`:

```python
async def provision_twilio_number(contractor_id: str, country_code: str = "US", area_code: str = "") -> str:
    """Buy a Twilio phone number in the contractor's country and assign it.

    For EU/BR countries, creates a regulatory bundle first using the contractor's
    business address. Returns the provisioned phone number (E.164 format).
    """
    from twilio.rest import Client
    from app.config import settings

    if country_code not in COUNTRY_NAMES:
        raise Exception(f"Unsupported country: {country_code}")

    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    loop = asyncio.get_event_loop()

    # For regulatory countries, create a bundle first
    bundle_sid = None
    if country_code in REGULATORY_COUNTRIES:
        contractor = await get_contractor(contractor_id)
        if not contractor:
            raise Exception("Contractor not found")
        business_address = contractor.get("business_address", "")
        business_city = contractor.get("business_city", "")
        business_name = contractor.get("business_name", "")
        if not business_address or not business_city:
            raise Exception("Business address and city required for number provisioning in this country")

        bundle_sid = await _create_regulatory_bundle(
            client, loop, country_code, business_name, business_address, business_city
        )

    # Search for available numbers
    # Note: sms_enabled only for US/CA — EU/BR local numbers often don't support SMS
    search_params = {"voice_enabled": True}
    if country_code in ("US", "CA"):
        search_params["sms_enabled"] = True
    if area_code:
        search_params["area_code"] = area_code

    numbers = await loop.run_in_executor(
        None,
        lambda: client.available_phone_numbers(country_code).local.list(**search_params, limit=1)
    )

    if not numbers and area_code:
        # Retry without area code
        search_params.pop("area_code", None)
        numbers = await loop.run_in_executor(
            None,
            lambda: client.available_phone_numbers(country_code).local.list(**search_params, limit=1)
        )

    if not numbers:
        raise Exception(f"No phone numbers available in {COUNTRY_NAMES.get(country_code, country_code)}")

    # Buy the number (bundle_sid goes here, NOT in search)
    webhook_url = f"{settings.cloud_run_url}/webhooks/twilio/incoming"
    status_url = f"{settings.cloud_run_url}/webhooks/twilio/status"

    purchase_params = {
        "phone_number": numbers[0].phone_number,
        "voice_url": webhook_url,
        "voice_method": "POST",
        "status_callback": status_url,
        "status_callback_method": "POST",
        "sms_url": f"{settings.cloud_run_url}/webhooks/twilio/mms-incoming",
        "sms_method": "POST",
    }
    if bundle_sid:
        purchase_params["bundle_sid"] = bundle_sid

    purchased = await loop.run_in_executor(
        None,
        lambda: client.incoming_phone_numbers.create(**purchase_params)
    )

    # Update contractor profile with the number
    await update_contractor(contractor_id, {"twilio_number": purchased.phone_number})

    logger.info(f"Provisioned {redact_phone(purchased.phone_number)} ({country_code}) for contractor {contractor_id}")
    return purchased.phone_number
```

- [ ] **Step 3: Update the provision-number API endpoint (async-safe, sanitized errors)**

In `app/api/contractors.py`, replace the `api_provision_number` endpoint:

```python
@router.post("/{contractor_id}/provision-number")
async def api_provision_number(contractor_id: str, request: Request):
    """Provision a Twilio number for a contractor."""
    require_contractor_access(request, contractor_id)
    from app.db.contractors import provision_twilio_number, REGULATORY_COUNTRIES

    # Get contractor's country_code
    contractor = await get_contractor(contractor_id)
    if not contractor:
        return {"status": "error", "message": "Contractor not found"}
    country_code = contractor.get("country_code", "US")

    # For regulatory countries, validate address is provided
    if country_code in REGULATORY_COUNTRIES:
        if not contractor.get("business_address") or not contractor.get("business_city"):
            return {"status": "error", "message": "Business address and city are required for number provisioning in your country. Please update your profile."}

    try:
        number = await provision_twilio_number(contractor_id, country_code=country_code)
        return {"status": "ok", "phone_number": number}
    except Exception as e:
        logger.error(f"Number provisioning failed for {contractor_id}: {e}", exc_info=True)
        # Return sanitized error messages — don't expose internal details
        error_msg = str(e)
        if "address" in error_msg.lower() or "rejected" in error_msg.lower():
            return {"status": "error", "message": "Address verification failed. Please check your business address."}
        if "no phone numbers" in error_msg.lower() or "no twilio" in error_msg.lower():
            return {"status": "error", "message": "No phone numbers available in your area. Please try a different city."}
        if "unsupported" in error_msg.lower():
            return {"status": "error", "message": "Your country is not yet supported for number provisioning."}
        return {"status": "error", "message": "Failed to provision phone number. Please try again or contact support."}
```

- [ ] **Step 4: Verify syntax**

```bash
cd "/Volumes/Extreme Pro/myprojects/Kevin"
python3 -m py_compile app/db/contractors.py
python3 -m py_compile app/api/contractors.py
echo "Compile OK"
```

- [ ] **Step 5: Commit**

```bash
git add app/db/contractors.py app/api/contractors.py
git commit -m "feat: country-aware number provisioning with Twilio regulatory bundles"
```

---

### Task 3: Regional Dial-In Numbers

**Files:**
- Modify: `app/config.py`
- Modify: `app/services/warm_transfer.py`
- Modify: `app/webhooks/vapi_events.py`

**Review fixes addressed:** CRITICAL-1 (no @property on BaseSettings), IMPORTANT-2 (warm_transfer contractor lookup), IMPORTANT-3 (voip.py is a no-op — removed), IMPORTANT-6 (backward compat).

- [ ] **Step 1: Add dial_in_numbers config and helper function**

In `app/config.py`, keep the existing `dial_in_number` for backward compat and add the new field. After line 53:

```python
    # Dial-in number (DEPRECATED — use dial_in_numbers instead)
    dial_in_number: str = "+16504222696"

    # Regional dial-in numbers (JSON string, parsed by get_dial_in_number helper)
    # One per supported country. Provision new numbers as countries are added.
    dial_in_numbers: str = ""
```

At the bottom of the file, after `settings = get_settings()`, add:

```python
import json as _json
_dial_in_cache: dict | None = None

def get_dial_in_number(country_code: str = "US") -> str:
    """Get the dial-in number for a country, falling back to US, then legacy field."""
    global _dial_in_cache
    if _dial_in_cache is None:
        try:
            _dial_in_cache = _json.loads(settings.dial_in_numbers) if settings.dial_in_numbers else {}
        except (_json.JSONDecodeError, TypeError):
            logger.warning("Failed to parse dial_in_numbers config — using legacy dial_in_number")
            _dial_in_cache = {}
    if _dial_in_cache:
        return _dial_in_cache.get(country_code, _dial_in_cache.get("US", settings.dial_in_number))
    return settings.dial_in_number
```

Also add logger import if not already present at top of config.py. Actually, config.py doesn't have a logger — keep it simple without logging:

```python
import json as _json
_dial_in_cache: dict | None = None

def get_dial_in_number(country_code: str = "US") -> str:
    """Get the dial-in number for a country, falling back to US, then legacy field."""
    global _dial_in_cache
    if _dial_in_cache is None:
        try:
            _dial_in_cache = _json.loads(settings.dial_in_numbers) if settings.dial_in_numbers else {}
        except (_json.JSONDecodeError, TypeError):
            _dial_in_cache = {}
    if _dial_in_cache:
        return _dial_in_cache.get(country_code, _dial_in_cache.get("US", settings.dial_in_number))
    return settings.dial_in_number
```

- [ ] **Step 2: Update warm_transfer.py (look up contractor from Firestore)**

In `app/services/warm_transfer.py`, add import at top:

```python
from app.config import settings, get_dial_in_number
from app.db.contractors import get_contractor
```

Replace line 69 (`conference_number=settings.dial_in_number,`) with:

```python
            # Look up contractor's country for regional dial-in
            _contractor = await get_contractor(active_call.contractor_id) if active_call.contractor_id else None
            _country = _contractor.get("country_code", "US") if _contractor else "US"
```

And update the `send_dial_in_message` call:

```python
            conference_number=get_dial_in_number(_country),
```

The full block around line 66-73 becomes:

```python
        # Look up contractor's country for regional dial-in
        _contractor = await get_contractor(active_call.contractor_id) if active_call.contractor_id else None
        _country = _contractor.get("country_code", "US") if _contractor else "US"

        # Send user the dial-in number + PIN via Telegram
        await send_dial_in_message(
            chat_id=settings.telegram_chat_id,
            conference_number=get_dial_in_number(_country),
            pin=pin,
            caller_phone=active_call.caller_phone,
            caller_name=active_call.caller_name,
        )
```

- [ ] **Step 3: Update vapi_events.py**

In `app/webhooks/vapi_events.py`, add import:

```python
from app.config import get_dial_in_number
```

Replace line 96:

```python
        "forwardingPhoneNumber": settings.dial_in_number,
```

with:

```python
        "forwardingPhoneNumber": get_dial_in_number("US"),  # Vapi is US-only legacy
```

- [ ] **Step 4: Verify syntax**

```bash
cd "/Volumes/Extreme Pro/myprojects/Kevin"
python3 -m py_compile app/config.py
python3 -m py_compile app/services/warm_transfer.py
python3 -m py_compile app/webhooks/vapi_events.py
echo "Compile OK"
```

- [ ] **Step 5: Commit**

```bash
git add app/config.py app/services/warm_transfer.py app/webhooks/vapi_events.py
git commit -m "feat: regional dial-in numbers (per-country mapping, backward compat)"
```

---

### Task 4: Per-Language Gemini Voice Selection

**Files:**
- Modify: `app/services/gemini_pipeline.py`

- [ ] **Step 1: Replace voice constants with language mapping**

In `app/services/gemini_pipeline.py`, replace the voice constants (lines 22-24):

```python
# Gemini voice options — male voices sound best per benchmarks
GEMINI_VOICE_DEFAULT = "Puck"       # Male, warm, American
GEMINI_VOICE_SPANISH = "Orus"       # Male, multilingual
```

with:

```python
# Per-language Gemini voice selection (male voices for Kevin persona)
GEMINI_VOICES = {
    "en": "Puck",      # Warm, upbeat American — best-tested English voice
    "pt": "Orus",      # Authoritative, clear — suits Brazilian Portuguese formality
    "de": "Charon",    # Calm, professional — matches German business tone
    "fr": "Puck",      # Adapts well to French prosody
    "it": "Puck",      # Expressive, warm — suits Italian's melodic cadence
    "es": "Charon",    # Clear, composed — suits Castilian Spanish
}
GEMINI_VOICE_DEFAULT = "Puck"
```

- [ ] **Step 2: Update voice selection logic**

In `app/services/gemini_pipeline.py`, replace the voice selection block:

```python
        # Voice selection
        user_language = self._contractor_config.get("user_language", "en")
        self._voice = GEMINI_VOICE_SPANISH if (user_language and user_language != "en") else GEMINI_VOICE_DEFAULT
```

with:

```python
        # Voice selection — pick the best voice for the contractor's language
        user_language = self._contractor_config.get("user_language", "en")
        self._voice = GEMINI_VOICES.get(user_language, GEMINI_VOICE_DEFAULT)
```

- [ ] **Step 3: Verify syntax**

```bash
cd "/Volumes/Extreme Pro/myprojects/Kevin"
python3 -m py_compile app/services/gemini_pipeline.py
echo "Compile OK"
```

- [ ] **Step 4: Commit**

```bash
git add app/services/gemini_pipeline.py
git commit -m "feat: per-language Gemini voice selection (6 languages)"
```

---

### Task 5: Call Forwarding Instructions API

**Files:**
- Create: `app/api/forwarding.py`
- Modify: `app/main.py` (register router)

**Review fix addressed:** IMPORTANT-1 (file path — consistently using `app/api/forwarding.py`), IMPORTANT-7 (auth).

- [ ] **Step 1: Create the forwarding instructions module at `app/api/forwarding.py`**

```python
"""Per-country call forwarding instructions served to the iOS app during onboarding."""

from fastapi import APIRouter, Depends, Query

from app.middleware.auth import verify_api_token

router = APIRouter(prefix="/api", dependencies=[Depends(verify_api_token)])

# Standard GSM/carrier forwarding codes per country.
FORWARDING_CODES = {
    "US": {
        "forward_all": "*72{number}",
        "forward_unanswered": "*71{number}",
        "disable": "*73",
        "notes": "Works on all major US carriers (AT&T, Verizon, T-Mobile).",
        "recommended": "forward_unanswered",
    },
    "CA": {
        "forward_all": "*72{number}",
        "forward_unanswered": "*71{number}",
        "disable": "*73",
        "notes": "Works on all major Canadian carriers (Bell, Rogers, Telus).",
        "recommended": "forward_unanswered",
    },
    "BR": {
        "forward_all": "**21*{number}#",
        "forward_unanswered": "**61*{number}#",
        "disable": "##21#",
        "notes": "Standard GSM codes. Works on Vivo, Claro, TIM, Oi.",
        "recommended": "forward_unanswered",
    },
    "GB": {
        "forward_all": "**21*{number}#",
        "forward_unanswered": "**61*{number}#",
        "disable": "##21#",
        "notes": "Standard GSM codes. Works on EE, Vodafone, Three, O2.",
        "recommended": "forward_unanswered",
    },
    "DE": {
        "forward_all": "**21*{number}#",
        "forward_unanswered": "**61*{number}#",
        "disable": "##21#",
        "notes": "Standard GSM codes. Works on Telekom, Vodafone, O2.",
        "recommended": "forward_unanswered",
    },
    "FR": {
        "forward_all": "**21*{number}#",
        "forward_unanswered": "**61*{number}#",
        "disable": "##21#",
        "notes": "Standard GSM codes. Works on Orange, SFR, Bouygues, Free.",
        "recommended": "forward_unanswered",
    },
    "IT": {
        "forward_all": "**21*{number}#",
        "forward_unanswered": "**61*{number}#",
        "disable": "##21#",
        "notes": "Standard GSM codes. Works on TIM, Vodafone, WindTre, Iliad.",
        "recommended": "forward_unanswered",
    },
    "ES": {
        "forward_all": "**21*{number}#",
        "forward_unanswered": "**61*{number}#",
        "disable": "##21#",
        "notes": "Standard GSM codes. Works on Movistar, Vodafone, Orange.",
        "recommended": "forward_unanswered",
    },
    "PT": {
        "forward_all": "**21*{number}#",
        "forward_unanswered": "**61*{number}#",
        "disable": "##21#",
        "notes": "Standard GSM codes. Works on MEO, NOS, Vodafone.",
        "recommended": "forward_unanswered",
    },
}

FALLBACK_MESSAGE = (
    "If these codes don't work with your carrier, "
    "search for 'call forwarding' in your carrier's app or contact their support."
)


@router.get("/forwarding-instructions")
async def get_forwarding_instructions(country_code: str = Query("US", max_length=2)):
    """Return call forwarding instructions for a country."""
    instructions = FORWARDING_CODES.get(country_code.upper())
    if not instructions:
        return {
            "supported": False,
            "country_code": country_code.upper(),
            "message": f"Call forwarding instructions not available for {country_code}. {FALLBACK_MESSAGE}",
        }
    return {
        "supported": True,
        "country_code": country_code.upper(),
        **instructions,
        "fallback_message": FALLBACK_MESSAGE,
    }
```

- [ ] **Step 2: Register the router in main.py**

In `app/main.py`, add with the other router imports and registrations:

```python
from app.api.forwarding import router as forwarding_router
app.include_router(forwarding_router)
```

- [ ] **Step 3: Verify syntax**

```bash
cd "/Volumes/Extreme Pro/myprojects/Kevin"
python3 -m py_compile app/api/forwarding.py
echo "Compile OK"
```

- [ ] **Step 4: Commit**

```bash
git add app/api/forwarding.py app/main.py
git commit -m "feat: add call forwarding instructions API endpoint (9 countries)"
```

---

### Task 6: Update Phone Normalization

**Files:**
- Modify: `app/db/contractors.py`

- [ ] **Step 1: Update get_contractor_by_owner_phone**

In `app/db/contractors.py`, update the `get_contractor_by_owner_phone` function's normalization:

Replace:

```python
    from app.utils.phone import normalize_phone
    normalized = normalize_phone(owner_phone)
```

with:

```python
    from app.utils.phone import normalize_phone
    # Try parsing as E.164 first (no region needed), fall back to US
    normalized = normalize_phone(owner_phone, default_region=None)
    if not normalized:
        normalized = normalize_phone(owner_phone, default_region="US")
```

Note: Pass `None` (not empty string) — the `phonenumbers` library expects `None` to skip region-based parsing.

- [ ] **Step 2: Verify the normalize_phone function handles None**

Check that `phonenumbers.parse(number, None)` works — it does, this is the documented way to parse E.164 numbers without a default region.

- [ ] **Step 3: Verify syntax**

```bash
cd "/Volumes/Extreme Pro/myprojects/Kevin"
python3 -m py_compile app/db/contractors.py
echo "Compile OK"
```

- [ ] **Step 4: Commit**

```bash
git add app/db/contractors.py
git commit -m "fix: improve phone normalization to handle international numbers"
```

---

### Task 7: Add country_code to Settings API

**Files:**
- Modify: `app/api/settings.py`

**Review fix addressed:** CRITICAL-5 (validate country_code against allowlist).

- [ ] **Step 1: Add country_code to SettingsUpdate model**

In `app/api/settings.py`, add to the `SettingsUpdate` class:

```python
    country_code: Optional[str] = None
```

- [ ] **Step 2: Handle country_code in the update handler with validation**

In the `api_update_settings` function, add handling for `country_code` alongside the existing `voice_engine` handling:

```python
    # country_code lives on the main contractor document
    if "country_code" in updates:
        cc = updates.pop("country_code")
        supported = {"US", "CA", "BR", "GB", "DE", "FR", "IT", "ES", "PT"}
        if cc and cc.upper() in supported:
            try:
                db = get_firestore_client()
                db.collection("contractors").document(contractor_id).update({"country_code": cc.upper()})
            except Exception as e:
                logger.error(f"country_code update failed for {contractor_id}: {e}", exc_info=True)
                return {"error": "Failed to save country_code"}
        elif cc:
            return {"error": f"Unsupported country code: {cc}"}
```

- [ ] **Step 3: Verify syntax**

```bash
cd "/Volumes/Extreme Pro/myprojects/Kevin"
python3 -m py_compile app/api/settings.py
echo "Compile OK"
```

- [ ] **Step 4: Commit**

```bash
git add app/api/settings.py
git commit -m "feat: expose country_code in settings API with validation"
```

---

### Task 8: Deploy and Test

**Files:**
- No code changes — deploy + manual verification

- [ ] **Step 1: Deploy the backend**

```bash
cd "/Volumes/Extreme Pro/myprojects/Kevin"
gcloud run deploy kevin-api --source . --project kevin-491315 --region us-central1 --allow-unauthenticated --account=deli@ellaexecutivesearch.com
```

- [ ] **Step 2: Test country detection**

```bash
cd "/Volumes/Extreme Pro/myprojects/Kevin"
python3 -c "
from app.db.contractors import detect_country_from_phone
tests = [
    ('+14155551234', 'US'),
    ('+442071234567', 'GB'),
    ('+5511987654321', 'BR'),
    ('+4930123456', 'DE'),
    ('+33123456789', 'FR'),
    ('+3902123456', 'IT'),
    ('+34612345678', 'ES'),
    ('+351211234567', 'PT'),
    ('+16131234567', 'CA'),
]
for phone, expected in tests:
    result = detect_country_from_phone(phone)
    status = 'OK' if result == expected else f'FAIL (got {result})'
    print(f'{phone} -> {result} {status}')
"
```

- [ ] **Step 3: Test forwarding instructions API**

```bash
curl -s -H "Authorization: Bearer <token>" "https://kevin-api-752910912062.us-central1.run.app/api/forwarding-instructions?country_code=DE" | python3 -m json.tool
curl -s -H "Authorization: Bearer <token>" "https://kevin-api-752910912062.us-central1.run.app/api/forwarding-instructions?country_code=BR" | python3 -m json.tool
```

- [ ] **Step 4: Test Gemini voice selection**

Set test contractor's `user_language` to "de", make a test call, check logs:

```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="kevin-api" AND jsonPayload.message=~"Gemini Live session established"' --project kevin-491315 --limit=5 --format='value(timestamp, jsonPayload.message)' --freshness=10m --account=deli@ellaexecutivesearch.com
```

Expected: `voice=Charon` in log output.

- [ ] **Step 5: Verify existing US flow still works**

Make a test call to the existing US number to confirm no regression. All defaults are "US" so existing contractors should work identically.
