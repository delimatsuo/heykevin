import Foundation
import XCTest
@testable import Kevin

final class ContactSyncTests: XCTestCase {
    private func parse(_ body: String, status: Int = 200) -> ContactSyncUploadResult {
        let response = HTTPURLResponse(
            url: URL(string: "https://example.invalid/api/contacts/bulk-sync")!,
            statusCode: status,
            httpVersion: nil,
            headerFields: nil
        )!
        return ContactSyncResponseParser.parse(data: Data(body.utf8), response: response)
    }

    func testSyncedReplyReportsCounts() {
        XCTAssertEqual(
            parse(#"{"status": "ok", "synced": 412, "removed": 3}"#),
            .synced(synced: 412, removed: 3)
        )
    }

    func testUnchangedAndRateLimitedReplies() {
        XCTAssertEqual(parse(#"{"status": "skipped", "reason": "no_changes"}"#), .unchanged)
        XCTAssertEqual(parse(#"{"status": "rate_limited", "retry_after_seconds": 120}"#), .rateLimited)
    }

    func testServerErrorsAreFailures() {
        // The deployed server reports a failed write as "ok" with an error key.
        XCTAssertEqual(parse(#"{"status": "ok", "synced": 0, "removed": 0, "error": "boom"}"#), .failed)
        XCTAssertEqual(parse(#"{"status": "error", "synced": 0, "removed": 0}"#), .failed)
        XCTAssertEqual(parse(#"{"status": "rejected", "reason": "too_many_contacts"}"#), .failed)
        XCTAssertEqual(parse(#"{"detail": "List should have at most 5000 items"}"#, status: 422), .failed)
        XCTAssertEqual(parse("not json"), .failed)
    }

    func testOnlyRepliesThatHoldTheListAllowCachingTheHash() {
        XCTAssertEqual(
            ContactSyncManager.result(for: .synced(synced: 412, removed: 3), uploadedCount: 415).serverHasList,
            true
        )
        XCTAssertEqual(ContactSyncManager.result(for: .unchanged, uploadedCount: 415).serverHasList, true)
        XCTAssertEqual(ContactSyncManager.result(for: .rateLimited, uploadedCount: 415).serverHasList, false)
        XCTAssertEqual(ContactSyncManager.result(for: .failed, uploadedCount: 415).serverHasList, false)
    }

    func testResultsShownToTheOwner() {
        XCTAssertEqual(
            ContactSyncManager.result(for: .synced(synced: 412, removed: 3), uploadedCount: 415).result,
            .success(synced: 412, removed: 3)
        )
        XCTAssertEqual(
            ContactSyncManager.result(for: .unchanged, uploadedCount: 415).result,
            .success(synced: 415, removed: 0)
        )
        XCTAssertEqual(ContactSyncManager.result(for: .rateLimited, uploadedCount: 415).result, .rateLimited)
        XCTAssertEqual(ContactSyncManager.result(for: .failed, uploadedCount: 415).result, .error("Sync failed"))
    }
}
