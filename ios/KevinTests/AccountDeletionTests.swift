import Foundation
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
