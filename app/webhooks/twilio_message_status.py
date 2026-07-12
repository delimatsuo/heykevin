"""Authenticated, payload-free Twilio outbound message status callbacks."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.middleware.twilio_verify import verify_twilio_signature
from app.services import message_delivery


router = APIRouter()


@router.post("/webhooks/twilio/message-status/{receipt_id}", status_code=204)
async def handle_message_status(
    receipt_id: str,
    request: Request,
    _=Depends(verify_twilio_signature),
):
    form = await request.form()
    outcome = await message_delivery.handle_provider_status(
        receipt_id=receipt_id,
        provider_message_sid=str(form.get("MessageSid") or ""),
        provider_status=str(form.get("MessageStatus") or ""),
        provider_error_code=form.get("ErrorCode") or "",
    )
    if outcome == "error":
        raise HTTPException(status_code=503, detail="Receipt storage unavailable")
    return Response(status_code=204)
