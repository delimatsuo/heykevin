"""Public vCard download endpoint with HMAC-signed URLs."""

import re

from fastapi import APIRouter, Response

from app.db.contractors import get_contractor
from app.services.vcard import generate_vcard, verify_vcard_signature
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/vcard")


@router.get("/{contractor_id}.vcf")
async def download_vcard(contractor_id: str, expires: int = 0, sig: str = ""):
    """Download a contractor's vCard. Requires valid HMAC signature."""
    if not verify_vcard_signature(contractor_id, expires, sig):
        return Response(content="Invalid or expired link", status_code=403)

    contractor = await get_contractor(contractor_id)
    if not contractor:
        return Response(content="Not found", status_code=404)

    vcf = generate_vcard(contractor)
    safe_name = re.sub(r'["\r\n\x00-\x1f]', '', contractor.get("business_name", "contact"))
    return Response(
        content=vcf,
        media_type="text/vcard",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}.vcf"',
        },
    )
