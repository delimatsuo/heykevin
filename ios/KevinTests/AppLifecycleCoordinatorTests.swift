import Foundation
import SwiftUI
import XCTest
@testable import Kevin

@MainActor
private final class StepBarrier {
    var pauseOnEntry = false
    private(set) var entryCount = 0
    private var entryWaiters: [CheckedContinuation<Void, Never>] = []
    private var releaseWaiters: [CheckedContinuation<Void, Never>] = []

    func enter() async {
        entryCount += 1
        let waiters = entryWaiters
        entryWaiters.removeAll()
        for waiter in waiters {
            waiter.resume()
        }
        guard pauseOnEntry else { return }
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            releaseWaiters.append(continuation)
        }
    }

    func waitForEntries(_ targetCount: Int = 1) async {
        while entryCount < targetCount {
            await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
                entryWaiters.append(continuation)
            }
        }
    }

    func releaseAll() {
        let waiters = releaseWaiters
        releaseWaiters.removeAll()
        for waiter in waiters {
            waiter.resume()
        }
    }

    func openAndPassThrough() {
        pauseOnEntry = false
        releaseAll()
    }
}

@MainActor
private final class LifecycleHarness {
    var auth = CallAuthContext(contractorId: "c-alpha", bearerToken: "tok-alpha", generation: 1)
    var session: AccountSessionState = .ready
    var onboarded = true
    var consentedContractorIDs: Set<String> = ["c-alpha"]
    var currentNow = Date(timeIntervalSince1970: 1_000)

    var listenerStartCount = 0
    var subscriptionCleanupAuths: [CallAuthContext] = []
    var coldLaunchSleeps: [UInt64] = []
    var contactSyncSleeps: [UInt64] = []
    var profileFetches: [(contractorId: String, bearerToken: String)] = []
    var profileResponsesByContractor: [String: [String: Any]] = [
        "c-alpha": [
            "contractor_id": "c-alpha",
            "subscription_status": "active",
            "subscription_tier": "business",
        ],
        "c-beta": [
            "contractor_id": "c-beta",
            "subscription_status": "trial",
            "subscription_tier": "personal",
        ],
    ]
    var appliedProfiles: [(profile: [String: Any], auth: CallAuthContext)] = []
    var entitlementVerifications: [CallAuthContext] = []
    var activeCallChecks = 0
    var syncCalls: [CallAuthContext] = []
    var syncResult: SyncResult = .success(synced: 12, removed: 0)

    let coldLaunchSleepBarrier = StepBarrier()
    let contactSyncSleepBarrier = StepBarrier()
    let profileFetchBarrier = StepBarrier()
    let entitlementVerifyBarrier = StepBarrier()
    let contactSyncCallBarrier = StepBarrier()

    lazy var coordinator = AppLifecycleCoordinator(
        currentAuth: { [unowned self] in self.auth },
        sessionState: { [unowned self] in self.session },
        isOnboarded: { [unowned self] in self.onboarded },
        hasContactsConsent: { [unowned self] cid in self.consentedContractorIDs.contains(cid) },
        startTransactionListener: { [unowned self] in self.listenerStartCount += 1 },
        onAuthChangeSubscriptionCleanup: { [unowned self] updatedAuth in
            self.subscriptionCleanupAuths.append(updatedAuth)
        },
        fetchContractorProfile: { [unowned self] cid, token in
            await self.handleFetchProfile(contractorId: cid, bearerToken: token)
        },
        applySubscriptionProfile: { [unowned self] profile, capturedAuth in
            self.appliedProfiles.append((profile: profile, auth: capturedAuth))
            return profile["subscription_status"] as? String
        },
        verifyCurrentEntitlements: { [unowned self] capturedAuth in
            await self.handleVerifyEntitlements(capturedAuth)
        },
        checkForActiveCall: { [unowned self] in
            self.activeCallChecks += 1
        },
        syncContacts: { [unowned self] capturedAuth in
            await self.handleSyncContacts(capturedAuth)
        },
        sleepNanoseconds: { [unowned self] ns in
            await self.handleSleep(ns)
        },
        now: { [unowned self] in self.currentNow },
        coldLaunchDelayNanoseconds: 3_000_000_000,
        contactSyncDelayNanoseconds: 2_000_000_000,
        contactSyncThrottleInterval: 3_600
    )

    private func handleSleep(_ ns: UInt64) async {
        if ns == 3_000_000_000 {
            coldLaunchSleeps.append(ns)
            await coldLaunchSleepBarrier.enter()
        } else if ns == 2_000_000_000 {
            contactSyncSleeps.append(ns)
            await contactSyncSleepBarrier.enter()
        }
    }

    private func handleFetchProfile(contractorId: String, bearerToken: String) async -> [String: Any]? {
        profileFetches.append((contractorId: contractorId, bearerToken: bearerToken))
        await profileFetchBarrier.enter()
        return profileResponsesByContractor[contractorId]
    }

    private func handleVerifyEntitlements(_ capturedAuth: CallAuthContext) async {
        entitlementVerifications.append(capturedAuth)
        await entitlementVerifyBarrier.enter()
    }

    private func handleSyncContacts(_ capturedAuth: CallAuthContext) async -> SyncResult {
        syncCalls.append(capturedAuth)
        await contactSyncCallBarrier.enter()
        if Task.isCancelled {
            return .error("Cancelled")
        }
        return syncResult
    }

    func openAllBarriers() {
        coldLaunchSleepBarrier.openAndPassThrough()
        contactSyncSleepBarrier.openAndPassThrough()
        profileFetchBarrier.openAndPassThrough()
        entitlementVerifyBarrier.openAndPassThrough()
        contactSyncCallBarrier.openAndPassThrough()
    }

    func settle(waitForIdleTasks: Bool = true) async {
        if waitForIdleTasks {
            var spins = 0
            while (coordinator.hasActiveForegroundRefreshTask || coordinator.hasActiveContactSyncTask) && spins < 200 {
                await Task.yield()
                spins += 1
            }
        }
        for _ in 0..<15 {
            await Task.yield()
        }
    }
}

@MainActor
final class AppLifecycleCoordinatorTests: XCTestCase {
    private var activeHarness: LifecycleHarness?

    override func tearDown() {
        activeHarness?.openAllBarriers()
        activeHarness = nil
        super.tearDown()
    }

    private func makeHarness() -> LifecycleHarness {
        let h = LifecycleHarness()
        activeHarness = h
        return h
    }

    func testColdLaunchBackgroundBeforeDelayCancelsProfileAndEntitlementRequests() async {
        let h = makeHarness()
        h.consentedContractorIDs.removeAll()
        h.coldLaunchSleepBarrier.pauseOnEntry = true

        h.coordinator.handleScenePhaseChange(.active)
        await h.coldLaunchSleepBarrier.waitForEntries(1)

        XCTAssertTrue(h.coordinator.isInForeground)
        XCTAssertTrue(h.coordinator.hasStartedTransactionListener)
        XCTAssertEqual(h.listenerStartCount, 1)
        XCTAssertTrue(h.coordinator.hasActiveForegroundRefreshTask)
        XCTAssertEqual(h.coldLaunchSleeps, [3_000_000_000])

        // Background before the 3s delay resolves cancels the task immediately.
        h.coordinator.handleScenePhaseChange(.background)
        XCTAssertFalse(h.coordinator.isInForeground)
        XCTAssertFalse(h.coordinator.hasActiveForegroundRefreshTask)

        // Even when the injected sleep barrier returns normally without throwing,
        // explicit Task.isCancelled / flight / foreground checks abort all downstream steps.
        h.coldLaunchSleepBarrier.openAndPassThrough()
        await h.settle()

        XCTAssertTrue(h.profileFetches.isEmpty)
        XCTAssertTrue(h.appliedProfiles.isEmpty)
        XCTAssertTrue(h.entitlementVerifications.isEmpty)
        XCTAssertEqual(h.activeCallChecks, 0)
        XCTAssertNil(h.coordinator.completedInitialRefreshAuth)

        // Subsequent .active runs the full initial refresh once and stamps completedInitialRefreshAuth.
        h.coordinator.handleScenePhaseChange(.active)
        await h.settle()

        XCTAssertEqual(h.listenerStartCount, 1, "Transaction listener must start at most once")
        XCTAssertEqual(h.coldLaunchSleeps.count, 2)
        XCTAssertEqual(h.profileFetches.count, 1)
        XCTAssertEqual(h.profileFetches.first?.contractorId, "c-alpha")
        XCTAssertEqual(h.profileFetches.first?.bearerToken, "tok-alpha")
        XCTAssertEqual(h.appliedProfiles.count, 1)
        XCTAssertEqual(h.appliedProfiles.first?.auth, h.auth)
        XCTAssertEqual(h.entitlementVerifications, [h.auth])
        XCTAssertEqual(h.activeCallChecks, 1)
        XCTAssertEqual(h.coordinator.completedInitialRefreshAuth, h.auth)

        // Warm foreground (.background -> .active) skips 3s sleep, profile fetch, and entitlement check,
        // calling checkForActiveCall directly.
        h.coordinator.handleScenePhaseChange(.background)
        h.coordinator.handleScenePhaseChange(.active)
        await h.settle()

        XCTAssertEqual(h.coldLaunchSleeps.count, 2, "Warm foreground must not sleep 3s")
        XCTAssertEqual(h.profileFetches.count, 1, "Warm foreground must not re-fetch profile")
        XCTAssertEqual(h.entitlementVerifications.count, 1, "Warm foreground must not re-verify entitlements")
        XCTAssertEqual(h.coordinator.completedInitialRefreshAuth, h.auth)
        XCTAssertEqual(h.activeCallChecks, 2)
    }

    func testInactiveScenePhaseDoesNotCancelInFlightForegroundTasksAndRapidForegroundChurnIsSingleFlight() async {
        let h = makeHarness()

        // VoIP / PushKit background wake safety: coordinator starts with isInForeground == false,
        // so readiness/auth/consent events before .active run zero foreground tasks.
        XCTAssertFalse(h.coordinator.isInForeground)
        h.coordinator.handleSessionReadinessChange()
        h.coordinator.handleAuthChange()
        h.coordinator.handleContactsConsentChange()
        await h.settle()

        XCTAssertFalse(h.coordinator.isInForeground)
        XCTAssertFalse(h.coordinator.hasStartedTransactionListener)
        XCTAssertEqual(h.subscriptionCleanupAuths, [h.auth])
        XCTAssertTrue(h.coldLaunchSleeps.isEmpty)
        XCTAssertTrue(h.contactSyncSleeps.isEmpty)
        XCTAssertTrue(h.profileFetches.isEmpty)
        XCTAssertTrue(h.entitlementVerifications.isEmpty)
        XCTAssertTrue(h.syncCalls.isEmpty)
        XCTAssertEqual(h.activeCallChecks, 0)

        // Pause during 3s & 2s delays and during fetchContractorProfile.
        h.coldLaunchSleepBarrier.pauseOnEntry = true
        h.contactSyncSleepBarrier.pauseOnEntry = true
        h.profileFetchBarrier.pauseOnEntry = true

        h.coordinator.handleScenePhaseChange(.active)
        await h.coldLaunchSleepBarrier.waitForEntries(1)
        await h.contactSyncSleepBarrier.waitForEntries(1)

        // .active -> .inactive -> .active during the 3s and 2s delays must NOT cancel or duplicate tasks.
        h.coordinator.handleScenePhaseChange(.inactive)
        XCTAssertTrue(h.coordinator.isInForeground)
        XCTAssertTrue(h.coordinator.hasActiveForegroundRefreshTask)
        XCTAssertTrue(h.coordinator.hasActiveContactSyncTask)

        h.coordinator.handleScenePhaseChange(.active)
        h.coordinator.handleScenePhaseChange(.inactive)
        h.coordinator.handleScenePhaseChange(.active)

        XCTAssertEqual(h.listenerStartCount, 1)
        XCTAssertEqual(h.coldLaunchSleeps.count, 1)
        XCTAssertEqual(h.contactSyncSleeps.count, 1)

        // Advance past the 3s sleep into fetchContractorProfile and bounce .inactive -> .active again.
        h.coldLaunchSleepBarrier.openAndPassThrough()
        await h.profileFetchBarrier.waitForEntries(1)

        h.coordinator.handleScenePhaseChange(.inactive)
        XCTAssertTrue(h.coordinator.isInForeground)
        XCTAssertTrue(h.coordinator.hasActiveForegroundRefreshTask)
        h.coordinator.handleScenePhaseChange(.active)

        XCTAssertEqual(h.profileFetches.count, 1)
        XCTAssertEqual(h.listenerStartCount, 1)

        // Release remaining barriers and verify single-flight completion.
        h.profileFetchBarrier.openAndPassThrough()
        h.contactSyncSleepBarrier.openAndPassThrough()
        await h.settle()

        XCTAssertEqual(h.profileFetches.count, 1)
        XCTAssertEqual(h.entitlementVerifications.count, 1)
        XCTAssertEqual(h.activeCallChecks, 1)
        XCTAssertEqual(h.syncCalls.count, 1)
        XCTAssertEqual(h.listenerStartCount, 1)
        XCTAssertEqual(h.coordinator.completedInitialRefreshAuth, h.auth)
    }

    func testLockedColdLaunchTransitionsToReadyAndRunsInitialRefreshOnce() async {
        let h = makeHarness()
        h.session = .storageUnavailable(reason: "Protected data locked")

        h.coordinator.handleScenePhaseChange(.active)
        await h.settle()

        XCTAssertTrue(h.coordinator.isInForeground)
        XCTAssertEqual(h.listenerStartCount, 1)
        XCTAssertTrue(h.coldLaunchSleeps.isEmpty)
        XCTAssertTrue(h.contactSyncSleeps.isEmpty)
        XCTAssertTrue(h.profileFetches.isEmpty)
        XCTAssertTrue(h.entitlementVerifications.isEmpty)
        XCTAssertTrue(h.syncCalls.isEmpty)
        XCTAssertEqual(h.activeCallChecks, 0)
        XCTAssertNil(h.coordinator.completedInitialRefreshAuth)

        // Device unlocks -> sessionState becomes .ready while still .active.
        h.session = .ready
        h.coordinator.handleSessionReadinessChange()
        await h.settle()

        XCTAssertEqual(h.coldLaunchSleeps, [3_000_000_000])
        XCTAssertEqual(h.contactSyncSleeps, [2_000_000_000])
        XCTAssertEqual(h.profileFetches.count, 1)
        XCTAssertEqual(h.entitlementVerifications, [h.auth])
        XCTAssertEqual(h.activeCallChecks, 1)
        XCTAssertEqual(h.syncCalls, [h.auth])
        XCTAssertEqual(h.coordinator.completedInitialRefreshAuth, h.auth)
    }

    func testContactSyncRequiresOwnedConsentAndCancelsWhenConsentRevoked() async {
        let h = makeHarness()
        h.consentedContractorIDs.removeAll()

        h.coordinator.handleScenePhaseChange(.active)
        await h.settle()

        XCTAssertTrue(h.contactSyncSleeps.isEmpty)
        XCTAssertTrue(h.syncCalls.isEmpty)
        XCTAssertFalse(h.coordinator.hasActiveContactSyncTask)

        // Granting consent via handleContactsConsentChange must NOT auto-start a competing sync
        // (SettingsView / AccountRestoreCoordinator run an explicit manual force: true sync).
        h.consentedContractorIDs.insert(h.auth.contractorId)
        h.coordinator.handleContactsConsentChange()
        await h.settle()

        XCTAssertTrue(h.contactSyncSleeps.isEmpty)
        XCTAssertTrue(h.syncCalls.isEmpty)
        XCTAssertFalse(h.coordinator.hasActiveContactSyncTask)

        // Next foreground with consent starts the 2s delayed sync; revoking consent mid-delay cancels it.
        h.coordinator.handleScenePhaseChange(.background)
        h.contactSyncSleepBarrier.pauseOnEntry = true
        h.coordinator.handleScenePhaseChange(.active)
        await h.contactSyncSleepBarrier.waitForEntries(1)
        XCTAssertTrue(h.coordinator.hasActiveContactSyncTask)

        h.consentedContractorIDs.remove(h.auth.contractorId)
        h.coordinator.handleContactsConsentChange()
        XCTAssertFalse(h.coordinator.hasActiveContactSyncTask)

        h.contactSyncSleepBarrier.openAndPassThrough()
        await h.settle()

        XCTAssertTrue(h.syncCalls.isEmpty)
        XCTAssertNil(h.coordinator.lastContactSyncAt[h.auth.contractorId])
    }

    func testContactSyncThrottlesPerContractorForOneHourAndStampsOnlyAfterCompletion() async {
        let h = makeHarness()
        let authA = CallAuthContext(contractorId: "c-alpha", bearerToken: "tok-alpha", generation: 1)
        let authB = CallAuthContext(contractorId: "c-beta", bearerToken: "tok-beta", generation: 2)
        let authA2 = CallAuthContext(contractorId: "c-alpha", bearerToken: "tok-alpha", generation: 3)
        h.auth = authA
        h.consentedContractorIDs = ["c-alpha", "c-beta"]
        h.currentNow = Date(timeIntervalSince1970: 1_000)

        // Cancelling during the 2s delay must NOT stamp lastContactSyncAt.
        h.contactSyncSleepBarrier.pauseOnEntry = true
        h.coordinator.handleScenePhaseChange(.active)
        await h.contactSyncSleepBarrier.waitForEntries(1)
        h.coordinator.handleScenePhaseChange(.background)
        h.contactSyncSleepBarrier.openAndPassThrough()
        await h.settle()

        XCTAssertTrue(h.syncCalls.isEmpty)
        XCTAssertNil(h.coordinator.lastContactSyncAt["c-alpha"])

        // Cancelling during syncContacts execution (returning .error("Cancelled")) must NOT stamp either.
        h.contactSyncCallBarrier.pauseOnEntry = true
        h.coordinator.handleScenePhaseChange(.active)
        await h.contactSyncCallBarrier.waitForEntries(1)
        h.coordinator.handleScenePhaseChange(.background)
        h.contactSyncCallBarrier.openAndPassThrough()
        await h.settle()

        XCTAssertEqual(h.syncCalls, [authA])
        XCTAssertNil(h.coordinator.lastContactSyncAt["c-alpha"])

        // Completing syncContacts at t = 1000 stamps lastContactSyncAt["c-alpha"] = 1000.
        h.coordinator.handleScenePhaseChange(.active)
        await h.settle()

        XCTAssertEqual(h.syncCalls, [authA, authA])
        XCTAssertEqual(h.coordinator.lastContactSyncAt["c-alpha"], Date(timeIntervalSince1970: 1_000))

        // Foreground at t = 4599 (3599s <= 3600s) and t = 4600 (3600s <= 3600s) are throttled.
        for throttledTime: TimeInterval in [4_599, 4_600] {
            h.currentNow = Date(timeIntervalSince1970: throttledTime)
            h.coordinator.handleScenePhaseChange(.background)
            h.coordinator.handleScenePhaseChange(.active)
            await h.settle()
            XCTAssertEqual(h.syncCalls.count, 2, "Should remain throttled at t = \(throttledTime)")
        }

        // Account B at t = 1010 is NOT throttled by Account A's timestamp.
        h.currentNow = Date(timeIntervalSince1970: 1_010)
        h.auth = authB
        h.coordinator.handleAuthChange()
        await h.settle()

        XCTAssertEqual(h.syncCalls, [authA, authA, authB])
        XCTAssertEqual(h.coordinator.lastContactSyncAt["c-beta"], Date(timeIntervalSince1970: 1_010))

        // Account A at t = 4601 (3601s > 3600s) syncs again.
        h.currentNow = Date(timeIntervalSince1970: 4_601)
        h.auth = authA2
        h.coordinator.handleAuthChange()
        await h.settle()

        XCTAssertEqual(h.syncCalls, [authA, authA, authB, authA2])
        XCTAssertEqual(h.coordinator.lastContactSyncAt["c-alpha"], Date(timeIntervalSince1970: 4_601))
    }

    func testAuthChangeCancelsPendingForegroundRefreshAndContactSyncAndReRunsInitialRefreshForNewAccount() async {
        let h = makeHarness()
        let authA = CallAuthContext(contractorId: "c-alpha", bearerToken: "tok-alpha", generation: 1)
        let authB = CallAuthContext(contractorId: "c-beta", bearerToken: "tok-beta", generation: 2)
        h.auth = authA
        h.consentedContractorIDs = ["c-alpha", "c-beta"]

        // Pause Account A inside fetchContractorProfile and contactSync 2s delay.
        h.profileFetchBarrier.pauseOnEntry = true
        h.contactSyncSleepBarrier.pauseOnEntry = true

        h.coordinator.handleScenePhaseChange(.active)
        await h.profileFetchBarrier.waitForEntries(1)
        await h.contactSyncSleepBarrier.waitForEntries(1)

        XCTAssertTrue(h.coordinator.hasActiveForegroundRefreshTask)
        XCTAssertTrue(h.coordinator.hasActiveContactSyncTask)

        // Switch A -> B mid-flight while remaining in foreground.
        h.profileFetchBarrier.pauseOnEntry = false
        h.contactSyncSleepBarrier.pauseOnEntry = false
        h.auth = authB
        h.coordinator.handleAuthChange()

        XCTAssertEqual(h.subscriptionCleanupAuths, [authB])

        // Release Account A's suspended barriers; Account A's late profile must NOT apply or stamp,
        // and Account B's initial refresh + contact sync must run to completion.
        h.profileFetchBarrier.releaseAll()
        h.contactSyncSleepBarrier.releaseAll()
        await h.settle()

        XCTAssertEqual(h.appliedProfiles.map(\.auth), [authB], "Account A late profile must not apply")
        XCTAssertEqual(h.entitlementVerifications, [authB], "Account A entitlements must not run after switch")
        XCTAssertEqual(h.syncCalls, [authB], "Account A cancelled sync must not run")
        XCTAssertEqual(h.coordinator.completedInitialRefreshAuth, authB)

        // Switching B -> A re-runs initial refresh for Account A because completedInitialRefreshAuth was reset.
        h.auth = authA
        h.coordinator.handleAuthChange()
        await h.settle()

        XCTAssertEqual(h.subscriptionCleanupAuths, [authB, authA])
        XCTAssertEqual(h.appliedProfiles.map(\.auth), [authB, authA])
        XCTAssertEqual(h.entitlementVerifications, [authB, authA])
        XCTAssertEqual(h.coordinator.completedInitialRefreshAuth, authA)

        // Signing out while tasks are pending immediately cancels both foreground tasks.
        let authC = CallAuthContext(contractorId: "c-gamma", bearerToken: "tok-gamma", generation: 3)
        let signedOut = CallAuthContext(contractorId: "", bearerToken: "", generation: 4)
        h.consentedContractorIDs.insert("c-gamma")
        h.coldLaunchSleepBarrier.pauseOnEntry = true
        h.contactSyncSleepBarrier.pauseOnEntry = true
        h.auth = authC
        h.coordinator.handleAuthChange()
        await h.coldLaunchSleepBarrier.waitForEntries(1)
        await h.contactSyncSleepBarrier.waitForEntries(1)
        XCTAssertTrue(h.coordinator.hasActiveForegroundRefreshTask)
        XCTAssertTrue(h.coordinator.hasActiveContactSyncTask)

        h.auth = signedOut
        h.coordinator.handleAuthChange()
        XCTAssertFalse(h.coordinator.hasActiveForegroundRefreshTask)
        XCTAssertFalse(h.coordinator.hasActiveContactSyncTask)
        h.openAllBarriers()
        await h.settle()
    }

    func testBackgroundDuringEntitlementVerificationPreventsCompletionStampAndReRunsNextForeground() async {
        let h = makeHarness()
        h.consentedContractorIDs.removeAll()
        h.entitlementVerifyBarrier.pauseOnEntry = true

        h.coordinator.handleScenePhaseChange(.active)
        await h.entitlementVerifyBarrier.waitForEntries(1)

        XCTAssertEqual(h.profileFetches.count, 1)
        XCTAssertEqual(h.appliedProfiles.count, 1)
        XCTAssertEqual(h.entitlementVerifications.count, 1)
        XCTAssertNil(h.coordinator.completedInitialRefreshAuth)

        // Background while verifyCurrentEntitlements is suspended.
        h.coordinator.handleScenePhaseChange(.background)
        XCTAssertFalse(h.coordinator.hasActiveForegroundRefreshTask)

        h.entitlementVerifyBarrier.openAndPassThrough()
        await h.settle()

        XCTAssertNil(h.coordinator.completedInitialRefreshAuth)
        XCTAssertEqual(h.activeCallChecks, 0)

        // Next foreground must re-run the initial refresh since entitlement verification did not complete in foreground.
        h.coordinator.handleScenePhaseChange(.active)
        await h.settle()

        XCTAssertEqual(h.profileFetches.count, 2)
        XCTAssertEqual(h.entitlementVerifications.count, 2)
        XCTAssertEqual(h.activeCallChecks, 1)
        XCTAssertEqual(h.coordinator.completedInitialRefreshAuth, h.auth)
    }
}
