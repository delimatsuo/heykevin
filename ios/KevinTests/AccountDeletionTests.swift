import Foundation
import Security
import XCTest
@testable import Kevin

/// The account-deletion contract: local state (contractor ID, token,
/// onboarding) may only be cleared when the server confirmed the deletion.
/// Clearing it on failure strands the user — logged out locally while the
/// account stays active and billing, with no credentials left to retry.
final class AccountDeletionTests: XCTestCase {
    private func httpResponse(status: Int) throws -> HTTPURLResponse {
        try XCTUnwrap(HTTPURLResponse(
            url: XCTUnwrap(URL(string: "https://example.com/api/contractors/c1")),
            statusCode: status,
            httpVersion: "HTTP/1.1",
            headerFields: nil
        ))
    }

    func testBodylessSuccessStatusMeansFailed() throws {
        // Fail-closed: a 200 whose body we cannot positively parse as the
        // backend's {"status": "ok"} may be a middlebox or LB error page.
        // Wiping credentials on it would strand a still-billing account.
        XCTAssertEqual(
            AccountDeletionResponseParser.parse(response: try httpResponse(status: 200)),
            .failed
        )
    }

    func testHTMLBodyOn200MeansFailed() throws {
        let data = Data("<html>maintenance</html>".utf8)
        XCTAssertEqual(
            AccountDeletionResponseParser.parse(
                response: try httpResponse(status: 200),
                data: data
            ),
            .failed
        )
    }

    func testUnauthorizedMeansAlreadyDeleted() throws {
        // After a deletion commits server-side, the token stops authenticating
        // (active==True filter). 401 on a deletion attempt is the only signal
        // the app will ever get that the account is already gone — treating it
        // as failure would strand the user in a permanent retry loop.
        XCTAssertEqual(
            AccountDeletionResponseParser.parse(response: try httpResponse(status: 401)),
            .deleted
        )
    }

    func testNotFoundMeansAlreadyDeleted() throws {
        // 404 = the contractor document no longer exists; the desired end
        // state already holds.
        XCTAssertEqual(
            AccountDeletionResponseParser.parse(response: try httpResponse(status: 404)),
            .deleted
        )
    }

    func testServerErrorMeansFailed() throws {
        XCTAssertEqual(
            AccountDeletionResponseParser.parse(response: try httpResponse(status: 500)),
            .failed
        )
    }

    func testGateRefusalMeansFailed() throws {
        XCTAssertEqual(
            AccountDeletionResponseParser.parse(response: try httpResponse(status: 403)),
            .failed
        )
    }

    func testLegacyErrorBodyWithStatus200MeansFailed() throws {
        // Backends deployed before the 5xx fix report failure as
        // 200 {"status": "error", ...}. That must never clear local state.
        let data = try JSONSerialization.data(withJSONObject: [
            "status": "error",
            "message": "Failed to deactivate account",
        ])
        XCTAssertEqual(
            AccountDeletionResponseParser.parse(
                response: try httpResponse(status: 200),
                data: data
            ),
            .failed
        )
    }

    func testSuccessBodyWithStatus200MeansDeleted() throws {
        let data = try JSONSerialization.data(withJSONObject: ["status": "ok"])
        XCTAssertEqual(
            AccountDeletionResponseParser.parse(
                response: try httpResponse(status: 200),
                data: data
            ),
            .deleted
        )
    }

    func testNonHTTPResponseMeansFailed() {
        XCTAssertEqual(AccountDeletionResponseParser.parse(response: nil), .failed)
    }
}

/// The delete flow's first step: a user with an active or purchased
/// subscription must be told deletion does not cancel their Apple
/// subscription — deleting is a soft deactivate and Apple may keep renewing —
/// before any confirmation.
final class AccountDeletionFlowTests: XCTestCase {
    func testActiveSubscriptionWarnsBeforeConfirming() {
        XCTAssertEqual(
            AccountDeletionFlow.firstStep(subscriptionStatus: "active", subscriptionTier: "personal"),
            .warnActiveSubscription
        )
    }

    func testLapsedPurchasersStillWarn() {
        // "expired" includes Apple's billing-retry window (DID_FAIL_TO_RENEW
        // sets it while the subscription still exists and may yet renew), and
        // "cancelled" subscriptions run to period end — both can still charge
        // a deleted account, but only for someone who actually purchased.
        for status in ["expired", "cancelled"] {
            XCTAssertEqual(
                AccountDeletionFlow.firstStep(subscriptionStatus: status, subscriptionTier: "personal"),
                .warnActiveSubscription,
                "status \(status)"
            )
        }
    }

    func testLapsedTrialWithoutPurchaseIsNotWarned() {
        // The trial sweep marks never-subscribed accounts "expired" too;
        // tier stays "none" for them. Warning those users about charges
        // that cannot exist would be false and erode the real warning.
        for tier in ["none", ""] {
            XCTAssertEqual(
                AccountDeletionFlow.firstStep(subscriptionStatus: "expired", subscriptionTier: tier),
                .confirmDelete,
                "tier \(tier)"
            )
        }
    }

    func testNeverSubscribedStatusesGoStraightToConfirmation() {
        for status in ["trial", ""] {
            XCTAssertEqual(
                AccountDeletionFlow.firstStep(subscriptionStatus: status, subscriptionTier: "none"),
                .confirmDelete,
                "status \(status)"
            )
        }
    }

    func testActiveStatusWarnsEvenWithStaleTierCache() {
        // status "active" implies a subscription regardless of what the
        // cached tier says — never skip the warning on a tier cache miss.
        XCTAssertEqual(
            AccountDeletionFlow.firstStep(subscriptionStatus: "active", subscriptionTier: "none"),
            .warnActiveSubscription
        )
    }

    func testWarningTitleAndBodyConstantsMatchExactApprovedCopy() {
        XCTAssertEqual(
            String(localized: AccountDeletionFlow.warningTitle, locale: Locale(identifier: "en")),
            "Check Apple Subscription"
        )
        XCTAssertEqual(
            String(localized: AccountDeletionFlow.warningBody, locale: Locale(identifier: "en")),
            "Deleting your Hey Kevin account does not cancel your Apple subscription. Check Manage Subscription first to make sure automatic renewal is off."
        )
    }
}

/// The cached Keychain status defaults to "trial" and can be stale; the
/// server is the source of truth. At deletion time the flow fetches the
/// authoritative status and falls back to the cache only when the fetch
/// fails — otherwise an active subscriber on a fresh reinstall deletes
/// without ever seeing the billing warning.
final class AccountDeletionResolveTests: XCTestCase {
    func testFreshServerValuesWin() {
        let r = AccountDeletionFlow.resolve(
            freshStatus: "active", freshTier: "personal",
            cachedStatus: "trial", cachedTier: "none"
        )
        XCTAssertEqual(r.status, "active")
        XCTAssertEqual(r.tier, "personal")
    }

    func testMissingFetchFallsBackToCache() {
        let r = AccountDeletionFlow.resolve(
            freshStatus: nil, freshTier: nil,
            cachedStatus: "active", cachedTier: "business"
        )
        XCTAssertEqual(r.status, "active")
        XCTAssertEqual(r.tier, "business")
    }

    func testPartialFetchFallsBackFieldwise() {
        let r = AccountDeletionFlow.resolve(
            freshStatus: "expired", freshTier: nil,
            cachedStatus: "trial", cachedTier: "personal"
        )
        XCTAssertEqual(r.status, "expired")
        XCTAssertEqual(r.tier, "personal")
    }
}

@MainActor
final class AccountDeletionCompletionTests: XCTestCase {
    @MainActor
    private final class Fixture {
        let state = AppState(inMemory: true)
        var fence = AccountDeletionFence()
        var bearerWrites: [String] = []
        var onBearerWrite: (() -> Void)?

        init() {
            state.contractorId = "account-a"
            state.kevinNumber = "fixture-number"
            state.countryCode = "US"
            state.isOnboarded = true
            XCTAssertTrue(state.finishAccountRecovery(bearerToken: "fixture-bearer-a"))
        }

        func begin() throws -> AccountDeletionToken {
            try XCTUnwrap(fence.begin(auth: state.currentAuthContext()))
        }

        func complete(_ outcome: AccountDeletionOutcome, token: AccountDeletionToken) -> AccountDeletionOutcome? {
            AccountDeletionFlow.complete(
                outcome, token: token, appState: state,
                finish: { self.fence.finish(token: $0, auth: $1) },
                writeBearer: { self.bearerWrites.append($0); self.onBearerWrite?() }
            )
        }
    }

    func testSuccessInvalidatesOwnedCallAndWritesEmptyBearerLastOnlyOnce() throws {
        let f = Fixture()
        let state = f.state
        let token = try f.begin()
        state.pushToken = "fixture-apns"; state.voipToken = "fixture-voip"
        state.isVerizonCarrier = true
        state.readCallIds = ["fixture-read"]; state.unreadCallCount = 1
        state.notificationCallSid = "fixture-call"; state.notificationCallMessage = "fixture-message"
        state.serviceBinding = ServiceBinding(countryCode: "US")
        state.setActiveCall(callSid: "fixture-call", callerPhone: "", callerName: "", authContext: token.auth)
        let lease = try XCTUnwrap(state.ownedActiveCallLease)
        state.updateActiveCallTranscript(text: "fixture line", lease: lease)
        state.captureScreeningTranscript(for: lease)
        XCTAssertEqual(state.screeningTranscript(for: lease)?.lines, ["fixture line"])

        f.onBearerWrite = {
            XCTAssertEqual(state.contractorId, "")
            XCTAssertEqual(state.kevinNumber, "")
            XCTAssertEqual(state.countryCode, "")
            XCTAssertFalse(state.isOnboarded)
        }
        XCTAssertEqual(f.complete(.deleted, token: token), .deleted)
        XCTAssertNil(f.complete(.deleted, token: token))
        XCTAssertEqual(f.bearerWrites, [""])
        XCTAssertFalse(f.fence.isPending)
        XCTAssertEqual(state.sessionState, .notOnboarded)
        XCTAssertFalse(state.currentAuthContext().isValid)
        XCTAssertGreaterThan(state.sessionGeneration, token.auth.generation)
        XCTAssertNil(state.ownedActiveCallLease)
        XCTAssertEqual(state.callLifecycleSnapshot.callSid, "")
        XCTAssertNil(state.screeningTranscript(for: lease))
        XCTAssertTrue(state.readCallIds.isEmpty)
        XCTAssertEqual(state.unreadCallCount, 0)
        XCTAssertEqual(state.notificationCallSid, "")
        XCTAssertEqual(state.notificationCallMessage, "")
        XCTAssertNil(state.serviceBinding)
        XCTAssertEqual(state.pushToken, "fixture-apns")
        XCTAssertEqual(state.voipToken, "fixture-voip")
        XCTAssertTrue(state.isVerizonCarrier)
    }

    func testFailureReturnsErrorWithoutChangingSessionAndPermitsRetry() throws {
        let f = Fixture()
        let state = f.state
        let token = try f.begin()
        state.businessName = "fixture business"
        state.contactsUploadConsent = true
        state.isVerizonCarrier = true
        state.setActiveCall(callSid: "fixture-call", callerPhone: "", callerName: "", authContext: token.auth)
        let lease = try XCTUnwrap(state.ownedActiveCallLease)

        XCTAssertEqual(f.complete(.failed, token: token), .failed)
        XCTAssertNil(f.complete(.deleted, token: token), "A consumed failure cannot later delete the account")
        XCTAssertFalse(f.fence.isPending)
        XCTAssertEqual(state.currentAuthContext(), token.auth)
        XCTAssertEqual(state.sessionState, .ready)
        XCTAssertTrue(state.isOnboarded)
        XCTAssertEqual(state.kevinNumber, "fixture-number")
        XCTAssertEqual(state.countryCode, "US")
        XCTAssertEqual(state.ownedActiveCallLease, lease)
        XCTAssertEqual(state.businessName, "fixture business")
        XCTAssertTrue(state.contactsUploadConsent)
        XCTAssertTrue(state.isVerizonCarrier)
        XCTAssertTrue(f.bearerWrites.isEmpty)
        XCTAssertNotEqual(try f.begin(), token)
    }

    func testLateSuccessAndFailureLeaveNewAccountAndPendingDeletionAlone() throws {
        let f = Fixture()
        let stale = try f.begin()
        f.state.contractorId = "account-b"
        XCTAssertTrue(f.state.finishAccountRecovery(bearerToken: "fixture-bearer-b"))
        f.state.kevinNumber = "second-number"
        let current = try f.begin()

        XCTAssertNil(f.complete(.deleted, token: stale))
        XCTAssertNil(f.complete(.failed, token: stale))
        XCTAssertEqual(f.fence.activeToken, current.id)
        XCTAssertEqual(f.state.currentAuthContext(), current.auth)
        XCTAssertEqual(f.state.kevinNumber, "second-number")
        XCTAssertTrue(f.state.isOnboarded)
        XCTAssertTrue(f.bearerWrites.isEmpty)
        XCTAssertEqual(f.complete(.failed, token: current), .failed)
    }

    func testSameCredentialsAfterAccountSwitchCannotConsumeOldOperation() throws {
        let f = Fixture()
        let old = try f.begin()
        f.state.contractorId = "account-b"
        f.state.contractorId = old.auth.contractorId
        let current = f.state.currentAuthContext()
        XCTAssertEqual(current.contractorId, old.auth.contractorId)
        XCTAssertEqual(current.bearerToken, old.auth.bearerToken)
        XCTAssertGreaterThan(current.generation, old.auth.generation)

        // Before Settings observes the switch, the old operation is still recorded.
        XCTAssertEqual(f.fence.activeToken, old.id)
        XCTAssertNil(f.complete(.deleted, token: old))
        XCTAssertEqual(f.state.currentAuthContext(), current)
        XCTAssertTrue(f.state.isOnboarded)
        XCTAssertTrue(f.bearerWrites.isEmpty)
    }

    func testConfirmedDeletionAccountStateInvariants() throws {
        let f = Fixture()
        let state = f.state
        state.businessName = "fixture business"; state.appleUserId = "fixture-apple"
        state.subscriptionStatus = "active"; state.subscriptionTier = "businessPro"
        state.subscriptionUUID = "00000000-0000-4000-8000-000000000001"
        state.contactsUploadConsent = true
        XCTAssertTrue(state.hasBusinessProEntitlement)
        let token = try f.begin()
        XCTAssertEqual(f.complete(.deleted, token: token), .deleted)
        XCTAssertEqual(state.sessionState, .notOnboarded)
        XCTAssertEqual(f.bearerWrites, [""])

        expectResetGap("deleted-profile-and-apple-identity", state.businessName.isEmpty && state.appleUserId.isEmpty)
        expectResetGap("deleted-subscription-cache", state.subscriptionStatus != "active"
            && state.subscriptionTier != "businessPro" && state.subscriptionUUID.isEmpty)
        expectResetGap("deleted-explicit-contact-consent", !state.contactsUploadConsent)
    }

    private func expectResetGap(_ tag: String, _ invariant: Bool, file: String = #filePath, line: Int = #line) {
        XCTExpectFailure("Account deletion reset: \(tag)", strict: true, failingBlock: {
            guard !invariant else { return }
            let location = XCTSourceCodeLocation(filePath: file, lineNumber: line)
            record(XCTIssue(type: .assertionFailure, compactDescription: tag,
                            sourceCodeContext: XCTSourceCodeContext(location: location)))
        }, issueMatcher: { $0.type == .assertionFailure && $0.compactDescription == tag })
    }
}

/// Ordinary AppState hydration in an isolated defaults suite with a synthetic Keychain transport.
@MainActor
final class AccountDeletionPersistenceTests: XCTestCase {
    private static let preferencesA: [String: Any] = [
        "isOnboarded": true, "kevinNumber": "number-A", "countryCode": "US",
        "forwardingActivated": true, "forwardingActivatedFor": "number-A",
        "userName": "owner-A", "businessName": "business-A", "businessAddress": "address-A",
        "businessCity": "city-A", "serviceType": "service-A", "kevinMode": "business",
        "ringThroughContacts": false, "contactsUploadConsent": true, "isVerizonCarrier": true,
        "sitToneEnabled": true, "autoReplySms": true, "smartInterruption": false, "kevinLanguage": "fr",
        "jobberConnected": true, "googleCalendarConnected": true,
        "readCallIds_account-A": ["read-A"], "readCallIds_account-B": [String](),
        // An explicitly empty legacy value prevents this suite read from falling through to another domain.
        "contractorId": "", "contractorApiToken": "", "appleUserId": "",
    ]
    private static let secureA = [
        "contractorId": "account-A", "contractorApiToken": "bearer-A", "appleUserId": "apple-A",
        "subscriptionStatus": "active", "subscriptionTier": "businessPro",
        "subscriptionUUID": "00000000-0000-4000-8000-00000000000a",
    ]

    /// A value snapshot taken from the raw stores, independent of any AppState or process-local epoch.
    private struct Snapshot {
        let defaults: [String: Any]
        let secureData: Data

        init(defaults: [String: Any], secure: [String: String]) throws {
            self.defaults = defaults
            let encoder = JSONEncoder()
            encoder.outputFormatting = .sortedKeys
            secureData = try encoder.encode(secure)
        }

        func secureValues() throws -> [String: String] {
            try JSONDecoder().decode([String: String].self, from: secureData)
        }
    }

    private final class SecureBacking {
        var values: [String: String]
        var locked: Bool
        var deniedReadKey: String?
        var ignoredRemovals: Set<String> = []
        var attemptedRemovals: [String] = []
        var writes: [String] = []

        init(values: [String: String], locked: Bool) {
            self.values = values
            self.locked = locked
        }

        func manager() -> KeychainManager {
            KeychainManager(reader: { key in
                if self.locked || key == self.deniedReadKey { return .error(errSecInteractionNotAllowed) }
                return self.values[key].map(KeychainReadResult.value) ?? .missing
            }, writer: { key, value in
                self.writes.append(key)
                guard !self.locked else { return false }
                self.values[key] = value
                return true
            }, remover: { key in
                self.attemptedRemovals.append(key)
                if !self.locked && !self.ignoredRemovals.contains(key) { self.values.removeValue(forKey: key) }
            })
        }
    }

    @MainActor
    private final class Launch {
        let suite: String
        let defaults: UserDefaults
        let secure: SecureBacking
        let api: APIClient
        let state: AppState
        var fence = AccountDeletionFence()

        init(suite: String, defaults: UserDefaults, secure: SecureBacking) {
            self.suite = suite
            self.defaults = defaults
            self.secure = secure
            let persistence = AccountPersistence(defaults: defaults, keychain: secure.manager(),
                                                 epoch: CallSessionEpoch(notificationCenter: NotificationCenter()))
            api = APIClient(persistence: persistence)
            state = AppState(persistence: persistence)
        }

        func snapshot() throws -> Snapshot {
            try Snapshot(defaults: XCTUnwrap(defaults.persistentDomain(forName: suite)), secure: secure.values)
        }

        func begin() throws -> AccountDeletionToken {
            try XCTUnwrap(fence.begin(auth: state.currentAuthContext()))
        }

        func complete(_ outcome: AccountDeletionOutcome, _ token: AccountDeletionToken,
                      beforeBearer: () -> Void = {}, afterBearer: () -> Void = {}) -> AccountDeletionOutcome? {
            AccountDeletionFlow.complete(outcome, token: token, appState: state,
                finish: { self.fence.finish(token: $0, auth: $1) },
                writeBearer: {
                    beforeBearer()
                    self.api.contractorToken = $0
                    afterBearer()
                })
        }
    }

    private static func drainMainQueue() async {
        await withCheckedContinuation { continuation in
            DispatchQueue.main.async { continuation.resume() }
        }
    }

    /// Settled launches reopen the original suite. Interrupted checkpoints receive their own domain.
    private func open(_ snapshot: Snapshot, existingSuite: String? = nil, locked: Bool = false) throws -> Launch {
        let suite = existingSuite ?? "Kevin.AccountDeletionTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        if existingSuite == nil {
            addTeardownBlock { @MainActor in
                await AccountDeletionPersistenceTests.drainMainQueue()
                defaults.removePersistentDomain(forName: suite)
            }
            defaults.setPersistentDomain(snapshot.defaults, forName: suite)
        }
        let domain = try XCTUnwrap(defaults.persistentDomain(forName: suite))
        XCTAssertEqual(domain as NSDictionary, snapshot.defaults as NSDictionary)
        let secure = try SecureBacking(values: snapshot.secureValues(), locked: locked)
        return Launch(suite: suite, defaults: defaults, secure: secure)
    }

    private func readyA(preferences: [String: Any]? = nil, secure: [String: String]? = nil) async throws -> Launch {
        let image = try Snapshot(defaults: preferences ?? Self.preferencesA, secure: secure ?? Self.secureA)
        let launch = try open(image)
        launch.state.refreshSecureStorageForActiveUse()
        XCTAssertEqual(launch.state.sessionState, .ready)
        XCTAssertEqual(launch.state.currentAuthContext().contractorId, "account-A")
        XCTAssertEqual(launch.api.contractorToken, "bearer-A")
        await Self.drainMainQueue()
        launch.secure.writes.removeAll()
        return launch
    }

    private func reopen(_ launch: Launch, locked: Bool = false) throws -> Launch {
        let fresh = try open(launch.snapshot(), existingSuite: launch.suite, locked: locked)
        fresh.state.refreshSecureStorageForActiveUse()
        return fresh
    }

    func testConfirmedDeletionSettlesAndFreshStateHydratesAccountResidue() async throws {
        let original = try await readyA()
        let token = try original.begin()
        XCTAssertEqual(original.complete(.deleted, token), .deleted)
        XCTAssertNil(original.complete(.deleted, token))
        XCTAssertEqual(original.secure.attemptedRemovals, ["contractorId", "contractorApiToken"])
        await Self.drainMainQueue()

        let image = try original.snapshot()
        var expectedDefaults = Self.preferencesA
        expectedDefaults["kevinNumber"] = ""
        expectedDefaults["countryCode"] = ""
        expectedDefaults["isOnboarded"] = false
        XCTAssertEqual(image.defaults as NSDictionary, expectedDefaults as NSDictionary)
        var expectedSecure = Self.secureA
        expectedSecure.removeValue(forKey: "contractorId")
        expectedSecure.removeValue(forKey: "contractorApiToken")
        XCTAssertEqual(try image.secureValues(), expectedSecure)

        let fresh = try reopen(original)
        let state = fresh.state
        XCTAssertEqual(state.sessionState, .notOnboarded)
        XCTAssertFalse(state.isOnboarded)
        XCTAssertEqual(state.contractorId, "")
        XCTAssertEqual(fresh.api.contractorToken, "")
        XCTAssertFalse(state.currentAuthContext().isValid)
        XCTAssertEqual(state.kevinNumber, "")
        XCTAssertEqual(state.countryCode, "")
        XCTAssertTrue(state.readCallIds.isEmpty)
        XCTAssertEqual(state.userName, "owner-A")
        XCTAssertEqual(state.businessName, "business-A")
        XCTAssertEqual(state.businessAddress, "address-A")
        XCTAssertEqual(state.businessCity, "city-A")
        XCTAssertEqual(state.serviceType, "service-A")
        XCTAssertEqual(state.mode, "business")
        XCTAssertEqual(state.appleUserId, "apple-A")
        XCTAssertEqual(state.subscriptionStatus, "active")
        XCTAssertEqual(state.subscriptionTier, "businessPro")
        XCTAssertEqual(state.subscriptionUUID, Self.secureA["subscriptionUUID"])
        XCTAssertTrue(state.contactsUploadConsent)
        XCTAssertTrue(state.jobberConnected)
        XCTAssertTrue(state.googleCalendarConnected)
        XCTAssertTrue(state.forwardingActivated)
        XCTAssertFalse(state.ringThroughContacts)
        XCTAssertTrue(state.isVerizonCarrier)
        XCTAssertTrue(state.sitToneEnabled)
        XCTAssertTrue(state.autoReplySms)
        XCTAssertFalse(state.smartInterruption)
        XCTAssertEqual(state.kevinLanguage, "fr")

        expectDurableGap("durable-deleted-business-name", state.businessName.isEmpty)
        expectDurableGap("durable-deleted-apple-user-id", state.appleUserId.isEmpty)
        expectDurableGap("durable-deleted-subscription-status", state.subscriptionStatus != "active")
        expectDurableGap("durable-deleted-subscription-tier", state.subscriptionTier != "businessPro")
        expectDurableGap("durable-deleted-subscription-uuid", state.subscriptionUUID.isEmpty)
        expectDurableGap("durable-deleted-contact-consent", !state.contactsUploadConsent)
    }

    private func expectDurableGap(_ tag: String, _ invariant: Bool, file: String = #filePath, line: Int = #line) {
        XCTExpectFailure("Account deletion reset: \(tag)", strict: true, failingBlock: {
            guard !invariant else { return }
            let location = XCTSourceCodeLocation(filePath: file, lineNumber: line)
            record(XCTIssue(type: .assertionFailure, compactDescription: tag,
                            sourceCodeContext: XCTSourceCodeContext(location: location)))
        }, issueMatcher: { $0.type == .assertionFailure && $0.compactDescription == tag })
    }

    func testFailureAndLateReplyPreserveTheStoredAccountAndAllowRetry() async throws {
        let original = try await readyA()
        let baseline = try original.snapshot()
        let first = try original.begin()
        XCTAssertEqual(original.complete(.failed, first), .failed)
        XCTAssertNil(original.complete(.deleted, first))
        let retry = try original.begin()
        XCTAssertNotEqual(first, retry)
        XCTAssertNil(original.complete(.deleted, first))
        XCTAssertEqual(original.fence.activeToken, retry.id)
        await Self.drainMainQueue()
        let after = try original.snapshot()
        XCTAssertEqual(after.defaults as NSDictionary, baseline.defaults as NSDictionary)
        XCTAssertEqual(after.secureData, baseline.secureData)
        XCTAssertTrue(original.secure.attemptedRemovals.isEmpty)
        XCTAssertTrue(original.secure.writes.isEmpty)
        let fresh = try reopen(original)
        XCTAssertEqual(fresh.state.sessionState, .ready)
        XCTAssertTrue(fresh.state.isOnboarded)
        XCTAssertEqual(fresh.state.currentAuthContext().contractorId, "account-A")
        XCTAssertEqual(fresh.state.currentAuthContext().bearerToken, "bearer-A")
        XCTAssertEqual(fresh.state.kevinNumber, "number-A")
        XCTAssertEqual(fresh.state.countryCode, "US")
        XCTAssertEqual(fresh.state.businessName, "business-A")
        XCTAssertTrue(fresh.state.contactsUploadConsent)
        XCTAssertTrue(fresh.state.isVerizonCarrier)
    }

    func testSnapshotsBeforeAndAfterBearerRemovalHydrateBeforeQueuedDefaults() async throws {
        let original = try await readyA()
        let token = try original.begin()
        var beforeBearer: Result<Snapshot, Error>?
        var afterBearer: Result<Snapshot, Error>?
        let result = original.complete(.deleted, token, beforeBearer: {
            beforeBearer = Result { try original.snapshot() }
        }, afterBearer: {
            afterBearer = Result { try original.snapshot() }
        })
        XCTAssertEqual(result, .deleted)
        let before = try XCTUnwrap(beforeBearer).get()
        let after = try XCTUnwrap(afterBearer).get()
        XCTAssertEqual(try before.secureValues()["contractorApiToken"], "bearer-A")
        XCTAssertNil(try after.secureValues()["contractorApiToken"])
        await Self.drainMainQueue()
        XCTAssertEqual(try original.snapshot().defaults["isOnboarded"] as? Bool, false)

        for image in [before, after] {
            XCTAssertNil(try image.secureValues()["contractorId"])
            XCTAssertEqual(image.defaults["isOnboarded"] as? Bool, true)
            XCTAssertEqual(image.defaults["kevinNumber"] as? String, "number-A")
            XCTAssertEqual(image.defaults["countryCode"] as? String, "US")
            let fork = try open(image)
            fork.state.refreshSecureStorageForActiveUse()
            XCTAssertEqual(fork.state.sessionState, .needsRecovery)
            XCTAssertTrue(fork.state.isOnboarded)
            XCTAssertEqual(fork.state.kevinNumber, "number-A")
            XCTAssertEqual(fork.state.countryCode, "US")
            XCTAssertEqual(fork.state.contractorId, "")
            XCTAssertEqual(fork.api.contractorToken, try image.secureValues()["contractorApiToken"] ?? "")
            XCTAssertFalse(fork.state.currentAuthContext().isValid)
        }
    }

    func testDeniedBearerReadAtFenceLeavesDeletionPendingAndStorageIntact() async throws {
        let original = try await readyA()
        let token = try original.begin()
        let baseline = try original.snapshot()
        original.secure.deniedReadKey = "contractorApiToken"
        XCTAssertFalse(original.state.currentAuthContext().isValid)
        XCTAssertNil(original.complete(.deleted, token))
        XCTAssertEqual(original.fence.activeToken, token.id)
        XCTAssertTrue(original.secure.attemptedRemovals.isEmpty)
        XCTAssertTrue(original.secure.writes.isEmpty)
        await Self.drainMainQueue()
        let after = try original.snapshot()
        XCTAssertEqual(after.defaults as NSDictionary, baseline.defaults as NSDictionary)
        XCTAssertEqual(after.secureData, baseline.secureData)
        original.secure.deniedReadKey = nil
        XCTAssertEqual(original.state.currentAuthContext(), token.auth)
        XCTAssertEqual(original.complete(.failed, token), .failed)
        XCTAssertEqual(try reopen(original).state.sessionState, .ready)
    }

    func testDeniedBearerReadInsideAcceptedCompletionSkipsItsRemoval() async throws {
        let original = try await readyA()
        let token = try original.begin()
        let result = original.complete(.deleted, token, beforeBearer: {
            original.secure.deniedReadKey = "contractorApiToken"
        })
        XCTAssertEqual(result, .deleted)
        XCTAssertFalse(original.fence.isPending)
        // The setter sees the read error as an empty old value and skips an equal write.
        XCTAssertEqual(original.secure.attemptedRemovals, ["contractorId"])
        XCTAssertEqual(original.state.sessionGeneration, token.auth.generation + 2)
        XCTAssertNil(original.secure.values["contractorId"])
        XCTAssertEqual(original.secure.values["contractorApiToken"], "bearer-A")
        await Self.drainMainQueue()
        let fresh = try reopen(original)
        XCTAssertEqual(fresh.state.sessionState, .notOnboarded)
        XCTAssertEqual(fresh.api.contractorToken, "bearer-A")
        XCTAssertFalse(fresh.state.currentAuthContext().isValid)
    }

    func testIgnoredProtectedRemovalsLeaveLocalCredentialsBehindSignedOutGate() async throws {
        let original = try await readyA()
        let token = try original.begin()
        original.secure.ignoredRemovals = ["contractorId", "contractorApiToken"]
        XCTAssertEqual(original.complete(.deleted, token), .deleted)
        XCTAssertFalse(original.fence.isPending)
        XCTAssertEqual(original.secure.attemptedRemovals, ["contractorId", "contractorApiToken"])
        XCTAssertEqual(original.state.sessionGeneration, token.auth.generation + 3)
        await Self.drainMainQueue()
        let image = try original.snapshot()
        XCTAssertEqual(try image.secureValues(), Self.secureA)
        XCTAssertEqual(image.defaults["isOnboarded"] as? Bool, false)

        let locked = try open(image, locked: true)
        locked.state.refreshSecureStorageForActiveUse()
        XCTAssertTrue(locked.state.sessionState.isUnavailable)
        XCTAssertFalse(locked.state.currentAuthContext().isValid)
        XCTAssertEqual(locked.secure.values, Self.secureA)
        locked.secure.locked = false
        locked.state.refreshSecureStorageForActiveUse()
        XCTAssertEqual(locked.state.sessionState, .notOnboarded)
        XCTAssertEqual(locked.state.contractorId, "")
        XCTAssertEqual(locked.api.contractorToken, "bearer-A")

        let unlocked = try reopen(original)
        XCTAssertEqual(unlocked.state.sessionState, .notOnboarded)
        XCTAssertFalse(unlocked.state.isOnboarded)
        XCTAssertEqual(unlocked.state.currentAuthContext().contractorId, "account-A")
        XCTAssertEqual(unlocked.state.currentAuthContext().bearerToken, "bearer-A")
        XCTAssertTrue(unlocked.state.currentAuthContext().isValid)
    }

    func testDifferentExistingAccountRestoresThroughNonRecoveryOnboarding() async throws {
        let original = try await readyA()
        let token = try original.begin()
        XCTAssertEqual(original.complete(.deleted, token), .deleted)
        await Self.drainMainQueue()
        let signedOut = try reopen(original)
        let state = signedOut.state
        XCTAssertEqual(state.sessionState, .notOnboarded)
        XCTAssertEqual(state.contractorId, "")
        XCTAssertEqual(state.kevinNumber, "")
        XCTAssertEqual(state.appleUserId, "apple-A")
        XCTAssertTrue(state.contactsUploadConsent)
        XCTAssertNil(state.pendingAccountRecovery)

        let uuidB = "00000000-0000-4000-8000-00000000000b"
        var lookups = 0
        var profiles = 0
        var reconciliations = 0
        let coordinator = AccountRestoreCoordinator(state: state, lookupHandler: { apple, identityToken in
            lookups += 1
            XCTAssertEqual(apple, "apple-B")
            XCTAssertEqual(identityToken, "identity-B")
            return .found(contractorId: "account-B", apiToken: "bearer-B")
        }, profileFetcher: { account, bearer in
            profiles += 1
            XCTAssertEqual(account, "account-B")
            XCTAssertEqual(bearer, "bearer-B")
            return ["contractor_id": account, "active": true, "subscription_status": "trial",
                    "subscription_tier": "personal", "subscription_uuid": uuidB,
                    "twilio_number": "number-B", "country_code": "CA", "owner_name": "owner-B",
                    "business_name": "business-B", "business_address": "address-B", "business_city": "city-B",
                    "service_type": "service-B", "effective_mode": "personal"]
        }, notificationReconciler: { reconciliations += 1 },
           contactSyncHandler: { _ in XCTFail("Consent from account A must not invoke the sync handler for B") })

        let revision = try XCTUnwrap(coordinator.beginAttempt())
        let outcome = await coordinator.restoreAccount(appleUserId: "apple-B", appleIdentityToken: "identity-B",
            isRecoveryMode: false, retainedContractorId: state.contractorId, retainedAppleUserId: state.appleUserId,
            retainedKevinNumber: state.kevinNumber, attemptRevision: revision)
        XCTAssertEqual(outcome, .success(contractorId: "account-B", needsProvisioning: false))
        XCTAssertEqual(lookups, 1)
        XCTAssertEqual(profiles, 1)
        XCTAssertEqual(reconciliations, 1)
        XCTAssertTrue(state.readCallIds.isEmpty)
        state.markCallAsRead("read-B")
        await Self.drainMainQueue()
        let image = try signedOut.snapshot()
        XCTAssertEqual(image.defaults["readCallIds_account-A"] as? [String], ["read-A"])
        XCTAssertEqual(image.defaults["readCallIds_account-B"] as? [String], ["read-B"])
        XCTAssertEqual(image.defaults["contactsUploadConsent"] as? Bool, false)
        let fresh = try reopen(signedOut)
        let b = fresh.state
        XCTAssertEqual(b.sessionState, .ready)
        XCTAssertTrue(b.isOnboarded)
        XCTAssertEqual(b.currentAuthContext().contractorId, "account-B")
        XCTAssertEqual(b.currentAuthContext().bearerToken, "bearer-B")
        XCTAssertEqual(b.appleUserId, "apple-B")
        XCTAssertEqual(b.userName, "owner-B")
        XCTAssertEqual(b.businessName, "business-B")
        XCTAssertEqual(b.businessAddress, "address-B")
        XCTAssertEqual(b.businessCity, "city-B")
        XCTAssertEqual(b.serviceType, "service-B")
        XCTAssertEqual(b.mode, "personal")
        XCTAssertEqual(b.kevinNumber, "number-B")
        XCTAssertEqual(b.countryCode, "CA")
        XCTAssertEqual(b.subscriptionStatus, "trial")
        XCTAssertEqual(b.subscriptionTier, "personal")
        XCTAssertEqual(b.subscriptionUUID, uuidB)
        XCTAssertEqual(b.readCallIds, ["read-B"])
        XCTAssertFalse(b.contactsUploadConsent)
        XCTAssertFalse(b.jobberConnected)
        XCTAssertFalse(b.googleCalendarConnected)
        XCTAssertTrue(b.isVerizonCarrier)
    }

    func testLegacyIdentityMatchingRestoredAccountDoesNotEstablishConsentOwnership() async throws {
        var preferences = Self.preferencesA
        preferences["contractorId"] = "account-B"
        preferences["contractorApiToken"] = "legacy-bearer-B"
        let original = try await readyA(preferences: preferences)
        let token = try original.begin()
        var removed: Result<Snapshot, Error>?
        XCTAssertEqual(original.complete(.deleted, token, afterBearer: {
            removed = Result { try original.snapshot() }
        }), .deleted)
        let checkpoint = try XCTUnwrap(removed).get()
        XCTAssertNil(try checkpoint.secureValues()["contractorId"])
        XCTAssertNil(try checkpoint.secureValues()["contractorApiToken"])
        XCTAssertEqual(checkpoint.defaults["contractorId"] as? String, "account-B")
        XCTAssertEqual(checkpoint.defaults["contactsUploadConsent"] as? Bool, true)
        await Self.drainMainQueue()

        let signedOut = try reopen(original)
        let state = signedOut.state
        XCTAssertEqual(state.sessionState, .notOnboarded)
        XCTAssertEqual(state.contractorId, "account-B")
        XCTAssertEqual(signedOut.api.contractorToken, "legacy-bearer-B")
        XCTAssertEqual(state.appleUserId, "apple-A")
        XCTAssertTrue(state.contactsUploadConsent)
        XCTAssertNil(state.pendingAccountRecovery)

        var lookups = 0
        var profiles = 0
        var reconciliations = 0
        var syncTargets: [String] = []
        let coordinator = AccountRestoreCoordinator(state: state, lookupHandler: { apple, identityToken in
            lookups += 1
            XCTAssertEqual(apple, "apple-B")
            XCTAssertEqual(identityToken, "identity-B")
            return .found(contractorId: "account-B", apiToken: "verified-bearer-B")
        }, profileFetcher: { account, bearer in
            profiles += 1
            XCTAssertEqual(account, "account-B")
            XCTAssertEqual(bearer, "verified-bearer-B")
            return ["contractor_id": account, "active": true, "twilio_number": "number-B",
                    "owner_name": "owner-B", "subscription_status": "trial",
                    "subscription_tier": "personal", "country_code": "CA"]
        }, notificationReconciler: { reconciliations += 1 },
           contactSyncHandler: { syncTargets.append($0) })
        let revision = try XCTUnwrap(coordinator.beginAttempt())
        let outcome = await coordinator.restoreAccount(appleUserId: "apple-B", appleIdentityToken: "identity-B",
            isRecoveryMode: false, retainedContractorId: state.contractorId,
            retainedAppleUserId: state.appleUserId, retainedKevinNumber: state.kevinNumber,
            attemptRevision: revision)
        XCTAssertEqual(outcome, .success(contractorId: "account-B", needsProvisioning: false))
        XCTAssertEqual(lookups, 1)
        XCTAssertEqual(profiles, 1)
        XCTAssertEqual(reconciliations, 1)
        XCTAssertEqual(syncTargets, ["account-B"], "Only the injected counting callback is invoked")
        await Self.drainMainQueue()
        let image = try signedOut.snapshot()
        let fresh = try reopen(signedOut)
        XCTAssertEqual(fresh.state.sessionState, .ready)
        XCTAssertEqual(fresh.state.currentAuthContext().contractorId, "account-B")
        XCTAssertEqual(fresh.state.currentAuthContext().bearerToken, "verified-bearer-B")
        XCTAssertEqual(fresh.state.appleUserId, "apple-B")
        XCTAssertEqual(fresh.state.userName, "owner-B")
        XCTAssertEqual(image.defaults["contactsUploadConsent"] as? Bool, fresh.state.contactsUploadConsent)
        expectDurableGap("durable-legacy-consent-owner", !fresh.state.contactsUploadConsent)
        expectDurableGap("durable-legacy-no-contact-sync", syncTargets.isEmpty)
    }

    func testQueuedDefaultsReadCurrentPropertyWhileConsentAndReadIDsWriteImmediately() async throws {
        let launch = try await readyA()
        launch.state.contactsUploadConsent = false
        launch.state.markCallAsRead("second-read-A")
        launch.state.countryCode = "CA"
        let immediate = try launch.snapshot()
        XCTAssertEqual(immediate.defaults["contactsUploadConsent"] as? Bool, false)
        XCTAssertEqual(Set(try XCTUnwrap(immediate.defaults["readCallIds_account-A"] as? [String])),
                       ["read-A", "second-read-A"])
        XCTAssertEqual(immediate.defaults["countryCode"] as? String, "US")

        // This block runs between the two queued country writes. The first sees the latest property value.
        let observed: String? = await withCheckedContinuation { continuation in
            DispatchQueue.main.async {
                continuation.resume(returning: launch.defaults.persistentDomain(forName: launch.suite)?["countryCode"] as? String)
            }
            launch.state.countryCode = "GB"
        }
        XCTAssertEqual(observed, "GB")
        await Self.drainMainQueue()
        XCTAssertEqual(try launch.snapshot().defaults["countryCode"] as? String, "GB")
    }

    func testLegacyOnlyCredentialsMigrateFromTheOwnedSuite() async throws {
        let keys = ["contractorId", "contractorApiToken", "appleUserId"]
        var preferences = Self.preferencesA
        var secure = Self.secureA
        for key in keys {
            preferences[key] = try XCTUnwrap(secure.removeValue(forKey: key))
        }
        let launch = try await readyA(preferences: preferences, secure: secure)
        let migrated = try launch.snapshot()
        for key in keys {
            XCTAssertNil(migrated.defaults[key], key)
            XCTAssertEqual(try migrated.secureValues()[key], Self.secureA[key], key)
        }
        XCTAssertEqual(launch.state.appleUserId, "apple-A")
    }

    func testDuplicateLegacyCredentialsCanRepopulateRemovedSecureItems() async throws {
        var preferences = Self.preferencesA
        preferences["contractorId"] = "legacy-account"
        preferences["contractorApiToken"] = "legacy-bearer"
        preferences["readCallIds_legacy-account"] = [String]()
        let original = try await readyA(preferences: preferences)
        let token = try original.begin()
        var justRemoved: Result<Snapshot, Error>?
        let result = original.complete(.deleted, token, afterBearer: {
            justRemoved = Result { try original.snapshot() }
        })
        XCTAssertEqual(result, .deleted)
        let image = try XCTUnwrap(justRemoved).get()
        XCTAssertNil(try image.secureValues()["contractorId"])
        XCTAssertNil(try image.secureValues()["contractorApiToken"])
        XCTAssertEqual(image.defaults["contractorId"] as? String, "legacy-account")
        XCTAssertEqual(image.defaults["contractorApiToken"] as? String, "legacy-bearer")

        // A queued epoch notification may read the bearer before the next AppState is constructed.
        await Self.drainMainQueue()
        let fresh = try reopen(original)
        XCTAssertEqual(fresh.state.sessionState, .notOnboarded)
        XCTAssertEqual(fresh.state.currentAuthContext().contractorId, "legacy-account")
        XCTAssertEqual(fresh.state.currentAuthContext().bearerToken, "legacy-bearer")
        XCTAssertTrue(fresh.state.currentAuthContext().isValid)
        let migrated = try fresh.snapshot()
        XCTAssertNil(migrated.defaults["contractorId"])
        XCTAssertNil(migrated.defaults["contractorApiToken"])
        XCTAssertEqual(try migrated.secureValues()["contractorId"], "legacy-account")
        XCTAssertEqual(try migrated.secureValues()["contractorApiToken"], "legacy-bearer")
    }
}
