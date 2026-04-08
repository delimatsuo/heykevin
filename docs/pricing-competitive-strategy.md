# Kevin AI — Pricing & Competitive Strategy

## Context

Kevin AI needs a pricing model for two tiers (Personal and Business) that covers API costs, positions competitively against Jobber Copilot and other AI receptionists, and accounts for the AI diagnostic feature unique to Kevin.

---

## Per-User Cost Model

### Personal Mode (~5 calls/day, 150/month)

| Service | Calculation | Monthly Cost |
|---|---|---|
| Twilio Voice | 225 min x $0.0085 | $1.91 |
| Twilio Media Streams | 225 min x $0.004 | $0.90 |
| Twilio SMS | ~50 msgs x $0.012 | $0.60 |
| Twilio Number | 1 number | $1.15 |
| Deepgram Nova-3 | 225 min x $0.0077 | $1.73 |
| Claude Sonnet 4 | ~150 calls x 6.5 exchanges | $3.37 |
| ElevenLabs Flash | ~150 calls x 6.5 x 110 chars | $8.58 |
| Cloud Run (shared) | Proportional | ~$0.50 |
| **Total cost per user** | | **~$18.74/mo** |

### Business Mode (~20 calls/day, 600/month)

| Service | Calculation | Monthly Cost |
|---|---|---|
| Twilio Voice | 1,500 min x $0.0085 | $12.75 |
| Twilio Media Streams | 1,500 min x $0.004 | $6.00 |
| Twilio SMS | ~600 msgs x $0.012 | $7.20 |
| Twilio Number | 1 number | $1.15 |
| Deepgram Nova-3 | 1,500 min x $0.0077 | $11.55 |
| Claude Sonnet 4 | ~600 calls x 6.5 exchanges | $13.46 |
| ElevenLabs Flash | ~600 calls x 6.5 x 110 chars | $34.32 |
| Cloud Run (shared) | Proportional | ~$1.50 |
| **Subtotal (base)** | | **~$87.93/mo** |

### Business Mode + AI Diagnostics (add-on)

| Service | Calculation | Monthly Cost |
|---|---|---|
| Gemini Vision API | ~100 photo analyses x $0.002/image | $0.20 |
| Twilio MMS (inbound) | ~100 MMS x $0.01 | $1.00 |
| Twilio MMS (outbound results) | ~100 x $0.02 | $2.00 |
| Extra Claude (job card + estimate) | Marginal | ~$1.00 |
| **Diagnostics add-on cost** | | **~$4.20/mo** |

**Total business + diagnostics cost: ~$92/mo**

---

## Competitive Landscape

| Competitor | Price | Per-Call Cost | AI Diagnostics | Multi-language | CRM Integration |
|---|---|---|---|---|---|
| **Jobber Copilot** | ~$39-49/mo ADD-ON (requires $49-249/mo Jobber plan) | Included (limits apply) | No | Limited | Native (Jobber only) |
| **Rosie AI** | $49-299/mo | Unlimited mins | No | EN/ES only | Jobber, HCP, ServiceTitan |
| **Smith.ai (AI)** | $97-390/mo | $3.25-9.75/call | No | Yes | Many via Zapier |
| **Ruby** | $235-1,640/mo | $3.28-4.70/min | No | EN/ES | Limited |
| **Goodcall** | $59-249/mo | By unique caller | No | Limited | Google Business |
| **Upfirst** | $25-160/mo | ~$0.53-0.83/call | No | Limited | Basic |
| **iOS 26 Call Screen** | Free | Free | No | No | None |
| **Kevin AI (Personal)** | ? | ~$0.12/call | No | All languages | N/A |
| **Kevin AI (Business)** | ? | ~$0.15/call | YES (unique) | All languages | Jobber + GCal |

### Kevin's Differentiators vs. Jobber Copilot

1. **AI Photo/Video Diagnostics** — No competitor offers this. Caller sends a photo → Gemini analyzes → sends estimate + scheduling link. This alone justifies a premium.
2. **Works without Jobber** — Jobber Copilot requires a Jobber subscription ($49-249/mo). Kevin works standalone or with Jobber/GCal.
3. **All languages** — Kevin auto-detects and responds in any language. Jobber is English-focused.
4. **Intelligent call screening** — Trust scoring, spam detection with SIT tones, urgency escalation. Jobber treats all calls equally.
5. **Personal mode** — Dual-use for contractors who use one phone. Jobber has no personal mode.
6. **Voice quality** — ElevenLabs voices vs. Jobber's (reported as robotic by users).

### Jobber Copilot Weaknesses (from user reviews)

- "Sounds robotic" — #1 complaint
- Poor handling of complex questions
- No warm transfer mid-call
- No urgency prioritization
- No multilingual support
- Locked into Jobber ecosystem
- No photo/video diagnostics

---

## Proposed Pricing

### Option A: Competitive Undercut

| Tier | Price | Margin | Target |
|---|---|---|---|
| **Personal** | $9.99/mo | ~47% ($4.73 margin on $18.74 avg cost for 5 calls/day; lighter users = higher margin) | Anyone with a phone |
| **Business** | $49.99/mo | ~43% on a 10 call/day user ($26.58 cost), negative on 20 call/day heavy user | Solo contractors, small shops |
| **Business Pro** | $79.99/mo | ~13% on 20 call/day heavy user, ~60% on 10 call/day | Contractors with AI diagnostics + Jobber |

**Rationale:** Undercuts Jobber Copilot ($39-49 add-on + $49-249 base = $88-298 total). Kevin Business at $49.99 standalone is cheaper than Jobber Core + Copilot ($88+). Business Pro with diagnostics is still cheaper than Jobber Connect + Copilot ($168+).

### Option B: Value-Based (Premium Positioning)

| Tier | Price | Margin | Target |
|---|---|---|---|
| **Personal** | $14.99/mo | ~60% on light user (2-3 calls/day) | Anyone with a phone |
| **Business** | $69.99/mo | ~20% on 20 call/day heavy user, ~60% on 10 call/day | Solo contractors |
| **Business Pro** | $99.99/mo | ~8% on 20 call/day heavy user, ~50% on average user | Full-featured: diagnostics + scheduling |

**Rationale:** Positioned between Rosie ($49-299) and Smith.ai ($97-390). The AI diagnostics feature is unique — no competitor has it. Justify the premium by the value: a single captured lead ($275-1,200 revenue) pays for 3-12 months of Kevin.

---

## Key Strategic Notes

1. **Heavy users lose money at low price points.** A 20 call/day contractor costs ~$88/mo in API fees. At $49.99, that's a loss. However, most "20 call/day" contractors likely average 10-12, where costs are ~$44/mo.

2. **Volume discounts from providers help at scale.** ElevenLabs Scale plan ($99/mo for 2M chars) covers ~4-5 heavy business users. Deepgram Growth pricing drops to $0.0065/min. At 100+ users, per-user costs drop 20-30%.

3. **The real moat is AI diagnostics.** No one else does photo/video → diagnosis → estimate → booking. This is worth a premium and justifies Business Pro.

4. **Personal mode is a funnel.** A plumber signs up for Personal ($9.99) → realizes Kevin handles work calls too → upgrades to Business ($49-69). Personal users also have zero incremental cost for features (no diagnostics, no Jobber, no job cards).

5. **iOS 26 Call Screen is both threat and validator.** Apple made call screening mainstream, but their version is basic (no message-taking, no routing, no scheduling). Kevin is the "pro" version.

---

## Next Steps

- [ ] Finalize pricing tier selection (Option A vs B)
- [ ] Build the inbound MMS webhook to complete the diagnostic flow
- [ ] Test full diagnostic pipeline end-to-end
- [ ] Add Stripe billing integration
- [ ] Plan App Store submission with subscription pricing
