# Compatibility for calls spanning a backend deployment

The September 14 production-readiness review found an N1 release defect before
dispatch. Production `407bf0bc7b6604f33f0113e28c3a2ba82e48b7dc` writes and consumes
`/call_commands/{call_sid}`. The staged N1 source uses the durable
`/active_calls/{call_sid}/message_intent` decision instead. Established voice
WebSockets can continue on their original revision after HTTP traffic moves.
Either direction of a traffic switch can therefore lose Take a message delivery.

The replacement work starts at `ef9adafda38967fefcf974604e02b22912fcca66`.
Production dispatch is held until the repaired candidate passes exact-HEAD CI
and refreshed staging. The reviewed rollout uses one deployment and a prepared
forward-recovery patch; see the release packet below. The owner's latest continuation authorizes proceeding with the release;
only the owner may approve the GitHub production environment. No iOS publication,
caller communication, provider configuration change or interruption is authorized
by this source repair.

## Repair contract

- The active-call transaction remains the authority for every new action.
- A committed message decision can publish one enriched legacy command for an
  older surviving consumer. Transaction retries and duplicate requests cannot
  create another publication attempt. An ambiguous write is never replayed.
- Publication and command deletion do not prove instruction acceptance. Legacy
  consumers delete before delivery, so their durable status remains pending.
- New consumers adopt legacy commands using their pinned call, owner and stream
  token, or the direct-ring conference. Enriched commands must also match the
  existing operation and claim. Rejected or replaced commands remain untouched.
- All three voice engines keep their existing once-per-operation delivery and
  acknowledgment behavior. Failed acknowledgment or cleanup cannot repeat delivery.
- Legacy `accepted=True`, incompatible call/redirect state, a different owner,
  a rotated stream token or a replaced conference prevents a new message action.
- No compatibility command creates a transfer or bypasses current arbitration.

## Evidence required

The offline fixture in `tests/fixtures/legacy_call_commands_407.py` contains the
actual old decline, accept, direct-ring and three voice command handlers. It
records their source commit, locations and function hashes; tests replace all
provider and database access with fictional in-memory doubles.

Verify old writers with new consumers, new writers with old consumers, duplicate
requests, callback retries, lost projection responses, command replacement,
delivery/acknowledgment failures and call/owner/token changes. Deliberately remove
critical guards to prove the relevant checks fail. Run the backend suite with
network and application-default credentials blocked. Refresh independent review,
exact-HEAD CI and staging evidence after the repair.

## Legacy transition and recovery limitation

The old accept handler independently writes `accepted=True`, allocates a fresh
conference and redirects without consulting the durable operation. A compatibility
adapter cannot make that unmodified writer atomic. The negative fixture must
retain this limitation rather than claiming that new-reader guards remove it.

Revision `kevin-api-00269-42l` is the verified existing production revision, with
promotional offers disabled and the rollback containment guards present. It is
**not a qualified ordinary rollback target while newer live sessions or unresolved
operations remain**. Restoring HTTP traffic alone does not migrate WebSockets or
preserve the new operation-ownership contract.

The final independent review distinguishes this pre-existing legacy behavior
from the new delivery regression. It approves a single normal rollout with a
tested notification-suppression patch for slower forward recovery; a prior
notification-suppressed deployment is optional, not a correctness prerequisite.
The [release packet](../../releases/2026-09-14-rolling-call-compatibility.md) records
that concrete plan and its limits. No maintenance or caller interruption is
proposed. Do not claim global operation ownership before old workers cease, use
a fixed timer as drain proof, or delete records to manufacture a drained state.
Physical iPhone and provider delivery acceptance remain separate.
