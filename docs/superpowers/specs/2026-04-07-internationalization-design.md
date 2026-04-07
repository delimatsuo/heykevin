# Kevin AI Internationalization — Design Spec

**Date:** 2026-04-07
**Status:** Approved for implementation

## Context

Kevin AI currently only provisions US Twilio numbers, hardcodes US phone normalization, uses a single US dial-in number for call pickup, and offers only 2 voice options. This limits the product to US-based contractors.

This spec prepares Kevin to operate in 9 countries: US, Canada, Brazil, UK, Germany, France, Italy, Spain, Portugal — covering 7 languages (EN, PT, DE, FR, IT, ES) across 3 regions (North America, South America, Europe).

**Scope:** Backend internationalization only. iOS app localization (translating UI strings) is a separate follow-up.

---

## 1. Contractor Profile & Country Detection

### New Fields on Contractor Document

| Field | Type | Description |
|-------|------|-------------|
| `country_code` | string | ISO 3166-1 alpha-2 (e.g., "US", "BR", "DE"). Auto-detected from `owner_phone`, editable. |
| `business_address` | string | Street address. Required for EU/BR Twilio regulatory bundles. |
| `business_city` | string | City name. Used for local number provisioning. |
| `business_country_name` | string | Full country name (e.g., "Germany"). For Twilio regulatory submission. |

### Onboarding Flow

1. User enters personal phone number (existing step)
2. Backend parses country code from phone using `phonenumbers` library → sets `country_code`
3. Backend collects business name + address (new step, all users — keeps UX consistent and satisfies EU/BR regulatory requirements)
4. Backend provisions a local Twilio number in that country
5. If provisioning fails (regulatory rejection, no numbers available), returns a clear error

### Phone Normalization

`normalize_phone()` in `app/utils/phone.py` already accepts a `default_region` parameter. All call sites that currently hardcode "US" will pass the contractor's `country_code` instead.

**Supported countries and their ISO codes:**

| Country | Code | Phone Prefix |
|---------|------|-------------|
| United States | US | +1 |
| Canada | CA | +1 |
| Brazil | BR | +55 |
| United Kingdom | GB | +44 |
| Germany | DE | +49 |
| France | FR | +33 |
| Italy | IT | +39 |
| Spain | ES | +34 |
| Portugal | PT | +351 |

---

## 2. Country-Aware Number Provisioning

### Current State

`provision_twilio_number()` in `app/db/contractors.py` hardcodes `available_phone_numbers("US")`.

### New Flow

```
provision_twilio_number(contractor_id, country_code, area_code="")
    │
    ├── country_code in ("US", "CA", "GB")
    │   └── Provision directly — no regulatory bundle needed
    │
    └── country_code in ("DE", "FR", "IT", "ES", "PT", "BR")
        ├── Create Twilio Regulatory Bundle with business address
        ├── Submit for approval (usually instant for business addresses)
        └── Provision local number using bundle SID
```

### Number Types Per Country

| Country | Number Type | Notes |
|---------|-----------|-------|
| US, CA | `local` | Area code optional |
| GB | `local` | City-level or national |
| DE, FR, IT, ES, PT | `local` | City-level based on business address |
| BR | `local` | DDD area code based on business address |

### Error Handling

- If no numbers available in the user's city → retry with country-level search (no city filter)
- If still none → return error to app: "No numbers available in your area. Please try a different city."
- If regulatory bundle rejected → return error: "Address verification failed. Please check your business address."
- If regulatory bundle pending (not instant approval) → return status "pending" to app, poll for approval. Twilio typically approves business bundles within minutes, but some may take up to 24 hours. The app should show "Your number is being set up — we'll notify you when it's ready."
- Never silently fall back to a US number

---

## 3. Regional Dial-In Numbers

### Current State

One hardcoded US number `+16504222696` in `app/config.py`, used when the contractor taps "Pick Up" in the iOS app to join a call conference.

### New Design

A mapping of country code to dial-in number, stored in settings/config:

```python
dial_in_numbers: dict = {
    "US": "+16504222696",   # existing
    "CA": "<to provision>",
    "BR": "<to provision>",
    "GB": "<to provision>",
    "DE": "<to provision>",
    "FR": "<to provision>",
    "IT": "<to provision>",
    "ES": "<to provision>",
    "PT": "<to provision>",
}
```

- **One-time setup:** Provision 8 new Twilio numbers (one per additional country), all pointing to the same webhook
- **Selection:** When the iOS app taps "Pick Up", backend returns the dial-in number matching `contractor.country_code`
- **Isolation:** Conference ID in the request distinguishes calls — shared number, private conferences
- **Fallback:** If a country's dial-in isn't provisioned yet, use the US number
- **Cost:** ~$8-15/month total for all 9 numbers

### Config Change

Replace `dial_in_number: str` with `dial_in_numbers: dict` in `app/config.py`. Add a helper:

```python
def get_dial_in_number(country_code: str) -> str:
    return settings.dial_in_numbers.get(country_code, settings.dial_in_numbers.get("US", ""))
```

Update `app/api/voip.py` to use `get_dial_in_number(contractor["country_code"])` instead of `settings.dial_in_number`.

---

## 4. Per-Language Voice Selection (Gemini)

### Current State

Two voices: `Puck` (English) and `Orus` (non-English) in `app/services/gemini_pipeline.py`.

### New Design

Language-to-voice mapping for male voices (Kevin is a male assistant):

```python
GEMINI_VOICES = {
    "en": "Puck",      # Warm, upbeat American — best-tested English voice
    "pt": "Orus",      # Authoritative, clear — suits Brazilian Portuguese formality
    "de": "Charon",    # Calm, professional — matches German business tone
    "fr": "Puck",      # Adapts well to French prosody
    "it": "Puck",      # Expressive, warm — suits Italian's melodic cadence
    "es": "Charon",    # Clear, composed — suits Castilian Spanish
}
```

### Selection Logic

```python
user_language = contractor_config.get("user_language", "en")
self._voice = GEMINI_VOICES.get(user_language, GEMINI_VOICES["en"])
```

- Voice selected based on contractor's `user_language` (already exists on profile)
- Gemini auto-adapts pronunciation to the language
- Default: "en" → Puck
- All voices are Gemini built-in voices that support all target languages natively

### ElevenLabs Pipeline (No Change)

The legacy ElevenLabs pipeline keeps its current behavior: Eric for English, Daniel (multilingual) for everything else. No changes — ElevenLabs is being phased out in favor of Gemini.

### Future Adjustment

This mapping is easy to change (one string per language). After launch, native speakers can evaluate and we adjust individual voices.

---

## 5. Call Forwarding Instructions Per Country

### Context

Call forwarding is a carrier setting the user configures on their phone. We cannot automate it. Each country uses different dialing codes, and some carriers have variations.

### Design

A static configuration mapping country codes to forwarding instructions, served via API for the iOS app to display during onboarding.

### Standard Forwarding Codes

| Country | Forward All Calls | Forward When Unanswered | Disable Forwarding |
|---------|-------------------|------------------------|-------------------|
| US/CA | `*72{number}` | `*71{number}` | `*73` |
| BR | `*21{number}` | `*61{number}` | `##21#` |
| GB | `**21*{number}#` | `**61*{number}#` | `##21#` |
| DE | `**21*{number}#` | `**61*{number}#` | `##21#` |
| FR | `**21*{number}#` | `**61*{number}#` | `##21#` |
| IT | `**21*{number}#` | `**61*{number}#` | `##21#` |
| ES | `**21*{number}#` | `**61*{number}#` | `##21#` |
| PT | `**21*{number}#` | `**61*{number}#` | `##21#` |

EU countries all use standard GSM codes. US/CA use carrier-specific codes. Brazil uses a variant.

### Implementation

- Store instructions in `app/config/forwarding_instructions.py` as a dict keyed by country code
- Each entry includes: forward-all code, forward-when-unanswered code, disable code, carrier-specific notes, and a "contact your carrier" fallback message
- New API endpoint: `GET /api/forwarding-instructions?country_code=DE`
- iOS app displays instructions during onboarding after number provisioning
- Default recommendation: "forward when unanswered" (`*61` variant) — most users want Kevin to answer only when they don't

### Carrier Coverage

These standard codes work on 90%+ of carriers in each country. Edge cases (MVNOs, VoIP-only carriers) may differ. The instructions include a fallback: "If these codes don't work with your carrier, search for 'call forwarding' in your carrier's app or contact their support."

---

## Files Changed

### New Files

| File | Purpose |
|------|---------|
| `app/config/forwarding_instructions.py` | Per-country call forwarding codes and instructions |

### Modified Files

| File | Change |
|------|--------|
| `app/db/contractors.py` | Add `country_code`, `business_address`, `business_city`, `business_country_name` defaults. Update `provision_twilio_number()` for country-aware provisioning with regulatory bundles. |
| `app/config.py` | Replace `dial_in_number: str` with `dial_in_numbers: dict`. Add `get_dial_in_number()` helper. |
| `app/api/voip.py` | Use `get_dial_in_number(country_code)` instead of `settings.dial_in_number`. |
| `app/api/settings.py` | Expose `country_code` in settings update (writes to main contractor doc). |
| `app/api/contractors.py` | Accept and validate new fields during contractor creation. Parse `country_code` from phone. |
| `app/services/gemini_pipeline.py` | Replace 2-voice selection with `GEMINI_VOICES` dict lookup by language. |
| `app/utils/phone.py` | No code change needed — already accepts `default_region`. Call sites updated. |
| `app/webhooks/twilio_incoming.py` | Pass contractor's `country_code` to `normalize_phone()` calls. |
| `app/services/lookup.py` | Pass contractor's `country_code` to phone normalization. |

### New API Endpoint

| Endpoint | Method | Description |
|----------|--------|-------------|
| `GET /api/forwarding-instructions` | GET | Returns forwarding instructions for a country. Query param: `country_code`. |

---

## What Is NOT Changing

- `VoicePipeline` (ElevenLabs pipeline) — no changes, legacy path
- iOS app UI strings — localization is a separate follow-up
- Post-call processing — already supports multiple languages via Claude translation
- Twilio webhook URLs — same endpoints, same routing logic
- RTDB structure — no changes
- Push notifications — no changes
- Contact/call history — no changes

---

## Verification Plan

1. **Unit test phone normalization:** Verify `normalize_phone()` works for numbers from all 9 countries with correct `default_region`
2. **Provisioning test (US/CA/UK):** Create test contractor with UK phone, verify local UK number provisioned
3. **Provisioning test (EU):** Create test contractor with German phone/address, verify regulatory bundle created and German number provisioned
4. **Dial-in test:** Verify "Pick Up" uses the correct regional dial-in number for a non-US contractor
5. **Voice test:** Set test contractor `user_language` to each supported language, make test call, verify correct Gemini voice is used
6. **Forwarding instructions API:** Call endpoint for each country, verify correct codes returned
7. **End-to-end:** Full flow for a German contractor — onboard, provision number, configure forwarding, receive call, Kevin answers in German with Charon voice, post-call SMS sent
