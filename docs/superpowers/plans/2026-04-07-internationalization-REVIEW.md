# Plan Review Report (Round 2)

**Plan**: `docs/superpowers/plans/2026-04-07-internationalization.md`
**Review Date**: 2026-04-08
**Reviewers**: Staff Engineer, Security Analyst, Architect
**Debate Rounds**: 2
**Final Verdict**: ALL_APPROVE

---

## Executive Summary

The revised plan addresses all 5 critical and 7 warning findings from Round 1. All three reviewers approve. Three minor warnings were raised for the implementer to address inline: deduplicate the country allowlist constant, use contractor's country_code as phone normalization fallback, and document the 30s provisioning latency as an accepted tradeoff.

## Verdict Breakdown

| Reviewer | Verdict | Critical | Warnings | Nits |
|----------|---------|----------|----------|------|
| Staff Engineer | APPROVE WITH CONDITIONS | 0 | 3 | 3 |
| Security Analyst | APPROVE | 0 | 2 | 3 |
| Architect | APPROVE | 0 | 1 | 2 |

## Original Findings — All Fixed

| # | Finding | Status |
|---|---------|--------|
| C1 | @property on Pydantic BaseSettings | FIXED — standalone function with cache |
| C2 | 30s sync polling blocks HTTP | PARTIALLY FIXED — async sleep, pragmatic compromise |
| C3 | sms_enabled fails for EU/BR | FIXED — conditional on US/CA only |
| C4 | regulation_sid="" invalid | FIXED — proper lookup via regulations.list() |
| C5 | country_code not validated | FIXED — validated in all 4 entry points |
| W1 | File path contradiction Task 5 | FIXED — consistently app/api/forwarding.py |
| W2 | warm_transfer nonexistent attr | FIXED — Firestore lookup by contractor_id |
| W3 | voip.py step is no-op | FIXED — removed |
| W4 | bundle_sid in search params | FIXED — only in purchase_params |
| W5 | Exception messages leak internals | FIXED — sanitized responses |
| W6 | dial_in_number backward compat | FIXED — deprecated field preserved |
| W7 | Forwarding endpoint auth | FIXED — Depends(verify_api_token) added |

## New Warnings (Address During Implementation)

1. **Deduplicate SUPPORTED_COUNTRIES** — Import from `app/db/contractors.py` instead of hardcoding the set in 4 files. All three reviewers flagged this.

2. **Phone normalization fallback** — Task 6 falls back to `default_region="US"` for non-E.164 numbers. Should use the contractor's `country_code` instead. Staff Engineer flagged.

3. **30s provisioning latency** — The regulatory bundle polling still holds the HTTP connection for up to 30s. All reviewers agree this is acceptable for MVP (one-time-per-contractor, async sleep doesn't block event loop) but should be documented as a known limitation.

## Nits (Optional)

1. Plan shows two versions of `get_dial_in_number` in Task 3 Step 1 — remove the first one with logger.warning
2. `_dial_in_cache` never invalidated — fine for Cloud Run, add a comment
3. `body.dict()` deprecated in Pydantic v2 — consistent with existing code, not a regression
4. Error sanitization uses keyword matching — consider typed exceptions in future

## Reviewer Sign-Off

- [x] Staff Engineer: APPROVE WITH CONDITIONS
- [x] Security Analyst: APPROVE
- [x] Architect: APPROVE
