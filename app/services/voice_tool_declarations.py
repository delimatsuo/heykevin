"""Provider-specific tool declarations used during tenant voice calls."""

from app.config import settings
from app.services.entitlements import effective_mode

# --- Jobber tool definitions (only included if contractor has Jobber connected) ---

ANTHROPIC_JOBBER_TOOLS = [
    {
        "name": "check_customer",
        "description": "Look up the caller in the business's customer database by phone number. Returns customer name and address if found.",
        "input_schema": {
            "type": "object",
            "properties": {
                "phone": {
                    "type": "string",
                    "description": "The caller's phone number in E.164 format (e.g. +14155551234)",
                }
            },
            "required": ["phone"],
        },
    },
]

# --- Google Calendar tool definitions (fallback when Jobber is not connected) ---

ANTHROPIC_CALENDAR_TOOLS = [
    {
        "name": "check_availability",
        "description": "Check the business owner's Google Calendar for available appointment slots in the next 7 days.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days_ahead": {
                    "type": "integer",
                    "description": "Number of days ahead to check (default 7, max 14)",
                }
            },
            "required": [],
        },
    },
    {
        "name": "book_appointment",
        "description": "Create an appointment on the business owner's Google Calendar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short description of the appointment (e.g. 'Faucet repair - John Smith')",
                },
                "start_time": {
                    "type": "string",
                    "description": (
                        "Start time in ISO 8601 with the business owner's timezone offset. "
                        "Copy start_iso from check_availability when available. "
                        "Never use Z; 12pm Eastern is 2026-08-21T12:00:00-04:00, not 12:00:00Z."
                    ),
                },
                "end_time": {
                    "type": "string",
                    "description": (
                        "End time in ISO 8601 with the business owner's timezone offset. "
                        "Copy end_iso from check_availability when available. Never use Z."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": "Additional notes about the appointment",
                },
            },
            "required": ["title", "start_time", "end_time"],
        },
    },
]


def personal_mode_disables_tools(contractor_config: dict) -> bool:
    """A config without either mode field can still use its connected tools."""
    mode = contractor_config.get("effective_mode")
    if not mode and "mode" in contractor_config:
        mode = effective_mode(contractor_config)
    return mode == "personal"


def build_gemini_voice_tools(contractor_config: dict) -> list[dict]:
    """Build fresh Gemini declarations after the transport's staging safety check."""
    if personal_mode_disables_tools(contractor_config):
        return []

    from app.services.entitlements import has_entitled_token

    has_jobber = has_entitled_token(contractor_config, "jobber")
    has_gcal = has_entitled_token(contractor_config, "google_calendar")

    declarations = []

    if has_jobber:
        declarations.append(
            {
                "name": "check_customer",
                "description": "Look up the caller in the business's customer database by phone number.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "phone": {"type": "STRING", "description": "Phone number in E.164 format"}
                    },
                    "required": ["phone"],
                },
            }
        )
    elif has_gcal:
        declarations.extend([
            {
                "name": "check_availability",
                "description": "Check the business owner's calendar for available appointment slots.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "days_ahead": {"type": "INTEGER", "description": "Days ahead to check (default 7, max 14)"}
                    },
                },
            },
            {
                "name": "book_appointment",
                "description": "Create an appointment on the business owner's calendar.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "title": {"type": "STRING", "description": "Short description of the appointment"},
                        "start_time": {
                            "type": "STRING",
                            "description": (
                                "Start time in ISO 8601 with the business owner's timezone offset. "
                                "Copy start_iso from check_availability. Never use Z."
                            ),
                        },
                        "end_time": {
                            "type": "STRING",
                            "description": (
                                "End time in ISO 8601 with the business owner's timezone offset. "
                                "Copy end_iso from check_availability. Never use Z."
                            ),
                        },
                        "description": {"type": "STRING", "description": "Additional notes"},
                    },
                    "required": ["title", "start_time", "end_time"],
                },
            },
        ])

    request_context = contractor_config.get("service_request_context")
    if (
        isinstance(request_context, dict)
        and request_context.get("customer_key")
        and request_context.get("open_service_requests")
        and settings.service_request_recovery_enabled is True
        and contractor_config.get("service_request_mutations_enabled") is True
        and contractor_config.get("integration_write_status") == "approved"
        and has_entitled_token(contractor_config, "google_calendar")
    ):
        from app.services.receptionist_tools import gemini_tool_declarations

        declarations.extend(gemini_tool_declarations())

    return [{"function_declarations": declarations}] if declarations else []
