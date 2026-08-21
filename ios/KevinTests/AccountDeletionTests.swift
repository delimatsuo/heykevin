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

/// The delete flow's first step: a user with an active auto-renewing
/// subscription must be told deletion does not cancel it — deleting is a
/// soft deactivate and Apple keeps charging — before any confirmation.
final class AccountDeletionFlowTests: XCTestCase {
    func testActiveSubscriptionWarnsBeforeConfirming() {
        XCTAssertEqual(
            AccountDeletionFlow.firstStep(subscriptionStatus: "active"),
            .warnActiveSubscription
        )
    }

    func testNonActiveStatusesGoStraightToConfirmation() {
        for status in ["trial", "expired", "cancelled", ""] {
            XCTAssertEqual(
                AccountDeletionFlow.firstStep(subscriptionStatus: status),
                .confirmDelete,
                "status \(status)"
            )
        }
    }
}
