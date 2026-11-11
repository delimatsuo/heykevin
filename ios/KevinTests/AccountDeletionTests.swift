import Combine
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

    private struct CompletionResetSnapshot: Equatable {
        let contractorId: String
        let userName: String
        let businessName: String
        let businessAddress: String
        let businessCity: String
        let serviceType: String
        let mode: String
        let kevinNumber: String
        let countryCode: String
        let forwardingActivated: Bool
        let jobberConnected: Bool
        let googleCalendarConnected: Bool
        let appleUserId: String
        let appleIdentityToken: String
        let needsReauth: Bool
        let subscriptionStatus: String
        let subscriptionTier: String
        let subscriptionUUID: String
        let isOnboarded: Bool
        let bearerWrites: [String]

        @MainActor
        init(state: AppState, bearerWrites: [String]) {
            contractorId = state.contractorId
            userName = state.userName
            businessName = state.businessName
            businessAddress = state.businessAddress
            businessCity = state.businessCity
            serviceType = state.serviceType
            mode = state.mode
            kevinNumber = state.kevinNumber
            countryCode = state.countryCode
            forwardingActivated = state.forwardingActivated
            jobberConnected = state.jobberConnected
            googleCalendarConnected = state.googleCalendarConnected
            appleUserId = state.appleUserId
            appleIdentityToken = state.appleIdentityToken
            needsReauth = state.needsReauth
            subscriptionStatus = state.subscriptionStatus
            subscriptionTier = state.subscriptionTier
            subscriptionUUID = state.subscriptionUUID
            isOnboarded = state.isOnboarded
            self.bearerWrites = bearerWrites
        }

        static func expected(bearerWrites: [String]) -> Self {
            Self(
                contractorId: "",
                userName: "",
                businessName: "",
                businessAddress: "",
                businessCity: "",
                serviceType: "",
                mode: "business",
                kevinNumber: "",
                countryCode: "",
                forwardingActivated: false,
                jobberConnected: false,
                googleCalendarConnected: false,
                appleUserId: "",
                appleIdentityToken: "",
                needsReauth: false,
                subscriptionStatus: "trial",
                subscriptionTier: "none",
                subscriptionUUID: "",
                isOnboarded: false,
                bearerWrites: bearerWrites
            )
        }

        private init(
            contractorId: String,
            userName: String,
            businessName: String,
            businessAddress: String,
            businessCity: String,
            serviceType: String,
            mode: String,
            kevinNumber: String,
            countryCode: String,
            forwardingActivated: Bool,
            jobberConnected: Bool,
            googleCalendarConnected: Bool,
            appleUserId: String,
            appleIdentityToken: String,
            needsReauth: Bool,
            subscriptionStatus: String,
            subscriptionTier: String,
            subscriptionUUID: String,
            isOnboarded: Bool,
            bearerWrites: [String]
        ) {
            self.contractorId = contractorId
            self.userName = userName
            self.businessName = businessName
            self.businessAddress = businessAddress
            self.businessCity = businessCity
            self.serviceType = serviceType
            self.mode = mode
            self.kevinNumber = kevinNumber
            self.countryCode = countryCode
            self.forwardingActivated = forwardingActivated
            self.jobberConnected = jobberConnected
            self.googleCalendarConnected = googleCalendarConnected
            self.appleUserId = appleUserId
            self.appleIdentityToken = appleIdentityToken
            self.needsReauth = needsReauth
            self.subscriptionStatus = subscriptionStatus
            self.subscriptionTier = subscriptionTier
            self.subscriptionUUID = subscriptionUUID
            self.isOnboarded = isOnboarded
            self.bearerWrites = bearerWrites
        }
    }

    func testSuccessInvalidatesOwnedCallAndWritesEmptyBearerLastOnlyOnce() throws {
        let f = Fixture()
        let state = f.state
        state.userName = "fixture-owner"
        state.businessName = "fixture-business"
        state.businessAddress = "fixture-address"
        state.businessCity = "fixture-city"
        state.serviceType = "fixture-service"
        state.mode = "personal"
        state.appleUserId = "fixture-apple"
        state.appleIdentityToken = "fixture-identity-token"
        state.needsReauth = true
        state.forwardingActivated = true
        state.jobberConnected = true
        state.googleCalendarConnected = true
        state.subscriptionStatus = "active"
        state.subscriptionTier = "businessPro"
        state.subscriptionUUID = "uuid-a"
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

        var notOnboardedTransitions: [CompletionResetSnapshot] = []
        let cancellable = state.$sessionState.dropFirst().sink { sessionState in
            guard sessionState == .notOnboarded else { return }
            notOnboardedTransitions.append(CompletionResetSnapshot(state: state, bearerWrites: f.bearerWrites))
        }
        defer { cancellable.cancel() }

        var bearerCallbackSnapshots: [CompletionResetSnapshot] = []
        f.onBearerWrite = {
            XCTAssertEqual(state.sessionState, .notOnboarded)
            bearerCallbackSnapshots.append(CompletionResetSnapshot(state: state, bearerWrites: f.bearerWrites))
        }
        XCTAssertEqual(f.complete(.deleted, token: token), .deleted)
        XCTAssertNil(f.complete(.deleted, token: token))
        XCTAssertEqual(notOnboardedTransitions, [.expected(bearerWrites: [])])
        XCTAssertEqual(bearerCallbackSnapshots, [.expected(bearerWrites: [""])])
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
        let cases: [(status: String, tier: String, uuid: String)] = [
            ("active", "businessPro", "00000000-0000-4000-8000-000000000001"),
            ("expired", "personal", "00000000-0000-4000-8000-000000000002"),
        ]
        for entry in cases {
            let f = Fixture()
            let state = f.state
            state.userName = "fixture owner"
            state.businessName = "fixture business"
            state.businessAddress = "fixture address"
            state.businessCity = "fixture city"
            state.serviceType = "fixture service"
            state.mode = "personal"
            state.appleUserId = "fixture-apple"
            state.appleIdentityToken = "fixture-apple-token"
            state.needsReauth = true
            state.forwardingActivated = true
            state.jobberConnected = true
            state.googleCalendarConnected = true
            state.subscriptionStatus = entry.status
            state.subscriptionTier = entry.tier
            state.subscriptionUUID = entry.uuid
            state.contactsUploadConsent = true
            XCTAssertTrue(state.hasOwnedContactsUploadConsent(for: "account-a"), entry.status)
            if entry.status == "active" {
                XCTAssertTrue(state.isSubscriptionActive)
                XCTAssertTrue(state.hasBusinessEntitlement)
                XCTAssertTrue(state.hasBusinessProEntitlement)
            } else {
                XCTAssertFalse(state.isSubscriptionActive)
                XCTAssertFalse(state.hasBusinessEntitlement)
                XCTAssertFalse(state.hasBusinessProEntitlement)
            }
            let token = try f.begin()
            XCTAssertTrue(state.persistRecoveredCredentials([
                "contractorId": "account-a",
                "appleUserId": "fixture-apple",
            ]))
            XCTAssertTrue(state.recoveryCommitPending)
            XCTAssertNotNil(state.pendingAccountRecovery)

            XCTAssertEqual(f.complete(.deleted, token: token), .deleted, entry.status)
            XCTAssertEqual(state.sessionState, .notOnboarded, entry.status)
            XCTAssertEqual(f.bearerWrites, [""], entry.status)
            XCTAssertFalse(state.contactsUploadConsent, entry.status)
            XCTAssertNil(state.contactsUploadConsentOwner, entry.status)
            XCTAssertEqual(state.userName, "", entry.status)
            XCTAssertEqual(state.businessName, "", entry.status)
            XCTAssertEqual(state.businessAddress, "", entry.status)
            XCTAssertEqual(state.businessCity, "", entry.status)
            XCTAssertEqual(state.serviceType, "", entry.status)
            XCTAssertEqual(state.mode, "business", entry.status)
            XCTAssertEqual(state.appleUserId, "", entry.status)
            XCTAssertEqual(state.appleIdentityToken, "", entry.status)
            XCTAssertFalse(state.needsReauth, entry.status)
            XCTAssertFalse(state.forwardingActivated, entry.status)
            XCTAssertFalse(state.jobberConnected, entry.status)
            XCTAssertFalse(state.googleCalendarConnected, entry.status)
            XCTAssertEqual(state.subscriptionStatus, "trial", entry.status)
            XCTAssertEqual(state.subscriptionTier, "none", entry.status)
            XCTAssertEqual(state.subscriptionUUID, "", entry.status)
            XCTAssertTrue(state.isSubscriptionActive, entry.status)
            XCTAssertFalse(state.hasBusinessEntitlement, entry.status)
            XCTAssertFalse(state.hasBusinessProEntitlement, entry.status)
            XCTAssertFalse(state.recoveryCommitPending, entry.status)
            XCTAssertNil(state.pendingAccountRecovery, entry.status)
        }
    }
}

/// Ordinary AppState hydration in an isolated defaults suite with a synthetic Keychain transport.
@MainActor
final class AccountDeletionPersistenceTests: XCTestCase {
    private static let preferencesA: [String: Any] = [
        "isOnboarded": true, "kevinNumber": "number-A", "countryCode": "US",
        "forwardingActivated": true, "forwardingActivatedFor": "number-A",
        "userName": "owner-A", "businessName": "business-A", "businessAddress": "address-A",
        "businessCity": "city-A", "serviceType": "service-A", "kevinMode": "personal",
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
    private static let deletedSecureRemovalOrder = [
        "contractorId", "appleUserId", "subscriptionStatus",
        "subscriptionTier", "subscriptionUUID", "contractorApiToken",
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
        XCTAssertEqual(original.state.mode, "personal")
        let token = try original.begin()
        XCTAssertEqual(original.complete(.deleted, token), .deleted)
        XCTAssertNil(original.complete(.deleted, token))
        XCTAssertEqual(original.secure.attemptedRemovals, Self.deletedSecureRemovalOrder)
        XCTAssertTrue(original.secure.writes.isEmpty)
        await Self.drainMainQueue()

        let image = try original.snapshot()
        var expectedDefaults = Self.preferencesA
        expectedDefaults["kevinNumber"] = ""
        expectedDefaults["countryCode"] = ""
        expectedDefaults["isOnboarded"] = false
        expectedDefaults["contactsUploadConsent"] = false
        expectedDefaults["userName"] = ""
        expectedDefaults["businessName"] = ""
        expectedDefaults["businessAddress"] = ""
        expectedDefaults["businessCity"] = ""
        expectedDefaults["serviceType"] = ""
        expectedDefaults["kevinMode"] = "business"
        expectedDefaults["forwardingActivated"] = false
        expectedDefaults["jobberConnected"] = false
        expectedDefaults["googleCalendarConnected"] = false
        expectedDefaults.removeValue(forKey: "forwardingActivatedFor")
        expectedDefaults.removeValue(forKey: "readCallIds_account-A")
        XCTAssertEqual(image.defaults as NSDictionary, expectedDefaults as NSDictionary)
        XCTAssertNil(image.defaults["forwardingActivatedFor"])
        XCTAssertNil(image.defaults["readCallIds_account-A"])
        XCTAssertEqual(image.defaults["readCallIds_account-B"] as? [String], [])
        XCTAssertEqual(image.defaults["contractorId"] as? String, "")
        XCTAssertEqual(image.defaults["contractorApiToken"] as? String, "")
        XCTAssertEqual(image.defaults["appleUserId"] as? String, "")
        XCTAssertEqual(try image.secureValues(), [:])

        let fresh = try reopen(original)
        XCTAssertTrue(fresh.secure.writes.isEmpty)
        let state = fresh.state
        XCTAssertEqual(state.sessionState, .notOnboarded)
        XCTAssertFalse(state.isOnboarded)
        XCTAssertEqual(state.contractorId, "")
        XCTAssertEqual(fresh.api.contractorToken, "")
        XCTAssertFalse(state.currentAuthContext().isValid)
        XCTAssertEqual(state.kevinNumber, "")
        XCTAssertEqual(state.countryCode, "")
        XCTAssertTrue(state.readCallIds.isEmpty)
        XCTAssertEqual(state.userName, "")
        XCTAssertEqual(state.businessName, "")
        XCTAssertEqual(state.businessAddress, "")
        XCTAssertEqual(state.businessCity, "")
        XCTAssertEqual(state.serviceType, "")
        XCTAssertEqual(state.mode, "business")
        XCTAssertEqual(state.appleUserId, "")
        XCTAssertEqual(state.appleIdentityToken, "")
        XCTAssertEqual(state.subscriptionStatus, "trial")
        XCTAssertEqual(state.subscriptionTier, "none")
        XCTAssertEqual(state.subscriptionUUID, "")
        XCTAssertFalse(state.hasBusinessEntitlement)
        XCTAssertFalse(state.hasBusinessProEntitlement)
        XCTAssertFalse(state.contactsUploadConsent)
        XCTAssertNil(state.contactsUploadConsentOwner)
        XCTAssertFalse(state.jobberConnected)
        XCTAssertFalse(state.googleCalendarConnected)
        XCTAssertFalse(state.forwardingActivated)
        XCTAssertFalse(state.needsReauth)
        XCTAssertFalse(state.ringThroughContacts)
        XCTAssertTrue(state.isVerizonCarrier)
        XCTAssertTrue(state.sitToneEnabled)
        XCTAssertTrue(state.autoReplySms)
        XCTAssertFalse(state.smartInterruption)
        XCTAssertEqual(state.kevinLanguage, "fr")
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
        let drained = try original.snapshot()
        XCTAssertEqual(drained.defaults["isOnboarded"] as? Bool, false)
        XCTAssertEqual(drained.defaults["kevinNumber"] as? String, "")
        XCTAssertEqual(drained.defaults["countryCode"] as? String, "")
        XCTAssertEqual(drained.defaults["businessName"] as? String, "")

        for image in [before, after] {
            let secure = try image.secureValues()
            XCTAssertNil(secure["contractorId"])
            XCTAssertNil(secure["appleUserId"])
            XCTAssertNil(secure["subscriptionStatus"])
            XCTAssertNil(secure["subscriptionTier"])
            XCTAssertNil(secure["subscriptionUUID"])
            XCTAssertNil(image.defaults["readCallIds_account-A"])
            XCTAssertEqual(image.defaults["isOnboarded"] as? Bool, true)
            XCTAssertEqual(image.defaults["kevinNumber"] as? String, "number-A")
            XCTAssertEqual(image.defaults["countryCode"] as? String, "US")
            XCTAssertEqual(image.defaults["businessName"] as? String, "business-A")
            let fork = try open(image)
            fork.state.refreshSecureStorageForActiveUse()
            XCTAssertEqual(fork.state.sessionState, .needsRecovery)
            XCTAssertTrue(fork.state.isOnboarded)
            XCTAssertEqual(fork.state.kevinNumber, "number-A")
            XCTAssertEqual(fork.state.countryCode, "US")
            XCTAssertEqual(fork.state.contractorId, "")
            XCTAssertEqual(fork.api.contractorToken, secure["contractorApiToken"] ?? "")
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
        // A denied Keychain read is not verified absence, so the setter still attempts deletion.
        XCTAssertEqual(original.secure.attemptedRemovals, Self.deletedSecureRemovalOrder)
        XCTAssertEqual(original.state.sessionGeneration, token.auth.generation + 3)
        XCTAssertTrue(original.secure.values.isEmpty)
        await Self.drainMainQueue()
        let fresh = try reopen(original)
        XCTAssertEqual(fresh.state.sessionState, .notOnboarded)
        XCTAssertEqual(fresh.api.contractorToken, "")
        XCTAssertFalse(fresh.state.currentAuthContext().isValid)
        XCTAssertEqual(fresh.state.userName, "")
        XCTAssertEqual(fresh.state.businessName, "")
        XCTAssertEqual(fresh.state.businessAddress, "")
        XCTAssertEqual(fresh.state.businessCity, "")
        XCTAssertEqual(fresh.state.serviceType, "")
        XCTAssertEqual(fresh.state.mode, "business")
        XCTAssertEqual(fresh.state.appleUserId, "")
        XCTAssertEqual(fresh.state.subscriptionStatus, "trial")
        XCTAssertEqual(fresh.state.subscriptionTier, "none")
        XCTAssertEqual(fresh.state.subscriptionUUID, "")
    }

    func testIgnoredProtectedRemovalsLeaveLocalCredentialsBehindSignedOutGate() async throws {
        let original = try await readyA()
        let token = try original.begin()
        original.secure.ignoredRemovals = Set(Self.secureA.keys)
        XCTAssertEqual(original.complete(.deleted, token), .deleted)
        XCTAssertFalse(original.fence.isPending)
        XCTAssertEqual(original.secure.attemptedRemovals, Self.deletedSecureRemovalOrder)
        XCTAssertEqual(original.state.sessionGeneration, token.auth.generation + 3)
        await Self.drainMainQueue()
        let image = try original.snapshot()
        XCTAssertEqual(try image.secureValues(), Self.secureA)
        XCTAssertEqual(image.defaults["isOnboarded"] as? Bool, false)

        let locked = try open(image, locked: true)
        locked.state.refreshSecureStorageForActiveUse()
        XCTAssertTrue(locked.secure.writes.isEmpty)
        XCTAssertTrue(locked.state.sessionState.isUnavailable)
        XCTAssertFalse(locked.state.currentAuthContext().isValid)
        XCTAssertEqual(locked.secure.values, Self.secureA)
        locked.secure.locked = false
        locked.state.refreshSecureStorageForActiveUse()
        XCTAssertTrue(locked.secure.writes.isEmpty)
        XCTAssertEqual(locked.state.sessionState, .notOnboarded)
        XCTAssertEqual(locked.state.contractorId, "")
        XCTAssertEqual(locked.state.appleUserId, "")
        XCTAssertEqual(locked.state.subscriptionStatus, "trial")
        XCTAssertEqual(locked.state.subscriptionTier, "none")
        XCTAssertEqual(locked.state.subscriptionUUID, "")
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
        XCTAssertEqual(state.appleUserId, "")
        XCTAssertFalse(state.contactsUploadConsent)
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
        XCTAssertNil(image.defaults["readCallIds_account-A"])
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
        let readyImage = try original.snapshot()
        XCTAssertNil(readyImage.defaults["contractorId"])
        XCTAssertNil(readyImage.defaults["contractorApiToken"])
        let token = try original.begin()
        var removed: Result<Snapshot, Error>?
        XCTAssertEqual(original.complete(.deleted, token, afterBearer: {
            removed = Result { try original.snapshot() }
        }), .deleted)
        XCTAssertFalse(original.state.contactsUploadConsent)
        let checkpoint = try XCTUnwrap(removed).get()
        XCTAssertNil(try checkpoint.secureValues()["contractorId"])
        XCTAssertNil(try checkpoint.secureValues()["contractorApiToken"])
        XCTAssertNil(checkpoint.defaults["contractorId"])
        XCTAssertNil(checkpoint.defaults["contractorApiToken"])
        XCTAssertEqual(checkpoint.defaults["contactsUploadConsent"] as? Bool, false)
        await Self.drainMainQueue()

        let signedOut = try reopen(original)
        let state = signedOut.state
        XCTAssertEqual(state.sessionState, .notOnboarded)
        XCTAssertEqual(state.contractorId, "")
        XCTAssertEqual(signedOut.api.contractorToken, "")
        XCTAssertEqual(state.appleUserId, "")
        XCTAssertFalse(state.contactsUploadConsent)
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
        XCTAssertTrue(syncTargets.isEmpty)
        await Self.drainMainQueue()
        let image = try signedOut.snapshot()
        let fresh = try reopen(signedOut)
        XCTAssertEqual(fresh.state.sessionState, .ready)
        XCTAssertEqual(fresh.state.currentAuthContext().contractorId, "account-B")
        XCTAssertEqual(fresh.state.currentAuthContext().bearerToken, "verified-bearer-B")
        XCTAssertEqual(fresh.state.appleUserId, "apple-B")
        XCTAssertEqual(fresh.state.userName, "owner-B")
        XCTAssertFalse(fresh.state.contactsUploadConsent)
        XCTAssertEqual(image.defaults["contactsUploadConsent"] as? Bool, false)
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
        preferences["appleUserId"] = "legacy-apple"
        preferences["readCallIds_legacy-account"] = [String]()
        let original = try await readyA(preferences: preferences)
        let readyImage = try original.snapshot()
        XCTAssertNil(readyImage.defaults["contractorId"])
        XCTAssertNil(readyImage.defaults["contractorApiToken"])
        XCTAssertNil(readyImage.defaults["appleUserId"])

        let token = try original.begin()
        var justRemoved: Result<Snapshot, Error>?
        let result = original.complete(.deleted, token, afterBearer: {
            justRemoved = Result { try original.snapshot() }
        })
        XCTAssertEqual(result, .deleted)
        let image = try XCTUnwrap(justRemoved).get()
        XCTAssertNil(try image.secureValues()["contractorId"])
        XCTAssertNil(try image.secureValues()["contractorApiToken"])
        XCTAssertNil(try image.secureValues()["appleUserId"])
        XCTAssertNil(image.defaults["contractorId"])
        XCTAssertNil(image.defaults["contractorApiToken"])
        XCTAssertNil(image.defaults["appleUserId"])

        await Self.drainMainQueue()
        let fresh = try reopen(original)
        XCTAssertEqual(fresh.state.sessionState, .notOnboarded)
        XCTAssertEqual(fresh.state.currentAuthContext().contractorId, "")
        XCTAssertEqual(fresh.state.currentAuthContext().bearerToken, "")
        XCTAssertEqual(fresh.state.appleUserId, "")
        XCTAssertFalse(fresh.state.currentAuthContext().isValid)

        // Also prove deletion itself scrubs duplicate legacy defaults injected right before completion
        // without relying on an earlier readMigratingLegacy call.
        let injected = try await readyA()
        let injectedToken = try injected.begin()
        injected.defaults.set("late-legacy-account", forKey: "contractorId")
        injected.defaults.set("late-legacy-apple", forKey: "appleUserId")
        var afterDirectScrub: Result<Snapshot, Error>?
        let injectedResult = injected.complete(.deleted, injectedToken, beforeBearer: {
            injected.defaults.set("late-legacy-bearer", forKey: "contractorApiToken")
        }, afterBearer: {
            afterDirectScrub = Result { try injected.snapshot() }
        })
        XCTAssertEqual(injectedResult, .deleted)
        let scrubbed = try XCTUnwrap(afterDirectScrub).get()
        XCTAssertNil(try scrubbed.secureValues()["contractorId"])
        XCTAssertNil(try scrubbed.secureValues()["contractorApiToken"])
        XCTAssertNil(try scrubbed.secureValues()["appleUserId"])
        XCTAssertNil(scrubbed.defaults["contractorId"])
        XCTAssertNil(scrubbed.defaults["contractorApiToken"])
        XCTAssertNil(scrubbed.defaults["appleUserId"])
        await Self.drainMainQueue()
        let reopenedInjected = try reopen(injected)
        XCTAssertEqual(reopenedInjected.state.sessionState, .notOnboarded)
        XCTAssertEqual(reopenedInjected.state.currentAuthContext().contractorId, "")
        XCTAssertEqual(reopenedInjected.state.currentAuthContext().bearerToken, "")
        XCTAssertEqual(reopenedInjected.state.appleUserId, "")
        XCTAssertFalse(reopenedInjected.state.currentAuthContext().isValid)
    }

    func testConfirmedDeletionClearsRecoveryMarkerAndTrialKeychainEntries() async throws {
        var trialSecure = Self.secureA
        trialSecure["subscriptionStatus"] = "trial"
        trialSecure["subscriptionTier"] = "none"
        trialSecure["subscriptionUUID"] = "uuid-trial"

        let original = try await readyA(secure: trialSecure)
        XCTAssertEqual(original.state.subscriptionStatus, "trial")
        XCTAssertEqual(original.state.subscriptionTier, "none")
        XCTAssertEqual(original.state.subscriptionUUID, "uuid-trial")
        let token = try original.begin()

        let checkpoint = AccountRecoveryCheckpoint(
            contractorId: "account-A",
            appleUserId: "apple-A",
            allowsUnfinishedSetup: false
        )
        original.state.beginAccountRecovery(checkpoint: checkpoint)
        XCTAssertTrue(original.state.recoveryCommitPending)
        XCTAssertEqual(original.state.pendingAccountRecovery, checkpoint)
        XCTAssertNotNil(original.secure.values["accountRecoveryCommit"])
        original.secure.writes.removeAll()

        XCTAssertEqual(original.complete(.deleted, token), .deleted)
        XCTAssertEqual(
            original.secure.attemptedRemovals,
            ["accountRecoveryCommit"] + Self.deletedSecureRemovalOrder
        )
        XCTAssertTrue(original.secure.writes.isEmpty)
        XCTAssertFalse(original.state.recoveryCommitPending)
        XCTAssertNil(original.state.pendingAccountRecovery)
        await Self.drainMainQueue()

        let image = try original.snapshot()
        XCTAssertEqual(try image.secureValues(), [:])

        let fresh = try reopen(original)
        XCTAssertTrue(fresh.secure.writes.isEmpty)
        XCTAssertEqual(fresh.state.sessionState, .notOnboarded)
        XCTAssertEqual(fresh.state.contractorId, "")
        XCTAssertEqual(fresh.state.appleUserId, "")
        XCTAssertFalse(fresh.state.recoveryCommitPending)
        XCTAssertNil(fresh.state.pendingAccountRecovery)
        XCTAssertEqual(fresh.state.subscriptionStatus, "trial")
        XCTAssertEqual(fresh.state.subscriptionTier, "none")
        XCTAssertEqual(fresh.state.subscriptionUUID, "")

        // Also verify deletion removes a post-recovery "complete" marker alongside trial/none cache entries.
        let recovered = try await readyA(secure: trialSecure)
        XCTAssertTrue(recovered.state.finishAccountRecovery(bearerToken: "bearer-A"))
        XCTAssertEqual(recovered.secure.values["accountRecoveryCommit"], "complete")
        recovered.secure.writes.removeAll()

        let recoveredToken = try recovered.begin()
        XCTAssertEqual(recovered.complete(.deleted, recoveredToken), .deleted)
        XCTAssertEqual(
            recovered.secure.attemptedRemovals,
            ["accountRecoveryCommit"] + Self.deletedSecureRemovalOrder
        )
        XCTAssertTrue(recovered.secure.writes.isEmpty)
        await Self.drainMainQueue()

        let recoveredImage = try recovered.snapshot()
        XCTAssertEqual(try recoveredImage.secureValues(), [:])
        let reopenedRecovered = try reopen(recovered)
        XCTAssertTrue(reopenedRecovered.secure.writes.isEmpty)
        XCTAssertEqual(reopenedRecovered.state.sessionState, .notOnboarded)
        XCTAssertEqual(reopenedRecovered.state.contractorId, "")
        XCTAssertEqual(reopenedRecovered.state.appleUserId, "")
    }

    func testHistoricalPreFixDeletedStoreWithMigratedIdentityRejectsUnownedAndMismatchedConsent() async throws {
        var baseSecure = Self.secureA
        baseSecure.removeValue(forKey: "contractorId")
        baseSecure.removeValue(forKey: "contractorApiToken")

        let variants: [(name: String, isOnboarded: Bool, owner: String?)] = [
            ("bare-signed-out", false, nil),
            ("bare-interrupted-onboarded", true, nil),
            ("owned-by-deleted-account-A", false, "account-A"),
            ("stale-owner-matching-migrated-B", false, "account-B"),
        ]

        for variant in variants {
            var defaults = Self.preferencesA
            defaults["isOnboarded"] = variant.isOnboarded
            defaults["kevinNumber"] = ""
            defaults["countryCode"] = ""
            defaults["contractorId"] = "account-B"
            defaults["contractorApiToken"] = "legacy-bearer-B"
            defaults["contactsUploadConsent"] = true
            if let owner = variant.owner {
                defaults["contactsUploadConsentOwner"] = owner
            } else {
                defaults.removeValue(forKey: "contactsUploadConsentOwner")
            }

            let signedOut = try open(Snapshot(defaults: defaults, secure: baseSecure))
            signedOut.state.refreshSecureStorageForActiveUse()
            XCTAssertEqual(signedOut.state.contractorId, "account-B", variant.name)
            XCTAssertFalse(signedOut.state.contactsUploadConsent, variant.name)
            XCTAssertNil(signedOut.state.contactsUploadConsentOwner, variant.name)
            XCTAssertFalse(signedOut.state.hasOwnedContactsUploadConsent(for: "account-B"), variant.name)

            var syncTargets: [String] = []
            let coordinator = AccountRestoreCoordinator(state: signedOut.state, lookupHandler: { apple, identityToken in
                XCTAssertEqual(apple, "apple-B")
                XCTAssertEqual(identityToken, "identity-B")
                return .found(contractorId: "account-B", apiToken: "verified-bearer-B")
            }, profileFetcher: { account, bearer in
                XCTAssertEqual(account, "account-B")
                XCTAssertEqual(bearer, "verified-bearer-B")
                return ["contractor_id": account, "active": true, "twilio_number": "number-B",
                        "owner_name": "owner-B", "subscription_status": "trial",
                        "subscription_tier": "personal", "country_code": "CA"]
            }, notificationReconciler: {},
               contactSyncHandler: { syncTargets.append($0) })

            let revision = try XCTUnwrap(coordinator.beginAttempt())
            let outcome = await coordinator.restoreAccount(
                appleUserId: "apple-B",
                appleIdentityToken: "identity-B",
                isRecoveryMode: false,
                retainedContractorId: signedOut.state.contractorId,
                retainedAppleUserId: signedOut.state.appleUserId,
                retainedKevinNumber: signedOut.state.kevinNumber,
                attemptRevision: revision
            )
            XCTAssertEqual(outcome, .success(contractorId: "account-B", needsProvisioning: false), variant.name)
            XCTAssertTrue(syncTargets.isEmpty, variant.name)
            await Self.drainMainQueue()
            let fresh = try reopen(signedOut)
            XCTAssertFalse(fresh.state.contactsUploadConsent, variant.name)
            XCTAssertNil(fresh.state.contactsUploadConsentOwner, variant.name)
            XCTAssertFalse(fresh.state.hasOwnedContactsUploadConsent(for: "account-B"), variant.name)
        }
    }

    func testLockedColdLaunchPreservesOwnedConsentUntilUnlockAndSameAccountRecoveryKeepsConsent() async throws {
        var bareLegacyPrefs = Self.preferencesA
        bareLegacyPrefs["contractorId"] = "stale-legacy-id"
        bareLegacyPrefs["contractorApiToken"] = "stale-legacy-token"
        bareLegacyPrefs["appleUserId"] = "stale-legacy-apple"

        let lockedBare = try open(Snapshot(defaults: bareLegacyPrefs, secure: Self.secureA), locked: true)
        lockedBare.state.refreshSecureStorageForActiveUse()
        XCTAssertTrue(lockedBare.state.sessionState.isUnavailable)
        XCTAssertFalse(lockedBare.state.contactsUploadConsent)
        XCTAssertNil(lockedBare.state.contactsUploadConsentOwner)
        XCTAssertFalse(lockedBare.state.hasOwnedContactsUploadConsent(for: "account-A"))
        let whileLockedBare = try lockedBare.snapshot()
        XCTAssertEqual(whileLockedBare.defaults["contactsUploadConsent"] as? Bool, true)
        XCTAssertEqual(whileLockedBare.defaults["contractorId"] as? String, "stale-legacy-id")
        XCTAssertEqual(whileLockedBare.defaults["contractorApiToken"] as? String, "stale-legacy-token")
        XCTAssertEqual(whileLockedBare.defaults["appleUserId"] as? String, "stale-legacy-apple")

        lockedBare.secure.locked = false
        lockedBare.state.refreshSecureStorageForActiveUse()
        XCTAssertEqual(lockedBare.state.sessionState, .ready)
        XCTAssertTrue(lockedBare.state.contactsUploadConsent)
        XCTAssertEqual(lockedBare.state.contactsUploadConsentOwner, "account-A")
        XCTAssertTrue(lockedBare.state.hasOwnedContactsUploadConsent(for: "account-A"))
        let unlockedBareSnap = try lockedBare.snapshot()
        XCTAssertEqual(unlockedBareSnap.defaults["contactsUploadConsent"] as? Bool, true)
        XCTAssertEqual(unlockedBareSnap.defaults["contactsUploadConsentOwner"] as? String, "account-A")
        XCTAssertNil(unlockedBareSnap.defaults["contractorId"])
        XCTAssertNil(unlockedBareSnap.defaults["contractorApiToken"])
        XCTAssertNil(unlockedBareSnap.defaults["appleUserId"])

        let lockedExplicit = try open(unlockedBareSnap, locked: true)
        lockedExplicit.state.refreshSecureStorageForActiveUse()
        XCTAssertTrue(lockedExplicit.state.sessionState.isUnavailable)
        XCTAssertFalse(lockedExplicit.state.contactsUploadConsent)
        XCTAssertNil(lockedExplicit.state.contactsUploadConsentOwner)
        XCTAssertEqual(try lockedExplicit.snapshot().defaults["contactsUploadConsent"] as? Bool, true)
        XCTAssertEqual(try lockedExplicit.snapshot().defaults["contactsUploadConsentOwner"] as? String, "account-A")

        lockedExplicit.secure.locked = false
        lockedExplicit.state.refreshSecureStorageForActiveUse()
        XCTAssertEqual(lockedExplicit.state.sessionState, .ready)
        XCTAssertTrue(lockedExplicit.state.contactsUploadConsent)
        XCTAssertEqual(lockedExplicit.state.contactsUploadConsentOwner, "account-A")
        XCTAssertTrue(lockedExplicit.state.hasOwnedContactsUploadConsent(for: "account-A"))

        lockedExplicit.secure.values.removeValue(forKey: "contractorApiToken")
        let recovering = try reopen(lockedExplicit)
        XCTAssertEqual(recovering.state.sessionState, .needsRecovery)
        XCTAssertEqual(recovering.state.contractorId, "account-A")
        XCTAssertTrue(recovering.state.hasOwnedContactsUploadConsent(for: "account-A"))

        var syncTargets: [String] = []
        let coordinator = AccountRestoreCoordinator(state: recovering.state, lookupHandler: { apple, identityToken in
            XCTAssertEqual(apple, "apple-A")
            XCTAssertEqual(identityToken, "identity-A")
            return .found(contractorId: "account-A", apiToken: "recovered-bearer-A")
        }, profileFetcher: { account, bearer in
            XCTAssertEqual(account, "account-A")
            XCTAssertEqual(bearer, "recovered-bearer-A")
            return ["contractor_id": account, "active": true, "twilio_number": "number-A",
                    "owner_name": "owner-A", "business_name": "business-A",
                    "subscription_status": "active", "subscription_tier": "businessPro",
                    "subscription_uuid": Self.secureA["subscriptionUUID"] ?? "", "country_code": "US"]
        }, notificationReconciler: {},
           contactSyncHandler: { syncTargets.append($0) })

        let revision = try XCTUnwrap(coordinator.beginAttempt())
        let outcome = await coordinator.restoreAccount(
            appleUserId: "apple-A",
            appleIdentityToken: "identity-A",
            isRecoveryMode: true,
            retainedContractorId: recovering.state.contractorId,
            retainedAppleUserId: recovering.state.appleUserId,
            retainedKevinNumber: recovering.state.kevinNumber,
            attemptRevision: revision
        )
        XCTAssertEqual(outcome, .success(contractorId: "account-A", needsProvisioning: false))
        XCTAssertEqual(syncTargets, ["account-A"])
        await Self.drainMainQueue()

        let fresh = try reopen(recovering)
        XCTAssertEqual(fresh.state.sessionState, .ready)
        XCTAssertTrue(fresh.state.contactsUploadConsent)
        XCTAssertEqual(fresh.state.contactsUploadConsentOwner, "account-A")
        XCTAssertTrue(fresh.state.hasOwnedContactsUploadConsent(for: "account-A"))

        fresh.state.revokeContactsUploadConsent()
        XCTAssertFalse(fresh.state.contactsUploadConsent)
        XCTAssertNil(fresh.state.contactsUploadConsentOwner)
        XCTAssertFalse(fresh.state.hasOwnedContactsUploadConsent(for: "account-A"))
        let revokedSnap = try fresh.snapshot()
        XCTAssertEqual(revokedSnap.defaults["contactsUploadConsent"] as? Bool, false)
        XCTAssertNil(revokedSnap.defaults["contactsUploadConsentOwner"])

        fresh.state.grantContactsUploadConsent(for: "account-A")
        XCTAssertTrue(fresh.state.contactsUploadConsent)
        XCTAssertEqual(fresh.state.contactsUploadConsentOwner, "account-A")
        XCTAssertTrue(fresh.state.hasOwnedContactsUploadConsent(for: "account-A"))
        let grantedSnap = try fresh.snapshot()
        XCTAssertEqual(grantedSnap.defaults["contactsUploadConsent"] as? Bool, true)
        XCTAssertEqual(grantedSnap.defaults["contactsUploadConsentOwner"] as? String, "account-A")

        fresh.secure.values.removeValue(forKey: "contractorId")
        fresh.secure.values.removeValue(forKey: "contractorApiToken")
        XCTAssertEqual(fresh.state.contractorId, "account-A")
        fresh.state.refreshSecureStorageForActiveUse()
        XCTAssertEqual(fresh.state.sessionState, .needsRecovery)
        XCTAssertFalse(fresh.state.contactsUploadConsent)
        XCTAssertNil(fresh.state.contactsUploadConsentOwner)
        XCTAssertFalse(fresh.state.hasOwnedContactsUploadConsent(for: "account-A"))
        let wipedSnap = try fresh.snapshot()
        XCTAssertEqual(wipedSnap.defaults["contactsUploadConsent"] as? Bool, false)
        XCTAssertNil(wipedSnap.defaults["contactsUploadConsentOwner"])
    }
}
