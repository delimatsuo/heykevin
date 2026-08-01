# Task 4.8 Provider-Approval Mechanism

Status: the mechanism described here is real and wired into
`scripts/run_voice_architecture_bakeoff.py` — cryptographic signature
verification, durable nonce admission, and nonproduction-only credential
resolution all genuinely execute. A fully signed, reviewed, valid envelope run
through the normal (non-`--emit-signing-payload`) CLI still stops at
`blocked_external_verification_required` (see "Known open item" below for the
one mode that exits 0 instead); nothing in this document authorizes provider
execution, staging, or production action. See "What stays blocked regardless"
below.

This document supersedes `docs/security/task-4-8-synthetic-preparation.md`
(merged as PR #133) as the description of how Task 3.4/4.8 authorization
actually works in this repository.

## What changed and why

PR #133 modeled Task 3.4/4.8 authorization as nine separate institutional
roles — an owner runtime authorizer, an independent staff engineer, a
separate pre-auth system owner, an identity security owner, a trust and
revocation owner, a privacy and provider owner, a production safety owner, a
custody and residue owner, and a one-use envelope issuer (see the gate matrix
in `docs/security/task-4-8-synthetic-preparation.md`). Those roles cannot
exist for a solo developer, so that package was `not_authorized` by
construction, with every gate permanently `unmet_external`. It could never
become satisfiable without inventing organizational structure that does not
exist here.

This mechanism instead implements Task 3.4's own spec text verbatim (see
`docs/superpowers/plans/2026-07-22-voice-architecture-bakeoff-and-lifecycle-control.md`,
"Task 3.4: Seal and approve provider execution"):

> one trusted sole-owner signature plus a mandatory envelope-bound advisory
> technical-review receipt with no unresolved P1; the receipt cannot
> authorize a run.

That is: one person (the project owner) signs with a personal Ed25519 key,
and a procedurally separate reviewer (not the same person or session) issues
an advisory receipt. Neither party is an institutional role — both are real
actions a solo developer can actually take — and the receipt is explicitly
advisory: it narrows what can be signed over, but it never substitutes for
the owner's own signature.

## The six real pieces

| File | What it does |
| --- | --- |
| `app/services/voice_bakeoff_nonce_ledger.py` | `FileBackedNonceLedger` — a persisted, file-locked, one-use nonce/approval-id/binding admission ledger. Replay of a consumed nonce, approval ID, or `binding_digest:epoch` pair is rejected, even across separate process invocations. |
| `app/services/voice_bakeoff_credential_broker.py` | `NonproductionCredentialBroker` — resolves a credential grant only when environment-provided nonproduction values match the approval's own digest-pinned references exactly, and the resolved account/region is not on a hardcoded single-entry production denylist (`kevin-491315:us-central1` — this project's own GCP hosting project; see "Scope boundary" below). |
| `app/services/voice_bakeoff_residue_audit.py` | `audit_residue()` — inspects (never deletes) a destination directory for files or symlinks older than a TTL. Read-only; a human decides what to do with anything it finds. |
| `scripts/sign_voice_bakeoff_approval.py` | The owner's personal signing CLI. Signs a canonical JSON payload with the owner's own Ed25519 key under the real `_APPROVAL_DOMAIN` domain-separation constant from `app/services/voice_bakeoff_security_contracts.py`. |
| `scripts/request_voice_bakeoff_review.py` | Builds a digest-only review request package (never the raw approval contents) and validates/parses an independent reviewer's response into a `TechnicalReviewReceipt`. **Not a standalone CLI** — it has no `argparse`/`__main__` entry point; it is a small function library (`build_receipt_request`, `parse_review_response`, `reviewer_is_procedurally_separate`) meant to be driven from a Python shell or a short script you write. |
| `scripts/run_voice_architecture_bakeoff.py` | The runner. Performs real Ed25519 verification (via `OfflineApprovalVerifier`), real nonce-ledger admission, real credential-broker checks, and a post-verdict residue audit, gated in earliest-boundary-first order. |

The runner's new/changed CLI surface (confirmed against `--help` output):

- `--nonce-ledger <path>` — **required**. Path to the `FileBackedNonceLedger` JSON file (created if missing). **Limitation:** the runner hardcodes `epoch=1` for every admission keyed on `manifest_digest` (there is no `--epoch` flag). That means a given manifest can be successfully admitted **at most once, ever**, against a given ledger file — a second, legitimately re-signed approval for the *same* manifest (for example, after fixing a typo and re-signing) is rejected forever by that same ledger. If you need to re-issue an approval for the same manifest, do not reuse the same `--nonce-ledger` file — point `--nonce-ledger` at a fresh path instead. The runner's `"nonce already consumed"` rejection message covers this binding/epoch collision case too, not only literal nonce replay, so that message does not always mean the nonce string itself was reused. Adding a `--epoch` flag to make this operator-controlled is deferred, out of scope for this fix.
- `--residue-destination <path>` — **required**. Directory the residue audit inspects after every run (created lazily; if it doesn't exist yet, the audit trivially passes with `remaining_paths: []` — that is not proof anything was actually checked).
- `--trust-owner-public-key <hex>` — **optional**, but verification always fails closed without it. The owner's Ed25519 public key as 64 hex characters (32 bytes). This is **not a secret** — the same trust model as an SSH `authorized_keys` entry. Must be the exact same value at `--emit-signing-payload` time and at final-verification time (see below); it is bound into the signed message, so a mismatch produces a payload the real signature won't verify against.
- `--emit-signing-payload <path>` — **optional** mode switch. Runs every shape/digest/binding check that doesn't require a signature yet, then writes the exact JSON payload dict the verifier will check a signature against.

## The real end-to-end workflow

This did not exist end-to-end before Task 6's fix round, and nothing else
currently documents it start to finish. All flag names below are copied from
the runner's actual `argparse` definitions, not paraphrased.

**0. Create your owner key.** On a first run, the owner's Ed25519 private
key does not exist yet — but Step 1 below requires `--trust-owner-public-key`
(derived from that key), and Step 2 (`sign_voice_bakeoff_approval.py`) needs
a `--payload` file that only Step 1 produces. Neither step can go first on
its own, so start here instead. Break the cycle by running the signing CLI
once against a throwaway placeholder payload, purely to mint the key file —
`load_or_create_owner_key()` creates `--key` (if it doesn't already exist,
mode `0600`) *before* it ever reads `--payload`, so the payload's actual
content does not matter for this one call. There is no dedicated
"create-key" flag; this is the real, minimal command that accomplishes it:

```bash
mkdir -p ~/.config/hey-kevin
echo '{}' > /tmp/bootstrap_placeholder_payload.json
python scripts/sign_voice_bakeoff_approval.py \
  --key ~/.config/hey-kevin/bakeoff_owner_key.pem \
  --payload /tmp/bootstrap_placeholder_payload.json \
  --domain-name approval
```

This prints a signature to stdout — discard it; it is not tied to any real
approval and is not used anywhere. What matters is the key file this
command leaves behind at `~/.config/hey-kevin/bakeoff_owner_key.pem`. Every
later invocation of `sign_voice_bakeoff_approval.py` against the same
`--key` path reuses that same file (`load_or_create_owner_key()` loads an
existing key rather than regenerating it), so you only do this once, ever,
per key path.

Now derive the matching **public** key hex from that same private key
file — the value `--trust-owner-public-key` needs below, in Step 1. There is
currently no shipped helper for this (a known, deliberate documentation-only
gap — not something this task built new code for). Derive it manually:

```bash
python3 -c "
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pathlib
raw = pathlib.Path('~/.config/hey-kevin/bakeoff_owner_key.pem').expanduser().read_bytes()
print(Ed25519PrivateKey.from_private_bytes(raw).public_key().public_bytes_raw().hex())
"
```

**1. Emit the signing payload.** Run the normal arguments plus
`--emit-signing-payload`:

```bash
python scripts/run_voice_architecture_bakeoff.py \
  --arm B1 \
  --manifest /path/to/manifest.json \
  --approval /path/to/approval.json \
  --dry-run \
  --nonce-ledger /path/to/nonce_ledger.json \
  --residue-destination /path/to/residue \
  --trust-owner-public-key <64-hex-char Ed25519 public key> \
  --emit-signing-payload /path/to/signing_payload.json
```

`--approval` still needs a syntactically valid but not-yet-real
`owner_authorization.signature` value at this point (128 hex characters,
e.g. 128 zeros) — its value is excluded from the approval's own
`self_digest` and from the signed message itself, so a placeholder there
doesn't change the bytes this mode emits. On success this prints
`{"error_count": 0, "verdict": "signing_payload_emitted"}` and exits **0**,
and writes the canonical payload to `signing_payload.json`. On any local
failure it prints `rejected_local_preflight` and exits **2** — including
when `--trust-owner-public-key` is absent or malformed
(`"cannot emit signing payload without a valid --trust-owner-public-key"`).

**2. Sign the payload with your own key.**

```bash
python scripts/sign_voice_bakeoff_approval.py \
  --key ~/.config/hey-kevin/bakeoff_owner_key.pem \
  --payload /path/to/signing_payload.json \
  --domain-name approval
```

Note the flag is `--domain-name`, not `--domain`: the real domain constants
in `voice_bakeoff_security_contracts.py` are NUL-terminated
(`_APPROVAL_DOMAIN = b"hey-kevin/voice-bakeoff/approval/v1\x00"`), and a NUL
byte cannot survive as a process argv element, so free-text domains could
never reproduce the exact bytes `OfflineApprovalVerifier.verify()` checks
against. `--domain-name approval` is a symbolic name that maps to the real
constant internally. This reuses the same key file Step 0 already created
at `~/.config/hey-kevin/bakeoff_owner_key.pem` — `load_or_create_owner_key()`
loads an existing key file rather than regenerating it, so this step signs
with the same key whose public half you already derived and passed as
`--trust-owner-public-key` in Step 1. The command prints the hex-encoded
signature to stdout.

**3. Embed the signature.** Paste step 2's stdout into the approval JSON's
`owner_authorization.signature` field, replacing the placeholder.

**4. Obtain an independent technical review receipt**, from a procedurally
separate reviewer — not yourself, not the same session that produced the
signature in step 2. `scripts/request_voice_bakeoff_review.py` provides the
functions for this, called from a Python shell or short driver script (it
has no CLI of its own):

```python
from scripts.request_voice_bakeoff_review import (
    build_receipt_request,
    parse_review_response,
)

# Digests and metadata only — never the raw approval contents — so a
# compromised or careless reviewer process can't leak sensitive detail it
# was never given.
request = build_receipt_request(
    approval["self_digest"],
    approval["manifest_digest"],
    source_sha=approval["source_sha"],
    manifest_digest=approval["manifest_digest"],
)
# ... send `request` to a procedurally separate reviewer and get their
# response back as a dict ...

receipt = parse_review_response(
    reviewer_response,
    expected_payload_digest=approval["self_digest"],
    expected_binding_digest=approval["manifest_digest"],
)
```

The runner rejects a receipt whose `provenance_ref` equals the signer's
`owner_authorization.identity` (`reviewer_is_procedurally_separate()`), so
signing and reviewing from the same session cannot satisfy both
requirements. The approval JSON's `technical_review` object needs exactly
six fields — `review_digest`, `provenance_ref`, `source_sha`,
`manifest_digest`, `unresolved_p1_count` (must be `0`), and `advisory_only`
(must be `true`) — populate them from the receipt plus the approval's own
`source_sha`/`manifest_digest`. For a complete worked example of every
required field on both the manifest and the approval envelope, see
`tests/unit/test_run_voice_architecture_bakeoff.py`'s `_write_valid_cli_fixture`
and `_approval()` helpers — that is the authoritative, tested shape; it is
not duplicated in full here to avoid a second copy that can drift out of
sync.

**5. Run the runner normally** — the same command as step 1, minus
`--emit-signing-payload`:

```bash
python scripts/run_voice_architecture_bakeoff.py \
  --arm B1 \
  --manifest /path/to/manifest.json \
  --approval /path/to/approval.json \
  --dry-run \
  --nonce-ledger /path/to/nonce_ledger.json \
  --residue-destination /path/to/residue \
  --trust-owner-public-key <the same 64-hex-char public key as step 1>
```

A fully signed, reviewed, valid envelope now reaches
`{"error_count": 0, "verdict": "blocked_external_verification_required", "residue_audit": {"passed": true, "remaining_paths": []}}`
and exits **3**. `error_count: 0` means local verification passes
completely — signature, nonce admission, and credential-broker checks all
genuinely ran and genuinely succeeded. It does **not** mean a provider was
contacted or that execution is now possible. **`--execute-provider` remains
permanently rejected regardless of this outcome** — see below. Re-running
the exact same envelope a second time now fails with `rejected_local_preflight`
(exit 2, one error) because the nonce ledger has already admitted it — nonces
are genuinely one-use, durably, across process invocations.

### Verdict and exit-code reference

| Mode | Condition | Verdict | Exit code |
| --- | --- | --- | --- |
| normal | any local check fails | `rejected_local_preflight` | 2 |
| normal | every local check (shape, signature, nonce, credential broker) passes | `blocked_external_verification_required` | 3 |
| `--emit-signing-payload` | any local check fails, or `--trust-owner-public-key` absent/invalid | `rejected_local_preflight` | 2 |
| `--emit-signing-payload` | every check up to (not including) real signature verification passes | `signing_payload_emitted` | **0** |
| any mode | `--execute-provider` passed, or a required argument is missing | (argparse usage error — no JSON printed) | 2 |

`--execute-provider` is not a recognized argument at all — it isn't in the
parser's `add_argument` calls — so passing it is rejected by `argparse`
itself before `main()`'s own code runs, before any file, subprocess, or
harness call happens. A dedicated test
(`test_execute_provider_is_rejected_before_inputs_or_subprocess`) confirms
this by monkeypatching `_load`, `subprocess.check_output`, and
`run_offline_self_check` to raise if called, then asserting none of them
were reached.

Exit code 2 is therefore overloaded: it covers both "the CLI invocation
itself was malformed" (argparse's own usage-error code) and "local
preflight ran and found a real problem" (`rejected_local_preflight`). Check
the printed JSON's `verdict` field, not just the exit code, to tell them
apart — an argparse usage error prints an argparse error message to stderr
and no JSON at all, while `rejected_local_preflight` prints valid JSON to
stdout.

## Known open item: `--emit-signing-payload` exits 0

Every other mode and path through this runner is designed to never signal
unqualified success — the best case anywhere else is exit 3
(`blocked_external_verification_required`). `--emit-signing-payload` is the
one exception: it exits 0 on success. This is flagged as a minor interface
inconsistency for a future fix. It is not resolved as part of this
documentation task — noting it factually, not hiding it, not fixing it here.

## Scope boundary: only one production denylist is wired in

The original plan for this rebuild intended to also wire
`ExecutionFirewallResolver` / `DeclaredProductionDenylist` (from
`app/services/voice_bakeoff_execution_firewall_contracts.py`) into the
runner as a second, broader, multi-provider production denylist, alongside
the credential broker's single-entry check. That mechanism requires real
production destination/identity digests for Twilio, Deepgram, Gemini, and
ElevenLabs — data that does not exist anywhere in this plan. Fabricating
placeholder production identifiers to fill it in would have been worse than
not wiring it in at all, so this was investigated and deliberately deferred;
the project owner confirmed accepting the narrower scope.

Concretely, that means: **`PRODUCTION_ACCOUNT_REGION_DENYLIST` in
`voice_bakeoff_credential_broker.py` — one hardcoded entry,
`kevin-491315:us-central1`, this project's own GCP hosting project — is
currently the sole production guard the runner actually enforces.** It is
not a narrow backstop sitting alongside a broader mechanism; it is the only
mechanism actually consulted. The module's own comment states this
directly. `ExecutionFirewallResolver`/`DeclaredProductionDenylist` still
exist as a stdlib-only, unwired, defense-in-depth model (see
`docs/security/voice-architecture-bakeoff-controls.md`, "Offline
execution-firewall model") — they are not connected to the runner and were
not connected by this work. Wiring them in remains real future work,
gated on sourcing real per-provider production identity/destination data
first. This is a documented, deliberate decision, not an oversight.

## What stays blocked regardless

**`tests/support/voice_bakeoff_task_4_8_gate_validator.py` — the separate
"gate package" paperwork validator — is untouched by this entire body of
work and is not satisfied by any of it.** Its own verdict type is
`Literal["preparation_incomplete", "preparation_complete_external_gates_required"]`
— there is no state in that type meaning "authorized" or "clear to run";
even its best case still says external gates are required. This mechanism
does not attempt to satisfy that validator, and a passing/clean dry run from
`run_voice_architecture_bakeoff.py` — even one that reaches `error_count: 0`
— does not mean that separate validator would pass. They check different
things and neither substitutes for the other.

**`--execute-provider` remains permanently rejected**, independent of
anything above. Real network/provider execution is out of scope for this
entire plan, not just blocked pending a few more steps. Wiring it up for
real would additionally require Task 4.7's offline gates and Task 3.5's
caller-UX acceptance contract (`docs/voice-architecture-caller-ux-acceptance.md`,
currently an unsealed proposal) — neither of which this work touches. See
`docs/security/voice-architecture-bakeoff-controls.md`, "Approval and
execution", for the full ordered list of what future connected execution
would still require.

## Verification

```bash
pytest tests/unit/test_voice_bakeoff_nonce_ledger.py \
  tests/unit/test_voice_bakeoff_credential_broker.py \
  tests/unit/test_voice_bakeoff_residue_audit.py \
  tests/unit/test_sign_voice_bakeoff_approval.py \
  tests/unit/test_request_voice_bakeoff_review.py \
  tests/unit/test_run_voice_architecture_bakeoff.py \
  -v
```

`tests/unit/test_run_voice_architecture_bakeoff.py::test_cli_valid_local_envelope_stops_at_external_verification`
and `::test_emit_signing_payload_closes_the_loop_with_the_real_signing_cli`
are the closest things to an executable spec of the workflow above — the
latter signs a real payload with the actual `sign_payload()` function from
`scripts/sign_voice_bakeoff_approval.py` (not a runner-internal shortcut)
and confirms the runner's own verifier accepts it.
