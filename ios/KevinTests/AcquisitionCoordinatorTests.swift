import XCTest
@testable import Kevin

@MainActor
final class AcquisitionCoordinatorTests: XCTestCase {
    @MainActor final class Harness {
        var auth = CallAuthContext(contractorId: "cnt_123", bearerToken: "token_abc", generation: 1)
        var isEligible = true
        var eligibilityChecks: [String] = []
        var tokenRequests = 0
        var generatedToken = "mock-apple-ads-token"
        var posts: [(token: String, bearerToken: String)] = []
        var postResult = AttributionPostResult(status: .recorded, retryAfterSeconds: nil)
        var sleeps: [Double] = []

        lazy var coordinator = AcquisitionCoordinator(
            eligibilityChecker: { [unowned self] bearer in
                eligibilityChecks.append(bearer)
                return isEligible
            },
            tokenRequester: { [unowned self] in
                tokenRequests += 1
                return generatedToken
            },
            attributionPoster: { [unowned self] token, bearer in
                posts.append((token: token, bearerToken: bearer))
                return postResult
            },
            sleepHandler: { [unowned self] seconds in
                sleeps.append(seconds)
            },
            authProvider: { [unowned self] in auth }
        )
    }

    func testIneligibleBackendCausesZeroTokenRequestsAndZeroPosts() async {
        let h = Harness()
        h.isEligible = false

        await h.coordinator.evaluateAndRecord()

        XCTAssertEqual(h.eligibilityChecks.count, 1)
        XCTAssertEqual(h.eligibilityChecks.first, "token_abc")
        XCTAssertEqual(h.tokenRequests, 0)
        XCTAssertTrue(h.posts.isEmpty)
    }

    func testSuccessfulAttributionRecording() async {
        let h = Harness()
        h.isEligible = true
        h.postResult = AttributionPostResult(status: .recorded, retryAfterSeconds: nil)

        await h.coordinator.evaluateAndRecord()

        XCTAssertEqual(h.eligibilityChecks.count, 1)
        XCTAssertEqual(h.tokenRequests, 1)
        XCTAssertEqual(h.posts.count, 1)
        XCTAssertEqual(h.posts.first?.token, "mock-apple-ads-token")
        XCTAssertEqual(h.posts.first?.bearerToken, "token_abc")
        XCTAssertTrue(h.sleeps.isEmpty)
    }

    func testTerminalResponsesStopRetryingImmediately() async {
        let terminalStatuses: [AttributionStatus] = [
            .recorded, .alreadyRecorded, .unattributed, .ineligible, .disabled, .exhausted
        ]

        for status in terminalStatuses {
            let h = Harness()
            h.isEligible = true
            h.postResult = AttributionPostResult(status: status, retryAfterSeconds: nil)

            await h.coordinator.evaluateAndRecord()

            XCTAssertEqual(h.posts.count, 1, "Status \(status) should stop after 1 post")
            XCTAssertTrue(h.sleeps.isEmpty)
        }
    }

    func testRetryableStatusRetriesUpToThreeTimesWithFiveSecondSpacing() async {
        let h = Harness()
        h.isEligible = true
        h.postResult = AttributionPostResult(status: .retryable, retryAfterSeconds: 5)

        await h.coordinator.evaluateAndRecord()

        XCTAssertEqual(h.posts.count, 3)
        XCTAssertEqual(h.sleeps.count, 2)
        XCTAssertEqual(h.sleeps, [5.0, 5.0])
    }

    func testAuthRotationMidFlightAbortsWithoutPostingForNewAccount() async {
        let h = Harness()
        h.isEligible = true

        h.coordinator = AcquisitionCoordinator(
            eligibilityChecker: { [unowned h] bearer in
                h.eligibilityChecks.append(bearer)
                return true
            },
            tokenRequester: { [unowned h] in
                h.tokenRequests += 1
                // User logs out or switches accounts mid-flight
                h.auth = CallAuthContext(contractorId: "cnt_999", bearerToken: "token_xyz", generation: 2)
                return "mock-token"
            },
            attributionPoster: { [unowned h] token, bearer in
                h.posts.append((token: token, bearerToken: bearer))
                return AttributionPostResult(status: .recorded, retryAfterSeconds: nil)
            },
            sleepHandler: { [unowned h] sec in h.sleeps.append(sec) },
            authProvider: { [unowned h] in h.auth }
        )

        await h.coordinator.evaluateAndRecord()

        XCTAssertEqual(h.eligibilityChecks.count, 1)
        XCTAssertEqual(h.tokenRequests, 1)
        XCTAssertTrue(h.posts.isEmpty, "Should abort after auth rotation before posting")
    }

    func testHeldAwaitCancellationAtEligibility() async {
        let enteredEligibility = expectation(description: "enteredEligibility")
        let releaseEligibility = expectation(description: "releaseEligibility")

        let h = Harness()
        h.coordinator = AcquisitionCoordinator(
            eligibilityChecker: { _ in
                enteredEligibility.fulfill()
                await self.fulfillment(of: [releaseEligibility], timeout: 2.0)
                return true
            },
            tokenRequester: {
                h.tokenRequests += 1
                return "token"
            },
            attributionPoster: { token, bearer in
                h.posts.append((token: token, bearerToken: bearer))
                return AttributionPostResult(status: .recorded, retryAfterSeconds: nil)
            },
            sleepHandler: { _ in },
            authProvider: { h.auth }
        )

        let task = Task { @MainActor in
            await h.coordinator.evaluateAndRecord()
        }

        await fulfillment(of: [enteredEligibility], timeout: 2.0)
        task.cancel()
        releaseEligibility.fulfill()
        _ = await task.value

        XCTAssertEqual(h.tokenRequests, 0, "Cancelled flight at eligibility must not request token")
        XCTAssertTrue(h.posts.isEmpty)
    }

    func testHeldAwaitCancellationAtPOST() async {
        let enteredPOST = expectation(description: "enteredPOST")
        let releasePOST = expectation(description: "releasePOST")

        let h = Harness()
        h.coordinator = AcquisitionCoordinator(
            eligibilityChecker: { _ in true },
            tokenRequester: { "mock-token" },
            attributionPoster: { token, bearer in
                enteredPOST.fulfill()
                await self.fulfillment(of: [releasePOST], timeout: 2.0)
                return AttributionPostResult(status: .retryable, retryAfterSeconds: 5)
            },
            sleepHandler: { sec in
                h.sleeps.append(sec)
            },
            authProvider: { h.auth }
        )

        let task = Task { @MainActor in
            await h.coordinator.evaluateAndRecord()
        }

        await fulfillment(of: [enteredPOST], timeout: 2.0)
        task.cancel()
        releasePOST.fulfill()
        _ = await task.value

        XCTAssertTrue(h.sleeps.isEmpty, "Cancelled flight during POST must not sleep or retry")
    }

    func testHeldAwaitCancellationDuringTokenGeneration() async {
        let enteredToken = expectation(description: "enteredToken")
        let releaseToken = expectation(description: "releaseToken")

        let h = Harness()
        h.coordinator = AcquisitionCoordinator(
            eligibilityChecker: { _ in true },
            tokenRequester: {
                enteredToken.fulfill()
                await self.fulfillment(of: [releaseToken], timeout: 2.0)
                return "token-after-wait"
            },
            attributionPoster: { token, bearer in
                h.posts.append((token: token, bearerToken: bearer))
                return AttributionPostResult(status: .recorded, retryAfterSeconds: nil)
            },
            sleepHandler: { _ in },
            authProvider: { h.auth }
        )

        let task = Task { @MainActor in
            await h.coordinator.evaluateAndRecord()
        }

        await fulfillment(of: [enteredToken], timeout: 2.0)
        task.cancel()
        releaseToken.fulfill()
        _ = await task.value

        XCTAssertTrue(h.posts.isEmpty, "Cancelled flight during token generation must not post attribution")
    }

    func testHeldAwaitCancellationDuringRetrySleep() async {
        let enteredSleep = expectation(description: "enteredSleep")
        let releaseSleep = expectation(description: "releaseSleep")

        let h = Harness()
        h.coordinator = AcquisitionCoordinator(
            eligibilityChecker: { _ in true },
            tokenRequester: { "mock-token" },
            attributionPoster: { token, bearer in
                h.posts.append((token: token, bearerToken: bearer))
                return AttributionPostResult(status: .retryable, retryAfterSeconds: 5)
            },
            sleepHandler: { sec in
                enteredSleep.fulfill()
                await self.fulfillment(of: [releaseSleep], timeout: 2.0)
            },
            authProvider: { h.auth }
        )

        let task = Task { @MainActor in
            await h.coordinator.evaluateAndRecord()
        }

        await fulfillment(of: [enteredSleep], timeout: 2.0)
        task.cancel()
        releaseSleep.fulfill()
        _ = await task.value

        XCTAssertEqual(h.posts.count, 1, "Cancelled flight during retry sleep must not execute subsequent attempts")
    }

    func testHeldAwaitAccountReplacementStartsReplacementAndStopsStaleWork() async {
        let enteredEligibilityA = expectation(description: "enteredEligibilityA")
        let releaseEligibilityA = expectation(description: "releaseEligibilityA")
        let enteredEligibilityB = expectation(description: "enteredEligibilityB")

        let h = Harness()
        let authA = CallAuthContext(contractorId: "cnt_A", bearerToken: "token_A", generation: 1)
        let authB = CallAuthContext(contractorId: "cnt_B", bearerToken: "token_B", generation: 1)
        h.auth = authA

        h.coordinator = AcquisitionCoordinator(
            eligibilityChecker: { bearer in
                if bearer == "token_A" {
                    enteredEligibilityA.fulfill()
                    await self.fulfillment(of: [releaseEligibilityA], timeout: 2.0)
                    return true
                } else if bearer == "token_B" {
                    enteredEligibilityB.fulfill()
                    return true
                }
                return false
            },
            tokenRequester: {
                h.tokenRequests += 1
                return "token_for_\(h.auth.contractorId)"
            },
            attributionPoster: { token, bearer in
                h.posts.append((token: token, bearerToken: bearer))
                return AttributionPostResult(status: .recorded, retryAfterSeconds: nil)
            },
            sleepHandler: { _ in },
            authProvider: { h.auth }
        )

        let taskA = Task { @MainActor in
            await h.coordinator.evaluateAndRecord()
        }

        await fulfillment(of: [enteredEligibilityA], timeout: 2.0)

        // Switch to Account B while Flight A is held
        h.auth = authB
        let taskB = Task { @MainActor in
            await h.coordinator.evaluateAndRecord()
        }

        await fulfillment(of: [enteredEligibilityB], timeout: 2.0)

        releaseEligibilityA.fulfill()

        _ = await taskA.value
        _ = await taskB.value

        XCTAssertEqual(h.posts.count, 1)
        XCTAssertEqual(h.posts.first?.bearerToken, "token_B")
        XCTAssertEqual(h.posts.first?.token, "token_for_cnt_B")
    }

    func testHeldAwaitSignoutCancelsActiveFlightAndStopsStaleWork() async {
        let enteredEligibility = expectation(description: "enteredEligibility")
        let releaseEligibility = expectation(description: "releaseEligibility")

        let h = Harness()
        h.auth = CallAuthContext(contractorId: "cnt_active", bearerToken: "token_active", generation: 1)

        h.coordinator = AcquisitionCoordinator(
            eligibilityChecker: { bearer in
                enteredEligibility.fulfill()
                await self.fulfillment(of: [releaseEligibility], timeout: 2.0)
                return true
            },
            tokenRequester: {
                h.tokenRequests += 1
                return "mock-token"
            },
            attributionPoster: { token, bearer in
                h.posts.append((token: token, bearerToken: bearer))
                return AttributionPostResult(status: .recorded, retryAfterSeconds: nil)
            },
            sleepHandler: { _ in },
            authProvider: { h.auth }
        )

        let task = Task { @MainActor in
            await h.coordinator.evaluateAndRecord()
        }

        await fulfillment(of: [enteredEligibility], timeout: 2.0)

        // Sign out
        h.auth = CallAuthContext(contractorId: "", bearerToken: "", generation: 0)
        await h.coordinator.handleAuthChange()

        releaseEligibility.fulfill()
        _ = await task.value

        XCTAssertEqual(h.tokenRequests, 0)
        XCTAssertTrue(h.posts.isEmpty)
    }

    func testHeldAwaitSameIdTokenRotationStartsReplacementFlight() async {
        let enteredEligibilityGen1 = expectation(description: "enteredEligibilityGen1")
        let releaseEligibilityGen1 = expectation(description: "releaseEligibilityGen1")
        let enteredEligibilityGen2 = expectation(description: "enteredEligibilityGen2")

        let h = Harness()
        // ID ("cnt_123") and bearer ("token_abc") remain identical; ONLY generation changes
        let authGen1 = CallAuthContext(contractorId: "cnt_123", bearerToken: "token_abc", generation: 1)
        let authGen2 = CallAuthContext(contractorId: "cnt_123", bearerToken: "token_abc", generation: 2)
        h.auth = authGen1

        h.coordinator = AcquisitionCoordinator(
            eligibilityChecker: { bearer in
                if h.auth.generation == 1 {
                    enteredEligibilityGen1.fulfill()
                    await self.fulfillment(of: [releaseEligibilityGen1], timeout: 2.0)
                    return true
                } else if h.auth.generation == 2 {
                    enteredEligibilityGen2.fulfill()
                    return true
                }
                return false
            },
            tokenRequester: {
                h.tokenRequests += 1
                return "token_gen_\(h.auth.generation)"
            },
            attributionPoster: { token, bearer in
                h.posts.append((token: token, bearerToken: bearer))
                return AttributionPostResult(status: .recorded, retryAfterSeconds: nil)
            },
            sleepHandler: { _ in },
            authProvider: { h.auth }
        )

        let task1 = Task { @MainActor in
            await h.coordinator.evaluateAndRecord()
        }

        await fulfillment(of: [enteredEligibilityGen1], timeout: 2.0)

        // Rotate generation while ID and bearer remain identical
        h.auth = authGen2
        let task2 = Task { @MainActor in
            await h.coordinator.evaluateAndRecord()
        }

        await fulfillment(of: [enteredEligibilityGen2], timeout: 2.0)

        releaseEligibilityGen1.fulfill()
        _ = await task1.value
        _ = await task2.value

        XCTAssertEqual(h.posts.count, 1)
        XCTAssertEqual(h.posts.first?.bearerToken, "token_abc")
        XCTAssertEqual(h.posts.first?.token, "token_gen_2")
    }

    func testHeldFlightRegressionOldCompletionDoesNotCancelReplacementFlightAndConcurrentStartSharesFlight() async {
        let enteredOpA = expectation(description: "enteredOpA")
        let releaseOpA = expectation(description: "releaseOpA")
        let enteredOpB = expectation(description: "enteredOpB")
        let releaseOpB = expectation(description: "releaseOpB")

        let h = Harness()
        let authA = CallAuthContext(contractorId: "cnt_A", bearerToken: "token_A", generation: 1)
        let authB = CallAuthContext(contractorId: "cnt_B", bearerToken: "token_B", generation: 1)
        h.auth = authA

        h.coordinator = AcquisitionCoordinator(
            eligibilityChecker: { bearer in
                if bearer == "token_A" {
                    enteredOpA.fulfill()
                    await self.fulfillment(of: [releaseOpA], timeout: 2.0)
                    return true
                } else if bearer == "token_B" {
                    enteredOpB.fulfill()
                    await self.fulfillment(of: [releaseOpB], timeout: 2.0)
                    return true
                }
                return false
            },
            tokenRequester: {
                h.tokenRequests += 1
                return "token_for_\(h.auth.contractorId)"
            },
            attributionPoster: { token, bearer in
                h.posts.append((token: token, bearerToken: bearer))
                return AttributionPostResult(status: .recorded, retryAfterSeconds: nil)
            },
            sleepHandler: { _ in },
            authProvider: { h.auth }
        )

        // 1. Start old flight A and hold it inside its operation
        let taskA = Task { @MainActor in
            await h.coordinator.evaluateAndRecord()
        }
        await fulfillment(of: [enteredOpA], timeout: 2.0)

        // 2. Replace account and start replacement flight B, held in its own operation
        h.auth = authB
        let taskB1 = Task { @MainActor in
            await h.coordinator.evaluateAndRecord()
        }
        await fulfillment(of: [enteredOpB], timeout: 2.0)

        // 3. Release and join old flight A while replacement flight B stays pending (held on releaseOpB)
        releaseOpA.fulfill()
        _ = await taskA.value

        // 4. Call start again for replacement account B while flight B is still pending
        let enteredTaskB2 = expectation(description: "enteredTaskB2")
        let taskB2 = Task { @MainActor in
            enteredTaskB2.fulfill()
            await h.coordinator.evaluateAndRecord()
        }
        await fulfillment(of: [enteredTaskB2], timeout: 2.0)

        // 5. Release replacement flight B gate
        releaseOpB.fulfill()

        // 6. Join remaining tasks
        _ = await taskB1.value
        _ = await taskB2.value

        // Assert flight B executed only once (shared existing flight instead of issuing a second operation)
        XCTAssertEqual(h.tokenRequests, 1)
        XCTAssertEqual(h.posts.count, 1)
        XCTAssertEqual(h.posts.first?.bearerToken, "token_B")
        XCTAssertEqual(h.posts.first?.token, "token_for_cnt_B")
    }

    func testSameContextConcurrentStartSharesSingleFlight() async {
        let enteredToken = expectation(description: "enteredToken")
        let releaseToken = expectation(description: "releaseToken")

        let h = Harness()
        h.coordinator = AcquisitionCoordinator(
            eligibilityChecker: { _ in true },
            tokenRequester: {
                enteredToken.fulfill()
                await self.fulfillment(of: [releaseToken], timeout: 2.0)
                h.tokenRequests += 1
                return "mock-token"
            },
            attributionPoster: { token, bearer in
                h.posts.append((token: token, bearerToken: bearer))
                return AttributionPostResult(status: .recorded, retryAfterSeconds: nil)
            },
            sleepHandler: { _ in },
            authProvider: { h.auth }
        )

        let task1 = Task { @MainActor in
            await h.coordinator.evaluateAndRecord()
        }

        await fulfillment(of: [enteredToken], timeout: 2.0)

        // Concurrent start with same auth context
        let enteredTask2 = expectation(description: "enteredTask2")
        let task2 = Task { @MainActor in
            enteredTask2.fulfill()
            await h.coordinator.evaluateAndRecord()
        }
        await fulfillment(of: [enteredTask2], timeout: 2.0)

        releaseToken.fulfill()
        _ = await task1.value
        _ = await task2.value

        XCTAssertEqual(h.tokenRequests, 1, "Concurrent starts with same auth must share single flight")
        XCTAssertEqual(h.posts.count, 1)
    }

    func testAttributionResponseParserStrictHandling() {
        let url = URL(string: "https://example.com/api/acquisition/apple-ads")!
        let resp200 = HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil)!

        // 1. 200 OK with recorded status (no delay)
        let jsonRecorded = """
        {"status": "recorded"}
        """.data(using: .utf8)!
        let resRecorded = AttributionResponseParser.parse(data: jsonRecorded, response: resp200)
        XCTAssertEqual(resRecorded.status, .recorded)
        XCTAssertNil(resRecorded.retryAfterSeconds)

        // 2. 200 OK with retryable and valid delay 10
        let jsonRetryable = """
        {"status": "retryable", "retry_after_seconds": 10}
        """.data(using: .utf8)!
        let resRetryable = AttributionResponseParser.parse(data: jsonRetryable, response: resp200)
        XCTAssertEqual(resRetryable.status, .retryable)
        XCTAssertEqual(resRetryable.retryAfterSeconds, 10)

        // 3. 200 OK with terminal status containing delay -> rejected to retryable(5)
        let jsonTerminalWithDelay = """
        {"status": "recorded", "retry_after_seconds": 10}
        """.data(using: .utf8)!
        let resTermDelay = AttributionResponseParser.parse(data: jsonTerminalWithDelay, response: resp200)
        XCTAssertEqual(resTermDelay.status, .retryable)
        XCTAssertEqual(resTermDelay.retryAfterSeconds, 5)

        // 4. 200 OK with delay out of range (> 15) -> rejected to retryable(5) (no clamping!)
        let jsonOutOfRange = """
        {"status": "retryable", "retry_after_seconds": 60}
        """.data(using: .utf8)!
        let resOutOfRange = AttributionResponseParser.parse(data: jsonOutOfRange, response: resp200)
        XCTAssertEqual(resOutOfRange.status, .retryable)
        XCTAssertEqual(resOutOfRange.retryAfterSeconds, 5)

        // 5. 200 OK with fraction delay -> rejected to retryable(5)
        let jsonFraction = """
        {"status": "retryable", "retry_after_seconds": 7.5}
        """.data(using: .utf8)!
        let resFraction = AttributionResponseParser.parse(data: jsonFraction, response: resp200)
        XCTAssertEqual(resFraction.status, .retryable)
        XCTAssertEqual(resFraction.retryAfterSeconds, 5)

        // 6. 200 OK with extra unknown keys -> rejected to retryable(5)
        let jsonExtra = """
        {"status": "recorded", "extra_field": "disallowed"}
        """.data(using: .utf8)!
        let resExtra = AttributionResponseParser.parse(data: jsonExtra, response: resp200)
        XCTAssertEqual(resExtra.status, .retryable)
        XCTAssertEqual(resExtra.retryAfterSeconds, 5)

        // 7. 200 OK with "unknown" status -> rejected to retryable(5)
        let jsonUnknownStatus = """
        {"status": "unknown"}
        """.data(using: .utf8)!
        let resUnknown = AttributionResponseParser.parse(data: jsonUnknownStatus, response: resp200)
        XCTAssertEqual(resUnknown.status, .retryable)
        XCTAssertEqual(resUnknown.retryAfterSeconds, 5)

        // 8. 401 Unauthorized -> ineligible
        let resp401 = HTTPURLResponse(url: url, statusCode: 401, httpVersion: nil, headerFields: nil)!
        let res401 = AttributionResponseParser.parse(data: Data(), response: resp401)
        XCTAssertEqual(res401.status, .ineligible)
        XCTAssertNil(res401.retryAfterSeconds)

        // 9. 403 Forbidden -> ineligible
        let resp403 = HTTPURLResponse(url: url, statusCode: 403, httpVersion: nil, headerFields: nil)!
        let res403 = AttributionResponseParser.parse(data: Data(), response: resp403)
        XCTAssertEqual(res403.status, .ineligible)
        XCTAssertNil(res403.retryAfterSeconds)

        // 10. 500 Server Error -> retryable(5)
        let resp500 = HTTPURLResponse(url: url, statusCode: 500, httpVersion: nil, headerFields: nil)!
        let res500 = AttributionResponseParser.parse(data: Data(), response: resp500)
        XCTAssertEqual(res500.status, .retryable)
        XCTAssertEqual(res500.retryAfterSeconds, 5)
    }
}
