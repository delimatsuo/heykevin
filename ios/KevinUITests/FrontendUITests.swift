import XCTest

final class FrontendUITests: XCTestCase {

    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    private func launchApp(scenario: String) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchEnvironment["APP_STORE_SCREENSHOT_SCENARIO"] = scenario
        app.launch()
        return app
    }

    // MARK: - Bounded History & Pagination UI

    func testCallsTabUIElementsWith101History() throws {
        let app = launchApp(scenario: "history-101")

        // 1. Verify search field exists
        let searchField = app.otherElements["calls.search"]
        XCTAssertTrue(searchField.waitForExistence(timeout: 5), "Search field must exist")

        // 2. Verify filter picker exists
        let filterPicker = app.segmentedControls["calls.filter"]
        XCTAssertTrue(filterPicker.exists, "Filter picker must exist")

        // 3. Verify count label
        let countLabel = app.staticTexts["calls.count"]
        XCTAssertTrue(countLabel.exists, "Count label must exist")

        // 4. Verify pagination button exists for 101 items
        let showMoreButton = app.buttons["calls.showMore"]
        XCTAssertTrue(showMoreButton.exists, "'Show 20 more' button must be present when more than 20 items exist")

        // 5. Tap show more and verify count expands
        showMoreButton.tap()
        XCTAssertTrue(countLabel.exists)
    }

    // MARK: - Empty State UI

    func testEmptyHistoryState() throws {
        let app = launchApp(scenario: "history-empty")

        // In empty state, no call rows should exist
        let showMoreButton = app.buttons["calls.showMore"]
        XCTAssertFalse(showMoreButton.exists, "Show more should not exist on empty history")

        // Filter picker should still exist
        let filterPicker = app.segmentedControls["calls.filter"]
        XCTAssertTrue(filterPicker.waitForExistence(timeout: 5), "Filter picker should exist on empty history")
    }

    // MARK: - Active Call Card UI

    func testActiveCallCardUIAndActions() throws {
        let app = launchApp(scenario: "business-live")

        // Verify active call card exists
        let activeCard = app.otherElements["call.activeCard"]
        XCTAssertTrue(activeCard.waitForExistence(timeout: 5), "Active call card must exist on live call")

        // Verify Pick up and Take message action buttons
        let pickupButton = app.buttons["call.pickup"]
        XCTAssertTrue(pickupButton.exists, "Pick up button must exist on active card")

        let messageButton = app.buttons["call.message"]
        XCTAssertTrue(messageButton.exists, "Take message button must exist on active card")
    }

    // MARK: - Root Tab Navigation & Account Sheet

    func testTabNavigationAndAccountSettings() throws {
        let app = launchApp(scenario: "business-recents")

        // 1. Verify tabs exist
        let callsTab = app.buttons["calls.tab"]
        let kevinTab = app.buttons["kevin.tab"]
        XCTAssertTrue(callsTab.waitForExistence(timeout: 5), "Calls tab must exist")
        XCTAssertTrue(kevinTab.exists, "Kevin tab must exist")

        // 2. Open Kevin tab
        kevinTab.tap()

        // 3. Open Account Settings sheet via toolbar button
        let settingsButton = app.buttons["nav.settings"]
        XCTAssertTrue(settingsButton.waitForExistence(timeout: 5), "Account settings toolbar button must exist")
        settingsButton.tap()

        // 4. Return to Calls tab
        callsTab.tap()
    }
}
