Synthetic preparation only — no runtime authorization — do not use for calls.

# Task 4.8 synthetic preparation package

## Operator state

This is a source-controlled, payload-free preparation record. It contains no
caller data, provider selection, credentials, identities, network destinations,
or runtime controls. Its recorded owner direction is limited to preparing this
synthetic package. The sealed owner runtime authorization remains missing.

The current state is `not_authorized`. Every one of the nine gates is
`unmet_external`. Advisory agent reviews are planning evidence only; they do
not change that state.

No caller-facing app, call-screening, or user-interface behavior changes. Do
not place calls, access data, contact a provider, resolve identity or
credentials, deploy, publish, or otherwise start a runtime action from this
package.

If the baseline, final local Git tree, manifest, artifact digest, status, or
gate matrix differs from this contract, treat it as
`invalid_local_package` and `not_authorized`. Do not use it for calls. The
only next allowed action is a separately authorized external gate process.

## Source identity and package binding

The immutable reviewed baseline is merged `main` commit
`13e105cb533ef611d4a9e5df0e30bb2c9c06e5b3`, whose Git tree is
`8656b9ed41b2ee4df7c149d865695c4c17a0309d`.

The manifest binds this overview, the static gate, the pure local verifier, and
their test by sorted path-to-content-digest entries. It intentionally does not
digest itself. The final package revision is instead the exact local Git tree
captured by the independent final review receipt. The baseline and final
package revision are different identities and must never be conflated.

## Gate matrix

### 1. Sealed owner runtime authorization

- State: `unmet_external`
- Required external authority: owner runtime authorizer
- Missing evidence: sealed runtime record
- Consequence: runtime blocked

### 2. Independent technical review

- State: `unmet_external`
- Required external authority: independent staff engineer
- Missing evidence: independent technical disposition
- Consequence: runtime blocked

### 3. Physically separate pre-auth store

- State: `unmet_external`
- Required external authority: separate pre-auth system owner
- Missing evidence: separate pre-auth store attestation
- Consequence: runtime blocked

### 4. Identity and credential broker

- State: `unmet_external`
- Required external authority: identity security owner
- Missing evidence: credential broker attestation
- Consequence: runtime blocked

### 5. Durable trust and revocation store

- State: `unmet_external`
- Required external authority: trust and revocation owner
- Missing evidence: trust and revocation attestation
- Consequence: runtime blocked

### 6. Provider privacy and region attestations

- State: `unmet_external`
- Required external authority: privacy and provider owner
- Missing evidence: provider privacy region attestation
- Consequence: runtime blocked

### 7. Production denylist

- State: `unmet_external`
- Required external authority: production safety owner
- Missing evidence: production denylist attestation
- Consequence: runtime blocked

### 8. Immutable custody and residue routing

- State: `unmet_external`
- Required external authority: custody and residue owner
- Missing evidence: custody and residue attestation
- Consequence: runtime blocked

### 9. One-use runtime envelope

- State: `unmet_external`
- Required external authority: one-use envelope issuer
- Missing evidence: sealed one-use envelope
- Consequence: runtime blocked

## Offline verification contract

The verifier accepts all five declared changed-file byte streams and
caller-supplied local Git change facts. It rejects unknown and nested fields,
duplicate or non-canonical JSON, path escape, symbolic links, mode changes,
non-synthetic state, execution-like state, altered baseline or content digest,
and sensitive literal patterns. Its diagnostics contain only a category and
location. It cannot show rejected content.

Before the verifier is imported, the separate static gate reads its bytes,
matches the immutable reviewed source digest, then rejects unsafe imports,
reflective loading, dynamic code, indirect call shapes, and process, network,
file, or configuration capability. Functional checks run only after that static
result remains `not_authorized`.

The established Task 4.8 completion validator and gate-status report remain
unchanged. This manifest is structurally incompatible with them and therefore
cannot become a completion or execution input.
