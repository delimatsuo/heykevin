import XCTest
@testable import Kevin

@MainActor
final class AccountRestoreTests: XCTestCase {
    @MainActor final class Harness {
        let store = RecoveryTestKeychain()
        let state: AppState
        var result: AppleLookupResult = .found(contractorId: "owner", apiToken: "new-token")
        var profile: [String: Any]? = ["contractor_id": "owner", "active": true,
            "subscription_status": "active", "subscription_tier": "personal", "twilio_number": "+15005550006"]
        var lookups = 0
        var notifications = 0
        var syncs = 0
        init() {
            state = AppState(inMemory: true, secureStore: store.client)
            state.isOnboarded = true; state.sessionState = .needsRecovery
            state.contractorId = "owner"; state.appleUserId = "apple"; state.kevinNumber = "+15005550006"
        }
        lazy var coordinator = AccountRestoreCoordinator(state: state,
            lookupHandler: { [unowned self] _, _ in lookups += 1; return result },
            profileFetcher: { [unowned self] id, token in
                XCTAssertEqual(id, "owner"); XCTAssertEqual(token, "new-token"); return profile
            }, notificationReconciler: { [unowned self] in notifications += 1 },
            contactSyncHandler: { [unowned self] _ in syncs += 1 })
        func restore(recovery: Bool = true, identityToken: String = "fresh") async -> AccountRestoreOutcome {
            let revision = coordinator.beginAttempt()!
            return await coordinator.restoreAccount(appleUserId: "apple", appleIdentityToken: identityToken,
                isRecoveryMode: recovery, retainedContractorId: state.contractorId,
                retainedAppleUserId: state.appleUserId, retainedKevinNumber: state.kevinNumber,
                attemptRevision: revision)
        }
    }

    private func parse(_ status: Int, _ body: String) -> AppleLookupResult {
        AppleLookupResponseParser.parse(data: Data(body.utf8), response: HTTPURLResponse(
            url: URL(string: "https://synthetic.test/lookup")!, statusCode: status, httpVersion: nil, headerFields: nil)!)
    }

    func testLookupDistinguishesFoundExplicitAbsenceAndAuthentication() {
        XCTAssertEqual(parse(401, "{}"), .authFailure)
        XCTAssertEqual(parse(404, "{}"), .notFound)
        XCTAssertEqual(parse(200, #"[{"error":"Not found"},404]"#), .notFound)
        XCTAssertEqual(parse(200, #"{"contractor_id":"owner","api_token":"token"}"#), .found(contractorId: "owner", apiToken: "token"))
    }

    func testAmbiguousLookupCannotBecomeNewAccount() {
        for (status, body) in [(500, "{}"), (429, "{}"), (200, "broken"), (200, "{}"),
            (200, #"{"contractor_id":"owner","api_token":""}"#),
            (200, #"[{"error":"Not found","extra":true},404]"#),
            (200, #"[{"error":"Not found"},404.5]"#), (200, #"{"error":"Not found"}"#)] {
            guard case .failure = parse(status, body) else { return XCTFail("Accepted ambiguous lookup: \(body)") }
        }
    }

    func testRestorePreservesNumberHydratesExpiredSubscriptionAndClearsOldCaches() async {
        let h = Harness()
        h.profile?["subscription_status"] = "expired"
        h.state.userName = "old name"; h.state.businessName = "old business"
        h.state.businessAddress = "old address"; h.state.subscriptionUUID = "old subscription"
        h.state.jobberConnected = true; h.state.googleCalendarConnected = true
        let result = await h.restore()
        XCTAssertEqual(result, .success(contractorId: "owner", needsProvisioning: false))
        XCTAssertEqual(h.state.sessionState, .ready)
        XCTAssertTrue(h.state.currentAuthContext().isValid)
        XCTAssertEqual(h.state.currentAuthContext().bearerToken, "new-token")
        XCTAssertEqual(h.state.kevinNumber, "+15005550006")
        XCTAssertEqual(h.state.subscriptionStatus, "expired")
        XCTAssertFalse(h.state.isSubscriptionActive)
        XCTAssertEqual(h.state.userName, ""); XCTAssertEqual(h.state.businessName, "")
        XCTAssertEqual(h.state.businessAddress, ""); XCTAssertEqual(h.state.subscriptionUUID, "")
        XCTAssertFalse(h.state.jobberConnected); XCTAssertFalse(h.state.googleCalendarConnected)
        XCTAssertEqual(h.notifications, 1)
    }

    func testEveryUnusableRecoveryProfileLeavesExistingAccountAndNeverCompletes() async {
        let invalid: [[String: Any]?] = [nil, [:], ["active": false], ["contractor_id": "other"],
            ["subscription_status": "unknown"], ["subscription_tier": ""], ["twilio_number": ""],
            ["twilio_number": "+15005550007"]]
        for change in invalid {
            let h = Harness()
            if let change {
                if change.isEmpty { h.profile = [:] }
                else { h.profile?.merge(change) { _, new in new } }
            } else { h.profile = nil }
            let result = await h.restore()
            guard case .failed = result else { return XCTFail("Unexpected completion: \(result)") }
            XCTAssertEqual(h.state.kevinNumber, "+15005550006")
            XCTAssertTrue(h.store.writes.isEmpty)
            XCTAssertEqual(h.notifications, 0)
        }
    }

    func testRecoveryNotFoundCannotCreateButFreshSignupCanContinue() async {
        let h = Harness(); h.result = .notFound
        guard case .notFound = await h.restore() else { return XCTFail("Recovery escaped") }
        XCTAssertTrue(h.store.writes.isEmpty)
        h.state.isOnboarded = false; h.state.contractorId = ""; h.state.kevinNumber = ""
        let outcome = await h.restore(recovery: false)
        XCTAssertEqual(outcome, .newAccountNeeded)
        XCTAssertEqual(h.notifications, 0)
    }

    func testWrongAppleIdentityAndMissingFreshTokenStopBeforeLookup() async {
        let h = Harness(); h.state.appleUserId = "original-apple"
        guard case .failed = await h.restore() else { return XCTFail("Wrong identity accepted") }
        XCTAssertEqual(h.lookups, 0); XCTAssertEqual(h.state.appleUserId, "original-apple")
        h.state.appleUserId = "apple"
        guard case .authFailed = await h.restore(identityToken: "") else { return XCTFail("Missing token accepted") }
        XCTAssertEqual(h.lookups, 0)
    }

    func testPersistenceFailureCannotPublishSessionAndRemainsRetryable() async {
        let h = Harness(); h.store.failingKey = "contractorApiToken"
        guard case .failed = await h.restore() else { return XCTFail("Failed persistence accepted") }
        XCTAssertEqual(h.state.sessionState, .needsRecovery); XCTAssertEqual(h.notifications, 0)
        h.store.failingKey = nil
        let retry = await h.restore()
        XCTAssertEqual(retry, .success(contractorId: "owner", needsProvisioning: false))
    }

    func testRestoredDefaultsWithoutTrustedIdDoNotUploadContacts() async {
        let h = Harness(); h.state.contractorId = ""; h.state.contactsUploadConsent = true
        _ = await h.restore()
        XCTAssertFalse(h.state.contactsUploadConsent); XCTAssertEqual(h.syncs, 0)
    }

    func testInterruptedCommitCannotTurnUnownedConsentIntoTrustedConsentAfterRestart() async {
        let h = Harness(); h.state.contractorId = ""; h.state.contactsUploadConsent = true
        h.store.failingKey = "subscriptionStatus"
        guard case .failed = await h.restore() else { return XCTFail("Partial save accepted") }
        XCTAssertEqual(h.store.values["contractorId"], "owner")
        XCTAssertFalse(h.state.contactsUploadConsent)
        h.store.failingKey = nil
        let restarted = AppState(inMemory: true, secureStore: h.store.client)
        restarted.isOnboarded = true; restarted.kevinNumber = "+15005550006"
        // Even if restored defaults retain the old bit, a pending commit cannot
        // establish that bit's original owner using its newly staged ID.
        restarted.contactsUploadConsent = true
        restarted.refreshSecureStorageForActiveUse()
        XCTAssertEqual(restarted.sessionState, .needsRecovery)
        XCTAssertEqual(restarted.contractorId, "owner")
        let coordinator = AccountRestoreCoordinator(state: restarted,
            lookupHandler: { _, _ in .found(contractorId: "owner", apiToken: "retry-token") },
            profileFetcher: { _, _ in h.profile }, notificationReconciler: {},
            contactSyncHandler: { _ in XCTFail("Unowned consent survived interrupted commit") })
        let outcome = await coordinator.restoreAccount(appleUserId: "apple", appleIdentityToken: "fresh",
            isRecoveryMode: true, retainedContractorId: restarted.contractorId,
            retainedAppleUserId: restarted.appleUserId, retainedKevinNumber: restarted.kevinNumber,
            attemptRevision: coordinator.beginAttempt()!)
        XCTAssertEqual(outcome, .success(contractorId: "owner", needsProvisioning: false))
        XCTAssertFalse(restarted.contactsUploadConsent)
    }

    func testKnownSameAccountConsentIsPreservedOnUninterruptedRestore() async {
        let h = Harness(); h.state.contactsUploadConsent = true
        _ = await h.restore()
        XCTAssertTrue(h.state.contactsUploadConsent); XCTAssertEqual(h.syncs, 1)
    }

    func testRecoveryCannotProvisionWhenNoNumberSurvivesLocallyOrOnServer() async {
        let h = Harness(); h.state.kevinNumber = ""; h.profile?["twilio_number"] = ""
        guard case .failed = await h.restore() else { return XCTFail("Recovery allowed provisioning") }
        XCTAssertTrue(h.store.writes.isEmpty)
    }

    func testBootstrapReturningAccountRequiresVerifiedProfileAndStorage() async {
        let response: [String: Any] = ["status": "ok", "contractor_id": "owner", "api_token": "initial", "existing": true]
        for failure in ["profile", "storage", "identity", "none"] {
            let h = Harness(); h.state.isOnboarded = false; h.state.contractorId = ""; h.state.kevinNumber = ""
            if failure == "profile" { h.profile = nil }
            if failure == "storage" { h.store.failingKey = "contractorApiToken" }
            if failure == "identity" { h.result = .found(contractorId: "another", apiToken: "wrong") }
            h.profile?["subscription_status"] = "expired"
            let outcome = await h.coordinator.restoreBootstrapResponse(response,
                appleUserId: "apple", appleIdentityToken: "fresh", attemptRevision: h.coordinator.beginAttempt()!)
            if failure == "none" {
                XCTAssertEqual(outcome, .success(contractorId: "owner", needsProvisioning: false))
                XCTAssertEqual(h.state.subscriptionStatus, "expired")
                XCTAssertEqual(h.state.currentAuthContext().bearerToken, "new-token")
                XCTAssertEqual(h.notifications, 1)
            } else {
                guard case .failed = outcome else { return XCTFail("Invalid bootstrap accepted: \(failure)") }
                XCTAssertFalse(h.state.isOnboarded); XCTAssertEqual(h.notifications, 0)
            }
        }
    }

    func testBootstrapNewAccountResponseAndFailuresRemainDistinct() async {
        let h = Harness()
        let responses: [[String: Any]?] = [nil, [:], ["status": "ok", "contractor_id": "owner"],
            ["status": "ok", "contractor_id": "owner", "api_token": "token"]]
        for (index, response) in responses.enumerated() {
            let outcome = await h.coordinator.restoreBootstrapResponse(response,
                appleUserId: "apple", appleIdentityToken: "fresh", attemptRevision: h.coordinator.beginAttempt()!)
            if index == 3 { XCTAssertEqual(outcome, .newAccountNeeded) }
            else { guard case .failed = outcome else { return XCTFail("Failed create treated as new account") } }
        }
        XCTAssertEqual(h.lookups, 0); XCTAssertTrue(h.store.writes.isEmpty)
    }

    func testFreshSignupConsentRequiresBothResponseAndLookupToMatchItsBoundIdentity() async {
        for variant in ["consented", "not-consented", "different-id", "different-apple", "different-response"] {
            let h = Harness(); h.state.isOnboarded = false; h.state.contractorId = ""; h.state.kevinNumber = ""
            h.state.contactsUploadConsent = true // A cached bit alone must never suffice.
            let context = SignupContinuation(contractorId: variant == "different-id" ? "other" : "owner",
                appleUserId: variant == "different-apple" ? "other-apple" : "apple",
                explicitContactConsent: variant != "not-consented")
            let response: [String: Any] = ["status": "ok",
                "contractor_id": variant == "different-response" ? "other" : "owner",
                "api_token": "initial", "existing": true]
            let outcome = await h.coordinator.restoreBootstrapResponse(response,
                appleUserId: "apple", appleIdentityToken: "fresh", signupContinuation: context,
                attemptRevision: h.coordinator.beginAttempt()!)
            if variant.hasPrefix("different") {
                guard case .failed = outcome else { return XCTFail("Unbound signup consent accepted") }
                XCTAssertEqual(h.syncs, 0); XCTAssertTrue(h.store.writes.isEmpty)
            } else {
                XCTAssertEqual(outcome, .success(contractorId: "owner", needsProvisioning: false))
                XCTAssertEqual(h.state.contactsUploadConsent, variant == "consented")
                XCTAssertEqual(h.syncs, variant == "consented" ? 1 : 0)
            }
        }
    }

    func testLateProfileCannotRestoreAfterAccountChangesBackToOriginalId() async {
        let h = Harness(); let entered = expectation(description: "profile entered")
        var continuation: CheckedContinuation<[String: Any]?, Never>?
        let coordinator = AccountRestoreCoordinator(state: h.state,
            lookupHandler: { _, _ in .found(contractorId: "owner", apiToken: "new-token") },
            profileFetcher: { _, _ in await withCheckedContinuation { continuation = $0; entered.fulfill() } },
            notificationReconciler: { XCTFail("Stale notifications") }, contactSyncHandler: { _ in XCTFail("Stale contacts") })
        let revision = coordinator.beginAttempt()!
        let task = Task { await coordinator.restoreAccount(appleUserId: "apple", appleIdentityToken: "fresh",
            isRecoveryMode: true, retainedContractorId: "owner", attemptRevision: revision) }
        await fulfillment(of: [entered], timeout: 2)
        h.state.contractorId = "other"; h.state.contractorId = "owner"
        continuation?.resume(returning: h.profile)
        let outcome = await task.value
        XCTAssertEqual(outcome, .superseded); XCTAssertTrue(h.store.writes.isEmpty)
    }

    func testCancelledAttemptCannotPublishLateLookupAndDuplicateIsBlocked() async {
        let h = Harness(); let entered = expectation(description: "lookup entered")
        var continuation: CheckedContinuation<AppleLookupResult, Never>?
        let coordinator = AccountRestoreCoordinator(state: h.state,
            lookupHandler: { _, _ in await withCheckedContinuation { continuation = $0; entered.fulfill() } },
            profileFetcher: { _, _ in XCTFail("Stale profile"); return nil },
            notificationReconciler: {}, contactSyncHandler: { _ in })
        let revision = coordinator.beginAttempt()!
        XCTAssertNil(coordinator.beginAttempt())
        let task = Task { await coordinator.restoreAccount(appleUserId: "apple", appleIdentityToken: "fresh",
            isRecoveryMode: true, attemptRevision: revision) }
        await fulfillment(of: [entered], timeout: 2)
        coordinator.cancelAttempt()
        continuation?.resume(returning: .found(contractorId: "owner", apiToken: "new-token"))
        let outcome = await task.value
        XCTAssertEqual(outcome, .superseded); XCTAssertTrue(h.store.writes.isEmpty)
    }
}
