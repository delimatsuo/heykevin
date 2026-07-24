# Caller-UX acceptance contract

**Status:** unsealed proposal. This document is a Task 3.5 documentation gate,
not an execution permit. It does not authorize provider probing, participant
recruitment, caller testing, recording, manifest sealing, or Task 4.8. It makes
no support claim for an unlisted language or access mode.

The values below are qualification-only acceptance criteria. They do not change
or characterize production behavior. Production routing, prompts, models, VAD,
pacing, silence handling, credentials, and deployment configuration remain
outside this contract.

## Seal prerequisites

Before sealing, the owner and the independent staff-architecture,
security/privacy, and conversation-product reviewers must approve this exact
contract with no unresolved P1. The sealed digest must bind every capability
probe, technical window, participant window, evaluator, and approval envelope.

The following remain open and prevent sealing:

- the exact nonproduction providers, accounts, regions, subprocessors, retention
  settings, and participant-processing notices;
- independent validation of the power calculation and localized scripts;
- a separately approved participant-data protocol and, if needed, a separate
  caller-side recording authorization;
- a clean exact-SHA production-isolation review and the external controls required
  by Task 3.4.

Insufficient evidence, an unresolved review finding, an unknown privacy setting,
or a failed hard gate yields `no_winner`. Averages, post-hoc pooling, or tuning
cannot waive that result.

## Evidence windows

The evaluation has three distinct stages:

1. two independently sealed, synthetic technical windows;
2. one later, separately approved closed-loop participant window for every arm
   that passes both technical windows.

The technical windows are not human studies. The participant window is not
duplicated and cannot rescue a failed technical hard gate. Thresholds, source,
models, prompts, dependency configuration, tasks, scoring, and analysis are
frozen before the first applicable window, with no in-window tuning.

## Qualified spoken-language scope

The proposed finite cohorts are:

| Cohort ID | Spoken-language variety | Inclusion boundary |
| --- | --- | --- |
| `en_us_general` | United States English | Fluent speakers of General American English, including declared US regional accents. |
| `es_north_american` | North American Spanish | Fluent speakers of Mexican Spanish or US Spanish with Mexican, Caribbean, or Central American influence. |
| `zh_standard_mandarin` | Standard Mandarin Chinese | Fluent speakers of Putonghua/Standard Mandarin, including declared Mainland-China and US-diaspora accents. This does not qualify Cantonese or another Chinese language. |

The only qualified code-switch pairs are both directions of:

- United States English and North American Spanish;
- United States English and Standard Mandarin.

Every bilingual participant and rater must demonstrate conversational fluency in
both languages through a scripted screening administered by a person fluent in
both. Each code-switch task begins in one language, switches to the other, and
returns, producing an observation in both directions without resetting call,
tenant, pending-question, or confirmed-fact state.

No result may be generalized to “all languages.” An unlisted language, accent
outside the declared inclusion boundary, or code-switch pair is unqualified even
if a provider can process it.

## Qualified access-mode scope

The proposed participant window is voice-only. TTY, RTT, speech-generating
devices, and DTMF interaction are unqualified. DTMF never bypasses authentication,
authorization, or lifecycle controls. A voice-capable caller receives the bounded
unsupported-mode explanation below and may continue by voice or end the call.
If voice transport is unavailable, the experiment makes no support claim and
does not invent an alternate path.

This exclusion is not evidence that the production product lacks an access mode.
It limits only what the bakeoff may claim.

## Caller-heard timing contract

For a delivered question, the first silence timer starts only after
`caller_playback_observed`, or after the separately sealed conservative
`playback_inferred` deadline when caller-side observation is unavailable. A
transport mark, queue receipt, generation completion, or last token does not arm
the timer and does not prove that the caller heard audio.

The proposed sequence is:

1. wait 10,000 ms for caller activity;
2. if none occurs, play one localized presence check;
3. after that act is caller-observed, or reaches the sealed conservative
   inference deadline, wait another 10,000 ms;
4. if no caller activity supersedes it, play one localized closure and allow the
   lifecycle controller to tear down only after that closure is caller-observed,
   or after the sealed transport-loss fallback.

Any caller activity cancels the active timer before a new act is reserved. Speech
beginning exactly at a timer boundary wins over the timer. Timer ordering is
decided by the monotonic event sequence, not wall-clock callback arrival.

A `more_time` request is allowed once per pending question. It cancels the active
timer and plays the localized acknowledgement. Once that acknowledgement is
caller-observed, an immutable deadline is set exactly 20,000 ms later. If the
original presence check has not occurred, expiry emits that one presence check and
then follows the normal second 10,000 ms wait. If the presence check already
occurred, expiry emits the localized closure without another presence check.
Further `more_time` requests do not cancel, replace, or move the immutable
deadline; they are recorded as caller activity without another acknowledgement or
unsupported promise.

The governing response latency and clear gates remain:

- ground-truth last caller-speech sample to first caller playback:
  p95 at most 1,500 ms and maximum 2,500 ms;
- interruption to Twilio clear: p95 at most 250 ms and maximum 500 ms;
- ground-truth intentional caller-speech onset to last audible assistant sample:
  p95 at most 750 ms and maximum 1,000 ms.

The last measure starts at caller-side speech onset and ends at the last
assistant sample audible in caller-side PCMU. It is measured for every
manifest-labeled intentional interruption. Backchannels and noise are separate
negative cases and must produce zero false clears. The rationale and non-
imperceptibility limitation are pinned in ADR 0002; the threshold cannot be
weakened after results are visible.

Failure-known dead-air is also bounded. From classification of a recoverable
failure to first caller playback of the repair, p95 is at most 1,500 ms and the
maximum is 2,500 ms. From classification as irrecoverable while every positive
closure binding remains proven to first caller playback of the local generic
closure, p95 is at most 750 ms and the maximum is 1,000 ms. A reconnect may wait
at most 3,000 ms for a proven new epoch; otherwise it becomes transport loss or
irrecoverable failure according to the observed transport state. The uncertain-
binding path intentionally emits no audio and is excluded from an audible
dead-air gate because speech itself would be an unauthorized disclosure.

## Localized fixed acts

Exact recordings, speakers, pronunciation, and acoustic normalization remain
sealed artifacts. Dual-fluent reviewers must approve the Spanish and Mandarin
utterances as pragmatic equivalents, not merely literal translations.

| Act | United States English | North American Spanish | Standard Mandarin |
| --- | --- | --- | --- |
| presence check | “Are you still there?” | “¿Sigue ahí?” | “请问您还在吗？” |
| more-time acknowledgement | “Take your time. I’ll wait twenty more seconds.” | “Tómese su tiempo. Esperaré veinte segundos más.” | “您慢慢来。我会再等二十秒。” |
| unsupported language | “This test can continue only in English, Spanish, or Mandarin. Please choose one of those languages.” | “Esta prueba solo puede continuar en inglés, español o mandarín. Elija uno de esos idiomas.” | “本次测试只能使用英语、西班牙语或普通话。请选择其中一种语言。” |
| unsupported access mode | “This voice test cannot use keypad or text calling. Please speak, or end the call.” | “Esta prueba de voz no admite el teclado ni llamadas de texto. Hable o finalice la llamada.” | “本次语音测试不支持按键或文字通话。请直接说话，或结束通话。” |
| simulated voicemail | “This test cannot record a message. You can end the call now.” | “Esta prueba no puede grabar un mensaje. Puede finalizar la llamada ahora.” | “本次测试不能录制留言。您现在可以结束通话。” |
| input repair | “Sorry, I didn’t catch that. Please say it once more.” | “Lo siento, no entendí. Dígalo una vez más, por favor.” | “抱歉，我没有听清。请再说一遍。” |
| output replay preface | “Sorry. I’ll repeat that once.” | “Lo siento. Lo repetiré una vez.” | “抱歉。我再重复一遍。” |
| reconnect repair | “We’re connected again. Please repeat only your last answer.” | “Ya estamos conectados de nuevo. Repita solo su última respuesta, por favor.” | “我们已重新连接。请只重复您刚才的回答。” |
| opt-out closure | “Okay. I’ll stop this test call now. Goodbye.” | “De acuerdo. Finalizaré esta llamada de prueba ahora. Adiós.” | “好的。我现在结束这次测试通话。再见。” |
| generic failure closure | “I’m sorry, I can’t continue this test call. Goodbye.” | “Lo siento, no puedo continuar con esta llamada de prueba. Adiós.” | “抱歉，我无法继续这次测试通话。再见。” |
| silence closure | “I can’t hear a response, so I’ll end this test call now. Goodbye.” | “No escucho una respuesta, así que finalizaré esta llamada de prueba. Adiós.” | “我没有听到回应，所以现在结束这次测试通话。再见。” |

The unsupported-language behavior is one deterministic local asset, not three
candidate-selected variants. It plays exactly once in this order: English,
Spanish, then Mandarin, using the three cells above with a 250 ms pause between
them. The finite unlisted-language challenges are French (`fr_fr`) and Modern
Standard Arabic (`ar_msa`). Their callers are not counted as qualified-language
support; fluent French and Arabic reviewers confirm only that the input was an
unlisted language and the fixed asset/order was used.

## Fallback and repair state matrix

All dynamic speech is a previously authorized semantic act. A fallback cannot
create a tool call, write, notification, transfer, recording, callback, owner
follow-up, or real voicemail.

| Trigger | Caller-facing behavior | Timer effect | Repetition | Result and side effects |
| --- | --- | --- | --- | --- |
| `repeat` after a complete caller-heard act | Replay the same complete authorized act in the active qualified language. | Cancel while caller is active; re-arm only if the replay is a question and becomes caller-observed. | Once per request; no content expansion. | Nonterminal; no real side effect. |
| `slower` after a complete caller-heard act | Replay the same complete authorized act using the sealed qualification-only slower rendering. | Same as `repeat`. | Once per request; does not change global or production pacing. | Nonterminal; no real side effect. |
| `more_time` with a pending question | Play the fixed acknowledgement and apply the immutable 20-second extension above. | Cancel the current timer once; later requests cannot move the extension deadline. | One acknowledgement and extension per pending question. | Nonterminal; no promise or notification. |
| Scripted in-call opt-out | Cancel pending speech and actions; stop new capture and provider forwarding; and revoke every provider, media, business, and non-closure capability. Retain only a dedicated one-use local-closure capability, revalidate every positive closure binding, atomically consume that capability to play the fixed opt-out closure, and then revoke and tear down. A failed predicate emits no audio. | Cancel all silence timers permanently. | Once; terminal. | Harness-controlled teardown; deletion is reported only after residue verification. |
| Actual or ambiguous participant withdrawal | Stop the entire research session and follow the consent-ledger withdrawal process. Any unscripted or ambiguous “stop,” “opt out,” or “I don’t want to continue” is withdrawal. Scripted opt-out applies only after the facilitator confirms before the call that the participant remains enrolled while role-playing that exact step. | Cancel all timers and further tasks. | Not applicable. | No replacement or deletion claim outside the sealed protocol. |
| Caller requests voicemail | Explain that voicemail is simulated and no message can be recorded. | Caller activity cancels the timer; a new timer requires a later delivered question. | Once per request. | Nonterminal opportunity to respond or hang up; no audio persistence, SMS, or owner promise. |
| Unlisted language | Play the fixed three-language choice once, then wait 10 seconds for a qualified-language response. | A one-time 10-second fallback timer starts from caller receipt/inference. | One prompt; a second unqualified response uses the generic failure closure only when every positive closure binding remains proven; otherwise it takes the uncertain-binding no-audio path. | No translation claim, routing change, or provider retry. |
| Unsupported access mode with usable voice | Play the fixed unsupported-mode explanation once. | No timer unless a later delivered question creates one. | Once. | Voice may continue; no auth bypass or alternate-channel promise. |
| Recoverable STT/input failure | Play the fixed input-repair act within the failure-known deadline. | Cancel the prior timer; a later question must earn a new receipt. | One repair total for the call across all recoverable classes. | Preserve only confirmed facts; no guessed input. |
| Recoverable generation/TTS/playout failure before caller-heard completion | Play the fixed output-replay preface and replay only a complete rendering of the affected authorized semantic act within the failure-known deadline. If that affected act has no complete rendering, classify the failure as irrecoverable; never replay a prior act. | No question timer until the repaired question is caller-observed. | One repair total for the call. | No new facts, questions, or action. A second failure becomes irrecoverable. |
| Recoverable reconnect with proven new epoch and unchanged binding | Play the fixed reconnect-repair act within the failure-known deadline and accept only a repeat of the unconfirmed answer. | Stale timers and acts remain cancelled; new timers require new receipt. | One repair total for the call. | Confirmed facts survive; partial or stale acts do not. |
| Irrecoverable conversational/provider failure with every positive closure binding proven | Play the fixed generic failure closure once from a deterministic local non-provider asset. Required proof is a current authenticated active-execution record plus current call, session, epoch, tenant, destination, privacy posture, unrevoked dedicated one-use local-closure capability, and caller-transport bindings. Atomically consume that capability to play; teardown occurs only after caller-side observation/inference and no superseding caller activity. | Cancel all question/silence timers. | No retry. | No reason disclosure, semantic repair, action, or promise. |
| Any uncertain active-execution, call, session, epoch, tenant, destination, authentication, privacy posture, capability, transport binding, or external outcome | Emit no outbound audio or semantic act. | Cancel all timers and revoke capabilities. | No retry. | Harness-controlled fail-closed teardown; no disclosure or claim about an external result. |
| Irrecoverable caller transport loss | Audible closure may be impossible. | Cancel all timers. | No retry. | Harness-controlled teardown with a bounded residue-only receipt. |

The recoverable repair budget is global to the call: at most one repair across
generation, STT, TTS, playout, and reconnect. Security/privacy failure, uncertain
binding, or uncertain external outcome never consumes that repair to continue.

## Participant-data and consent schedule

The closed-loop window remains blocked until a separate signed protocol supplies
the exact providers, regions, subprocessors, and custodians. Its minimum contract
is:

- adults aged 18 or older, recruited only in the United States under a reviewed
  jurisdiction list; no minors, customer callers, incidental speakers, real
  business data, or production identities;
- informed consent in every participant's validated preferred written language,
  with approved English, Spanish, Simplified Chinese, Traditional Chinese, French,
  and Arabic forms at minimum. Each form is approved for pragmatic equivalence
  and explains live provider processing, exact providers/subprocessors and
  regions, data categories, risks, recording default, withdrawal cutoff,
  deletion, incident handling, and whom to contact;
- synthetic personas and nonproduction phone identities only; participants are
  instructed not to state real names, phone numbers, addresses, health details,
  payment data, or customer information;
- raw participant audio and raw transcripts off by default. Transient audio
  forwarding is limited to the approved call. Any caller-side recording requires
  its own signed authorization; without it, the participant window cannot claim a
  caller-audio timing result and relies on the synthetic technical windows for
  that gate;
- the default no-recording participant window stores only structured task and
  survey fields and has no participant-audio semantic rater or seven-day retest.
  Three-rater semantic adjudication and retest use separately authorized synthetic
  technical-window artifacts. If participant-audio adjudication is later required,
  a separate signed recording authorization is mandatory and must bind a distinct
  KMS key, encrypted replay custody, streaming-only short-lived rater access, an
  at-most-14-day audio TTL, provider/log/backup/cache/derivative deletion, and
  deletion receipts before aggregate sealing;
- opt-out immediately stops new capture and provider forwarding, cancels pending
  acts, and revokes every provider, media, business, and non-closure capability.
  Only the dedicated one-use local-closure capability may remain long enough for
  the exhaustive positive-predicate recheck and atomic consumption; it is then
  revoked before teardown. Already collected material follows the consent
  schedule; deletion is never promised before the residue audit succeeds;
- a separate pseudonymous consent ledger, encrypted with a distinct KMS boundary,
  stores identity, consent version, jurisdiction, and withdrawal status. Only the
  research coordinator can link it to the random participant ID. Implementers,
  providers, evaluators, and raters cannot access that link;
- the evidence store accepts only the allowlisted participant ID, cohort,
  endpoint/network stratum, bounded event timings, fixed-choice rater labels, and
  fixed-choice survey responses. It rejects phone numbers, account IDs,
  transcripts, free text, raw provider payloads, and audio unless separately
  authorized;
- least-privileged access is limited to the research coordinator for the consent
  ledger, the evidence custodian for bounded events, and blinded raters for their
  assigned short-lived review surface. Access is logged and expires with the
  window;
- participant-level bounded evidence has a proposed 30-day TTL after window
  closure; the consent ledger has a proposed 90-day TTL after aggregate sealing.
  Earlier valid withdrawal triggers deletion from active storage, provider
  artifacts, request logs/traces, caches, exports, backups, and external or
  internal derivatives, followed by a deletion receipt. Anonymous published
  aggregates cannot be selectively removed after the disclosed sealing cutoff;
- a suspected disclosure, incidental speaker, unapproved data category,
  provider-setting drift, cross-region drift, or residue failure stops the window,
  revokes access, invokes the incident process, and yields `no_winner`.

## Participant and rater design

The proposed closed-loop design has five non-overlapping main strata with 72
complete adults each:

- 72 United States English participants complete monolingual English calls;
- 72 North American Spanish participants complete monolingual Spanish calls;
- 72 Standard Mandarin participants complete monolingual Mandarin calls;
- 72 English-Spanish bilingual participants complete the declared code-switch
  task in both directions in every arm call;
- 72 English-Mandarin bilingual participants complete the declared code-switch
  task in both directions in every arm call.

This produces 360 complete main-study participants. Recruit 90 per stratum to
allow up to 20% attrition. In addition, recruit 20 French and 20 Modern Standard
Arabic speakers to obtain 16 complete participants in each unlisted-language
challenge. Those 32 participants evaluate only the fixed unsupported-language
journey and do not count toward a qualified-language claim.

Each French and Arabic participant hears the shared multilingual asset once, not
once per candidate arm, and completes a fixed-choice, no-recording survey.
Sixteen of 16 in each language must identify that the test supports English,
Spanish, or Mandarin and that the next action is to choose one of those languages
or end the call. Failure yields `no_winner`. Every candidate arm's
unsupported-language trigger, exact asset, timer, and terminal path is evaluated
separately in both synthetic technical windows.

A participant belongs to one stratum and completes one synthetic task call with
every surviving arm. For two, three, or four surviving arms, all 2, 6, or 24
possible arm orders are replicated equally across the 72-person main stratum.
Task variants are balanced independently within each order. No participant
receives more than four arm calls. Breaks and order effects are recorded. A
replacement is allowed only before unblinding and must match stratum,
endpoint/network, and age band; there is no imputation or post-hoc pooling.

The sample size targets a paired standardized difference of 0.5 on the
preregistered seven-point comparison measures, 80% power, and family-wise
two-sided alpha 0.05 across at most six arm-pair comparisons. A conservative
Bonferroni planning alpha of 0.00833 gives a normal-approximation minimum of 49
complete paired observations; 72 is pinned to cover the paired-t correction and
exact replication of every possible arm order. The calculation and code must be
independently validated before sealing. If validation requires more than 72, the
higher number, rounded up to preserve exact order balance, replaces 72 before
recruitment; it can never be reduced after results are visible.

Binary hard gates use separate precision reporting. With 72 of 72 successes, the
exact one-sided 95% lower bound on success is about 95.9%; with zero of 72
failures, the exact one-sided 95% upper bound on failure is about 4.1%. Therefore
the 95% direct-relevance gate requires 72 of 72 in each main stratum and arm to
meet both the observed gate and the confidence rule. Zero-tolerance gates require
zero observed failures and report, but do not misrepresent, the 4.1% population
upper bound. A failed observed hard gate still yields `no_winner`.

Per surviving arm and main stratum, every core measure has 72 complete calls.
Each bilingual stratum provides 72 complete observations in each declared
code-switch direction per arm. The following challenge cells each have exactly
eight complete instances per arm and main stratum, assigned in a preregistered
schedule with at most three challenge cells in a call:

- correction;
- necessary follow-up;
- silence;
- caller speech at the timeout boundary;
- `more_time`;
- `repeat`;
- `slower`;
- scripted opt-out;
- simulated voicemail;
- unsupported access mode;
- backchannel/noise that must not interrupt;
- recoverable failure followed by success;
- recoverable failure followed by a second failure;
- authenticated generic failure closure;
- uncertain-binding fail-closed behavior;
- degraded network and reconnect;
- controlled noisy environment.

These eight-case cells prove finite journey coverage only; they do not estimate a
population rate. Any observed hard-gate violation still yields `no_winner`, and
the report must show the cell's wide exact confidence interval rather than pool it
into another cohort.

Intentional interruption is a core condition, not an eight-case challenge cell.
Every participant completes one intentional interruption in every arm call.
Within each arm and main stratum, the 72 calls are balanced as 24 early, 24
middle, and 24 final-position interruptions. The perceived-talk-over gate
therefore has 72 observations per arm and main stratum and uses the binary
precision rule above. Positions are reported separately as 24-case descriptive
strata, but the preregistered 72-case arm-by-main-stratum gate is never pooled
across arms or main strata.

Endpoint strata per main stratum are pinned at 12 iOS-cellular earpiece, 12
iOS-cellular speakerphone, 12 Android-cellular earpiece, 12 Android-cellular
speakerphone, 12 consumer-VoIP handset/headset, and 12 landline handset/speaker
participants. Age bands are 24 aged 18–34, 24 aged 35–54, and 24 aged 55 or
older. At least 18 per language stratum must represent a declared non-dominant
regional/diaspora accent. Speech rate is balanced using the scripted screening.
No health or diagnostic data is collected, and this diversity does not create an
accessibility support claim. The noisy-environment cell uses a sealed,
level-calibrated background-noise fixture rather than incidental speakers.

Three blinded raters score every semantic outcome in the synthetic technical
windows. Language-dependent cases use raters fluent in the cohort language;
code-switch cases use raters fluent in both, and the two unlisted-language
challenges use fluent French and Arabic raters respectively. Krippendorff's alpha
is computed separately for nominal and ordinal semantic labels and must be at
least 0.80 in every cohort and arm. Ten percent of synthetic cases, stratified by
arm, cohort, and challenge cell, are blindly rescored after at least seven days
and require at least 95% exact retest agreement. Adjudication follows the
preregistered rule and never exposes candidate identity. Participant-window
semantic ratings or retests are forbidden under the default no-recording path.

Participant outcomes remain analytically separate from rater reliability:

- objective task completion, direct-answer comprehension, pending-question
  comprehension, correction uptake, next-step expectation, and absence of
  premature closure are hard gates;
- caller effort, follow-up necessity, naturalness, and trust use preregistered
  seven-point participant scales as comparison metrics only;
- after every intentional interruption, 72 of 72 participants in each arm and
  main stratum must rate “The assistant stopped speaking quickly enough for me to
  continue” as 6 or 7 on a seven-point scale. No arm, main stratum, or
  interruption position may be pooled to hide a failure. Failure is an absolute
  participant `no_winner` gate; it is not merely a relative comparison;
- semantic completeness, relevance, safety content, one-question compliance,
  repair classification, and fallback correctness are blinded-rater outcomes.

Every observed hard-gate case must pass except gates with an explicitly stricter
rate above. One unauthorized action, unsupported promise, missed intentional
interruption, false clear, privacy failure, or evidence-integrity failure yields
`no_winner`. Silent termination also yields `no_winner` when the current
authenticated active-execution record, call, session, epoch, tenant, destination,
privacy posture, unrevoked closure capability, and caller-transport bindings are
all proven and usable. Mandatory no-audio teardown for uncertainty in any of those
bindings is the required fail-closed behavior, not a silent-termination failure.

## User-facing verification matrix

The sealed protocol must make the following caller-observable, using
counterbalanced tasks in each cohort and code-switch direction:

- direct answers precede any necessary follow-up, with at most one question;
- corrections replace stale facts on the next eligible turn;
- silence, boundary speech, `more_time`, repeat, slower speech, opt-out,
  simulated voicemail, and unsupported language/access-mode paths follow the
  exact state tables above;
- deliberate early, middle, and final interruptions stop caller-heard assistant
  audio within the ADR threshold, while labeled backchannels/noise do not clear;
- after interruption or reconnect, the response is coherent and contains no
  stale audio, repeated committed fact, answered question, or lost correction;
- one recoverable repair may succeed; a second failure cannot retry;
- authenticated generic closure is audible, bounded, and non-specific, while
  uncertain binding produces no disclosure or outbound speech;
- the caller understands the task result and next step, without a fabricated
  callback, owner notification, recording, or transfer;
- production routes and configuration remain untouched, and production calls are
  never used as qualification evidence.
