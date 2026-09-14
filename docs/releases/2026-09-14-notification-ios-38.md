# Notification iPhone release candidate — 1.2.12 (38)

The owner agreed on September 14 to prepare the implemented notification
enhancement for TestFlight, validate it on the owner's iPhone, and submit an
App Store release after the device results and release approval. This candidate
implements the active [N1 scope](../../kevin-prd.md).
Native frontend refactoring and later PRD features remain deferred.

## Candidate and Apple inventory

- Source commit: `813e0e3b72e90c709832d72129b82f5e6622775b`.
- Source tree: `96ffa86d7c1e556eed3c90825a03834748fe2269`.
- iOS subtree: `7b3e4de0c529984ec03fed3cfc8c86aeabe60dfb` (later documentation
  commits do not change the packaged application).
- Base: `2e60d09ad374c69c39f45f77b630aa7e4cc3135d`.
- Bundle: `com.kevin.callscreen`; team: `3FLG8W6B95`.
- Target: scheme `Kevin`, configuration `Release`, version `1.2.12`, build `38`.
- Read-only App Store Connect inventory before preparation: public `1.2.11`
  is associated with **build 36** and `READY_FOR_DISTRIBUTION`; the latest
  TestFlight build is **1.2.11 (37)**, `VALID` and `IN_BETA_TESTING`.
- The existing internal QA group automatically receives builds. Existing
  external tester groups and public links are outside this candidate's scope.

The version/build bump and generated project are accompanied by one necessary
packaging correction: `com.apple.developer.usernotifications.time-sensitive`
is enabled. The approved backend already sends `interruption-level=time-sensitive`.
[Apple requires the matching application capability](https://developer.apple.com/videos/play/wwdc2021/10091/).
The user controls Time Sensitive interruptions. Critical Alerts are not enabled,
and this feature promises no Silent Mode or Do Not Disturb bypass.

## Verification and delivery

- The 18 focused backend notification/urgent-handoff checks passed with
  `KEVIN_DISABLE_DOTENV=1`; three existing dependency deprecation warnings.
- Native unit suite: **127 passed, zero failures**, including all 22 call-action
  tests, on a fresh iPhone 16 / iOS 26.0 simulator with unit-test launch isolation.
  XcodeStorage result: `kevin-notification-38-unit-20260914T225103Z-11307`.
  The first invocation did not execute tests because another job held the
  shared Xcode lock. Once the lock was free, one full invocation passed.
- Source CI: all nine required jobs passed for source commit `813e0e3b...` in
  [run 34906616981](https://github.com/delimatsuo/heykevin/actions/runs/34906616981)
  on [PR #247](https://github.com/delimatsuo/heykevin/pull/247). The two deployment
  jobs were skipped. Actual runner time was 362 seconds.
- Independent fresh-context review used `gpt-6-astra`, high reasoning, and
  independently read the native result bundle. The final artifact review approved
  internal TestFlight delivery with no findings: it recomputed the IPA hash,
  checked all 21 packaged files against the extraction, verified signatures,
  entitlements, profile and executable UUID, and read Apple's validation result.
  Automatic Codex review reported exhausted
  code-review usage, and Cursor reported usage-based pricing was required.
  Neither remote review was retried and no billing settings were changed.
- Archive and export succeeded through the required XcodeStorage wrapper.
  Results: `kevin-notification-38-archive-auto-20260914T230535Z-60137` and
  `kevin-notification-38-export-20260914T230911Z-66666`. The initial archive
  command incorrectly forced a distribution identity under automatic signing;
  removing that command-line override resolved the conflict. Separate attempts
  refused to start while other Xcode jobs were active; their locks/processes
  were not changed.
- Exported IPA: 6,250,699 bytes, SHA-256
  `ad6dee32c55e393ed374856399c67e8dca77de5e4b8df824bbef8cdb536afb01`.
  Archive and IPA both contain version `1.2.12`, build `38`, the expected bundle
  identifier, `AppEnvironment=production` and the production backend URL.
- Strict deep signature validation passed. Authority is Apple Distribution:
  Travel Advisory LLC (`3FLG8W6B95`); signed APNs environment is production,
  Time Sensitive is enabled, `get-task-allow=false`, and Critical Alerts is absent.
  Distribution profile `639cf43f-e398-42cb-9927-3b610eb59e4c` matches the team,
  bundle and Time Sensitive capability and expires July 15, 2027. Remote
  notification/VoIP background modes and TwilioVoice `6.13.6` are present.
- Build SDK: iPhoneOS 26.0, Xcode `17A400`. The arm64 executable UUID is
  `2871C2A9-15E3-3484-A835-CF776BE6F703`.
- Apple package validation passed with no errors at `2026-09-14T23:10:15Z`,
  using altool `26.0.18 (170018)`.
- Upload succeeded with no errors at `2026-09-14T23:13:18Z`. Delivery UUID:
  `3038e2c7-4670-4067-99af-f371337332e9`; 6,250,699 bytes transferred in 7.394
  seconds.
- At `2026-09-14T23:18:44Z`, Apple reported build `38` under version `1.2.12`
  as **VALID**, **IN_BETA_TESTING**, unexpired, and present in the existing
  internal **QA** group (`ce552be4-c362-4021-9a90-b77d2e910d15`). The build ID
  matches the delivery UUID above. English What to Test instructions were
  published and read back for this build. External distribution was not changed.
- Physical-device acceptance is **pending**. Simulator or Apple processing
  results do not complete the PRD's phone checks.
- Production backend remains the separately released
  `7377c7ba402297625dec5de97a2250f50b1c8013`, revision `kevin-api-00270-l9s`.
  This iPhone candidate changes no backend code or runtime flags.

The archive, IPA, verification scripts and raw delivery logs were kept in an
ephemeral directory under the configured Codex scratch root, with exact-directory
cleanup on shell exit. The durable evidence is this record; no separate retained
binary archive was requested. The wrapper manages its own build caches/results.

Actions preflight bound repository and billing owner to `delimatsuo`, public
repository `delimatsuo/heykevin`, the source SHA above, and the isolated
`codex/notification-ios-release` branch. PR checks comprise seven Python shards,
quality and the strict fail-closed `Test` aggregator, all on standard Ubuntu
runners with 15/5-minute job timeouts and PR cancellation. The comparable run
used 361 runner seconds: ten rounded minutes, $0.06 at the gross reference rate
and **$0 chargeable runner cost** under
[GitHub's public-repository policy](https://docs.github.com/en/billing/concepts/product-billing/github-actions).
There were no active/queued repository runs at preflight, no Actions artifacts,
and 175 MB of caches under the unchanged 10 GB limit. Account-wide September
paid Actions usage was $60.77, with $7.52 recorded on September 14; the simple
month-end projection was $130.22. This candidate introduces no paid Actions
work, hosted macOS jobs or deployments.

## Owner iPhone test sequence

Use the owner's existing account and an owner-controlled second phone, with
fictional caller names and reasons. Install the candidate over the existing app;
keep existing account data. Open it while unlocked before testing so it can
refresh its notification and urgent-handoff registration. Record the installed
version/build, iOS version, notification/microphone permissions, preview setting,
and Focus setting. Keep customer identities and caller content out of the record.

| Check | Owner action | Expected result |
|---|---|---|
| Summary and navigation | Place an unknown-caller test call: “I'm Alex, calling about a routine service appointment.” Observe a locked-screen call and an unlocked-screen call. Expand the notification, then tap its body. | Caller/reason update in place; Pick up and Take a message are available. Body navigation opens that exact call without answering it. |
| Pick up | On a fresh screened call, choose Pick up. If another pickup surface is available while connecting, use it too. | Only the intended caller connects; two-way audio works; no second connection appears. |
| Take a message | On another screened call, choose Take a message. Watch the pending state and listen from the caller's phone. | Kevin keeps the caller connected. The UI shows pending until acknowledged, then message-taking; it does not claim the message is already recorded. A competing pickup cannot override the decision. |
| Unanswered urgent call | With Urgent alerts enabled, use a fictional urgent service request, such as “There is a water leak at my property and I need to speak to you urgently.” Do not answer. Time from Kevin's owner-wait transition. | Urgent lock-screen wording is generic. After the single 30-second wait Kevin says the owner is unavailable and takes a message. There is no repeated wait or claim that help was dispatched. |
| Native urgent answer | Make another fictional urgent call and answer the native incoming-call screen. | The existing incoming call is reused, with two-way audio and no duplicate outgoing call. |
| Saved preference | Disable Alert me for urgent calls, leave Settings, relaunch, and repeat the urgent scenario. Restore the owner's preferred setting afterward. | The saved preference persists; extra urgent banners/rings are suppressed while screening, transcription and fallback continue. |
| Old alert and dismissal | End test call A. While test call B is active, tap A's retained notification. On another call, dismiss its alert without choosing an action. | A opens its own summary or an honest unavailable state and never answers B. Dismissal alone does not answer or disconnect a caller. No late update revives an ended call. |
| Trusted-contact regression | Call from an owner-controlled trusted contact and answer. | Existing direct ring-through and two-way audio work without a duplicate call. |
| Owner delivery unavailable | Temporarily disconnect the owner's iPhone from the network. Start a fictional urgent call from the second phone, wait through the owner fallback, then reconnect the iPhone and open Kevin. Restore the original connectivity settings. | The caller remains connected and Kevin takes a message after the bounded wait. Any retained alert opens the correct ended-call result and cannot connect another call. This tests unavailable owner delivery, not a forced APNs-provider error. |

Record pass/fail and the observed result for each row. If a row fails, retain
the exact build, state and reproduction steps and fix the demonstrated defect.
Injected tests cover delayed responses, network failures and account changes;
their results must remain distinct from what was actually exercised on a phone.
Record the PRD's remaining notification-failure and delayed-summary acceptance
checks explicitly before marking N1 complete. App Store submission remains
pending device results and owner release approval.

## Routing evidence

Master model: gpt-6-astra
Builder model/tier: gemini-3.7-flash-low
Routing reason: Three pinned configuration values; parent generated the project,
verified the diff, and owns Git, release packaging and evidence.
agy transport=headless: true
Input tokens: 55278
Output tokens: 797
Total tokens: 56075
Retries: 0
Audit defects found: Existing missing Time Sensitive capability identified by the
independent staff review and confirmed against Apple's implementation guidance.
Audit disposition: Configuration correction applied; source and signed-artifact
reviews passed with no remaining findings. Physical-device acceptance is pending.
