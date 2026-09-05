import Foundation
import XCTest
@testable import Kevin

@MainActor
final class StoreReviewManagerTests: XCTestCase {
    private var testDefaults: UserDefaults!
    private var testSuiteName: String!

    override func setUp() {
        super.setUp()
        testSuiteName = "StoreReviewManagerTests.\(UUID().uuidString)"
        testDefaults = UserDefaults(suiteName: testSuiteName)!
    }

    override func tearDown() {
        testDefaults.removePersistentDomain(forName: testSuiteName)
        testDefaults = nil
        testSuiteName = nil
        super.tearDown()
    }

    // MARK: - Milestone Threshold Gating

    func testMilestoneGating_underThresholdDoesNotPrompt() {
        var prompted = false
        let manager = StoreReviewManager(
            userDefaults: testDefaults,
            currentDateProvider: { Date(timeIntervalSince1970: 1_000_000) },
            currentVersionProvider: { "1.2.11" },
            isCallActiveProvider: { false },
            reviewRequester: { prompted = true }
        )

        // 0 screened calls, no appointment confirmed
        XCTAssertEqual(manager.screenedCallCount, 0)
        XCTAssertFalse(manager.appointmentConfirmed)
        XCTAssertFalse(manager.isMilestoneMet)
        XCTAssertFalse(manager.requestReviewIfEligible())
        XCTAssertFalse(prompted)

        // 1 screened call
        manager.incrementScreenedCallCount()
        XCTAssertEqual(manager.screenedCallCount, 1)
        XCTAssertFalse(manager.isMilestoneMet)
        XCTAssertFalse(manager.requestReviewIfEligible())
        XCTAssertFalse(prompted)

        // 2 screened calls (threshold is 3)
        manager.incrementScreenedCallCount()
        XCTAssertEqual(manager.screenedCallCount, 2)
        XCTAssertFalse(manager.isMilestoneMet)
        XCTAssertFalse(manager.requestReviewIfEligible())
        XCTAssertFalse(prompted)
    }

    func testMilestoneGating_atThresholdDoesPrompt() {
        var prompted = false
        let manager = StoreReviewManager(
            userDefaults: testDefaults,
            currentDateProvider: { Date(timeIntervalSince1970: 1_000_000) },
            currentVersionProvider: { "1.2.11" },
            isCallActiveProvider: { false },
            reviewRequester: { prompted = true }
        )

        // Increment to 3 (at threshold)
        manager.incrementScreenedCallCount()
        manager.incrementScreenedCallCount()
        manager.incrementScreenedCallCount()
        XCTAssertEqual(manager.screenedCallCount, 3)
        XCTAssertTrue(manager.isMilestoneMet)

        let result = manager.requestReviewIfEligible()
        XCTAssertTrue(result)
        XCTAssertTrue(prompted)

        // Verify persisted state
        XCTAssertEqual(manager.lastPromptedVersion, "1.2.11")
        XCTAssertEqual(manager.lastPromptedDate?.timeIntervalSince1970, 1_000_000)
    }

    func testMilestoneGating_appointmentConfirmedDoesPrompt() {
        var prompted = false
        let manager = StoreReviewManager(
            userDefaults: testDefaults,
            currentDateProvider: { Date(timeIntervalSince1970: 1_000_000) },
            currentVersionProvider: { "1.2.11" },
            isCallActiveProvider: { false },
            reviewRequester: { prompted = true }
        )

        XCTAssertEqual(manager.screenedCallCount, 0)
        manager.recordAppointmentConfirmed()
        XCTAssertTrue(manager.appointmentConfirmed)
        XCTAssertTrue(manager.isMilestoneMet)

        let result = manager.requestReviewIfEligible()
        XCTAssertTrue(result)
        XCTAssertTrue(prompted)
    }

    // MARK: - Version Gating

    func testVersionGating_doesNotPromptTwiceOnSameVersion() {
        var promptCount = 0
        var simulatedVersion = "1.2.11"
        var simulatedDate = Date(timeIntervalSince1970: 1_000_000)

        let manager = StoreReviewManager(
            userDefaults: testDefaults,
            currentDateProvider: { simulatedDate },
            currentVersionProvider: { simulatedVersion },
            isCallActiveProvider: { false },
            reviewRequester: { promptCount += 1 }
        )

        manager.recordAppointmentConfirmed()

        // First prompt on version 1.2.11 succeeds
        XCTAssertTrue(manager.requestReviewIfEligible())
        XCTAssertEqual(promptCount, 1)
        XCTAssertFalse(manager.isVersionEligible)

        // Second prompt on same version fails
        XCTAssertFalse(manager.requestReviewIfEligible())
        XCTAssertEqual(promptCount, 1)

        // Advance date by 90 days (> 60 days) but keep version same -> still blocked by version gate
        simulatedDate = simulatedDate.addingTimeInterval(90 * 86_400)
        XCTAssertFalse(manager.requestReviewIfEligible())
        XCTAssertEqual(promptCount, 1)

        // Bump marketing version -> now version is eligible and date is eligible -> succeeds
        simulatedVersion = "1.2.12"
        XCTAssertTrue(manager.isVersionEligible)
        XCTAssertTrue(manager.requestReviewIfEligible())
        XCTAssertEqual(promptCount, 2)
        XCTAssertEqual(manager.lastPromptedVersion, "1.2.12")
    }

    // MARK: - Date Rate-Limiting

    func testDateRateLimiting_doesNotPromptWithin60Days() {
        var promptCount = 0
        var simulatedDate = Date(timeIntervalSince1970: 10_000_000)
        var simulatedVersion = "1.2.11"

        let manager = StoreReviewManager(
            userDefaults: testDefaults,
            currentDateProvider: { simulatedDate },
            currentVersionProvider: { simulatedVersion },
            isCallActiveProvider: { false },
            reviewRequester: { promptCount += 1 }
        )

        manager.recordAppointmentConfirmed()

        // First prompt
        XCTAssertTrue(manager.requestReviewIfEligible())
        XCTAssertEqual(promptCount, 1)

        // Bump version to 1.3.0 so version gate passes
        simulatedVersion = "1.3.0"

        // Advance 30 days (< 60 days)
        simulatedDate = simulatedDate.addingTimeInterval(30 * 86_400)
        XCTAssertFalse(manager.isDateEligible)
        XCTAssertFalse(manager.requestReviewIfEligible())
        XCTAssertEqual(promptCount, 1)

        // Advance to 59 days (< 60 days)
        simulatedDate = Date(timeIntervalSince1970: 10_000_000).addingTimeInterval(59 * 86_400)
        XCTAssertFalse(manager.isDateEligible)
        XCTAssertFalse(manager.requestReviewIfEligible())
        XCTAssertEqual(promptCount, 1)

        // Advance to 60 days (>= 60 days) -> succeeds
        simulatedDate = Date(timeIntervalSince1970: 10_000_000).addingTimeInterval(60 * 86_400)
        XCTAssertTrue(manager.isDateEligible)
        XCTAssertTrue(manager.requestReviewIfEligible())
        XCTAssertEqual(promptCount, 2)
    }

    // MARK: - Active Call Guard Rail

    func testActiveCallGuardRail_neverPromptsDuringCall() {
        var promptCount = 0
        var isCallActive = true

        let manager = StoreReviewManager(
            userDefaults: testDefaults,
            currentDateProvider: { Date(timeIntervalSince1970: 1_000_000) },
            currentVersionProvider: { "1.2.11" },
            isCallActiveProvider: { isCallActive },
            reviewRequester: { promptCount += 1 }
        )

        manager.recordAppointmentConfirmed()
        XCTAssertTrue(manager.isMilestoneMet)

        // Active call blocks prompt
        XCTAssertFalse(manager.isEligibleForReview)
        XCTAssertFalse(manager.requestReviewIfEligible())
        XCTAssertEqual(promptCount, 0)

        // Once call ends, prompt is allowed
        isCallActive = false
        XCTAssertTrue(manager.isEligibleForReview)
        XCTAssertTrue(manager.requestReviewIfEligible())
        XCTAssertEqual(promptCount, 1)
    }

    // MARK: - Feedback Support Tests

    func testFeedbackBuildMailtoURL() {
        guard let url = FeedbackSupport.buildMailtoURL(
            version: "1.2.11",
            build: "36",
            iOSVersion: "17.5",
            deviceModel: "iPhone16,1",
            contractorId: "cnt_abc123"
        ) else {
            XCTFail("Failed to build mailto URL")
            return
        }

        XCTAssertEqual(url.scheme, "mailto")
        let urlString = url.absoluteString
        XCTAssertTrue(urlString.contains("support@heykevin.one"))
        XCTAssertTrue(urlString.contains("subject=Hey%20Kevin%20Feedback%20(1.2.11)"))
        XCTAssertTrue(urlString.contains("App%20Version:%201.2.11%20(36)"))
        XCTAssertTrue(urlString.contains("iOS:%2017.5"))
        XCTAssertTrue(urlString.contains("Device:%20iPhone16,1"))
        XCTAssertTrue(urlString.contains("Account%20ID:%20cnt_abc123"))
    }

    func testFeedbackFallbackWhenMailUnavailable() {
        var openedURL: URL?
        FeedbackSupport.sendFeedback(
            contractorId: "test_contractor",
            canOpenURL: { _ in false },
            openURL: { openedURL = $0 }
        )

        XCTAssertEqual(openedURL, URL(string: "https://heykevin.one"))
    }

    func testFeedbackOpensMailtoWhenAvailable() {
        var openedURL: URL?
        FeedbackSupport.sendFeedback(
            contractorId: "test_contractor",
            canOpenURL: { _ in true },
            openURL: { openedURL = $0 }
        )

        XCTAssertNotNil(openedURL)
        XCTAssertEqual(openedURL?.scheme, "mailto")
        XCTAssertTrue(openedURL?.absoluteString.contains("support@heykevin.one") ?? false)
    }
}
