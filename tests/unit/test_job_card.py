import json
import os

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

from app.services import job_card


class _FakeResponse:
    status_code = 200

    def json(self):
        return {
            "content": [
                {"type": "thinking", "thinking": "internal scratchpad"},
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "call_type": "service_request",
                            "caller_name": "Jonathan",
                            "business_name": "",
                            "address": "",
                            "issue_description": "Caller needs a toilet replacement",
                            "urgency": "quote",
                            "message": "Please call back with pricing.",
                            "callback_number": "ending in 8667",
                        }
                    ),
                },
            ]
        }


class _FakeAsyncClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, *_args, **_kwargs):
        return _FakeResponse()


@pytest.mark.asyncio
async def test_extract_job_card_reads_text_content_block_after_non_text_blocks(monkeypatch):
    monkeypatch.setattr(job_card.httpx, "AsyncClient", lambda: _FakeAsyncClient())

    result = await job_card.extract_job_card(
        "Caller: Do you do toilet replacement? Kevin: Yes. Caller: Please call me back.",
        "+16506918667",
        contractor={"services": [{"name": "Toilet replacement"}]},
    )

    assert result["call_type"] == "service_request"
    assert result["caller_name"] == "Jonathan"
    assert result["issue_description"] == "Caller needs a toilet replacement"
    assert result["caller_phone"] == "+16506918667"
