# Hey Kevin Repository Autonomy Policy & Decision Framework

## 1. Scope and Precedence
- **Effective Date:** 2026-08-31.
- **Duration:** Standing policy until explicitly revoked or updated by Deli Matsuo.
- **Applicability:** Applies strictly to the Hey Kevin repository (`delimatsuo/heykevin`).
- **Precedence Rule:** The newest explicit owner instruction always wins in the event of any conflict.

## 2. Model Routing
- **Master Model Role:** Owns architecture, specification, brief creation, ambiguous judgment, independent audit, and all Git operations.
- **Builder Defaults:** Pinned implementation work defaults to `agy gemini-3.7-flash-high`.
- **Low/Medium Tiers:** `flash-low` and `flash-medium` are reserved exclusively for mechanical scaffolds, renames, boilerplate, count checks, and configuration changes.
- **Prohibited Models:** Never route implementation or audit tasks to agy Claude 4.6 models.
- **Delegation Test:** If every expected value and invariant can be deterministically pinned, delegate the task to the builder; otherwise, pinning remains master model work.

## 3. Builder Transport
- **Canonical Invocation Command:**
```bash
agy -p <brief> --model <tier> --output-format json --dangerously-skip-permissions --print-timeout 30m
```
- **Worktree Isolation:** Exactly one isolated Git worktree per slice.
- **Parallelism:** Independent file- and Git-isolated slices may run concurrently.
- **Builder Envelope:** The builder receives the exact base HEAD and tree hashes, strict file allowlists, non-negotiable invariants, expected test assertions/outputs, and explicit stop conditions.
- **Git Ownership:** The builder does not own Git and must not execute Git mutations.

## 4. Quality Floor & Verification
- **Dual-Role Execution:** Master writes the pinned brief; builder implements within boundaries.
- **Adversarial Audit:** Master independently audits the actual diff and executes mutation-effective, focused test probes prior to merge.
- **Anti-Self-Grading:** The worker never grades or validates its own work.
- **Authoritative Evidence:** Exact-current-HEAD CI evidence is authoritative; builder prose or claims are never accepted as proof.

## 5. PR Routing Evidence Template
Every PR must include the following literal evidence fields:
```markdown
Master model: <model_name>
Builder model/tier: <model_name_and_tier>
Routing reason: <justification>
agy transport=headless: <true/false>
Input tokens: <count or unavailable>
Output tokens: <count or unavailable>
Total tokens: <count or unavailable>
Retries: <count>
Audit defects found: <count and description>
Audit disposition: <pass/fail/remediated>
```
*Note: Any unknown usage metrics must be recorded as `unavailable` and must never be invented.*

## 6. CI Contract
- **Required Suites:** All seven Python test shards on every PR targeting `main` or `staging`, plus the quality suite and the stable fail-closed aggregator named `Test`.
- **Full-Suite Authority:** Once exact-current-HEAD CI is green, treat it as complete verification rather than repeating the entire suite serially within the local session.
- **Local Probe Gate:** Focused local tests and adversarial probe checks remain mandatory before pushing any commits.

## 7. Decision Rights & Escalation
- **Reversible Technical Choices:** Decided autonomously by the master model, documented in the PR log, and executed without prompting the owner.
- **Irreversible Actions:** Operations involving spend, new credentials, legal/terms agreements, or product scope changes halt immediately and produce a single paste-ready owner prompt.
- **Batching:** Gate approvals are batched at the end of the active work block.

## 8. Hard Prohibitions
The following actions are strictly forbidden:
- Executing `rm -rf` in any form.
- Reading `.env` or `.env.*` files.
- Executing `git push -f`, `--force`, or `--force-with-lease`.
- Creating provider accounts, invoking paid APIs, or incurring spend outside the Ultra plan.
- Accepting third-party terms of service or legal agreements.
- Accessing Apple accounts, device qualification, or code signing without a fresh envelope.
- Publishing features or changes to real users.
- Ingesting or manipulating real personal data (PII).
- Performing GitHub Actions runner migrations for Hey Kevin.
- Accessing or interacting with Whobert resources.

## 9. Existing Owner Gates
Standing owner approval remains mandatory for:
- Provider, cloud, spend, or credential provisioning.
- Production deployments, releases, or feature flag toggles.
- App Store submissions, Twilio configurations, real-user communication, and client Firestore security rule boundaries.
