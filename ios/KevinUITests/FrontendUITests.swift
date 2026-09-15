import XCTest

final class FrontendUITests: XCTestCase {

    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    private func launchApp(scenario: String) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchEnvironment["APP_STORE_SCREENSHOT_SCENARIO"] = scenario
        app.launchEnvironment["KEVIN_UNIT_TESTS"] = "0"
        app.launchArguments = ["-AppleLanguages", "(en-US)", "-AppleLocale", "en_US"]
        app.launch()
        return app
    }

    private func scrollToElement(_ element: XCUIElement, in app: XCUIApplication, maxSwipes: Int = 10) -> Bool {
        var swipes = 0
        while !element.isHittable && swipes < maxSwipes {
            app.swipeUp()
            swipes += 1
            if element.exists && element.isHittable {
                return true
            }
        }
        return element.exists
    }

    private func attachScreenshot(name: String, app: XCUIApplication) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    // MARK: - 1. Bounded History & Pagination UI

    func testBoundedHistoryPagination101AndExpansion() throws {
        let app = launchApp(scenario: "history-101")

        // 1. Verify search field and count label exist
        let searchField = app.textFields["calls.search"]
        XCTAssertTrue(searchField.waitForExistence(timeout: 5), "Search text field must exist")

        let countLabel = app.staticTexts["calls.count"]
        XCTAssertTrue(countLabel.waitForExistence(timeout: 5), "Count label must exist")
        XCTAssertEqual(countLabel.label, "Showing 20 of 100 calls")

        // 2. Find and tap 'Show 20 more' button to expand to 40
        let showMoreButton = app.buttons["calls.showMore"]
        _ = scrollToElement(showMoreButton, in: app)
        XCTAssertTrue(showMoreButton.exists, "'Show 20 more' button must be present when more than 20 items exist")
        showMoreButton.tap()

        XCTAssertTrue(countLabel.waitForExistence(timeout: 5))
        XCTAssertEqual(countLabel.label, "Showing 40 of 100 calls")

        // 3. Expand until capped at 100
        for _ in 0..<3 {
            if showMoreButton.exists && scrollToElement(showMoreButton, in: app) {
                showMoreButton.tap()
            }
        }

        XCTAssertTrue(countLabel.waitForExistence(timeout: 5))
        XCTAssertEqual(countLabel.label, "Showing the 100 most recent calls available in this history.")
        XCTAssertFalse(showMoreButton.exists, "'Show 20 more' button must disappear once all 100 calls are shown")

        attachScreenshot(name: "bounded-history-101-expanded", app: app)
    }

    // MARK: - 2. Search Matches Beyond Initial Page & Detail Preservation

    func testSearchCedarMatchesBeyondInitialPageAndPreservesOnDetailRoundtrip() throws {
        let app = launchApp(scenario: "history-101")

        let searchField = app.textFields["calls.search"]
        XCTAssertTrue(searchField.waitForExistence(timeout: 5))
        searchField.tap()
        searchField.typeText("Cedar")

        // Row 35 ('Cedar Plumber') must appear in search results
        let cedarRow = app.buttons["calls.row.CA_RECORD_035"]
        XCTAssertTrue(cedarRow.waitForExistence(timeout: 5), "Cedar row 35 must match search beyond initial 20 page")

        // Tap row to open historical detail
        cedarRow.tap()

        let doneButton = app.buttons["calls.detailDone"]
        XCTAssertTrue(doneButton.waitForExistence(timeout: 5), "Detail sheet Done button must exist")

        // Dismiss detail
        doneButton.tap()

        // Verify search query and results are preserved after returning
        XCTAssertTrue(searchField.waitForExistence(timeout: 5))
        XCTAssertTrue(cedarRow.exists, "Search state must be preserved after returning from detail")

        attachScreenshot(name: "search-cedar-detail-roundtrip", app: app)
    }

    // MARK: - 3. Kevin and Account Done Roundtrip from Both Tabs

    func testKevinAndAccountDoneRoundtripFromBothTabs() throws {
        let app = launchApp(scenario: "business-recents")

        // 1. From Calls tab, open Account Settings
        let settingsButton = app.buttons["nav.settings"]
        XCTAssertTrue(settingsButton.waitForExistence(timeout: 5))
        settingsButton.tap()

        let accountDone = app.buttons["settings.done"]
        XCTAssertTrue(accountDone.waitForExistence(timeout: 5), "Account sheet Done button must exist")
        accountDone.tap()

        // Verify Calls tab is still active
        let searchField = app.textFields["calls.search"]
        XCTAssertTrue(searchField.waitForExistence(timeout: 5), "Calls tab must remain active after account dismissal")

        // 2. Switch to Kevin tab
        let kevinTab = app.buttons["kevin.tab"]
        XCTAssertTrue(kevinTab.waitForExistence(timeout: 5))
        kevinTab.tap()

        // Open Account Settings from Kevin tab
        XCTAssertTrue(settingsButton.waitForExistence(timeout: 5))
        settingsButton.tap()

        XCTAssertTrue(accountDone.waitForExistence(timeout: 5))
        accountDone.tap()

        // Verify Kevin tab is still active
        XCTAssertTrue(kevinTab.waitForExistence(timeout: 5))

        attachScreenshot(name: "kevin-account-roundtrip", app: app)
    }

    // MARK: - 4. Initial Error & Retry

    func testInitialErrorAndRetry() throws {
        let app = launchApp(scenario: "history-error")

        let retryButton = app.buttons["Retry"]
        XCTAssertTrue(retryButton.waitForExistence(timeout: 5), "Retry button must exist on initial error state")

        retryButton.tap()

        // After Retry, calls should load successfully
        let countLabel = app.staticTexts["calls.count"]
        XCTAssertTrue(countLabel.waitForExistence(timeout: 5), "Calls must load successfully after Retry")

        attachScreenshot(name: "history-error-retry-success", app: app)
    }

    // MARK: - 5. Active Call Card & Full Live Transcript Detail

    func testActiveCardOpensFullTranscriptAndDoneRetainsCard() throws {
        let app = launchApp(scenario: "business-live")

        let activeCard = app.otherElements["call.activeCard"]
        XCTAssertTrue(activeCard.waitForExistence(timeout: 5), "Active call card must exist")

        let viewLiveButton = app.buttons["call.viewLive"]
        XCTAssertTrue(viewLiveButton.waitForExistence(timeout: 5), "'View live call' control must exist on active card")
        viewLiveButton.tap()

        let liveDoneButton = app.buttons["call.liveDone"]
        XCTAssertTrue(liveDoneButton.waitForExistence(timeout: 5), "Live transcript detail Done button must exist")

        liveDoneButton.tap()

        // Active card must remain visible on Calls tab after live detail dismissal
        XCTAssertTrue(activeCard.waitForExistence(timeout: 5), "Active call card must remain visible after live detail dismissal")

        attachScreenshot(name: "active-card-live-detail-done", app: app)
    }

    // MARK: - 6. Return from Kevin and Account Opens Exact Full Transcript

    func testReturnFromKevinAndAccountOpensExactFullTranscript() throws {
        let app = launchApp(scenario: "business-live")

        // 1. From Kevin tab, tap return to call
        let kevinTab = app.buttons["kevin.tab"]
        XCTAssertTrue(kevinTab.waitForExistence(timeout: 5))
        kevinTab.tap()

        let returnButton = app.buttons["call.return"]
        XCTAssertTrue(returnButton.waitForExistence(timeout: 5), "Compact return to call card must exist on Kevin tab")
        returnButton.tap()

        let liveDoneButton = app.buttons["call.liveDone"]
        XCTAssertTrue(liveDoneButton.waitForExistence(timeout: 5), "Live transcript detail must open from Kevin return card")
        liveDoneButton.tap()

        // 2. Open Account Settings and tap return to call from account sheet
        let settingsButton = app.buttons["nav.settings"]
        XCTAssertTrue(settingsButton.waitForExistence(timeout: 5))
        settingsButton.tap()

        XCTAssertTrue(returnButton.waitForExistence(timeout: 5), "Compact return to call card must exist in Account sheet")
        returnButton.tap()

        // Account sheet must close and live transcript detail must open
        XCTAssertTrue(liveDoneButton.waitForExistence(timeout: 5), "Live transcript detail must open from Account sheet return card")
        liveDoneButton.tap()

        attachScreenshot(name: "return-to-call-from-kevin-and-account", app: app)
    }

    // MARK: - 7. Fixture External Actions Disabled

    func testFixtureExternalActionButtonsDisabled() throws {
        let app = launchApp(scenario: "business-live")

        let pickupButton = app.buttons["call.pickup"]
        XCTAssertTrue(pickupButton.waitForExistence(timeout: 5))
        XCTAssertFalse(pickupButton.isEnabled, "Pick up button must be disabled in screenshot fixtures")

        let messageButton = app.buttons["call.message"]
        XCTAssertTrue(messageButton.waitForExistence(timeout: 5))
        XCTAssertFalse(messageButton.isEnabled, "Take message button must be disabled in screenshot fixtures")

        attachScreenshot(name: "fixture-actions-disabled", app: app)
    }

    // MARK: - 8. Retained Error Pull-To-Refresh and Retry

    func testRetainedErrorPullToRefreshAndRetry() throws {
        let app = launchApp(scenario: "history-retained-error")

        // 1. Initial load shows rows
        let firstRow = app.buttons["calls.row.CA_BIZ_001"]
        XCTAssertTrue(firstRow.waitForExistence(timeout: 5), "Initial call rows must be visible")

        // 2. Pull to refresh to trigger retained error
        let firstCell = app.cells.firstMatch
        if firstCell.exists {
            let start = firstCell.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.2))
            let finish = firstCell.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.9))
            start.press(forDuration: 0.1, thenDragTo: finish)
        } else {
            app.swipeDown()
        }

        // 3. Verify error banner appears AND same rows remain visible
        let retryButton = app.buttons["Retry"]
        XCTAssertTrue(retryButton.waitForExistence(timeout: 5), "Retry button in retained error banner must appear")
        XCTAssertTrue(firstRow.exists, "Rows must remain visible when refresh fails (retained error)")

        // 4. Tap Retry to recover
        retryButton.tap()

        // 5. Verify error is removed and rows remain
        let errorBanner = app.staticTexts["Failed to refresh calls."]
        XCTAssertFalse(errorBanner.waitForExistence(timeout: 3), "Error banner must be removed after successful retry")
        XCTAssertTrue(firstRow.exists, "Rows must still remain visible after retry")

        attachScreenshot(name: "history-retained-error-recovered", app: app)
    }

    // MARK: - 9. Settings Actions Disabled in Screenshot Fixtures

    func testSettingsSectionsAndActionsDisabledInScreenshotFixture() throws {
        let app = launchApp(scenario: "account-settings")

        // 1. Open Account Settings
        let settingsButton = app.buttons["nav.settings"]
        XCTAssertTrue(settingsButton.waitForExistence(timeout: 5))
        settingsButton.tap()

        let accountDone = app.buttons["settings.done"]
        XCTAssertTrue(accountDone.waitForExistence(timeout: 5))

        // Check Account & Plan View Plans button is disabled
        let viewPlans = app.buttons["settings.viewPlans"]
        if viewPlans.exists {
            XCTAssertFalse(viewPlans.isEnabled, "View Plans button should be disabled in screenshot fixtures")
        }

        // Check Delete Account button is disabled
        let deleteButton = app.buttons["Delete Account"]
        if deleteButton.exists {
            XCTAssertFalse(deleteButton.isEnabled, "Delete Account button should be disabled in screenshot fixtures")
        }

        accountDone.tap()

        // 2. On Kevin tab, check Kevin sections
        let kevinTab = app.buttons["kevin.tab"]
        XCTAssertTrue(kevinTab.waitForExistence(timeout: 5))
        kevinTab.tap()

        let screenAllToggle = app.switches["kevin.screenAllCalls"]
        if screenAllToggle.exists {
            XCTAssertFalse(screenAllToggle.isEnabled, "Screen all calls toggle must be disabled in screenshot fixtures")
        }

        attachScreenshot(name: "settings-fixtures-disabled", app: app)
    }
}
