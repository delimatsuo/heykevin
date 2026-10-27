import Foundation
import XCTest
@testable import Kevin

private final class UploadRecorder {
    var uploads: [(contacts: [(name: String, phone: String)], hash: String)] = []
    var reply: ContactSyncUploadResult = .synced(synced: 0, removed: 0)

    func upload(_ contacts: [(name: String, phone: String)], _ hash: String) async -> ContactSyncUploadResult {
        uploads.append((contacts, hash))
        return reply
    }
}

final class ContactSyncUploadTests: XCTestCase {
    private var defaults: UserDefaults!
    private var suiteName = ""

    override func setUp() {
        super.setUp()
        suiteName = "ContactSyncUploadTests.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suiteName)
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: suiteName)
        super.tearDown()
    }

    private func contacts(_ count: Int) -> [(name: String, phone: String)] {
        (0..<count).map { (name: "Contact \($0)", phone: String(format: "(202) 555-%04d", $0)) }
    }

    private func upload(
        _ list: [(name: String, phone: String)],
        force: Bool = false,
        with recorder: UploadRecorder
    ) async -> (result: SyncResult, completed: Bool) {
        await ContactSyncManager.upload(list, force: force, defaults: defaults, uploader: recorder.upload)
    }

    func testWholeListGoesInOneRequestWithItsHash() async {
        let recorder = UploadRecorder()
        recorder.reply = .synced(synced: 450, removed: 0)
        let list = contacts(450)

        let outcome = await upload(list, with: recorder)

        XCTAssertEqual(outcome.result, .success(synced: 450, removed: 0))
        XCTAssertEqual(recorder.uploads.count, 1)
        XCTAssertEqual(recorder.uploads[0].contacts.count, 450)
        XCTAssertEqual(recorder.uploads[0].hash, ContactSyncManager.listHash(list))
        XCTAssertEqual(defaults.string(forKey: ContactSyncManager.lastSyncHashKey), ContactSyncManager.listHash(list))
        XCTAssertGreaterThan(defaults.double(forKey: ContactSyncManager.lastSyncAttemptKey), 0)
    }

    func testListIsCappedAndHashedAsSent() async {
        let recorder = UploadRecorder()
        let list = contacts(5003)

        _ = await upload(list, with: recorder)

        XCTAssertEqual(recorder.uploads[0].contacts.count, 5000)
        XCTAssertEqual(recorder.uploads[0].hash, ContactSyncManager.listHash(Array(list.prefix(5000))))
    }

    func testAnUploadTheServerDidNotTakeIsSentAgain() async {
        let recorder = UploadRecorder()
        let list = contacts(300)

        for reply in [ContactSyncUploadResult.rateLimited, .failed] {
            recorder.reply = reply
            let outcome = await upload(list, with: recorder)
            XCTAssertFalse(outcome.completed)
            XCTAssertNil(defaults.string(forKey: ContactSyncManager.lastSyncHashKey))
            XCTAssertEqual(defaults.double(forKey: ContactSyncManager.lastSyncAttemptKey), 0)
        }
        recorder.reply = .synced(synced: 300, removed: 0)
        _ = await upload(list, with: recorder)

        XCTAssertEqual(recorder.uploads.count, 3)
    }

    func testAnUnchangedListIsSkippedUnlessTheOwnerAsks() async {
        let recorder = UploadRecorder()
        recorder.reply = .synced(synced: 300, removed: 0)
        let list = contacts(300)
        _ = await upload(list, with: recorder)

        let automatic = await upload(list, with: recorder)
        XCTAssertEqual(recorder.uploads.count, 1)
        XCTAssertTrue(automatic.completed)

        recorder.reply = .unchanged
        let manual = await upload(list, force: true, with: recorder)
        XCTAssertEqual(recorder.uploads.count, 2)
        XCTAssertEqual(manual.result, .success(synced: 300, removed: 0))
    }

    func testAnEmptyAddressBookUploadsNothing() async {
        let recorder = UploadRecorder()

        let outcome = await upload([], with: recorder)

        XCTAssertEqual(outcome.result, .success(synced: 0, removed: 0))
        XCTAssertTrue(recorder.uploads.isEmpty)
    }

    func testCacheKeysDifferFromBuildsThatCachedTruncatedUploads() {
        XCTAssertNotEqual(ContactSyncManager.lastSyncHashKey, "lastContactSyncHash")
        XCTAssertNotEqual(ContactSyncManager.lastSyncAttemptKey, "lastContactSyncAttemptAt")
    }

    func testListHashMatchesTheServer() {
        // Same vector as the server's test of _client_list_hash.
        let list = ["(202) 555-0143", "+1 212-555-0100", "415.555.0199", "(202) 555-0143"]
            .map { (name: "", phone: $0) }
        XCTAssertEqual(
            ContactSyncManager.listHash(list),
            "0336923ab55bbd2c9c60a38d2e5130f8c95dbc13a8f482bbacc3950d769c3b61"
        )
    }
}

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
