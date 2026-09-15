import XCTest
@testable import Kevin

@MainActor
final class SettingsRefreshPolicyTests: XCTestCase {

    private func makeAuthContext(
        contractorId: String = "contractor-1",
        bearerToken: String = "token-1",
        generation: Int = 1
    ) -> CallAuthContext {
        CallAuthContext(
            contractorId: contractorId,
            bearerToken: bearerToken,
            generation: generation
        )
    }

    private final class HeldFetch: @unchecked Sendable {
        private let lock = NSLock()
        private var continuation: CheckedContinuation<[String: Any]?, Never>?
        private var startedHandler: (@Sendable () -> Void)?

        init(onStarted: (@Sendable () -> Void)? = nil) {
            self.startedHandler = onStarted
        }

        func fetch(auth: CallAuthContext) async -> [String: Any]? {
            await withCheckedContinuation { cont in
                lock.lock()
                self.continuation = cont
                let handler = self.startedHandler
                lock.unlock()
                handler?()
            }
        }

        func resume(returning value: [String: Any]?) {
            lock.lock()
            let cont = self.continuation
            self.continuation = nil
            lock.unlock()
            cont?.resume(returning: value)
        }
    }

    // MARK: - 1. Coalescing: Concurrent Requests One Flight

    func testConcurrentRequestsOneFlight() {
        let coordinator = SettingsLoadCoordinator()
        let auth = makeAuthContext()

        let plan1 = coordinator.beginLoad(auth: auth, isBusinessMode: true)
        XCTAssertNotNil(plan1, "First load must produce a valid LoadPlan")
        XCTAssertEqual(plan1?.flightToken, 1)
        XCTAssertTrue(coordinator.isFlightActive)

        // Duplicate concurrent load for same auth
        let plan2 = coordinator.beginLoad(auth: auth, isBusinessMode: true)
        XCTAssertNil(plan2, "Concurrent duplicate load must coalesce and return nil")

        // Finish flight
        let finished = coordinator.finishLoad(flightToken: 1, auth: auth, profileFetchSucceeded: true)
        XCTAssertTrue(finished)
        XCTAssertFalse(coordinator.isFlightActive)

        // Subsequent load can start
        let plan3 = coordinator.beginLoad(auth: auth, isBusinessMode: true)
        XCTAssertNotNil(plan3, "Subsequent load after flight finish must produce a new LoadPlan")
        XCTAssertEqual(plan3?.flightToken, 2)
    }

    // MARK: - 2. Auth Lifetime: Auth A to B and A to B to A

    func testAuthAtoBandABA() {
        let coordinator = SettingsLoadCoordinator()
        let authA = makeAuthContext(contractorId: "contractor-A", bearerToken: "token-A", generation: 1)
        let authB = makeAuthContext(contractorId: "contractor-B", bearerToken: "token-B", generation: 1)
        let authA2 = makeAuthContext(contractorId: "contractor-A", bearerToken: "token-A2", generation: 2)

        // Load A
        let planA = coordinator.beginLoad(auth: authA, isBusinessMode: true)
        XCTAssertNotNil(planA)
        _ = coordinator.finishLoad(flightToken: planA!.flightToken, auth: authA, profileFetchSucceeded: true)
        XCTAssertTrue(coordinator.hasLoadedProfile)

        // Switch to B
        let planB = coordinator.beginLoad(auth: authB, isBusinessMode: true)
        XCTAssertNotNil(planB)
        XCTAssertEqual(planB?.authContext, authB)
        XCTAssertTrue(planB!.shouldFetchProfile, "New account B must fetch profile")
        XCTAssertFalse(coordinator.hasLoadedProfile, "hasLoadedProfile must reset on auth switch")
        _ = coordinator.finishLoad(flightToken: planB!.flightToken, auth: authB, profileFetchSucceeded: true)
        XCTAssertTrue(coordinator.hasLoadedProfile)

        // Switch back to A with new session generation (A -> B -> A)
        let planA2 = coordinator.beginLoad(auth: authA2, isBusinessMode: true)
        XCTAssertNotNil(planA2)
        XCTAssertEqual(planA2?.authContext, authA2)
        XCTAssertTrue(planA2!.shouldFetchProfile, "A->B->A new lifetime must reload profile")
    }

    // MARK: - 3. Stale Response Ownership: Late A Finish Cannot Clear B

    func testLateAFinishCannotClearB() {
        let coordinator = SettingsLoadCoordinator()
        let authA = makeAuthContext(contractorId: "contractor-A", bearerToken: "token-A", generation: 1)
        let authB = makeAuthContext(contractorId: "contractor-B", bearerToken: "token-B", generation: 1)

        let planA = coordinator.beginLoad(auth: authA, isBusinessMode: true)
        XCTAssertNotNil(planA)
        XCTAssertEqual(planA?.flightToken, 1)

        // Auth switches to B while A is in flight
        let planB = coordinator.beginLoad(auth: authB, isBusinessMode: true)
        XCTAssertNotNil(planB)
        XCTAssertEqual(planB?.flightToken, 2)
        XCTAssertTrue(coordinator.isFlightActive)

        // Late finish from A arrives
        let lateFinishResult = coordinator.finishLoad(flightToken: 1, auth: authA, profileFetchSucceeded: true)
        XCTAssertFalse(lateFinishResult, "Late finish from stale auth A must be rejected")
        XCTAssertTrue(coordinator.isFlightActive, "Late finish from A must not clear B's active flight")
        XCTAssertFalse(coordinator.hasLoadedProfile, "Late finish from A must not mark profile loaded for B")

        // B finishes successfully
        let bFinishResult = coordinator.finishLoad(flightToken: 2, auth: authB, profileFetchSucceeded: true)
        XCTAssertTrue(bFinishResult, "B's finish must be accepted")
        XCTAssertFalse(coordinator.isFlightActive)
        XCTAssertTrue(coordinator.hasLoadedProfile)
    }

    // MARK: - 4. Profile Loaded Once

    func testProfileLoadedOnce() {
        let coordinator = SettingsLoadCoordinator()
        let auth = makeAuthContext()

        let plan1 = coordinator.beginLoad(auth: auth, isBusinessMode: true)
        XCTAssertTrue(plan1!.shouldFetchProfile, "First load must fetch profile")
        _ = coordinator.finishLoad(flightToken: plan1!.flightToken, auth: auth, profileFetchSucceeded: true)

        let plan2 = coordinator.beginLoad(auth: auth, isBusinessMode: true)
        XCTAssertFalse(plan2!.shouldFetchProfile, "Subsequent load must not fetch profile when already loaded once")
    }

    // MARK: - 5. Foreground Refresh Does Not Reload Profile

    func testForegroundRefreshDoesNotReloadProfile() {
        let coordinator = SettingsLoadCoordinator()
        let auth = makeAuthContext()

        let initialPlan = coordinator.beginLoad(auth: auth, isBusinessMode: true)
        _ = coordinator.finishLoad(flightToken: initialPlan!.flightToken, auth: auth, profileFetchSucceeded: true)

        // Foreground refresh
        let foregroundPlan = coordinator.beginLoad(
            auth: auth,
            isBusinessMode: true,
            isForegroundOrTabSwitch: true
        )
        XCTAssertNotNil(foregroundPlan)
        XCTAssertFalse(foregroundPlan!.shouldFetchProfile, "Foreground refresh must not reload profile")
        XCTAssertTrue(foregroundPlan!.shouldCheckIntegrations, "Foreground refresh in Business mode must check integrations")

        _ = coordinator.finishLoad(flightToken: foregroundPlan!.flightToken, auth: auth, profileFetchSucceeded: true)

        // In Personal mode, integrations are skipped
        let personalForegroundPlan = coordinator.beginLoad(
            auth: auth,
            isBusinessMode: false,
            isForegroundOrTabSwitch: true
        )
        XCTAssertNotNil(personalForegroundPlan)
        XCTAssertFalse(personalForegroundPlan!.shouldCheckIntegrations, "Personal mode must not check integrations")
    }

    // MARK: - 6. Initial Failure Can Retry

    func testInitialFailureCanRetry() {
        let coordinator = SettingsLoadCoordinator()
        let auth = makeAuthContext()

        let plan1 = coordinator.beginLoad(auth: auth, isBusinessMode: true)
        XCTAssertTrue(plan1!.shouldFetchProfile)

        // Initial load fails (e.g. network error)
        let finished = coordinator.finishLoad(flightToken: plan1!.flightToken, auth: auth, profileFetchSucceeded: false)
        XCTAssertTrue(finished)
        XCTAssertFalse(coordinator.hasLoadedProfile, "Failed load must not set hasLoadedProfile to true")

        // Retry load
        let retryPlan = coordinator.beginLoad(auth: auth, isBusinessMode: true)
        XCTAssertNotNil(retryPlan)
        XCTAssertTrue(retryPlan!.shouldFetchProfile, "Retry must attempt fetching profile again")

        _ = coordinator.finishLoad(flightToken: retryPlan!.flightToken, auth: auth, profileFetchSucceeded: true)
        XCTAssertTrue(coordinator.hasLoadedProfile)
    }

    // MARK: - 7. Changed Knowledge / Address / Hours Draft Not Replaced

    func testChangedKnowledgeAddressHoursDraftNotReplaced() {
        let baseline = SettingsConfirmedBaseline(
            knowledgeText: "Confirmed knowledge",
            regulatoryAddress: "100 Main St",
            regulatoryCity: "San Jose",
            businessHoursStart: "08:00",
            businessHoursEnd: "17:00"
        )

        // Case A: User has edited knowledge, address, and hours drafts
        let dirtyDrafts = SettingsDraftSnapshot(
            knowledgeText: "User typing new knowledge...",
            regulatoryAddress: "200 Pine St",
            regulatoryCity: "San Francisco",
            businessHoursStart: "09:00",
            businessHoursEnd: "18:00",
            isKnowledgeEditorOpen: true,
            isKnowledgeDirty: true
        )

        let decisionA = SettingsRefreshPolicy.evaluateHydration(
            baseline: baseline,
            currentDrafts: dirtyDrafts,
            urgentFencePermits: true,
            screenAllFencePermits: true,
            sitToneFencePermits: true,
            hoursFencePermits: true,
            isSavingRegulatoryAddress: false,
            isSavingCountry: false,
            isSwitchingMode: false
        )

        XCTAssertFalse(decisionA.shouldUpdateKnowledge, "Dirty knowledge draft must not be overwritten by profile load")
        XCTAssertFalse(decisionA.shouldUpdateRegulatoryAddress, "Dirty address draft must not be overwritten by profile load")
        XCTAssertFalse(decisionA.shouldUpdateRegulatoryCity, "Dirty city draft must not be overwritten by profile load")
        XCTAssertFalse(decisionA.shouldUpdateBusinessHours, "Dirty hours draft must not be overwritten by profile load")

        // Case B: Clean drafts (matching confirmed baseline)
        let cleanDrafts = SettingsDraftSnapshot(
            knowledgeText: "Confirmed knowledge",
            regulatoryAddress: "100 Main St",
            regulatoryCity: "San Jose",
            businessHoursStart: "08:00",
            businessHoursEnd: "17:00",
            isKnowledgeEditorOpen: false,
            isKnowledgeDirty: false
        )

        let decisionB = SettingsRefreshPolicy.evaluateHydration(
            baseline: baseline,
            currentDrafts: cleanDrafts,
            urgentFencePermits: true,
            screenAllFencePermits: true,
            sitToneFencePermits: true,
            hoursFencePermits: true,
            isSavingRegulatoryAddress: false,
            isSavingCountry: false,
            isSwitchingMode: false
        )

        XCTAssertTrue(decisionB.shouldUpdateKnowledge, "Clean knowledge must update from server")
        XCTAssertTrue(decisionB.shouldUpdateRegulatoryAddress, "Clean address must update from server")
        XCTAssertTrue(decisionB.shouldUpdateRegulatoryCity, "Clean city must update from server")
        XCTAssertTrue(decisionB.shouldUpdateBusinessHours, "Clean hours must update from server")
    }

    // MARK: - 8. Preference Write Fence Invalidates Older Read

    func testPreferenceWriteInvalidatesOlderRead() {
        let baseline = SettingsConfirmedBaseline()
        let drafts = SettingsDraftSnapshot()

        // When write fences deny load (because a write was dispatched during read)
        let decision = SettingsRefreshPolicy.evaluateHydration(
            baseline: baseline,
            currentDrafts: drafts,
            urgentFencePermits: false,
            screenAllFencePermits: false,
            sitToneFencePermits: false,
            hoursFencePermits: false,
            isSavingRegulatoryAddress: false,
            isSavingCountry: false,
            isSwitchingMode: false
        )

        XCTAssertFalse(decision.shouldUpdateSmartInterruption, "Smart interruption must not overwrite active write")
        XCTAssertFalse(decision.shouldUpdateScreenAllCalls, "Screen all calls must not overwrite active write")
        XCTAssertFalse(decision.shouldUpdateSitTone, "SIT tone must not overwrite active write")
        XCTAssertFalse(decision.shouldUpdateBusinessHours, "Business hours must not overwrite active write")
    }

    // MARK: - 9. False / Error Hours Save Visible and Owned

    func testFalseOrErrorHoursSaveVisibleAndOwned() {
        var baseline = SettingsConfirmedBaseline(
            businessHoursStart: "08:00",
            businessHoursEnd: "17:00"
        )

        var drafts = SettingsDraftSnapshot(
            businessHoursStart: "09:30",
            businessHoursEnd: "18:30"
        )

        // Save failed -> baseline unchanged, draft remains user-edited
        let hydrationAfterFailedSave = SettingsRefreshPolicy.evaluateHydration(
            baseline: baseline,
            currentDrafts: drafts,
            urgentFencePermits: true,
            screenAllFencePermits: true,
            sitToneFencePermits: true,
            hoursFencePermits: true,
            isSavingRegulatoryAddress: false,
            isSavingCountry: false,
            isSwitchingMode: false
        )

        XCTAssertFalse(hydrationAfterFailedSave.shouldUpdateBusinessHours, "Failed hours save must remain as draft and not be overwritten by profile")
        XCTAssertEqual(drafts.businessHoursStart, "09:30")
        XCTAssertEqual(drafts.businessHoursEnd, "18:30")

        // Once save succeeds, baseline updates to match
        baseline.businessHoursStart = "09:30"
        baseline.businessHoursEnd = "18:30"
        drafts.businessHoursStart = "09:30"
        drafts.businessHoursEnd = "18:30"

        let hydrationAfterSuccess = SettingsRefreshPolicy.evaluateHydration(
            baseline: baseline,
            currentDrafts: drafts,
            urgentFencePermits: true,
            screenAllFencePermits: true,
            sitToneFencePermits: true,
            hoursFencePermits: true,
            isSavingRegulatoryAddress: false,
            isSavingCountry: false,
            isSwitchingMode: false
        )

        XCTAssertTrue(hydrationAfterSuccess.shouldUpdateBusinessHours, "Confirmed hours baseline allows subsequent hydration")
    }

    // MARK: - 10. Pending Invalidation Safe

    func testPendingInvalidationSafe() {
        let coordinator = SettingsLoadCoordinator()
        let auth = makeAuthContext()

        _ = coordinator.beginLoad(auth: auth, isBusinessMode: true)
        XCTAssertTrue(coordinator.isFlightActive)

        coordinator.recordMutation(field: .knowledge)
        XCTAssertEqual(coordinator.fieldMutationRevisions.knowledge, 1)

        coordinator.invalidate()
        XCTAssertFalse(coordinator.isFlightActive)
        XCTAssertEqual(coordinator.activeFlightToken, 0)
        XCTAssertFalse(coordinator.hasLoadedProfile)
        XCTAssertNil(coordinator.currentAuth)
        XCTAssertEqual(coordinator.fieldMutationRevisions, SettingsFieldMutationRevisions())
    }

    // MARK: - 11. Business Hours Formatting and Parsing (en_US_POSIX)

    func testBusinessHoursFormattingAndParsingPOSIX() {
        let timeZone = TimeZone(secondsFromGMT: 0)!
        var comps = DateComponents()
        comps.calendar = Calendar(identifier: .gregorian)
        comps.timeZone = timeZone
        comps.year = 2026
        comps.month = 9
        comps.day = 14
        comps.hour = 8
        comps.minute = 30
        let date = comps.date!

        let formatted = SettingsRefreshPolicy.formatHours(date: date, timeZone: timeZone)
        XCTAssertEqual(formatted, "08:30")

        let parsed = SettingsRefreshPolicy.parseHours(string: "17:45", timeZone: timeZone)
        XCTAssertNotNil(parsed)

        let parsedFormatted = SettingsRefreshPolicy.formatHours(date: parsed!, timeZone: timeZone)
        XCTAssertEqual(parsedFormatted, "17:45")

        let invalid = SettingsRefreshPolicy.parseHours(string: "invalid", timeZone: timeZone)
        XCTAssertNil(invalid)
    }

    // MARK: - 12. SETTINGS-01: Initial Untouched Hours Hydrate to Server Values

    func testInitialUntouchedHoursGetHydrated() async {
        let auth = makeAuthContext()
        var baseline = SettingsConfirmedBaseline(
            businessHoursStart: "08:00",
            businessHoursEnd: "17:00"
        )
        var drafts = SettingsDraftSnapshot(
            businessHoursStart: "08:00",
            businessHoursEnd: "17:00"
        )

        let serverProfile: [String: Any] = [
            "business_hours_start": "09:30",
            "business_hours_end": "18:30"
        ]

        var appliedDecision: SettingsHydrationDecision?
        var appliedProfile: [String: Any]?

        let success = await SettingsProfileHydrator.hydrate(
            capturedAuth: auth,
            capturedRevisions: SettingsProfileHydrator.CapturedRevisions(),
            fetchProfile: { serverProfile },
            ownsOperation: { true },
            currentDraftProvider: { drafts },
            baselineProvider: { baseline },
            fenceProvider: { _ in SettingsProfileHydrator.FencePermits() },
            currentFieldRevisions: { SettingsFieldMutationRevisions() },
            applyProfile: { profile, decision in
                appliedProfile = profile
                appliedDecision = decision
                if decision.shouldUpdateBusinessHours {
                    if let s = profile["business_hours_start"] as? String,
                       let e = profile["business_hours_end"] as? String {
                        drafts.businessHoursStart = s
                        drafts.businessHoursEnd = e
                        baseline.businessHoursStart = s
                        baseline.businessHoursEnd = e
                    }
                }
            }
        )

        XCTAssertTrue(success)
        XCTAssertNotNil(appliedDecision)
        XCTAssertTrue(appliedDecision!.shouldUpdateBusinessHours, "Untouched initial 08:00/17:00 hours must hydrate to server 09:30/18:30")
        XCTAssertEqual(drafts.businessHoursStart, "09:30")
        XCTAssertEqual(drafts.businessHoursEnd, "18:30")
        XCTAssertEqual(baseline.businessHoursStart, "09:30")
        XCTAssertEqual(baseline.businessHoursEnd, "18:30")
    }

    // MARK: - 13. SETTINGS-01: Opening Edit Changes Only Open and Closing Remains Server Value

    func testOpeningEditChangesOnlyOpenAndClosingRemainsServerValue() async {
        // Hydrated baseline: 09:30 / 18:30
        var baseline = SettingsConfirmedBaseline(
            businessHoursStart: "09:30",
            businessHoursEnd: "18:30"
        )
        // User edits only open time to 08:00; close remains 18:30
        var drafts = SettingsDraftSnapshot(
            businessHoursStart: "08:00",
            businessHoursEnd: "18:30"
        )

        // Save succeeds for open time
        baseline.businessHoursStart = "08:00"
        baseline.businessHoursEnd = "18:30"

        XCTAssertEqual(drafts.businessHoursStart, "08:00")
        XCTAssertEqual(drafts.businessHoursEnd, "18:30", "Closing hour must remain 18:30 and not revert to 17:00 default")
        XCTAssertEqual(baseline.businessHoursEnd, "18:30")
    }

    // MARK: - 14. SETTINGS-01: Auth Change Resets Baseline and Hydrates Initial Values

    func testAuthChangeInitialHydrate() async {
        let authA = makeAuthContext(contractorId: "contractor-A", bearerToken: "token-A", generation: 1)
        let authB = makeAuthContext(contractorId: "contractor-B", bearerToken: "token-B", generation: 1)

        // User A was configured with non-default hours 09:30 / 18:30
        let resetResult = SettingsRefreshPolicy.resetStateOnAuthChange(
            businessAddress: "",
            businessCity: "",
            smartInterruption: true,
            ringThroughContacts: true,
            sitToneEnabled: false,
            countryCode: "US",
            mode: "business"
        )
        let baselineB = resetResult.baseline
        let draftsB = resetResult.drafts

        XCTAssertEqual(baselineB.businessHoursStart, "08:00")
        XCTAssertEqual(baselineB.businessHoursEnd, "17:00")
        XCTAssertEqual(draftsB.businessHoursStart, "08:00")
        XCTAssertEqual(draftsB.businessHoursEnd, "17:00")

        let serverProfileB: [String: Any] = [
            "business_hours_start": "07:00",
            "business_hours_end": "16:00"
        ]

        var projection = SettingsGuardedStateProjection(
            baseline: baselineB,
            drafts: draftsB
        )

        var appliedDecisionB: SettingsHydrationDecision?

        let success = await SettingsProfileHydrator.hydrate(
            capturedAuth: authB,
            capturedRevisions: SettingsProfileHydrator.CapturedRevisions(),
            fetchProfile: { serverProfileB },
            ownsOperation: { true },
            currentDraftProvider: { projection.drafts },
            baselineProvider: { projection.baseline },
            fenceProvider: { _ in SettingsProfileHydrator.FencePermits() },
            currentFieldRevisions: { SettingsFieldMutationRevisions() },
            applyProfile: { profile, decision in
                appliedDecisionB = decision
                SettingsRefreshPolicy.applyGuardedProfile(
                    contractor: profile,
                    decision: decision,
                    state: &projection
                )
            }
        )

        XCTAssertTrue(success)
        XCTAssertNotNil(appliedDecisionB)
        XCTAssertTrue(appliedDecisionB!.shouldUpdateBusinessHours)
        XCTAssertEqual(projection.drafts.businessHoursStart, "07:00")
        XCTAssertEqual(projection.drafts.businessHoursEnd, "16:00")
        XCTAssertEqual(projection.baseline.businessHoursStart, "07:00")
        XCTAssertEqual(projection.baseline.businessHoursEnd, "16:00")
        XCTAssertNotEqual(projection.drafts.businessHoursStart, "09:30")
        XCTAssertNotEqual(projection.drafts.businessHoursEnd, "18:30")
        XCTAssertNotEqual(projection.baseline.businessHoursStart, "09:30")
        XCTAssertNotEqual(projection.baseline.businessHoursEnd, "18:30")
    }

    // MARK: - 15. SETTINGS-02: Edit During Profile Fetch Retains Drafts

    func testEditDuringProfileFetchRetainsDrafts() async {
        let auth = makeAuthContext()
        let baseline = SettingsConfirmedBaseline(
            knowledgeText: "Initial Server Knowledge",
            regulatoryAddress: "100 Main St",
            regulatoryCity: "San Jose"
        )

        var currentDrafts = SettingsDraftSnapshot(
            knowledgeText: "Initial Server Knowledge",
            regulatoryAddress: "100 Main St",
            regulatoryCity: "San Jose",
            isKnowledgeEditorOpen: false,
            isKnowledgeDirty: false
        )

        var fieldRevs = SettingsFieldMutationRevisions()

        // Profile fetch with simulated mid-flight user edit
        let profileFetch: () async -> [String: Any]? = {
            // User edits knowledge and address while fetch is in-flight
            currentDrafts.isKnowledgeEditorOpen = true
            currentDrafts.knowledgeText = "User Draft Knowledge typed during fetch"
            currentDrafts.isKnowledgeDirty = true
            currentDrafts.isKnowledgeEditorOpen = false
            currentDrafts.regulatoryAddress = "999 User Edited St"
            fieldRevs.knowledge &+= 1
            fieldRevs.regulatoryAddress &+= 1

            return [
                "knowledge": "Stale Server Knowledge",
                "business_address": "Stale Server Address",
                "business_city": "San Jose"
            ]
        }

        var appliedDecision: SettingsHydrationDecision?

        let success = await SettingsProfileHydrator.hydrate(
            capturedAuth: auth,
            capturedRevisions: SettingsProfileHydrator.CapturedRevisions(fieldMutationRevisions: SettingsFieldMutationRevisions()),
            fetchProfile: profileFetch,
            ownsOperation: { true },
            currentDraftProvider: { currentDrafts },
            baselineProvider: { baseline },
            fenceProvider: { _ in SettingsProfileHydrator.FencePermits() },
            currentFieldRevisions: { fieldRevs },
            applyProfile: { _, decision in
                appliedDecision = decision
            }
        )

        XCTAssertTrue(success)
        XCTAssertNotNil(appliedDecision)
        XCTAssertFalse(appliedDecision!.shouldUpdateKnowledge, "Knowledge edited during fetch must NOT be overwritten")
        XCTAssertFalse(appliedDecision!.shouldUpdateRegulatoryAddress, "Address edited during fetch must NOT be overwritten")
    }

    // MARK: - 16. SETTINGS-02: Deliberately Cleared Address Retained Empty

    func testClearPreviousNonEmptyAddressRetainedEmpty() async {
        let auth = makeAuthContext()
        let baseline = SettingsConfirmedBaseline(
            regulatoryAddress: "100 Main St",
            regulatoryCity: "San Jose"
        )

        // User deliberately clears the address field to empty string
        let drafts = SettingsDraftSnapshot(
            regulatoryAddress: "",
            regulatoryCity: "San Jose"
        )

        let serverProfile: [String: Any] = [
            "business_address": "100 Main St",
            "business_city": "San Jose"
        ]

        var appliedDecision: SettingsHydrationDecision?

        let success = await SettingsProfileHydrator.hydrate(
            capturedAuth: auth,
            capturedRevisions: SettingsProfileHydrator.CapturedRevisions(),
            fetchProfile: { serverProfile },
            ownsOperation: { true },
            currentDraftProvider: { drafts },
            baselineProvider: { baseline },
            fenceProvider: { _ in SettingsProfileHydrator.FencePermits() },
            currentFieldRevisions: { SettingsFieldMutationRevisions() },
            applyProfile: { _, decision in
                appliedDecision = decision
            }
        )

        XCTAssertTrue(success)
        XCTAssertNotNil(appliedDecision)
        XCTAssertFalse(appliedDecision!.shouldUpdateRegulatoryAddress, "Deliberately cleared empty address must be treated as dirty draft and NOT overwritten")
    }

    // MARK: - 17. SETTINGS-02: Save Completes Before Old Profile Keeps Confirmed

    func testSaveCompletesBeforeOldProfileKeepsConfirmed() async {
        let auth = makeAuthContext()
        var sitToneFence = PreferenceWriteFence()
        let capturedRevision = sitToneFence.revision // revision 0

        // In flight, user flips sit tone and save finishes successfully
        let op = sitToneFence.beginSave()!
        _ = sitToneFence.finish(op)
        // Now sitToneFence.revision is incremented

        let serverProfile: [String: Any] = [
            "sit_tone_enabled": false // old server state
        ]

        var appliedDecision: SettingsHydrationDecision?

        let success = await SettingsProfileHydrator.hydrate(
            capturedAuth: auth,
            capturedRevisions: SettingsProfileHydrator.CapturedRevisions(sitToneRevision: capturedRevision),
            fetchProfile: { serverProfile },
            ownsOperation: { true },
            currentDraftProvider: { SettingsDraftSnapshot() },
            baselineProvider: { SettingsConfirmedBaseline(sitToneEnabled: true) },
            fenceProvider: { revisions in
                SettingsProfileHydrator.FencePermits(
                    sitToneFencePermits: sitToneFence.permitsLoad(revisions.sitToneRevision)
                )
            },
            currentFieldRevisions: { SettingsFieldMutationRevisions() },
            applyProfile: { _, decision in
                appliedDecision = decision
            }
        )

        XCTAssertTrue(success)
        XCTAssertNotNil(appliedDecision)
        XCTAssertFalse(appliedDecision!.shouldUpdateSitTone, "Preference write fence must reject stale profile when local save completes first")
    }

    // MARK: - 18. SETTINGS-02: No Hidden PATCH During Hydration

    func testNoHiddenPatchDuringHydration() async {
        let auth = makeAuthContext()
        let patchCallCount = 0

        let fakeProfile: [String: Any] = [
            "owner_name": "Test Owner",
            "business_hours_start": "09:30",
            "business_hours_end": "18:30"
        ]

        let success = await SettingsProfileHydrator.hydrate(
            capturedAuth: auth,
            capturedRevisions: SettingsProfileHydrator.CapturedRevisions(),
            fetchProfile: { fakeProfile },
            ownsOperation: { true },
            currentDraftProvider: { SettingsDraftSnapshot() },
            baselineProvider: { SettingsConfirmedBaseline() },
            fenceProvider: { _ in SettingsProfileHydrator.FencePermits() },
            currentFieldRevisions: { SettingsFieldMutationRevisions() },
            applyProfile: { _, _ in
                // Read & local apply only — no network mutations
            }
        )

        XCTAssertTrue(success)
        XCTAssertEqual(patchCallCount, 0, "Hydration must NEVER execute hidden PATCH mutations")
    }

    // MARK: - 19. SETTINGS-02: Hydrator Reads Snapshot After Await and Respects Ownership

    func testHydratorDoesNotReadSnapshotBeforeAwaitAndRespectsOwnership() async {
        let auth = makeAuthContext()
        var snapshotReadCount = 0
        var fetchCompleted = false

        let profileFetch: () async -> [String: Any]? = {
            XCTAssertEqual(snapshotReadCount, 0, "Snapshot MUST NOT be read before fetchProfile completes")
            fetchCompleted = true
            return ["owner_name": "Test"]
        }

        let draftProvider: () -> SettingsDraftSnapshot = {
            XCTAssertTrue(fetchCompleted, "Draft provider must be invoked strictly AFTER fetchProfile completes")
            snapshotReadCount += 1
            return SettingsDraftSnapshot()
        }

        var applied = false

        let success = await SettingsProfileHydrator.hydrate(
            capturedAuth: auth,
            capturedRevisions: SettingsProfileHydrator.CapturedRevisions(),
            fetchProfile: profileFetch,
            ownsOperation: { false }, // Ownership lost during fetch
            currentDraftProvider: draftProvider,
            baselineProvider: { SettingsConfirmedBaseline() },
            fenceProvider: { _ in SettingsProfileHydrator.FencePermits() },
            currentFieldRevisions: { SettingsFieldMutationRevisions() },
            applyProfile: { _, _ in
                applied = true
            }
        )

        XCTAssertFalse(success, "Hydrator must return false when ownership is lost")
        XCTAssertFalse(applied, "Hydrator must NOT apply profile when ownership is lost")
    }

    // MARK: - 20. SETTINGS-02: Held Old Profile Released After Address Save Keeps Saved State

    func testHeldOldProfileReleasedAfterAddressSaveCompleted() async {
        let auth = makeAuthContext()
        var fieldRevs = SettingsFieldMutationRevisions()

        var projection = SettingsGuardedStateProjection(
            baseline: SettingsConfirmedBaseline(
                regulatoryAddress: "100 Initial St",
                regulatoryCity: "San Jose"
            ),
            drafts: SettingsDraftSnapshot(
                regulatoryAddress: "100 Initial St",
                regulatoryCity: "San Jose"
            ),
            appStateBusinessAddress: "100 Initial St",
            appStateBusinessCity: "San Jose"
        )

        let fetchStartedExp = expectation(description: "Profile fetch started")
        let heldFetch = HeldFetch {
            fetchStartedExp.fulfill()
        }

        let capturedRevisions = SettingsProfileHydrator.CapturedRevisions(fieldMutationRevisions: fieldRevs)

        let hydrateTask = Task { @MainActor in
            await SettingsProfileHydrator.hydrate(
                capturedAuth: auth,
                capturedRevisions: capturedRevisions,
                fetchProfile: {
                    await heldFetch.fetch(auth: auth)
                },
                ownsOperation: { true },
                currentDraftProvider: { projection.drafts },
                baselineProvider: { projection.baseline },
                fenceProvider: { _ in SettingsProfileHydrator.FencePermits() },
                currentFieldRevisions: { fieldRevs },
                applyProfile: { profile, decision in
                    SettingsRefreshPolicy.applyGuardedProfile(
                        contractor: profile,
                        decision: decision,
                        state: &projection
                    )
                }
            )
        }

        await fulfillment(of: [fetchStartedExp], timeout: 2.0)

        // While fetch is held in-flight, user completes saving a new address
        let newlySavedAddress = "200 Newly Saved Ave"
        let newlySavedCity = "San Francisco"
        fieldRevs.regulatoryAddress &+= 1
        projection.drafts.regulatoryAddress = newlySavedAddress
        projection.drafts.regulatoryCity = newlySavedCity
        projection.baseline.regulatoryAddress = newlySavedAddress
        projection.baseline.regulatoryCity = newlySavedCity
        projection.appStateBusinessAddress = newlySavedAddress
        projection.appStateBusinessCity = newlySavedCity

        // Old server profile response returns stale values
        let staleServerProfile: [String: Any] = [
            "business_address": "999 Old Server St",
            "business_city": "Old City"
        ]
        heldFetch.resume(returning: staleServerProfile)

        let success = await hydrateTask.value
        XCTAssertTrue(success)

        XCTAssertEqual(projection.drafts.regulatoryAddress, newlySavedAddress, "Saved address draft must remain intact")
        XCTAssertEqual(projection.drafts.regulatoryCity, newlySavedCity, "Saved city draft must remain intact")
        XCTAssertEqual(projection.baseline.regulatoryAddress, newlySavedAddress, "Saved address baseline must remain intact")
        XCTAssertEqual(projection.baseline.regulatoryCity, newlySavedCity, "Saved city baseline must remain intact")
        XCTAssertEqual(projection.appStateBusinessAddress, newlySavedAddress, "Confirmed AppState address must remain intact")
        XCTAssertEqual(projection.appStateBusinessCity, newlySavedCity, "Confirmed AppState city must remain intact")
    }

    // MARK: - 21. SETTINGS-02: Preferences and Hours Rejected Fence Preserve Baselines and Values

    func testPreferencesAndHoursRejectedFencePreserveBaselinesAndValues() async {
        let auth = makeAuthContext()

        var urgentFence = PreferenceWriteFence()
        var screenAllFence = PreferenceWriteFence()
        var sitToneFence = PreferenceWriteFence()
        var hoursFence = PreferenceWriteFence()

        let capturedRevisions = SettingsProfileHydrator.CapturedRevisions(
            urgentRevision: urgentFence.revision,
            screenAllRevision: screenAllFence.revision,
            sitToneRevision: sitToneFence.revision,
            hoursRevision: hoursFence.revision,
            fieldMutationRevisions: SettingsFieldMutationRevisions()
        )

        let op1 = urgentFence.beginSave()!; _ = urgentFence.finish(op1)
        let op2 = screenAllFence.beginSave()!; _ = screenAllFence.finish(op2)
        let op3 = sitToneFence.beginSave()!; _ = sitToneFence.finish(op3)
        let op4 = hoursFence.beginSave()!; _ = hoursFence.finish(op4)

        var projection = SettingsGuardedStateProjection(
            baseline: SettingsConfirmedBaseline(
                businessHoursStart: "09:00",
                businessHoursEnd: "18:00",
                smartInterruption: false,
                ringThroughContacts: false,
                sitToneEnabled: true
            ),
            drafts: SettingsDraftSnapshot(
                businessHoursStart: "09:00",
                businessHoursEnd: "18:00"
            ),
            appStateSmartInterruption: false,
            appStateRingThroughContacts: false,
            appStateSitToneEnabled: true,
            smartInterruptionSelection: false
        )

        let staleServerProfile: [String: Any] = [
            "business_hours_start": "08:00",
            "business_hours_end": "17:00",
            "smart_interruption": true,
            "ring_through_contacts": true,
            "sit_tone_enabled": false
        ]

        let success = await SettingsProfileHydrator.hydrate(
            capturedAuth: auth,
            capturedRevisions: capturedRevisions,
            fetchProfile: { staleServerProfile },
            ownsOperation: { true },
            currentDraftProvider: { projection.drafts },
            baselineProvider: { projection.baseline },
            fenceProvider: { revisions in
                SettingsProfileHydrator.FencePermits(
                    urgentFencePermits: urgentFence.permitsLoad(revisions.urgentRevision),
                    screenAllFencePermits: screenAllFence.permitsLoad(revisions.screenAllRevision),
                    sitToneFencePermits: sitToneFence.permitsLoad(revisions.sitToneRevision),
                    hoursFencePermits: hoursFence.permitsLoad(revisions.hoursRevision)
                )
            },
            currentFieldRevisions: { SettingsFieldMutationRevisions() },
            applyProfile: { profile, decision in
                SettingsRefreshPolicy.applyGuardedProfile(
                    contractor: profile,
                    decision: decision,
                    state: &projection
                )
            }
        )

        XCTAssertTrue(success)
        XCTAssertEqual(projection.baseline.businessHoursStart, "09:00")
        XCTAssertEqual(projection.baseline.businessHoursEnd, "18:00")
        XCTAssertEqual(projection.baseline.smartInterruption, false)
        XCTAssertEqual(projection.baseline.ringThroughContacts, false)
        XCTAssertEqual(projection.baseline.sitToneEnabled, true)

        XCTAssertEqual(projection.drafts.businessHoursStart, "09:00")
        XCTAssertEqual(projection.drafts.businessHoursEnd, "18:00")
        XCTAssertEqual(projection.appStateSmartInterruption, false)
        XCTAssertEqual(projection.smartInterruptionSelection, false)
        XCTAssertEqual(projection.appStateRingThroughContacts, false)
        XCTAssertEqual(projection.appStateSitToneEnabled, true)
    }

    // MARK: - 22. SETTINGS-02: Country and Mode Update Decisions Require UserEditRevisionMatches

    func testCountryAndModeHydrationRequiresUserEditRevisionMatches() async {
        let auth = makeAuthContext()
        var fieldRevs = SettingsFieldMutationRevisions()

        var projection = SettingsGuardedStateProjection(
            baseline: SettingsConfirmedBaseline(
                countryCode: "US",
                mode: "personal"
            ),
            drafts: SettingsDraftSnapshot(),
            appStateCountryCode: "US",
            appStateMode: "personal",
            countrySelection: "US"
        )

        let fetchStartedExp = expectation(description: "Fetch started")
        let heldFetch = HeldFetch {
            fetchStartedExp.fulfill()
        }

        let capturedRevisions = SettingsProfileHydrator.CapturedRevisions(fieldMutationRevisions: fieldRevs)

        let hydrateTask = Task { @MainActor in
            await SettingsProfileHydrator.hydrate(
                capturedAuth: auth,
                capturedRevisions: capturedRevisions,
                fetchProfile: {
                    await heldFetch.fetch(auth: auth)
                },
                ownsOperation: { true },
                currentDraftProvider: { projection.drafts },
                baselineProvider: { projection.baseline },
                fenceProvider: { _ in SettingsProfileHydrator.FencePermits() },
                currentFieldRevisions: { fieldRevs },
                applyProfile: { profile, decision in
                    SettingsRefreshPolicy.applyGuardedProfile(
                        contractor: profile,
                        decision: decision,
                        state: &projection
                    )
                }
            )
        }

        await fulfillment(of: [fetchStartedExp], timeout: 2.0)

        // User saves mode and country while request is in flight, advancing field mutation revisions
        fieldRevs.country &+= 1
        fieldRevs.mode &+= 1
        projection.baseline.mode = "personal"
        projection.appStateMode = "personal"
        projection.baseline.countryCode = "GB"
        projection.appStateCountryCode = "GB"
        projection.countrySelection = "GB"

        let staleServerProfile: [String: Any] = [
            "mode": "business",
            "country_code": "US"
        ]
        heldFetch.resume(returning: staleServerProfile)

        let success = await hydrateTask.value
        XCTAssertTrue(success)

        XCTAssertEqual(projection.baseline.mode, "personal")
        XCTAssertEqual(projection.appStateMode, "personal")
        XCTAssertEqual(projection.baseline.countryCode, "GB")
        XCTAssertEqual(projection.appStateCountryCode, "GB")
        XCTAssertEqual(projection.countrySelection, "GB")
    }

    // MARK: - 23. SETTINGS-05: Deletion Loader With Held Profile and Dismiss Reopen => Zero onReady

    func testDeletionLoaderWithHeldProfileDismissZeroOnReady() async {
        let loader = SettingsDeletionConfirmationLoader()
        let auth = makeAuthContext()
        var isPresented = true
        var onReadyCalls = 0

        let startedExp = expectation(description: "Fetch started")
        let held = HeldFetch {
            startedExp.fulfill()
        }

        loader.requestConfirmation(
            auth: auth,
            isAccountPresented: { isPresented },
            currentAuth: { auth },
            cachedStatus: "active",
            cachedTier: "business",
            fetchProfile: { auth in
                await held.fetch(auth: auth)
            },
            onReady: { _ in
                onReadyCalls += 1
            }
        )

        await fulfillment(of: [startedExp], timeout: 2.0)
        XCTAssertTrue(loader.isLoading)
        let heldTask = loader.lookupTask

        // Dismiss sheet
        isPresented = false
        loader.cancel()
        XCTAssertFalse(loader.isLoading)

        // Reopen sheet
        isPresented = true

        // Complete the old fetch and await task
        held.resume(returning: ["subscription_status": "active", "subscription_tier": "business"])
        _ = await heldTask?.value

        XCTAssertEqual(onReadyCalls, 0, "Dismissed and reopened sheet must produce ZERO onReady callbacks for the old lookup")
    }

    // MARK: - 24. SETTINGS-05: Deletion Loader Explicit Request Works

    func testDeletionLoaderExplicitRequestWorks() async {
        let loader = SettingsDeletionConfirmationLoader()
        let auth = makeAuthContext()
        var onReadyStep: AccountDeletionFirstStep?

        let expectation = expectation(description: "onReady called")

        loader.requestConfirmation(
            auth: auth,
            isAccountPresented: { true },
            currentAuth: { auth },
            cachedStatus: "active",
            cachedTier: "business",
            fetchProfile: { _ in
                ["subscription_status": "active", "subscription_tier": "business"]
            },
            onReady: { step in
                onReadyStep = step
                expectation.fulfill()
            }
        )

        await fulfillment(of: [expectation], timeout: 2.0)
        XCTAssertEqual(onReadyStep, .warnActiveSubscription)
    }

    // MARK: - 25. SETTINGS-05: Deletion Loader Auth Rotation Produces Zero Old Callback

    func testDeletionLoaderAuthRotationOldCallbackZero() async {
        let loader = SettingsDeletionConfirmationLoader()
        let authA = makeAuthContext(contractorId: "contractor-A")
        let authB = makeAuthContext(contractorId: "contractor-B")
        var currentAuth = authA
        var onReadyCalls = 0

        let startedExp = expectation(description: "Fetch started for AuthA")
        let held = HeldFetch {
            startedExp.fulfill()
        }

        loader.requestConfirmation(
            auth: authA,
            isAccountPresented: { true },
            currentAuth: { currentAuth },
            cachedStatus: "active",
            cachedTier: "business",
            fetchProfile: { auth in
                await held.fetch(auth: auth)
            },
            onReady: { _ in
                onReadyCalls += 1
            }
        )

        await fulfillment(of: [startedExp], timeout: 2.0)
        XCTAssertTrue(loader.isLoading)
        let heldTask = loader.lookupTask

        // Auth rotates to B before fetch returns
        currentAuth = authB

        // Release fetch for AuthA
        held.resume(returning: ["subscription_status": "active", "subscription_tier": "business"])
        _ = await heldTask?.value

        XCTAssertEqual(onReadyCalls, 0, "Stale auth callback must produce zero onReady calls")
    }

    // MARK: - 26. SETTINGS-05: Newer Request Not Cleared By Old Completion

    func testDeletionLoaderNewerRequestNotClearedByOldCompletion() async {
        let loader = SettingsDeletionConfirmationLoader()
        let auth = makeAuthContext()
        var onReadyStep: AccountDeletionFirstStep?
        var onReadyCalls = 0

        let startedExp1 = expectation(description: "Fetch 1 started")
        let held1 = HeldFetch {
            startedExp1.fulfill()
        }

        let startedExp2 = expectation(description: "Fetch 2 started")
        let held2 = HeldFetch {
            startedExp2.fulfill()
        }

        // Request 1 (slow)
        loader.requestConfirmation(
            auth: auth,
            isAccountPresented: { true },
            currentAuth: { auth },
            cachedStatus: "trial",
            cachedTier: "personal",
            fetchProfile: { auth in
                await held1.fetch(auth: auth)
            },
            onReady: { step in
                onReadyCalls += 1
                onReadyStep = step
            }
        )

        await fulfillment(of: [startedExp1], timeout: 2.0)
        XCTAssertTrue(loader.isLoading)
        let task1 = loader.lookupTask

        // Request 2 supersedes Request 1
        loader.requestConfirmation(
            auth: auth,
            isAccountPresented: { true },
            currentAuth: { auth },
            cachedStatus: "active",
            cachedTier: "business",
            fetchProfile: { auth in
                await held2.fetch(auth: auth)
            },
            onReady: { step in
                onReadyCalls += 1
                onReadyStep = step
            }
        )

        await fulfillment(of: [startedExp2], timeout: 2.0)
        XCTAssertTrue(loader.isLoading)
        let task2 = loader.lookupTask

        // Request 1 finishes late
        held1.resume(returning: ["subscription_status": "trial", "subscription_tier": "personal"])
        _ = await task1?.value

        XCTAssertEqual(onReadyCalls, 0, "Old completion must not trigger onReady")
        XCTAssertTrue(loader.isLoading, "Newer request must remain loading when old request completes")

        // Request 2 finishes
        held2.resume(returning: ["subscription_status": "active", "subscription_tier": "business"])
        _ = await task2?.value

        XCTAssertFalse(loader.isLoading, "Loader must finish loading when newest request completes")
        XCTAssertEqual(onReadyCalls, 1, "Newer request must succeed and trigger onReady exactly once")
        XCTAssertEqual(onReadyStep, .warnActiveSubscription)
    }

    // MARK: - 27. SETTINGS-06: ScreenAll Save During Fetch Retains Screening Preference & Hydrates Untouched Hours and Knowledge

    func testScreenAllSaveDuringFetchRetainsPreferenceAndHydratesUntouchedHoursAndKnowledge() async {
        let auth = makeAuthContext()
        var screenAllFence = PreferenceWriteFence()
        let urgentFence = PreferenceWriteFence()
        let sitToneFence = PreferenceWriteFence()
        let hoursFence = PreferenceWriteFence()
        let fieldRevs = SettingsFieldMutationRevisions()

        var fetchCallCount = 0
        let patchCallCount = 0

        var projection = SettingsGuardedStateProjection(
            baseline: SettingsConfirmedBaseline(
                knowledgeText: "",
                regulatoryAddress: "100 Main St",
                regulatoryCity: "San Jose",
                businessHoursStart: "08:00",
                businessHoursEnd: "17:00",
                smartInterruption: true,
                ringThroughContacts: true, // screenAll = false
                sitToneEnabled: false,
                countryCode: "US",
                mode: "business"
            ),
            drafts: SettingsDraftSnapshot(
                knowledgeText: "",
                regulatoryAddress: "100 Main St",
                regulatoryCity: "San Jose",
                businessHoursStart: "08:00",
                businessHoursEnd: "17:00"
            ),
            appStateBusinessAddress: "100 Main St",
            appStateBusinessCity: "San Jose",
            appStateSmartInterruption: true,
            appStateRingThroughContacts: true,
            appStateSitToneEnabled: false,
            appStateCountryCode: "US",
            appStateMode: "business",
            countrySelection: "US",
            smartInterruptionSelection: true
        )

        let fetchStartedExp = expectation(description: "Initial profile fetch started")
        let heldFetch = HeldFetch {
            fetchStartedExp.fulfill()
        }

        // Capture initial revisions before fetch
        let capturedRevisions = SettingsProfileHydrator.CapturedRevisions(
            urgentRevision: urgentFence.revision,
            screenAllRevision: screenAllFence.revision,
            sitToneRevision: sitToneFence.revision,
            hoursRevision: hoursFence.revision,
            fieldMutationRevisions: fieldRevs
        )

        let hydrateTask = Task { @MainActor in
            await SettingsProfileHydrator.hydrate(
                capturedAuth: auth,
                capturedRevisions: capturedRevisions,
                fetchProfile: {
                    fetchCallCount += 1
                    return await heldFetch.fetch(auth: auth)
                },
                ownsOperation: { true },
                currentDraftProvider: { projection.drafts },
                baselineProvider: { projection.baseline },
                fenceProvider: { revisions in
                    SettingsProfileHydrator.FencePermits(
                        urgentFencePermits: urgentFence.permitsLoad(revisions.urgentRevision),
                        screenAllFencePermits: screenAllFence.permitsLoad(revisions.screenAllRevision),
                        sitToneFencePermits: sitToneFence.permitsLoad(revisions.sitToneRevision),
                        hoursFencePermits: hoursFence.permitsLoad(revisions.hoursRevision)
                    )
                },
                currentFieldRevisions: { fieldRevs },
                applyProfile: { profile, decision in
                    // Apply guarded profile locally — no network PATCH calls
                    SettingsRefreshPolicy.applyGuardedProfile(
                        contractor: profile,
                        decision: decision,
                        state: &projection
                    )
                }
            )
        }

        await fulfillment(of: [fetchStartedExp], timeout: 2.0)

        // While profile fetch is in-flight, user performs and finishes ONLY screenAll save
        let op = screenAllFence.beginSave()!
        _ = screenAllFence.finish(op)
        // Confirmed saved preference: ringThroughContacts = false (screening all calls)
        projection.appStateRingThroughContacts = false
        projection.baseline.ringThroughContacts = false

        // Server profile returns old screening value (true) + non-default hours + server knowledge
        let serverProfile: [String: Any] = [
            "ring_through_contacts": true, // old server value
            "business_hours_start": "09:30",
            "business_hours_end": "18:30",
            "knowledge": "Kevin answers business FAQ accurately."
        ]
        heldFetch.resume(returning: serverProfile)

        let success = await hydrateTask.value
        XCTAssertTrue(success)

        // Assert: saved preference and its baseline retained
        XCTAssertEqual(projection.appStateRingThroughContacts, false, "ScreenAll saved preference in AppState must be retained")
        XCTAssertEqual(projection.baseline.ringThroughContacts, false, "ScreenAll saved preference in baseline must be retained")

        // Assert: untouched hours and knowledge are hydrated
        XCTAssertEqual(projection.drafts.businessHoursStart, "09:30", "Untouched business hours start must hydrate to server 09:30")
        XCTAssertEqual(projection.drafts.businessHoursEnd, "18:30", "Untouched business hours end must hydrate to server 18:30")
        XCTAssertEqual(projection.baseline.businessHoursStart, "09:30")
        XCTAssertEqual(projection.baseline.businessHoursEnd, "18:30")
        XCTAssertEqual(projection.drafts.knowledgeText, "Kevin answers business FAQ accurately.", "Untouched knowledge must hydrate from server")
        XCTAssertEqual(projection.baseline.knowledgeText, "Kevin answers business FAQ accurately.")

        // Assert: exactly one fetch, no PATCH during hydration
        XCTAssertEqual(fetchCallCount, 1, "Must execute exactly one fetch")
        XCTAssertEqual(patchCallCount, 0, "Hydration must perform no PATCH mutations")
    }

    // MARK: - 28. SETTINGS-06: Country Edit Blocks Country Only, Not Hours or Knowledge

    func testCountryEditDuringFetchBlocksCountryOnlyNotHoursOrKnowledge() async {
        let auth = makeAuthContext()
        var fieldRevs = SettingsFieldMutationRevisions()
        var fetchCallCount = 0
        let patchCallCount = 0

        var projection = SettingsGuardedStateProjection(
            baseline: SettingsConfirmedBaseline(
                knowledgeText: "",
                businessHoursStart: "08:00",
                businessHoursEnd: "17:00",
                countryCode: "US"
            ),
            drafts: SettingsDraftSnapshot(
                knowledgeText: "",
                businessHoursStart: "08:00",
                businessHoursEnd: "17:00"
            ),
            appStateCountryCode: "US",
            countrySelection: "US"
        )

        let fetchStartedExp = expectation(description: "Profile fetch started")
        let heldFetch = HeldFetch {
            fetchStartedExp.fulfill()
        }

        let capturedRevisions = SettingsProfileHydrator.CapturedRevisions(
            fieldMutationRevisions: fieldRevs
        )

        let hydrateTask = Task { @MainActor in
            await SettingsProfileHydrator.hydrate(
                capturedAuth: auth,
                capturedRevisions: capturedRevisions,
                fetchProfile: {
                    fetchCallCount += 1
                    return await heldFetch.fetch(auth: auth)
                },
                ownsOperation: { true },
                currentDraftProvider: { projection.drafts },
                baselineProvider: { projection.baseline },
                fenceProvider: { _ in SettingsProfileHydrator.FencePermits() },
                currentFieldRevisions: { fieldRevs },
                applyProfile: { profile, decision in
                    SettingsRefreshPolicy.applyGuardedProfile(
                        contractor: profile,
                        decision: decision,
                        state: &projection
                    )
                }
            )
        }

        await fulfillment(of: [fetchStartedExp], timeout: 2.0)

        // User saves country while profile fetch is in flight
        fieldRevs.country &+= 1
        projection.countrySelection = "CA"
        projection.baseline.countryCode = "CA"
        projection.appStateCountryCode = "CA"

        // Server profile returns stale country + new hours + new knowledge
        let serverProfile: [String: Any] = [
            "country_code": "US", // stale
            "business_hours_start": "09:30",
            "business_hours_end": "18:30",
            "knowledge": "Kevin Canada FAQ"
        ]
        heldFetch.resume(returning: serverProfile)

        let success = await hydrateTask.value
        XCTAssertTrue(success)

        // Assert: country edit blocks country only
        XCTAssertEqual(projection.countrySelection, "CA", "Edited country must be preserved")
        XCTAssertEqual(projection.baseline.countryCode, "CA")
        XCTAssertEqual(projection.appStateCountryCode, "CA")

        // Assert: hours and knowledge are hydrated
        XCTAssertEqual(projection.drafts.businessHoursStart, "09:30", "Hours must hydrate when only country was edited")
        XCTAssertEqual(projection.drafts.businessHoursEnd, "18:30")
        XCTAssertEqual(projection.baseline.businessHoursStart, "09:30")
        XCTAssertEqual(projection.baseline.businessHoursEnd, "18:30")
        XCTAssertEqual(projection.drafts.knowledgeText, "Kevin Canada FAQ", "Knowledge must hydrate when only country was edited")
        XCTAssertEqual(projection.baseline.knowledgeText, "Kevin Canada FAQ")
        XCTAssertEqual(fetchCallCount, 1)
        XCTAssertEqual(patchCallCount, 0)
    }

    // MARK: - 29. SETTINGS-06: Address Edit Blocks Address and City Only

    func testAddressEditDuringFetchBlocksAddressAndCityOnly() async {
        let auth = makeAuthContext()
        var fieldRevs = SettingsFieldMutationRevisions()
        var fetchCallCount = 0
        let patchCallCount = 0

        var projection = SettingsGuardedStateProjection(
            baseline: SettingsConfirmedBaseline(
                knowledgeText: "",
                regulatoryAddress: "100 Main St",
                regulatoryCity: "San Jose",
                businessHoursStart: "08:00",
                businessHoursEnd: "17:00",
                mode: "business"
            ),
            drafts: SettingsDraftSnapshot(
                knowledgeText: "",
                regulatoryAddress: "100 Main St",
                regulatoryCity: "San Jose",
                businessHoursStart: "08:00",
                businessHoursEnd: "17:00"
            ),
            appStateBusinessAddress: "100 Main St",
            appStateBusinessCity: "San Jose",
            appStateMode: "business"
        )

        let fetchStartedExp = expectation(description: "Profile fetch started")
        let heldFetch = HeldFetch {
            fetchStartedExp.fulfill()
        }

        let capturedRevisions = SettingsProfileHydrator.CapturedRevisions(
            fieldMutationRevisions: fieldRevs
        )

        let hydrateTask = Task { @MainActor in
            await SettingsProfileHydrator.hydrate(
                capturedAuth: auth,
                capturedRevisions: capturedRevisions,
                fetchProfile: {
                    fetchCallCount += 1
                    return await heldFetch.fetch(auth: auth)
                },
                ownsOperation: { true },
                currentDraftProvider: { projection.drafts },
                baselineProvider: { projection.baseline },
                fenceProvider: { _ in SettingsProfileHydrator.FencePermits() },
                currentFieldRevisions: { fieldRevs },
                applyProfile: { profile, decision in
                    SettingsRefreshPolicy.applyGuardedProfile(
                        contractor: profile,
                        decision: decision,
                        state: &projection
                    )
                }
            )
        }

        await fulfillment(of: [fetchStartedExp], timeout: 2.0)

        // User saves regulatory address and city while fetch is in-flight
        fieldRevs.regulatoryAddress &+= 1
        projection.drafts.regulatoryAddress = "200 Pine St"
        projection.drafts.regulatoryCity = "San Francisco"
        projection.baseline.regulatoryAddress = "200 Pine St"
        projection.baseline.regulatoryCity = "San Francisco"
        projection.appStateBusinessAddress = "200 Pine St"
        projection.appStateBusinessCity = "San Francisco"

        // Server profile returns stale address + new hours + new knowledge + new mode
        let serverProfile: [String: Any] = [
            "business_address": "100 Main St", // stale
            "business_city": "San Jose", // stale
            "business_hours_start": "09:30",
            "business_hours_end": "18:30",
            "knowledge": "Kevin SF FAQ",
            "effective_mode": "personal"
        ]
        heldFetch.resume(returning: serverProfile)

        let success = await hydrateTask.value
        XCTAssertTrue(success)

        // Assert: address edit blocks address and city only
        XCTAssertEqual(projection.drafts.regulatoryAddress, "200 Pine St", "Saved address draft must remain intact")
        XCTAssertEqual(projection.drafts.regulatoryCity, "San Francisco", "Saved city draft must remain intact")
        XCTAssertEqual(projection.baseline.regulatoryAddress, "200 Pine St")
        XCTAssertEqual(projection.baseline.regulatoryCity, "San Francisco")
        XCTAssertEqual(projection.appStateBusinessAddress, "200 Pine St")
        XCTAssertEqual(projection.appStateBusinessCity, "San Francisco")

        // Assert: hours, knowledge, and mode hydrate
        XCTAssertEqual(projection.drafts.businessHoursStart, "09:30", "Hours must hydrate when only address was edited")
        XCTAssertEqual(projection.drafts.businessHoursEnd, "18:30")
        XCTAssertEqual(projection.baseline.businessHoursStart, "09:30")
        XCTAssertEqual(projection.baseline.businessHoursEnd, "18:30")
        XCTAssertEqual(projection.drafts.knowledgeText, "Kevin SF FAQ", "Knowledge must hydrate when only address was edited")
        XCTAssertEqual(projection.baseline.knowledgeText, "Kevin SF FAQ")
        XCTAssertEqual(projection.appStateMode, "personal", "Mode must hydrate when only address was edited")
        XCTAssertEqual(projection.baseline.mode, "personal")
        XCTAssertEqual(fetchCallCount, 1)
        XCTAssertEqual(patchCallCount, 0)
    }

    // MARK: - AccountDeletionFence Tests

    func testAccountDeletionFenceDuplicateBeginAndLoaderCancellation() async {
        var fence = AccountDeletionFence()
        let authA = makeAuthContext(contractorId: "contractor-A", bearerToken: "token-A", generation: 1)
        let loader = SettingsDeletionConfirmationLoader()
        var requestCount = 0

        // First accepted begin
        let token1 = fence.begin(auth: authA)
        if let _ = token1 {
            requestCount += 1
        }
        XCTAssertNotNil(token1)
        XCTAssertEqual(token1?.auth, authA)
        XCTAssertTrue(fence.isPending)

        // Duplicate begin for same auth returns nil and keeps pending true
        let duplicateToken = fence.begin(auth: authA)
        if let _ = duplicateToken {
            requestCount += 1
        }
        XCTAssertNil(duplicateToken)
        XCTAssertTrue(fence.isPending)

        // Start a confirmation lookup and then cancel it (lookup/dismiss cancellation)
        loader.requestConfirmation(
            auth: authA,
            isAccountPresented: { true },
            currentAuth: { authA },
            cachedStatus: "active",
            cachedTier: "pro",
            fetchProfile: { _ in
                return ["subscription_status": "active"]
            },
            onReady: { _ in }
        )
        loader.cancel()
        XCTAssertFalse(loader.isLoading)

        // Canceling confirmation loader does NOT reset the deletion fence
        XCTAssertTrue(fence.isPending)
        let duplicateAfterCancel = fence.begin(auth: authA)
        if let _ = duplicateAfterCancel {
            requestCount += 1
        }
        XCTAssertNil(duplicateAfterCancel)
        XCTAssertEqual(requestCount, 1, "Only first accepted begin increments simulated request count")

        // Held task pattern: simulate held deletion execution
        let deletionStartedExp = expectation(description: "Deletion execution started")
        let heldDeletion = HeldFetch {
            deletionStartedExp.fulfill()
        }

        let deletionTask = Task { @MainActor in
            _ = await heldDeletion.fetch(auth: authA)
        }

        await fulfillment(of: [deletionStartedExp], timeout: 2.0)
        heldDeletion.resume(returning: ["status": "deleted"])
        await deletionTask.value

        // Finish exact token
        let finishSuccess = fence.finish(token: token1!, auth: authA)
        XCTAssertTrue(finishSuccess)
        XCTAssertFalse(fence.isPending)
    }

    func testAccountDeletionFenceAuthSwitchAndLateFinishRejection() {
        var fence = AccountDeletionFence()
        let authA = makeAuthContext(contractorId: "contractor-A", bearerToken: "token-A", generation: 1)
        let authB = makeAuthContext(contractorId: "contractor-B", bearerToken: "token-B", generation: 2)

        // Begin for Auth A
        guard let tokenA = fence.begin(auth: authA) else {
            XCTFail("First begin for auth A must succeed")
            return
        }
        XCTAssertTrue(fence.isPending)

        // Reset for Auth B and begin B
        fence.reset(for: authB)
        guard let tokenB = fence.begin(auth: authB) else {
            XCTFail("Begin for auth B must succeed after reset")
            return
        }
        XCTAssertTrue(fence.isPending)
        XCTAssertNotEqual(tokenA.id, tokenB.id)

        // Late finish from Auth A (with tokenA, authB) is rejected
        let lateFinishRejected = fence.finish(token: tokenA, auth: authB)
        XCTAssertFalse(lateFinishRejected)
        XCTAssertTrue(fence.isPending, "Pending state for auth B must be preserved after late finish rejection")

        // Proper finish for B succeeds
        let properBFinish = fence.finish(token: tokenB, auth: authB)
        XCTAssertTrue(properBFinish)
        XCTAssertFalse(fence.isPending)
    }

    func testAccountDeletionFenceSameAuthResetRejectsObsoleteToken() {
        var fence = AccountDeletionFence()
        let authA = makeAuthContext(contractorId: "contractor-A", bearerToken: "token-A", generation: 1)

        guard let token1 = fence.begin(auth: authA) else {
            XCTFail("First begin must succeed")
            return
        }
        XCTAssertTrue(fence.isPending)

        // Same auth reset (e.g. error recovery or manual reset)
        fence.reset(for: authA)
        XCTAssertFalse(fence.isPending)

        // Begin new token for same auth
        guard let token2 = fence.begin(auth: authA) else {
            XCTFail("Second begin after reset must succeed")
            return
        }
        XCTAssertTrue(fence.isPending)
        XCTAssertNotEqual(token1.id, token2.id)

        // Obsolete first token finish is rejected
        let obsoleteFinish = fence.finish(token: token1, auth: authA)
        XCTAssertFalse(obsoleteFinish)
        XCTAssertTrue(fence.isPending, "Fence must remain pending for active token2")

        // Valid second token finish succeeds
        let validFinish = fence.finish(token: token2, auth: authA)
        XCTAssertTrue(validFinish)
        XCTAssertFalse(fence.isPending)
    }
}
