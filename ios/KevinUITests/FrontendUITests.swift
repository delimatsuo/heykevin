import XCTest

final class FrontendUITests: XCTestCase {

    private var currentApp: XCUIApplication?

    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    override func tearDownWithError() throws {
        if let testRun = testRun, testRun.failureCount > 0, let app = currentApp {
            let screenshotAttachment = XCTAttachment(screenshot: app.screenshot())
            screenshotAttachment.name = "failure-screenshot"
            screenshotAttachment.lifetime = .keepAlways
            add(screenshotAttachment)

            let debugAttachment = XCTAttachment(string: app.debugDescription)
            debugAttachment.name = "failure-debug-description"
            debugAttachment.lifetime = .keepAlways
            add(debugAttachment)
        }
        currentApp = nil
    }

    private func launchApp(scenario: String) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchEnvironment["APP_STORE_SCREENSHOT_SCENARIO"] = scenario
        app.launchEnvironment["KEVIN_NATIVE_UI_REVIEW"] = "1"
        app.launchEnvironment["KEVIN_UNIT_TESTS"] = "0"
        app.launchArguments = ["-AppleLanguages", "(en-US)", "-AppleLocale", "en_US"]
        app.launch()
        self.currentApp = app
        return app
    }

    enum ScrollDirection {
        case down
        case up
    }

    @discardableResult
    private func scrollToElement(
        _ element: XCUIElement,
        in app: XCUIApplication,
        direction: ScrollDirection = .down,
        maxSwipes: Int = 35
    ) -> Bool {
        if element.exists && element.isHittable {
            return true
        }
        let container = app.collectionViews.firstMatch.exists ? app.collectionViews.firstMatch : app.tables.firstMatch
        var swipes = 0
        while swipes < maxSwipes {
            if element.exists && element.isHittable {
                return true
            }
            if container.exists {
                switch direction {
                case .down:
                    container.swipeUp()
                case .up:
                    container.swipeDown()
                }
            } else {
                switch direction {
                case .down:
                    app.swipeUp()
                case .up:
                    app.swipeDown()
                }
            }
            swipes += 1
            if element.exists && element.isHittable {
                return true
            }
        }
        return element.exists && element.isHittable
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
        let foundShowMore = scrollToElement(showMoreButton, in: app, direction: .down)
        XCTAssertTrue(foundShowMore && showMoreButton.exists, "'Show 20 more' button must be present when more than 20 items exist")
        showMoreButton.tap()

        // Scroll back to top before asserting count label
        let scrolledToTop40 = scrollToElement(countLabel, in: app, direction: .up)
        XCTAssertTrue(scrolledToTop40 && countLabel.exists)
        XCTAssertEqual(countLabel.label, "Showing 40 of 100 calls")

        // 3. Expand until capped at 100 (each tap grows by 20: 40 -> 60, 60 -> 80, 80 -> 100)
        for _ in 0..<3 {
            let foundNext = scrollToElement(showMoreButton, in: app, direction: .down)
            XCTAssertTrue(foundNext && showMoreButton.exists, "'Show 20 more' button must exist for remaining expansion")
            showMoreButton.tap()
        }

        // Scroll back to top before asserting final count label
        let scrolledToTop100 = scrollToElement(countLabel, in: app, direction: .up)
        XCTAssertTrue(scrolledToTop100 && countLabel.exists)
        XCTAssertEqual(countLabel.label, "Showing the 100 most recent calls available in this history.")

        // Verify button is absent when fully expanded
        _ = scrollToElement(showMoreButton, in: app, direction: .down)
        XCTAssertFalse(showMoreButton.exists, "'Show 20 more' button must disappear once all 100 calls are shown")
    }

    // MARK: - 2. Search Matches Beyond Initial Page & Detail Preservation

    func testSearchCedarMatchesBeyondInitialPageAndPreservesOnDetailRoundtrip() throws {
        let app = launchApp(scenario: "history-101")

        let searchField = app.textFields["calls.search"]
        XCTAssertTrue(searchField.waitForExistence(timeout: 5))
        searchField.tap()
        searchField.typeText("Cedar")

        // Row 35 ('Cedar Plumber') must appear in search results
        let cedarRow = app.descendants(matching: .any).matching(identifier: "calls.row.CA_RECORD_035").firstMatch
        XCTAssertTrue(cedarRow.waitForExistence(timeout: 5), "Cedar row 35 must match search beyond initial 20 page")
        XCTAssertTrue(cedarRow.label.contains("Cedar"), "Row label must contain Cedar")

        // Tap row to open historical detail
        cedarRow.tap()

        let doneButton = app.buttons["calls.detailDone"]
        XCTAssertTrue(doneButton.waitForExistence(timeout: 5), "Detail sheet Done button must exist")

        // Dismiss detail
        doneButton.tap()

        // Verify search query and results are preserved after returning
        XCTAssertTrue(searchField.waitForExistence(timeout: 5))
        XCTAssertTrue(cedarRow.waitForExistence(timeout: 5), "Search state must be preserved after returning from detail")
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

        // Verify Calls tab is still active and selected
        let callsTab = app.tabBars.buttons["Calls"]
        XCTAssertTrue(callsTab.waitForExistence(timeout: 5))
        XCTAssertTrue(callsTab.isSelected, "Calls tab must remain selected after account dismissal")

        let searchField = app.textFields["calls.search"]
        XCTAssertTrue(searchField.waitForExistence(timeout: 5), "Calls tab must remain active after account dismissal")

        // 2. Switch to Kevin tab
        let kevinTab = app.tabBars.buttons["Kevin"]
        XCTAssertTrue(kevinTab.waitForExistence(timeout: 5))
        kevinTab.tap()
        XCTAssertTrue(kevinTab.isSelected, "Kevin tab must be selected after tap")

        // Open Account Settings from Kevin tab
        XCTAssertTrue(settingsButton.waitForExistence(timeout: 5))
        settingsButton.tap()

        XCTAssertTrue(accountDone.waitForExistence(timeout: 5))
        accountDone.tap()

        // Verify Kevin tab is still active and selected
        XCTAssertTrue(kevinTab.waitForExistence(timeout: 5))
        XCTAssertTrue(kevinTab.isSelected, "Kevin tab must remain selected after account dismissal")
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
    }

    // MARK: - 6. Return from Kevin and Account Opens Exact Full Transcript

    func testReturnFromKevinAndAccountOpensExactFullTranscript() throws {
        let app = launchApp(scenario: "business-live")

        // 1. From Kevin tab, tap return to call
        let kevinTab = app.tabBars.buttons["Kevin"]
        XCTAssertTrue(kevinTab.waitForExistence(timeout: 5))
        kevinTab.tap()
        XCTAssertTrue(kevinTab.isSelected)

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
    }

    // MARK: - 8. Retained Error Pull-To-Refresh and Retry

    func testRetainedErrorPullToRefreshAndRetry() throws {
        let app = launchApp(scenario: "history-retained-error")

        // 1. Initial load shows rows
        let firstRow = app.descendants(matching: .any).matching(identifier: "calls.row.business-urgent").firstMatch
        XCTAssertTrue(firstRow.waitForExistence(timeout: 5), "Initial call rows must be visible")

        // 2. Pull to refresh to trigger retained error
        let scrollContainer = app.collectionViews.firstMatch.exists ? app.collectionViews.firstMatch : app.tables.firstMatch
        let start = scrollContainer.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.2))
        let finish = scrollContainer.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.85))
        start.press(forDuration: 0.1, thenDragTo: finish)

        // 3. Verify error banner appears AND same rows remain visible
        let retryButton = app.buttons["Retry"]
        XCTAssertTrue(retryButton.waitForExistence(timeout: 5), "Retry button in retained error banner must appear")
        XCTAssertTrue(firstRow.exists, "Rows must remain visible when refresh fails (retained error)")

        // 4. Tap Retry to recover
        retryButton.tap()

        // 5. Verify error is removed and rows remain
        let errorBanner = app.staticTexts["Failed to refresh calls."]
        let bannerDisappeared = expectation(for: NSPredicate(format: "exists == false"), evaluatedWith: errorBanner, handler: nil)
        wait(for: [bannerDisappeared], timeout: 5.0)
        XCTAssertTrue(firstRow.exists, "Rows must still remain visible after retry")
    }

    // MARK: - 9. Settings Actions Disabled in Screenshot Fixtures

    func testSettingsSectionsAndActionsDisabledInScreenshotFixture() throws {
        let app = launchApp(scenario: "account-settings")

        // 1. Account Settings is already open on launch in this fixture
        let accountDone = app.buttons["settings.done"]
        XCTAssertTrue(accountDone.waitForExistence(timeout: 5), "Account sheet Done button must exist on launch")

        // Check Account & Plan View Plans button is disabled
        let viewPlans = app.buttons["settings.viewPlans"]
        let foundViewPlans = scrollToElement(viewPlans, in: app, direction: .down)
        XCTAssertTrue(foundViewPlans && viewPlans.exists, "View Plans button must exist")
        XCTAssertFalse(viewPlans.isEnabled, "View Plans button should be disabled in screenshot fixtures")

        // Check Delete Account button is disabled
        let deleteButton = app.buttons["Delete Account"]
        let foundDelete = scrollToElement(deleteButton, in: app, direction: .down)
        XCTAssertTrue(foundDelete && deleteButton.exists, "Delete Account button must exist")
        XCTAssertFalse(deleteButton.isEnabled, "Delete Account button should be disabled in screenshot fixtures")

        accountDone.tap()

        // 2. On Kevin tab, check Kevin sections
        let kevinTab = app.tabBars.buttons["Kevin"]
        XCTAssertTrue(kevinTab.waitForExistence(timeout: 5))
        kevinTab.tap()
        XCTAssertTrue(kevinTab.isSelected)

        let screenAllToggle = app.switches["kevin.screenAllCalls"]
        let foundScreenAll = scrollToElement(screenAllToggle, in: app, direction: .down)
        XCTAssertTrue(foundScreenAll && screenAllToggle.exists, "Screen all calls toggle must exist")
        XCTAssertFalse(screenAllToggle.isEnabled, "Screen all calls toggle must be disabled in screenshot fixtures")
    }
}
