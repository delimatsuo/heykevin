# Voice architecture bakeoff qualification environment

**Status:** secret-free template. This document does not authorize a connected
run, caller test, staging change, or production access.

## Isolation contract

Every bakeoff dependency must use a dedicated nonproduction identity that is
technically unable to access production. Labels, configuration checks, and a
production denylist are defense in depth only; they do not establish isolation.

Before a sealed window, the approval packet must name the immutable resource and
credential-reference identifiers for:

| Surface | Required nonproduction boundary | Required proof |
| --- | --- | --- |
| Execution principal | Dedicated service account with no production IAM binding | IAM policy export and denied production-access probe |
| Telephony | Dedicated account/subaccount, number pool, and callback domain | Account identity, canonical HTTPS/WSS mapping, and subaccount policy evidence |
| Provider adapters | Dedicated project/account, region, endpoint, and credential reference per adapter | Control-plane identity, region, retention, training, trace, recording, cache, and deletion evidence |
| Auth-token store | Dedicated distributed store and least-privileged principal | Atomic consume/TTL configuration and isolation policy |
| Firestore/RTDB | Dedicated nonproduction project and databases | Project identity, rules, retention, and no-production reachability proof |
| Logging/evidence | Dedicated sink, encryption boundary, and aggregate-only report location | Sink/export policy, retention/TTL, and deletion-residue audit |

No credential value, caller identity, phone value, transcript, audio, raw provider
message, callback code, or production resource identifier belongs in this document
or in the checked-in manifest template.

## Mandatory controls

- The execution principal may resolve only the explicitly listed nonproduction
  credential references. Each reference must be unable to authenticate to a
  production provider, datastore, telephony resource, or log sink.
- Every external endpoint is an immutable configured HTTPS or WSS canonical URL;
  forwarded/client authority is never used.
- The authenticated token store is the only allowable pre-auth datastore. It
  stores only bounded digests and bindings, atomically consumes capabilities, and
  deletes them at expiry, rejection, revocation, and teardown.
- Request/response logging, tracing, recording, data sharing, tools, writes,
  notifications, transfers, automatic terminal actions, and unsanctioned
  resumption/cache are disabled. Any approved cache exception is synthetic-only,
  retention-pinned, and residue-audited.
- Connected windows have independent quota caps for requests, concurrency,
  duration, bytes, audio duration, retries, output, and spend. Cap exhaustion
  revokes the active execution and derived capabilities.

## Retention and deletion

The sealed manifest must record the retention/TTL and deletion proof location for
each dependency. The only permitted retained evidence is an aggregate receipt
with allowlisted counts, durations, bounded enums, and derived identifiers.

At window completion, the owner must verify deletion of token-store records,
adapter caches, provider artifacts, log exports, evidence objects, and backups or
document the provider-specific deletion receipt. A missing receipt is a failed
window, not an exception.

## Evidence locations

The sealed manifest references—not embeds—the following approved locations:

1. Immutable approval envelope and manifest digest.
2. Nonproduction identity/IAM and provider privacy attestations.
3. Canonical endpoint and allowlisted account mapping.
4. Quota/cost-cap configuration and execution receipt.
5. Aggregate evaluator result and blinded adjudication result.
6. Retention/deletion and post-window residue audit.

## Seal gate

The checked-in manifest begins with `authorization_status: "template_only"`.
Before it can become a sealed window, staff engineering, security/privacy, and
product must independently approve the populated immutable manifest. The runner
must validate that status, all references, approval identities, caps, and the
nonproduction boundary before DNS, credential resolution, socket creation, PSTN,
provider construction, or caller/media work.
