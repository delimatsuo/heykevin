# Country readiness after Hey Kevin 1.3.4 (45)

Observed October 1, 2026. Read-only audit of the released package, deployed
backend source, Apple availability and current primary provider guidance.

## Decision

**Keep commercial availability in the United States and Canada for now.**
Kevin has reusable international foundations, but no additional country is
qualified for a dependable Personal launch by the evidence reviewed here.
Listing a country in source is not evidence of successful local provisioning,
carrier forwarding, SMS, billing or customer acceptance.

**Owner direction after the audit, October 1: Brazil Personal first, Canada
second, United Kingdom third.** This replaces the audit's initial Canada-first
recommendation. Brazil's current local-number path remains unqualified, but
published individual-registration routes warrant investigation. The
[Brazil Personal qualification plan](../superpowers/plans/2026-10-01-brazil-personal-first.md)
records the provider comparison, carrier evidence and acceptance sequence.
No country settings or provider resources were changed by this audit.

## Release and live availability

| Check | Observed result |
| --- | --- |
| App Store Connect version | 1.3.4, build 45, `READY_FOR_SALE`, manual release |
| App territories | All 175 returned territories inspected; only USA and CAN are available |
| New-territory opt-in | Disabled |
| Personal, Business, Business Pro | All approved; each available only in USA and CAN |
| Store metadata | English (US) only |
| Public Apple lookup | US and Canada still returned 1.3.3 at the audit read; public catalog propagation of 1.3.4 was not established by that read |
| Production health | Healthy, `kevin-api-00277-neb`, source `b62eea38d9ab5656fb8d809fb872f05d9c81147d` |
| Packaged source | `f047121e5bb13af8a03c43195353f44615370828`, tree `0512222c26b38dd55859b6bbf0b266326fa9b1bb` |
| Source alignment | The packaged and deployed commits have no differences under `app`; fetched `origin/main` is `b8958b1816470cc21d7dcd7bff7ba9554c438fe8` |

Apple findings come from authenticated GETs for the exact app version, its build,
all territory availability records, each subscription's availability, and the
version localizations. The owner's September 30 build 45 phone test passed. No
test carrier/country was supplied, so it is not evidence for additional markets.

## Country assessment

| Market | Readiness | What stands between current service and expansion |
| --- | --- | --- |
| United States | Existing market | Preserve the established release and activation checks; one owner test does not qualify every carrier/plan |
| Brazil | First qualification priority; current Personal route not ready | Twilio's local-number route excludes individuals. Qualify its separate individual mobile route alongside Telnyx's published individual local/mobile route; inventory, Kevin's eligibility, carrier forwarding and margins remain open. See the linked Brazil plan. |
| Canada | Existing market; second qualification priority | Country-aware voice/SMS number provisioning exists, but setup still names US carriers; qualify Rogers/Bell/Telus forwarding, cancellation, charges and summaries; French UI is absent |
| United Kingdom | Third qualification priority | English reduces localization work, but current provisioning skips UK regulatory registration; identity/address handling, SMS sender capability and actual carrier tests are needed |
| Germany, France, Italy, Spain, Portugal | Partial implementation; not ready to launch | Generic address/bundle code does not implement complete country-specific identity/document review; carrier, SMS and language acceptance remain open |
| Australia and other countries | Outside the explicit supported-country list | Add deliberate admission and provisioning support; Australian provider eligibility also needs checking for the intended Personal model |

Current provider requirements: [UK](https://www.twilio.com/en-us/guidelines/gb/regulatory),
[Brazil](https://www.twilio.com/en-us/guidelines/br/regulatory),
[Australia](https://www.twilio.com/en-us/guidelines/au/regulatory).
The Brazil conclusion applies to the current local-number architecture, not to
every possible number type, carrier or service model. Australia's published
guidance includes a business/trade/profession-use condition; provider confirmation
is needed before assuming eligibility for a consumer Personal offering.

## Concrete implementation gaps

1. **Country admission and phone entry are incomplete.** The iPhone signup has
   a US example, a ten-digit minimum, no country selector and no country parameter
   on account creation. The backend supports US, CA, BR, GB, DE, FR, IT, ES and PT,
   but its country detector returns US for an unsupported international number.
   Synthetic AU and MX examples reproduced that fallback. Reject unsupported
   countries clearly before offering a trial or trying to provision a US number.
   See [onboarding](https://github.com/delimatsuo/heykevin/blob/f047121e5bb13af8a03c43195353f44615370828/ios/Kevin/Views/OnboardingView.swift#L294)
   and [country detection](https://github.com/delimatsuo/heykevin/blob/f047121e5bb13af8a03c43195353f44615370828/app/db/contractors.py#L161).

2. **Regulatory provisioning is incomplete.** UK is omitted from the regulatory
   country set despite current provider requirements. Other listed regulated
   countries collect business street/city, create an address with blank region
   and postcode, assign only that address to a bundle, then return the bundle
   after a 30-second wait even if review is pending. The source lacks complete
   individual/business identity, registration documents, durable approval states
   and remediation. Personal users see business-address terminology.
   See [provisioning](https://github.com/delimatsuo/heykevin/blob/f047121e5bb13af8a03c43195353f44615370828/app/db/contractors.py#L579)
   and [address UI](https://github.com/delimatsuo/heykevin/blob/f047121e5bb13af8a03c43195353f44615370828/ios/Kevin/Views/OnboardingView.swift#L700).

3. **Forwarding needs carrier qualification.** Country templates are implemented,
   but the iPhone strips the destination's `+`, has only US-style carrier choices
   for US/CA, and uses generic GSM fallback elsewhere. Vodafone UK's published
   unanswered-call instructions explicitly use a UK destination with `+44`;
   Rogers documents a voicemail interaction. These differences need controlled
   tests, not an assumption that a generic code or an international US number
   works everywhere. Profile country can also change while the existing number
   is retained, so instructions should be bound to the qualified number/carrier.
   See [dial-code construction](https://github.com/delimatsuo/heykevin/blob/f047121e5bb13af8a03c43195353f44615370828/ios/Kevin/Services/ForwardingInstructions.swift#L92),
   [Vodafone](https://www.vodafone.co.uk/help/mobile/how-do-i-manage-call-diverts)
   and [Rogers](https://www.rogers.com/support/mobility/use-call-forwarding).

4. **Voice capability does not establish SMS delivery.** Provisioning explicitly
   requires SMS capability only in US/CA. Owner summaries still pass the tenant's
   voice number as the SMS sender, without a capability or country-specific
   sender check. A Messaging Service may be configured, but its live setup was
   not verified here. Qualify text delivery separately from calls and push.
   See [number search](https://github.com/delimatsuo/heykevin/blob/f047121e5bb13af8a03c43195353f44615370828/app/db/contractors.py#L710)
   and [SMS sender selection](https://github.com/delimatsuo/heykevin/blob/f047121e5bb13af8a03c43195353f44615370828/app/services/sms.py#L44).

5. **Language and price presentation are uneven.** StoreKit's paywall uses
   localized prices, while the preceding mode cards hardcode $9.99/$49.99. The
   app declares English, Spanish and Brazilian Portuguese; 152 of 435 catalog
   entries have neither Spanish nor Portuguese translations. French, German and
   Italian app translations are absent. Multilingual voice engines are present,
   but deterministic Gemini/relay greetings are English and some summary paths
   lack explicit language selection. Test the actual configured engine and the
   full local-language flow rather than advertising universal language coverage.
   See [mode pricing](https://github.com/delimatsuo/heykevin/blob/f047121e5bb13af8a03c43195353f44615370828/ios/Kevin/Views/OnboardingView.swift#L391),
   [StoreKit pricing](https://github.com/delimatsuo/heykevin/blob/f047121e5bb13af8a03c43195353f44615370828/ios/Kevin/Services/SubscriptionManager.swift#L82)
   and [greetings](https://github.com/delimatsuo/heykevin/blob/f047121e5bb13af8a03c43195353f44615370828/app/services/receptionist_context.py#L70).

6. **Country economics are not qualified.** Published Twilio local-number costs
   are USD1.15/month in Canada, USD3.50 in the UK and USD4.25 in Brazil; inbound
   local voice is respectively USD0.0085, USD0.0100 and USD0.0100 per minute.
   These are list prices, not current invoices. They exclude AI, SMS, extra call
   legs, Apple fees, taxes and the customer's own forwarding charges. The normal
   customer flow has a per-call limit but no demonstrated country/tier monthly
   usage budget. Reusing one subscription price abroad is not a margin analysis.
   Sources: [Canada](https://www.twilio.com/en-us/voice/pricing/ca),
   [UK](https://www.twilio.com/en-us/voice/pricing/gb),
   [Brazil](https://www.twilio.com/en-us/voice/pricing/br).

## Recommended next work

1. Execute the [Brazil Personal qualification plan](../superpowers/plans/2026-10-01-brazil-personal-first.md):
   first establish an individual-eligible number/provider route and a compatible
   consumer carrier/plan, then complete country entry, verification, pt-BR,
   localized prices, notifications and sustainable usage limits.
2. Run a bounded owner-controlled Brazilian Personal pilot covering the usual
   number's forwarding, caller ID, screening, live takeover, locked-phone push,
   promised summaries, purchase/restore, cancellation and account deletion.
   Record anonymous outcomes and build/carrier/plan context.
3. Enable Brazil's app **and** Personal subscription only after qualification,
   with accurate local copy and the applicable release/spend decisions.
4. Qualify Canada second within the existing market, then the UK third. Preserve
   current US/Canada service and outstanding activation checks throughout.
   The old Brazil Business-first memo is superseded strategy, not an active queue.

## Verification limits

Two independent source audits covered backend and iOS at the packaged commit;
the master checked live Apple state, production health and current primary
provider sources. Synthetic country-function checks used no customer data.
All three evidence nodes were returned and reduced in code. A separate fresh
review found no material corrections to the original audit after inspecting its
report and cited source. All ten immutable source references were checked and
resolved. The later owner-priority update and additional Brazil research are
recorded separately in the linked qualification plan.
No number was purchased, call placed, customer contacted, country enabled,
subscription changed, ad activated, deployment performed or hosted CI triggered.

Cloud Run configuration inspection could not refresh the current gcloud login.
Current regulatory contact email and regional dial-in mapping were therefore
not verified. Twilio account-specific inventory, approvals, geographic
permissions, sender registration and invoices also remain unverified. These
limits prevent a positive launch-readiness claim; the confirmed source and
provider gaps already support keeping additional markets closed.
