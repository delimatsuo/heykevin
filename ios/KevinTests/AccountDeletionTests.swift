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
