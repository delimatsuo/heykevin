"""Admin support diagnostics helpers."""


def diagnose_contractor(
    contractor: dict,
    device: dict | None,
    recent_calls: list[dict],
) -> list[dict]:
    findings = []
    if not contractor.get("twilio_number"):
        findings.append({"severity": "critical", "code": "missing_twilio_number"})
    if contractor.get("subscription_status") == "expired":
        findings.append({"severity": "warning", "code": "subscription_expired"})
    if not device or not device.get("voip_token_present"):
        findings.append({"severity": "warning", "code": "missing_voip_token"})
    if not recent_calls:
        findings.append({"severity": "info", "code": "no_recent_calls"})
    return findings
