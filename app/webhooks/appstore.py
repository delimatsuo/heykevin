"""App Store Server Notifications V2 webhook.

Apple sends signed JWTs to this endpoint when subscription events occur.
We verify the JWT signature using Apple's public keys before processing.

Audit F-3: the chain walk previously verified internal consistency only —
each cert in x5c was checked to be signed by the next, and the leaf was
used to verify the JWS signature. Without anchoring the chain to Apple's
own root certificate, an attacker could mint a self-consistent chain
[attacker_leaf, attacker_intermediate, attacker_root], sign a forged
notification, and the webhook would accept it (subject only to the
bundleId check). This module now pins the chain root to one of two
bundled Apple Root CAs (G2/G3) by SHA-256 fingerprint and rejects any
chain that does not anchor to a trusted Apple root.

2026-09-03 follow-up: the chain walk above verified internal consistency
and the root anchor, but accepted a chain of any length and never checked
that the leaf/intermediate certs were actually the App Store Server
Notifications signing certificates, and a payload with no bundleId slipped
through (the old check was `if bundle_id and ...`, so an absent bundleId
was silently treated as "no check needed"). This module now also enforces:
(1) the x5c chain must contain exactly APPLE_CHAIN_LENGTH (3) certificates;
(2) the leaf certificate must carry the App Store Server Notifications
marker OID and the intermediate must carry the Apple WWDR marker OID,
matching apple/app-store-server-library-python's signed_data_verifier.py;
(3) bundleId is now fail-closed — a payload missing bundleId in all four
of `data`, `summary`, `externalPurchaseToken`, and `appData` is rejected
rather than silently passed. Separately,
`environment` mismatches (e.g. "Sandbox" vs a `production`-configured
service) are logged as a warning only, never rejected: App Store Connect's
sandbox notification URL may point at this same service for TestFlight
testers, and rejecting would drop those notifications until the URL
routing is confirmed (ruling, 2026-09-03).
"""

import base64
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.backends import default_backend
from cryptography.exceptions import InvalidSignature

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()

# 2026-09-03 follow-up (F-3 addendum): Apple's own verifier
# (apple/app-store-server-library-python, signed_data_verifier.py) enforces
# an exact 3-certificate chain and checks marker OIDs on the leaf and
# intermediate certificates:
#   if len(certificates) != 3: raise VerificationException(VerificationStatus.INVALID_CHAIN_LENGTH)
#   self.check_oid(trusted_chain[0].to_cryptography(), "1.2.840.113635.100.6.11.1")
#   self.check_oid(trusted_chain[1].to_cryptography(), "1.2.840.113635.100.6.2.1")
APPLE_CHAIN_LENGTH = 3
# App Store Server Notifications signing leaf
APPLE_LEAF_OID = "1.2.840.113635.100.6.11.1"
# Apple Worldwide Developer Relations (WWDR) intermediate
APPLE_INTERMEDIATE_OID = "1.2.840.113635.100.6.2.1"


def _load_trusted_apple_root_fingerprints() -> set[str]:
    """Load bundled Apple Root CA certs and return their SHA-256 fingerprints.

    The fingerprints below are computed from the DER-encoded root CAs
    distributed at https://www.apple.com/certificateauthority/. They were
    verified against Apple's public listing at the time of bundling:

      AppleRootCA-G3:
        63:34:3A:BF:B8:9A:6A:03:EB:B5:7E:9B:3F:5F:A7:BE:
        7C:4F:5C:75:6F:30:17:B3:A8:C4:88:C3:65:3E:91:79

      AppleRootCA-G2:
        C2:B9:B0:42:DD:57:83:0E:7D:11:7D:AC:55:AC:8A:E1:
        94:07:D3:8E:41:D8:8F:32:15:BC:3A:89:04:44:A0:50

    Apple Server Notifications V2 are signed by certs that chain to G3 in
    practice; G2 is bundled for forward / backward compatibility.
    """
    cert_dir = Path(__file__).resolve().parent.parent / "services" / "apple_certs"
    fingerprints: set[str] = set()
    for cert_path in (cert_dir / "AppleRootCA-G3.cer", cert_dir / "AppleRootCA-G2.cer"):
        if not cert_path.exists():
            logger.error("Missing bundled Apple Root CA: %s", cert_path)
            continue
        der = cert_path.read_bytes()
        fingerprints.add(hashlib.sha256(der).hexdigest())
    if not fingerprints:
        # Refuse to silently disable pinning — fail closed at module load.
        raise RuntimeError(
            "No Apple Root CA certificates bundled at app/services/apple_certs/."
            " Webhook signature verification cannot be safely performed."
        )
    return fingerprints


_TRUSTED_APPLE_ROOT_FINGERPRINTS: set[str] = _load_trusted_apple_root_fingerprints()


def _has_extension(cert: x509.Certificate, oid: str) -> bool:
    """Return True if `cert` carries the extension identified by `oid`."""
    try:
        cert.extensions.get_extension_for_oid(x509.ObjectIdentifier(oid))
        return True
    except x509.ExtensionNotFound:
        return False


def _decode_notification_payload(signed_payload: str) -> dict:
    """Decode and verify an Apple-signed JWS notification.

    Apple signs App Store Server Notifications as JWS with RS/ES.
    The x5c header contains the certificate chain; we verify:
    1. The certificate chain is valid and roots to Apple's CA
    2. The chain has exactly APPLE_CHAIN_LENGTH (3) certificates
    3. The leaf carries the App Store Server Notifications marker OID and
       the intermediate carries the Apple WWDR marker OID
    4. The JWS signature is valid
    5. bundleId is present (fail-closed) in one of data/summary/
       externalPurchaseToken/appData and matches settings.appstore_bundle_id
    6. environment, if present, is compared to settings.appstore_environment
       and a mismatch is logged as a warning only — never rejected

    Returns the decoded payload dict, raises ValueError on failure.
    """
    parts = signed_payload.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid JWS format")

    # Decode header
    padded_header = parts[0] + "=" * (4 - len(parts[0]) % 4)
    try:
        header = json.loads(base64.urlsafe_b64decode(padded_header))
    except Exception as e:
        raise ValueError(f"Invalid JWS header: {e}")

    # Extract certificate chain from x5c header
    x5c = header.get("x5c", [])
    if not x5c:
        raise ValueError("Missing x5c certificate chain in JWS header")

    try:
        # 2026-09-03 follow-up: x5c must be a list of non-empty strings
        # before we attempt to base64-decode any of it — otherwise a
        # malformed entry (e.g. a non-string) surfaces as an opaque
        # "Certificate/signature verification error" instead of a clear
        # rejection.
        if not isinstance(x5c, list) or not all(
            isinstance(c, str) and c for c in x5c
        ):
            raise ValueError("Malformed x5c certificate chain")

        # Parse every cert in the chain up front so we can anchor to the
        # trusted root before doing any signature work.
        cert_bytes_list = [base64.b64decode(c) for c in x5c]
        certs = [
            x509.load_der_x509_certificate(b, default_backend())
            for b in cert_bytes_list
        ]
        leaf_cert = certs[0]

        # 2026-09-03 follow-up: Apple's verifier requires the x5c chain to
        # contain exactly leaf, intermediate, root — no more, no fewer. See
        # apple/app-store-server-library-python signed_data_verifier.py:
        # `if len(certificates) != 3: raise VerificationException(...)`.
        if len(certs) != APPLE_CHAIN_LENGTH:
            raise ValueError(
                f"Certificate chain must have exactly {APPLE_CHAIN_LENGTH} "
                f"certificates, got {len(certs)}"
            )

        # F-3: anchor the chain to a known Apple Root CA. The last cert in
        # x5c MUST match (by DER fingerprint) one of our bundled Apple
        # roots. Without this check the chain is only internally
        # consistent — an attacker can mint their own root + intermediate +
        # leaf and the rest of this function would happily verify them.
        root_fingerprint = hashlib.sha256(cert_bytes_list[-1]).hexdigest()
        if root_fingerprint not in _TRUSTED_APPLE_ROOT_FINGERPRINTS:
            raise ValueError(
                "Certificate chain does not anchor to a trusted Apple Root CA"
            )

        # F-3: validity-window check on every cert in the chain. Even with
        # the root anchor pinned, an expired or not-yet-valid cert means
        # we should refuse the notification.
        now = datetime.now(timezone.utc)
        for idx, cert in enumerate(certs):
            not_before = cert.not_valid_before_utc
            not_after = cert.not_valid_after_utc
            if now < not_before or now > not_after:
                raise ValueError(
                    f"Certificate at chain position {idx} is outside its "
                    f"validity window (not_before={not_before.isoformat()} "
                    f"not_after={not_after.isoformat()})"
                )

        # 2026-09-03 follow-up: the leaf and intermediate must be the
        # actual App Store Server Notifications signing certificates, not
        # merely any chain that anchors to an Apple root. Apple's own
        # verifier checks these marker OIDs; without this a chain issued
        # for an unrelated Apple purpose (still anchored to the same root)
        # would otherwise verify.
        if not _has_extension(leaf_cert, APPLE_LEAF_OID):
            raise ValueError(
                "Leaf certificate lacks the App Store Server Notifications marker OID"
            )
        if not _has_extension(certs[1], APPLE_INTERMEDIATE_OID):
            raise ValueError(
                "Intermediate certificate lacks the Apple WWDR marker OID"
            )

        # Verify the chain: each cert must be signed by the next.
        for i in range(len(certs) - 1):
            child_cert = certs[i]
            parent_cert = certs[i + 1]

            # Verify child's signature against parent's public key
            try:
                parent_cert.public_key().verify(
                    child_cert.signature,
                    child_cert.tbs_certificate_bytes,
                    ec.ECDSA(child_cert.signature_hash_algorithm)
                )
            except Exception:
                raise ValueError(f"Certificate chain verification failed at position {i}")

        # Verify JWS signature using leaf cert's public key.
        #
        # RFC 7518 §3.4: an ES256 JWS signature is the raw 64-octet
        # concatenation R || S. `cryptography`'s verify() expects the DER
        # Dss-Sig-Value form (RFC 3279), so the raw bytes must be re-encoded
        # first. Passing them straight through raised InvalidSignature on
        # every genuine Apple notification, so the webhook answered 400 to
        # all of them and Apple gave up after its retry window.
        signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
        sig_bytes = base64.urlsafe_b64decode(parts[2] + "=" * (4 - len(parts[2]) % 4))
        alg = header.get("alg", "")

        if alg != "ES256":
            raise ValueError(f"Unsupported JWS algorithm: {alg}")
        if len(sig_bytes) != 64:
            raise ValueError(
                f"Invalid ES256 JWS signature length: {len(sig_bytes)} (expected 64)"
            )
        r = int.from_bytes(sig_bytes[:32], "big")
        s = int.from_bytes(sig_bytes[32:], "big")
        leaf_cert.public_key().verify(
            encode_dss_signature(r, s),
            signing_input,
            ec.ECDSA(hashes.SHA256()),
        )

    except ValueError:
        raise
    except InvalidSignature:
        raise ValueError("JWS signature verification failed")
    except Exception as e:
        raise ValueError(f"Certificate/signature verification error: {e}")

    # Decode payload
    padded_payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded_payload))
    except Exception as e:
        raise ValueError(f"Invalid JWS payload: {e}")

    # Validate bundle ID. 2026-09-03 follow-up: fail closed — a payload
    # with no bundleId in any section used to fall through the old
    # `if bundle_id and ...` truthy check unrejected. It is now rejected.
    #
    # Fix round 1 (2026-09-03, controller ruling): Apple's own verifier
    # reads bundleId from four mutually exclusive payload sections — data,
    # summary, externalPurchaseToken, appData — not just the first two.
    # Sourcing from only two rejected legitimate externalPurchaseToken- or
    # appData-shaped notifications outright ("Missing bundleId" -> HTTP 400
    # -> Apple retries, gives up, notification permanently lost). This is
    # strictly narrower than the pre-task truthy check; nothing loosened.
    from app.config import settings
    signed_data = payload.get("data")
    summary = payload.get("summary")
    external_purchase_token = payload.get("externalPurchaseToken")
    app_data = payload.get("appData")
    bundle_id = None
    bundle_section = None
    for section in (signed_data, summary, external_purchase_token, app_data):
        if (
            isinstance(section, dict)
            and isinstance(section.get("bundleId"), str)
            and section["bundleId"]
        ):
            bundle_id = section["bundleId"]
            bundle_section = section
            break
    if bundle_id is None:
        raise ValueError("Missing bundleId in notification payload")
    if bundle_id != settings.appstore_bundle_id:
        raise ValueError(f"Bundle ID mismatch: {bundle_id}")

    # 2026-09-03 follow-up: environment awareness is LOG ONLY, never a
    # rejection (ruling: App Store Connect's sandbox notification URL may
    # point at this same service for TestFlight testers; rejecting would
    # drop those notifications until the URL routing is confirmed). Read
    # from the section that actually supplied the bundleId (bound above),
    # not the exhausted loop variable.
    environment = bundle_section.get("environment")
    if (
        isinstance(environment, str)
        and environment
        and environment.lower() != str(settings.appstore_environment).lower()
    ):
        logger.warning(
            "App Store notification environment %s differs from configured %s",
            environment,
            settings.appstore_environment,
        )

    return payload


@router.post("/webhooks/appstore/notifications")
async def handle_appstore_notification(request: Request):
    """Handle App Store Server Notification V2.

    Apple sends signed JWS payloads when subscription lifecycle events occur.
    """
    try:
        try:
            body = await request.json()
        except Exception as e:
            logger.warning(f"App Store notification invalid JSON body: {e}")
            return JSONResponse(status_code=400, content={"error": "invalid json"})

        if not isinstance(body, dict):
            logger.warning("App Store notification body is not a JSON object")
            return JSONResponse(status_code=400, content={"error": "invalid json"})

        signed_payload = body.get("signedPayload")
        if not isinstance(signed_payload, str) or not signed_payload:
            logger.warning("Missing or non-string signedPayload in App Store notification")
            return JSONResponse(status_code=400, content={"error": "missing signedPayload"})

        try:
            payload = _decode_notification_payload(signed_payload)
        except ValueError as e:
            logger.warning(f"App Store notification rejected: {e}")
            return JSONResponse(status_code=400, content={"error": "invalid payload"})

        notification_type = payload.get("notificationType", "unknown")
        logger.info(f"App Store notification: {notification_type}")

        from app.services.subscription import handle_appstore_notification
        await handle_appstore_notification(payload)

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"App Store webhook error: {e}", exc_info=True)
        # Return HTTP 500 so Apple's retry mechanism will re-deliver the notification
        # rather than dropping lifecycle events on transient infrastructure failures.
        return JSONResponse(status_code=500, content={"error": "internal processing error"})
