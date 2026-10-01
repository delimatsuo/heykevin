# Brazil Personal first — country qualification plan

**Owner direction:** October 1, 2026: Brazil first for personal use, Canada second,
United Kingdom third.
**Status:** Active qualification plan; no additional country is launch-ready.
**Baseline:** [Country-readiness audit](../../audits/2026-10-01-country-readiness.md)
of iOS 1.3.4 (45) and its deployed backend.

This order replaces the audit's initial Canada-first recommendation and the
August Brazil memo's Business-first scope. The target customer is an individual
using an iPhone and an existing Brazilian mobile number. The product must work
without requiring that customer to register a company or supply a CNPJ.
Existing US/Canada service remains supported while expansion work focuses on
Brazil. Canada qualification follows Brazil; UK qualification follows Canada.

## First decision: an eligible consumer number and carrier combination

There are plausible consumer routes to qualify. Published registration rules
establish leads; they do not establish purchasable voice inventory, Kevin's
eligibility to assign numbers to end users, or a functioning call path.

| Route | Evidence checked October 1 | Decision still needed |
| --- | --- | --- |
| Twilio mobile number for an individual | [Brazil regulatory guidance](https://www.twilio.com/en-us/guidelines/br/regulatory) has a mobile-number individual path; its local-number path excludes individuals. | Confirm voice inventory, individual/end-user eligibility, documents, price and inbound forwarding. Kevin currently searches local numbers only. |
| Telnyx local/mobile number for an individual | [Brazil DID requirements](https://support.telnyx.com/en/articles/5464041-brazil-did-requirements) list CPF, identity evidence, Brazilian address and recent address proof for personal registration. | Confirm Kevin's account/service model, available DDD and number type, verification workflow, voice/SIP capabilities and a Brazil-specific quote. |

Compare these two routes before selecting a provider. Nvoip is a backup lead:
its [virtual-number page](https://www.nvoip.com.br/numero-virtual/) mentions SIP
and CPF in portability requirements, which does not establish new consumer DID
rental or Kevin's use eligibility. Plivo's published
[Brazil local-number requirements](https://www.plivo.com/phone-numbers/pricing/br/)
still require a CNPJ, so this research found no consumer advantage there.

If an external carrier qualifies, [Twilio BYOC](https://www.twilio.com/docs/voice/bring-your-own-carrier-byoc)
is one candidate for connecting its SIP traffic to the existing voice pipeline.
That architecture is untested for Kevin. Prove tenant mapping, original caller
and diversion information, authenticated ingress, media, latency, live pickup
and combined costs before selecting it. It does not remove number-registration
requirements or imply a migration of existing customers.

The next decision record must include account-specific eligibility and inventory,
the selected number type/DDD, an itemized quote, the identity-review lifecycle and
the exact supported carrier/plan. Choose the pilot locality from eligible testers
and inventory; the old memo's São Paulo DDD 11 is not an owner requirement.

## Carrier qualification

| Carrier | Current official evidence | What remains open |
| --- | --- | --- |
| TIM | [Siga-me](https://www.tim.com.br/servicos-tim/servicos-basicos/siga-me) documents postpaid conditional forwarding and excludes prepaid; the forwarded leg is charged as an outgoing call. | Controle eligibility, exact plan allowance/cost, acceptance of the selected destination and physical iPhone behavior. |
| Claro | [Mobile Siga-me FAQ](https://www.claro.com.br/faq/ligacoes-e-sms/o-que-e-o-siga-me) describes forwarding to fixed/mobile numbers with plan-dependent charges. | The reviewed page describes unconditional forwarding; external conditional forwarding and eligible retail offers remain unverified. |
| Vivo | Bounded research found [business fixed-line guidance](https://vivo.com.br/para-empresas/produtos-e-servicos/servicos-essenciais/voz-e-colaboracao/planos-ilimitados/voz-basica), not definitive consumer-mobile documentation. | Consumer-mobile availability, plan eligibility, charges, instructions and destination acceptance. Missing evidence does not establish unavailability. |

Start qualification with an eligible TIM postpaid offer because its published
conditional path is the clearest lead. This is a research sequence, not a carrier
endorsement or a reason to make users change plans. Add Claro and Vivo only when
their exact offers qualify. A first pilot need not cover all three carriers.

For each eligible combination, test no-answer, busy, powered-off and out-of-range
conditions separately; preserve ordinary answered calls. Include call waiting,
carrier voicemail, iPhone Live Voicemail, VoLTE/Wi-Fi Calling and original caller
identity. TIM's [call-waiting FAQ](https://www.tim.com.br/ajuda/perguntas-frequentes/facilidades)
documents an interaction with busy forwarding, so one successful no-answer call
cannot qualify every condition.

Capture the prior forwarding state and verify targeted removal and restoration.
Claro's [voicemail guidance](https://www.claro.com.br/servicos/utilidades/claro-recado)
says its broad cancellation command also removes voicemail forwarding; it is
not a harmless universal undo step. Verify the carrier's exact destination
format and codes on the intended offer before putting them in onboarding.
Measure same-DDD charges separately from cross-DDD and roaming; unlimited
ordinary minutes do not establish free forwarded calls.

## Bounded implementation sequence after the route decision

1. **Country admission and phone entry.** Add explicit Brazil/+55/DDD entry,
   normalized and validated numbers, and a clear unsupported-country response.
   Bind the account's country, provider, number type and capabilities together;
   changing a profile country must not reinterpret an already assigned number.
2. **Individual registration.** Implement the selected provider's required
   identity workflow, with pending, approved, rejected and retry states. Prefer
   provider-hosted verification if available; otherwise define secure collection,
   access, retention and deletion before accepting CPF/ID documents. Give honest
   service/trial status while the customer cannot yet receive calls.
3. **Forwarding setup and proof.** Show only qualified carrier/plan instructions,
   expected customer charges and how to undo setup. A direct call to Kevin's
   number is not proof that calls to the user's normal number are forwarded.
4. **Brazilian Portuguese and pricing.** Complete pt-BR signup, setup, paywall,
   notifications and errors; qualify greetings, conversation, transcript and
   summary with the actual voice engine. Use StoreKit storefront prices wherever
   a price appears. Set BRL pricing from measured costs and demand, not a guessed
   exchange conversion of the US price.
5. **Notification delivery.** Qualify push and in-app summaries. If SMS remains
   part of the offer, qualify a permitted, capable sender and include its cost;
   a voice DID alone does not establish SMS support. Removing the existing text
   promise is a separate product decision, not a silent fallback.
6. **Operating costs and limits.** Model number rental, all voice/SIP/media legs,
   AI, notifications, Apple proceeds/fees, taxes, FX and support. Separate Kevin's
   costs from the subscriber's carrier charges. Evaluate light, typical and heavy
   usage, choose included usage and enforce bounded limits with understandable
   customer behavior. No unlimited-use or margin claim is established today.

Before a live pilot, resolve the recording/privacy and cross-border data terms
for the selected provider flow, including caller notice and account deletion.
This plan records the work needed; it does not determine legal compliance.

## Small Personal pilot and acceptance

Propose five consenting Personal testers across the first qualified combinations.
This is a usability and reliability check, not proof of market conversion. Set
the number/call budget, test-account handling and stop conditions before running
it. The owner controls real calls; no participants have been recruited.

Each combination needs a recorded pass for:

- Individual signup, verification and number assignment, with useful recovery
  from pending/rejected verification and unavailable inventory.
- Calls to the usual personal number, accurate caller identity, natural pt-BR
  screening, readable transcript/summary, live takeover and locked-phone push.
- Ordinary known-caller behavior, unanswered/busy/unreachable cases, app/network
  failures and safe forwarding removal with the prior voicemail state restored.
- Accurate Brazil storefront price, sandbox purchase/restore/cancellation,
  subscription expiry, promised SMS delivery and account deletion.
- Actual provider and carrier charges reconciled to the proposed offer.

Retain anonymous outcomes with build/backend, carrier/offer, number type and DDD.
Keep phone numbers, CPF/ID documents and raw caller content out of Git evidence.
A failed route stays unsupported; do not generalize a pass to every offer on the
same carrier. No broad prepaid, nationwide or all-language claim follows from
this pilot.

After qualification, prepare matching pt-BR store/support/privacy copy and
Brazil availability for both the app and Personal subscription. Paid launch
and acquisition follow the existing release and spend decisions. The previous
US campaign allowance does not become a Brazil advertising budget.

## Evidence and current completion

The provider and carrier research nodes were combined in code: two expected,
two returned, ten findings. A separate fresh-context review found no material
corrections, including after reopening Twilio, Telnyx and TIM primary sources.
All 78 relative document links resolved and the documentation diff passed its
whitespace check. No application tests were needed for this documentation change.
The country audit's source and live-state snapshot remain the release baseline.
No provider account, country availability or live service was changed during
this planning update. Account-specific inventory, quotes and carrier-call
acceptance remain open.
