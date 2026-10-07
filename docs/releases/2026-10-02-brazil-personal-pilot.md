# Brazil Personal pilot: executable qualification plan

Prepared October 2, 2026. Priority: Brazil Personal, then Canada, then UK.
Evidence refreshed October 6, including the October 3 human support reply and
confirmed October 6 follow-up. The call matrix remains the proposed envelope.
Status: preparation only; no Brazil consumer route, carrier plan, new number,
budget, deployment or participant has been approved through this document.
The owner reported App Store 1.3.4 (45) released and working. That US device
result does not establish Brazil readiness. PR #281 merged the country
foundations; source availability does not establish deployed behavior.

## Provider decision

| Candidate | Evidence available | Evidence still required |
| --- | --- | --- |
| Twilio Local under existing Business bundle | Bundle approved October 1; October 3 support describes using Kevin documentation for main-account numbers and Local ordering through Console | Explicit approval of this exact bundle for unrelated Personal consumers, regulatory end-user identity, per-consumer documents, address/DDD rules and assignment lifecycle |
| Twilio Mobile registered to the individual | Published individual requirements; October 3 support confirms voice capability, private ordering and type-specific bundles | Exact end-user workflow, approved bundle, service eligibility, DDD inventory, hosted verification and complete costs |
| Telnyx Local/Mobile registered to the individual | Published personal requirements include CPF, identity and recent Brazilian address proof | Kevin service eligibility, reseller/end-user responsibilities, available DDD/type, price, account and integration qualification |

Twilio case **29797359** has a human answer dated October 3, 17:57 UTC, found
in a separate Gmail thread from the acknowledgement. It states that main-account
numbers use Kevin's documents; customer subaccounts use each customer's documents
and bundles. Local is self-service; Mobile and Toll Free require private ordering
and separate number-type bundles. This is general account-structure guidance,
not explicit approval of the particular Local–Business bundle for the dedicated
consumer assignment model. The public
[Twilio Brazil rules](https://www.twilio.com/en-us/guidelines/br/regulatory)
were refreshed October 6: Local excludes individuals and requires a Brazilian
registered business with CNPJ and Brazilian address; a Mobile individual route
is described. The existing bundle approval does not settle who the ultimate
end user is or waive those requirements.
The account-specific answer and actual voice inventory remain decisive.

At October 6, **21:36 EDT / October 7, 01:36 UTC**, an informational follow-up
was sent in the existing case and independently read back in Gmail's Sent state.
It asks for direct confirmation of this exact bundle/use/end user; otherwise the
supported Mobile/Individual path; DDDs, SMS and forwarding restrictions; hosted
verification; assignment/release requirements; and full fees/currency/billing.
It expressly requests no number order, registration or charges. No newer answer
was present at the pre-send refresh. Case status and provider inventory were
not independently refreshed through Console.

[Telnyx's Brazil requirements](https://support.telnyx.com/en/articles/5464041-brazil-did-requirements)
provide a credible alternative to investigate. They do not establish inventory,
permission for Kevin's service or an approved Kevin account. No account or order
was created. Do not start a provider migration before selecting a qualified
route. SIP/BYOC may preserve part of the Twilio pipeline but requires a separate
architecture and security review; it does not remove end-user registration.

Provider selection requires a written answer covering: dedicated consumer
assignment; registrant versus ultimate end user; identity/address/DDD rules;
voice capability and inventory; forwarding acceptance; rental/minimum term;
every call leg and SIP/media charge; SMS capability if promised; reassignment,
deletion and data-retention obligations. Save the answer in the private operating
record, with only a non-personal decision reference in Git.

## Dated quote and cost decision

The October 3 support reply quoted the following. Currency, taxes, setup or
registration fees, partial-month treatment and usage charges remain unresolved.
The five-number totals below multiply the quoted rental by five and assume one
full rental month; they are rental-only planning values, not a seven-day price,
complete quote, approved budget or commitment.

| Voice number type | Quoted monthly rental | Five-number, one-month arithmetic | Quoted lead time after approved bundle |
| --- | ---: | ---: | --- |
| Local | $4.25 | $21.25 | On demand through Console |
| Mobile | $8.00 | $40.00 | One day through private offering |
| Toll Free | $38.50 | $192.50 | 15–30 days through private offering |

Support states no minimum rental time. That does not establish prorating or
refunds. Local is the first route to resolve, Mobile the fallback to qualify.
No type has been selected. Do not assign a number before the exact route is
permitted and the complete quote is approved.

## Pilot envelope and admission

Proposed ceiling: five consenting adult Personal users, one qualified number
type/provider/DDD and one exact carrier plan, seven days, at most five numbers,
16 scripted calls per person plus ten diagnosed retests in total. Maximum 90
calls and 270 caller-connected minutes, with each call stopped at three minutes.
Provider billing may count multiple simultaneous legs; 270 is not a billable
minute or dollar cap. The owner performs the first smoke tests as participant
P01 before inviting the other four.

Count P01 smoke tests, baseline calls, failed dial attempts and every diagnosed
retest within the same **90-attempt** ceiling. No additional smoke allowance is
implied. Track caller-connected seconds separately within 270 minutes. Before
each attempt, reserve the worst-case rounded cost of every possible billable
leg, segment and carrier charge against the remaining approved caps, including
outstanding charges that have not reached an invoice. Unknown rates or exposure
block that attempt. The operator ends and confirms termination of every leg
after each scenario; a disconnected caller does not prove pickup/media legs ended.

**Spend approval is unset.** Obtain a quote and approve an absolute amount and
currency, rental term, and each participant's forwarding-charge cap before any
number purchase, registration fee or call. No advertisements or real App Store
purchases are part of qualification.

[TIM's Siga-me documentation](https://www.tim.com.br/servicos-tim/servicos-basicos/siga-me)
makes postpaid a useful research starting point: it describes conditional
forwarding, excludes prepaid and charges forwarded calls like outgoing calls
to the destination. Exact plan eligibility, destination formatting, roaming
and charges still need confirmation. A carrier webpage is not a passed device
test. Do not copy US forwarding codes into Brazil onboarding or recommend a
generic reset that could erase the user's voicemail settings.

This TIM source was refreshed October 6. It remains a research lead; the owner's
exact carrier, retail plan and two-digit DDD were requested for P01 and are
pending. No participant-specific dial string or restoration instruction is
qualified yet. Restoration must reinstate the observed original settings, not
merely disable all forwarding. Record successful ordinary answered and unanswered
calls before number release.

Admission checklist, each with a dated evidence reference:

- Provider approves the exact consumer use and end-user registration route.
- Exact voice-capable inventory, number area and itemized quote are confirmed.
- Carrier confirms activation, inspection and restoration instructions for the
  exact plan and destination. Record pre-existing forwarding/voicemail state
  privately before making changes.
- Owner approves the finite spend and participant charge ceilings.
- Registration UX covers pending, approved, rejected, expired and retry states
  in pt-BR. Prefer provider-hosted identity collection; do not collect CPF or ID
  documents in Kevin until secure handling and retention have been reviewed.
- Account creation currently starts the server trial. Decide and disclose how
  pending registration affects usable trial time before inviting participants;
  this source slice does not change trial or billing behavior.
- Reviewed backend SHA, actual serving revision, test app/build, number route,
  callbacks, APNs environment and Apple sandbox environment are bound together.
- Owner's direct-number, conditional-forwarding and restoration smoke tests pass.
- Every test caller and account holder consents to the controlled test. Use
  planned forwarding windows that exclude incidental ordinary callers. The
  owner approves a private record of whether audio, transcripts and summaries
  are captured, who can access them, their retention deadline and verified
  deletion procedure before execution. Existing platform retention alone does
  not settle pilot retention. The owner/operator controls all live calls.

## Call execution sheet

Use aliases P01–P05. The owner/operator keeps real numbers and registration
documents outside Git. Record iPhone/iOS, carrier/plan, DDD, Wi-Fi Calling,
VoLTE, call waiting and Live Voicemail settings. Each scenario is one test call;
cellular and Wi-Fi conditions are distributed across the matrix.

| Call | Scenario | Expected observation |
| --- | --- | --- |
| 1 | Before changes, answer ordinary mobile call | Baseline two-way audio |
| 2 | Before changes, leave ordinary call unanswered | Baseline voicemail/forwarding destination |
| 3 | Call the dedicated Kevin number directly | Correct Personal account, Portuguese screening |
| 4 | After setup, answer ordinary mobile call promptly | Ordinary answered calls remain usable |
| 5 | Unknown caller, no answer on usual mobile number | Confirmed forwarding and correct Kevin account |
| 6 | Known contact under selected forwarding condition | Behavior matches the product's known-contact promise |
| 7 | Busy with call waiting off | Confirmed plan-specific result |
| 8 | Busy with call waiting on | Confirmed plan-specific result; no assumed busy forwarding |
| 9 | Phone powered off | Confirmed plan-specific forwarding/voicemail result |
| 10 | Phone unreachable | Confirmed plan-specific result |
| 11 | Portuguese name, reason and callback in ordinary background noise | Understandable interaction, accurate useful summary |
| 12 | Caller interrupts Kevin and corrects a detail | Interruption handled and correction preserved |
| 13 | Owner accepts from locked phone | Two-way pickup audio and no routing loop |
| 14 | Owner ignores or declines; use offline app on one dedicated tester | Defined fallback, history and notification behavior |
| 15 | Restore original settings and answer ordinary call | Baseline ordinary calling restored |
| 16 | After restoration, leave call unanswered | Original voicemail/forwarding destination restored |

Portuguese synthetic script: “Olá, meu nome é Ana. Estou ligando para confirmar
o horário da visita amanhã. Pode pedir para me retornar?” Use a tester-controlled
callback number only when required. No real customer scenario or sensitive
information is needed. For call 12, correct “amanhã” to “sexta-feira”.

For every applicable call record: result, forwarding evidence, language,
interruption result, pickup audio, transcript/summary correctness, push latency,
summary latency and provider charge. Confirm intended device receipt of SMS
separately from API acceptance and provider delivery status. Voice-only numbers
cannot support a promised SMS channel without a separately qualified sender
route. An offline app is not an excuse to report SMS success without evidence.

Proposed usability targets, subject to owner acceptance: median participant
rating at least 4/5; push within 10 seconds of its server event; summary within
60 seconds of hangup; two-way pickup audio within five seconds of acceptance.
These are pilot targets, not existing guarantees.

Run Apple sandbox checks separately: Brazilian storefront price presentation,
purchase, user cancellation before purchase, restore/reinstall, accelerated
renewal/cancellation and expiry. Sandbox results are not revenue. Do not start
paid production subscriptions to prove qualification.

## Cost worksheet and launch decision

Fill the following from dated quotes or measured invoices; unknown is not zero.

| Cost/input | Unit | Approved ceiling | Actual | Evidence |
| --- | --- | --- | --- | --- |
| Registration/setup | one-time, currency | unset | untested | pending |
| Dedicated numbers | number × full rental term | unset | untested | pending |
| Inbound voice | billed leg-minute, rounding | unset | untested | pending |
| Pickup/conference/voicemail/SIP/media | each applicable leg-minute | unset | untested | pending |
| AI/STT/TTS/backend | usage units and currency | unset | untested | pending |
| SMS and carrier fees, if offered | billed segment/destination | unset | untested | pending |
| Participant forwarding | each mobile plan's billed leg-minute | unset per person | untested | pending |
| Support/verification workload | operator minutes | unset | untested | pending |
| Personal subscription proceeds | net of store fee, tax, FX/refunds | not pilot revenue | unknown | pending |

For a complete quote, calculate the Kevin ceiling as registration/setup plus
number-month rentals plus the sum of each distinct provider leg's rate times
its rounded billable units, SMS segments/carrier fees, AI/STT/TTS/backend units,
taxes and an explicit contingency. Express all amounts in the approved currency
with any FX assumption. The 270 connected minutes bound the scripted caller
time; pickup, conference, voicemail and media may add concurrently billed legs.
Calculate each participant's carrier charge separately using that plan's rate,
rounding, destination and roaming rules. Unknown components keep approval unset.

The release package still needs actual serving source/revision, reviewed
backend candidate, test app/APNs/Apple environment, exact qualified provider
route and callbacks, rollback and a forwarding-restoration receipt. GCP access
last failed reauthentication on October 6; no serving-source refresh or release
was performed in this continuation. The SMS enrollment source is separately
default off and US-only; it cannot qualify Brazil's voice or SMS route.

Calculate Kevin contribution from actual net subscription proceeds minus
Kevin-borne costs. Show the user's carrier forwarding charge separately so the
consumer sees the total price of using the service. Do not advertise unlimited
usage or a Brazil margin from a US headline subscription price.

Stop immediately for wrong-tenant routing, routing loops, broken ordinary calls,
unrestorable voicemail, data exposure, unexpected charges, any approved cap
being reached, or repeated failure of a promised function. Diagnose before
spending a retest. Any provider/type/plan/configuration change invalidates the
affected qualification and needs a new bounded test.

Passing requires the exact consumer route approved, all claimed functions
passing for the selected plan, all five restorations verified, charges
reconciled, and no unresolved critical defect. Keep only anonymous scenario,
build/SHA, UTC time, outcome, latency and cost evidence in Git. Do not release a
number while any participant still forwards calls to it. The result qualifies
this route and plan; it does not prove nationwide coverage.
