# Hey Kevin frontend concept

Open `index.html` in a browser. It is a standalone HTML
file with inline styles, icons and JavaScript; no build step is needed.

This folder is an interactive design proposal for owner review. The native app
and backend are unchanged. All calls, people, appointments and account details
are fictional. Changes last only for the current page session.

## Suggested review

1. Open the waiting call, read the transcript and pick up. Try taking a message
   on a fresh call, then use the review control to make the caller hang up.
2. Browse the chronological journal, search a caller and try the Unread and
   Spam filters. Open David's appointment request.
3. Open Kevin, change answering preferences and edit the sample business
   profile. Switch to Personal to compare the simpler set of controls.
4. Use the scenario selector to try failed pickup, failed settings saving and
   failed appointment confirmation. Each can be retried.
5. Try the older-notification scenario: Marcus's alert should open Marcus's
   ended call while Maya remains available through the live-call strip.
6. Hide notification details, try long content and inspect the loading, empty
   and retained-history error states. Start over restores the default scene.

## Proposal

Calls becomes the starting point; Kevin contains answering behavior. Settings
remains available through a labeled profile entry. The live caller stays
reachable across the app. Warm ivory, deep ink, fine journal rules and a clear
pickup action give the interface a calmer identity.

The three-role panel reviewed the current Swift interface, compared alternatives
and completed a debate round. Its rationale, source references and acceptance
contract are in `PANEL-REVIEW.md`.

## Delivery boundary

The HTML demonstrates navigation, layout and local interactions. It cannot
qualify native CallKit, APNs, carrier forwarding, device accessibility, calendar
providers, App Store billing or production behavior. A native refactor requires
the owner's separate design approval and a fresh implementation plan.

The live local preview, when started by Codex, is bound to `127.0.0.1` and stops
after six hours. The HTML file remains usable offline afterward.
