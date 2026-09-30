import Foundation
import XCTest
@testable import Kevin

@MainActor
final class SubscriptionVerificationTests: XCTestCase {
    func testLegacySuccessResponseRemainsActive() throws {
        let response = try httpResponse(status: 200)
        let data = try JSONSerialization.data(withJSONObject: [
            "status": "ok",
            "message": "already_processed",
        ])

        XCTAssertEqual(
            SubscriptionVerificationResponseParser.parse(data: data, response: response),
            .active
        )
    }

    func testInactiveAcknowledgementIsNotActive() throws {
        let response = try httpResponse(status: 200)
        let data = try JSONSerialization.data(withJSONObject: [
            "status": "ok",
            "message": "terminal_processed",
            "outcome": "inactive",
            "entitlement_active": false,
        ])

        XCTAssertEqual(
            SubscriptionVerificationResponseParser.parse(data: data, response: response),
            .inactive
        )
    }

    func testExplicitActiveEnvelopeIsActive() throws {
        let response = try httpResponse(status: 200)
        let data = try JSONSerialization.data(withJSONObject: [
            "status": "ok",
            "message": "updated",
            "outcome": "active",
            "entitlement_active": true,
        ])

        XCTAssertEqual(
            SubscriptionVerificationResponseParser.parse(data: data, response: response),
            .active
        )
    }

    func testBothVerifiedLegacySuccessMessagesRemainActive() throws {
        let response = try httpResponse(status: 200)
        for message in ["updated", "already_processed"] {
            let data = try JSONSerialization.data(withJSONObject: [
                "status": "ok",
                "message": message,
            ])
            XCTAssertEqual(
                SubscriptionVerificationResponseParser.parse(data: data, response: response),
                .active
            )
        }
    }

    func testUnverifiedOrInconsistentSuccessEnvelopeIsRejected() throws {
        let response = try httpResponse(status: 200)
        let payloads: [[String: Any]] = [
            ["status": "ok", "message": "verification_skipped"],
            ["status": "ok", "message": "future_success"],
            ["status": "ok"],
            [
                "status": "ok",
                "outcome": "active",
                "entitlement_active": false,
            ],
            [
                "status": "ok",
                "outcome": "inactive",
                "entitlement_active": true,
            ],
        ]

        for payload in payloads {
            let data = try JSONSerialization.data(withJSONObject: payload)
            XCTAssertEqual(
                SubscriptionVerificationResponseParser.parse(data: data, response: response),
                .rejected(reason: "invalid_success_response")
            )
        }
    }

    func testLegacyUpdateFailedRemainsRetryable() throws {
        let response = try httpResponse(
            status: 200,
            headers: ["Retry-After": "45"]
        )
        let data = try JSONSerialization.data(withJSONObject: [
            "status": "error",
            "message": "update_failed",
        ])

        XCTAssertEqual(
            SubscriptionVerificationResponseParser.parse(data: data, response: response),
            .retryable(after: 45)
        )
    }

    func testRetryableResponseHonorsRetryAfterHeader() throws {
        let response = try httpResponse(
            status: 429,
            headers: ["Retry-After": "42"]
        )
        let data = try JSONSerialization.data(withJSONObject: [
            "status": "retryable",
            "retry_after_seconds": 5,
        ])

        XCTAssertEqual(
            SubscriptionVerificationResponseParser.parse(data: data, response: response),
            .retryable(after: 42)
        )
    }

    func testOwnershipFailureIsRejected() throws {
        let response = try httpResponse(status: 409)
        let data = try JSONSerialization.data(withJSONObject: [
            "detail": "app_account_token_mismatch",
        ])

        XCTAssertEqual(
            SubscriptionVerificationResponseParser.parse(data: data, response: response),
            .rejected(reason: "app_account_token_mismatch")
        )
    }

    func testOnlyServerAcknowledgedOutcomesCanFinishTransactions() {
        XCTAssertTrue(SubscriptionServerResolution.active.shouldFinishTransaction)
        XCTAssertTrue(SubscriptionServerResolution.inactive.shouldFinishTransaction)
        XCTAssertFalse(
            SubscriptionServerResolution.retryable(after: 30).shouldFinishTransaction
        )
        XCTAssertFalse(
            SubscriptionServerResolution.rejected(reason: "ownership_mismatch")
                .shouldFinishTransaction
        )
    }

    func testConcurrentRequestsAreSingleFlight() async {
        let coordinator = SubscriptionVerificationCoordinator(cacheLifetime: 30)
        let counter = CallCounter()

        async let first = coordinator.result(for: "c1:tx1") {
            await counter.increment()
            try? await Task.sleep(nanoseconds: 50_000_000)
            return .active
        }
        async let second = coordinator.result(for: "c1:tx1") {
            await counter.increment()
            try? await Task.sleep(nanoseconds: 50_000_000)
            return .active
        }

        let firstOutcome = await first
        let secondOutcome = await second
        XCTAssertEqual([firstOutcome, secondOutcome], [.active, .active])
        let requestCount = await counter.value
        XCTAssertEqual(requestCount, 1)
    }

    func testRetryableResponseGatesSequentialCallsUntilDeadline() async {
        let coordinator = SubscriptionVerificationCoordinator(cacheLifetime: 30)
        let counter = CallCounter()

        _ = await coordinator.result(for: "c1:tx1") {
            await counter.increment()
            return .retryable(after: 0.08)
        }
        let gated = await coordinator.result(for: "c1:tx1") {
            await counter.increment()
            return .active
        }

        if case .retryable(let remaining) = gated {
            XCTAssertGreaterThan(remaining, 0)
            XCTAssertLessThanOrEqual(remaining, 0.08)
        } else {
            XCTFail("sequential caller should observe the retry gate")
        }
        let gatedRequestCount = await counter.value
        XCTAssertEqual(gatedRequestCount, 1)

        try? await Task.sleep(for: .milliseconds(100))
        let afterDeadline = await coordinator.result(for: "c1:tx1") {
            await counter.increment()
            return .active
        }
        XCTAssertEqual(afterDeadline, .active)
        let postDeadlineRequestCount = await counter.value
        XCTAssertEqual(postDeadlineRequestCount, 2)
    }

    func testDueRetryCallersRemainSingleFlight() async {
        let coordinator = SubscriptionVerificationCoordinator(cacheLifetime: 30)
        let counter = CallCounter()

        _ = await coordinator.result(for: "c1:tx1") {
            await counter.increment()
            return .retryable(after: 0.03)
        }
        try? await Task.sleep(for: .milliseconds(50))

        async let first = coordinator.result(for: "c1:tx1") {
            await counter.increment()
            try? await Task.sleep(for: .milliseconds(30))
            return .active
        }
        async let second = coordinator.result(for: "c1:tx1") {
            await counter.increment()
            return .active
        }

        let firstOutcome = await first
        let secondOutcome = await second
        let requestCount = await counter.value
        XCTAssertEqual(firstOutcome, .active)
        XCTAssertEqual(secondOutcome, .active)
        XCTAssertEqual(requestCount, 2)
    }

    func testRetryRunnerSurvivesTwoRetryableResponsesThenStopsOnActive() async {
        var delays: [TimeInterval] = []
        var outcomes: [SubscriptionServerResolution] = [
            .retryable(after: 1),
            .retryable(after: 1),
            .active,
        ]

        let terminal = await SubscriptionVerificationRetryRunner.run(
            initialDelay: 1,
            maximumAttempts: 3,
            jitter: { 0 },
            sleep: { delays.append($0) },
            operation: { outcomes.removeFirst() }
        )

        XCTAssertEqual(terminal, .active)
        XCTAssertEqual(delays, [30, 60, 120])
        XCTAssertTrue(outcomes.isEmpty)
    }

    func testRetryRunnerIsBoundedAndRejectedDoesNotRetry() async {
        let counter = MutableCounter()
        let exhausted = await SubscriptionVerificationRetryRunner.run(
            initialDelay: 1,
            maximumAttempts: 3,
            jitter: { 0 },
            sleep: { _ in },
            operation: {
                counter.value += 1
                return .retryable(after: 1)
            }
        )
        XCTAssertNil(exhausted)
        XCTAssertEqual(counter.value, 3)

        counter.value = 0
        let rejected = await SubscriptionVerificationRetryRunner.run(
            initialDelay: 1,
            maximumAttempts: 3,
            jitter: { 0 },
            sleep: { _ in },
            operation: {
                counter.value += 1
                return .rejected(reason: "ownership_mismatch")
            }
        )
        XCTAssertEqual(rejected, .rejected(reason: "ownership_mismatch"))
        XCTAssertEqual(counter.value, 1)
        XCTAssertFalse(rejected?.shouldFinishTransaction ?? true)
    }

    func testRetryPolicyNeverShortensLongServerRetryAfter() {
        XCTAssertEqual(
            SubscriptionVerificationRetryPolicy.delay(
                requested: 3_600,
                attempt: 2,
                jitterUnit: 0
            ),
            3_600
        )
        XCTAssertGreaterThanOrEqual(
            SubscriptionVerificationRetryPolicy.delay(
                requested: 3_600,
                attempt: 2,
                jitterUnit: 1
            ),
            3_600
        )
    }

    func testVerificationContextRejectsAccountOrTokenSwitch() {
        let context = SubscriptionVerificationContext(
            contractorID: "c1",
            bearerToken: "token-1"
        )

        XCTAssertTrue(context.matches(contractorID: "c1", bearerToken: "token-1", generation: 0))
        XCTAssertFalse(context.matches(contractorID: "c2", bearerToken: "token-1", generation: 0))
        XCTAssertFalse(context.matches(contractorID: "c1", bearerToken: "token-2", generation: 0))
    }

    func testVerificationContextIncludesGenerationInMatchesAndCacheNamespace() {
        let c1 = SubscriptionVerificationContext(
            contractorID: "c1",
            bearerToken: "token-1",
            generation: 1
        )
        let c2 = SubscriptionVerificationContext(
            contractorID: "c1",
            bearerToken: "token-1",
            generation: 2
        )

        XCTAssertEqual(c1.cacheNamespace, "c1:1")
        XCTAssertEqual(c2.cacheNamespace, "c1:2")
        XCTAssertTrue(c1.matches(contractorID: "c1", bearerToken: "token-1", generation: 1))
        XCTAssertFalse(c1.matches(contractorID: "c1", bearerToken: "token-1", generation: 2))
        XCTAssertNotEqual(c1, c2)
    }

    func testRestoreOperation_normalSyncVerifyOrder_succeeds() async throws {
        var callOrder: [String] = []
        let context = SubscriptionVerificationContext(
            contractorID: "c1",
            bearerToken: "token-1",
            generation: 1
        )

        let result = try await SubscriptionRestoreOperation.performRestore(
            context: context,
            isCurrent: { true },
            sync: {
                callOrder.append("sync")
            },
            verify: { passedContext in
                XCTAssertEqual(passedContext, context)
                callOrder.append("verify")
                return true
            }
        )

        XCTAssertTrue(result)
        XCTAssertEqual(callOrder, ["sync", "verify"])
    }

    func testRestoreOperation_syncThrows_throwsError() async {
        let context = SubscriptionVerificationContext(
            contractorID: "c1",
            bearerToken: "token-1",
            generation: 1
        )

        do {
            _ = try await SubscriptionRestoreOperation.performRestore(
                context: context,
                isCurrent: { true },
                sync: {
                    throw URLError(.notConnectedToInternet)
                },
                verify: { _ in
                    XCTFail("Verify should not be called on sync error")
                    return true
                }
            )
            XCTFail("Should have thrown error")
        } catch {
            XCTAssertTrue(error is URLError)
        }
    }

    func testRestoreOperation_staleContextBeforeStart_abortsWithoutSyncOrVerify() async throws {
        var syncCalled = false
        var verifyCalled = false
        let context = SubscriptionVerificationContext(
            contractorID: "c1",
            bearerToken: "token-1",
            generation: 1
        )

        let result = try await SubscriptionRestoreOperation.performRestore(
            context: context,
            isCurrent: { false },
            sync: { syncCalled = true },
            verify: { _ in
                verifyCalled = true
                return true
            }
        )

        XCTAssertFalse(result)
        XCTAssertFalse(syncCalled)
        XCTAssertFalse(verifyCalled)
    }

    func testRestoreOperation_staleContextAfterSync_abortsWithoutVerify() async throws {
        var currentGeneration = 1
        var verifyCalled = false
        let context = SubscriptionVerificationContext(
            contractorID: "c1",
            bearerToken: "token-1",
            generation: 1
        )

        let result = try await SubscriptionRestoreOperation.performRestore(
            context: context,
            isCurrent: { currentGeneration == 1 },
            sync: {
                // Generation changes during sync suspension (different session)
                currentGeneration = 2
            },
            verify: { _ in
                verifyCalled = true
                return true
            }
        )

        XCTAssertFalse(result)
        XCTAssertFalse(verifyCalled)
    }

    func testRestoreOperation_staleContextAfterVerify_abortsWithoutSuccess() async throws {
        var currentGeneration = 1
        let context = SubscriptionVerificationContext(
            contractorID: "c1",
            bearerToken: "token-1",
            generation: 1
        )

        let result = try await SubscriptionRestoreOperation.performRestore(
            context: context,
            isCurrent: { currentGeneration == 1 },
            sync: {},
            verify: { _ in
                // Generation changes during verify suspension
                currentGeneration = 2
                return true
            }
        )

        XCTAssertFalse(result)
    }

    // MARK: - SubscriptionVerificationResponseEffect Tests

    func testResponseEffect_current401_triggersReauth() {
        let context = SubscriptionVerificationContext(
            contractorID: "c1",
            bearerToken: "token-1",
            generation: 1
        )
        let currentAuth = CallAuthContext(
            contractorId: "c1",
            bearerToken: "token-1",
            generation: 1
        )
        var reauthCalled = false

        let applied = SubscriptionVerificationResponseEffect.apply(
            statusCode: 401,
            capturedContext: context,
            currentAuth: currentAuth,
            markNeedsReauth: { reauthCalled = true }
        )

        XCTAssertTrue(applied)
        XCTAssertTrue(reauthCalled)
    }

    func testResponseEffect_changedContractorId401_noEffect() {
        let context = SubscriptionVerificationContext(
            contractorID: "c1",
            bearerToken: "token-1",
            generation: 1
        )
        let currentAuth = CallAuthContext(
            contractorId: "c2",
            bearerToken: "token-1",
            generation: 1
        )
        var reauthCalled = false

        let applied = SubscriptionVerificationResponseEffect.apply(
            statusCode: 401,
            capturedContext: context,
            currentAuth: currentAuth,
            markNeedsReauth: { reauthCalled = true }
        )

        XCTAssertFalse(applied)
        XCTAssertFalse(reauthCalled)
    }

    func testResponseEffect_changedBearerToken401_noEffect() {
        let context = SubscriptionVerificationContext(
            contractorID: "c1",
            bearerToken: "token-1",
            generation: 1
        )
        let currentAuth = CallAuthContext(
            contractorId: "c1",
            bearerToken: "token-2",
            generation: 1
        )
        var reauthCalled = false

        let applied = SubscriptionVerificationResponseEffect.apply(
            statusCode: 401,
            capturedContext: context,
            currentAuth: currentAuth,
            markNeedsReauth: { reauthCalled = true }
        )

        XCTAssertFalse(applied)
        XCTAssertFalse(reauthCalled)
    }

    func testResponseEffect_sameIdAndTokenChangedGeneration401_noEffect() {
        let context = SubscriptionVerificationContext(
            contractorID: "c1",
            bearerToken: "token-1",
            generation: 1
        )
        let currentAuth = CallAuthContext(
            contractorId: "c1",
            bearerToken: "token-1",
            generation: 2
        )
        var reauthCalled = false

        let applied = SubscriptionVerificationResponseEffect.apply(
            statusCode: 401,
            capturedContext: context,
            currentAuth: currentAuth,
            markNeedsReauth: { reauthCalled = true }
        )

        XCTAssertFalse(applied)
        XCTAssertFalse(reauthCalled)
    }

    func testResponseEffect_non401StatusCode_noEffect() {
        let context = SubscriptionVerificationContext(
            contractorID: "c1",
            bearerToken: "token-1",
            generation: 1
        )
        let currentAuth = CallAuthContext(
            contractorId: "c1",
            bearerToken: "token-1",
            generation: 1
        )
        for non401 in [200, 204, 400, 403, 404, 409, 429, 500, 503] {
            var reauthCalled = false
            let applied = SubscriptionVerificationResponseEffect.apply(
                statusCode: non401,
                capturedContext: context,
                currentAuth: currentAuth,
                markNeedsReauth: { reauthCalled = true }
            )
            XCTAssertFalse(applied, "Status \(non401) should have no reauth effect")
            XCTAssertFalse(reauthCalled, "Status \(non401) should not call markNeedsReauth")
        }
    }

    // MARK: - Held-Continuation Restore Tests

    func testRestoreOperation_heldSync_generationChangesWhileSuspended_abortsWithoutVerifying() async throws {
        let gate = AsyncGate()
        defer { gate.open() }

        var currentGeneration = 1
        var verifyCallCount = 0
        let context = SubscriptionVerificationContext(
            contractorID: "c1",
            bearerToken: "token-1",
            generation: 1
        )

        let task = Task {
            try await SubscriptionRestoreOperation.performRestore(
                context: context,
                isCurrent: { currentGeneration == 1 },
                sync: {
                    await gate.signalAndPause()
                },
                verify: { _ in
                    verifyCallCount += 1
                    return true
                }
            )
        }

        let entered = await gate.waitForEntry()
        XCTAssertTrue(entered)
        XCTAssertEqual(verifyCallCount, 0)

        // Externally change generation while held suspended in sync
        currentGeneration = 2

        gate.open()
        let result = try await task.value

        XCTAssertFalse(result)
        XCTAssertEqual(verifyCallCount, 0)
    }

    func testRestoreOperation_heldSync_accountChangesWhileSuspended_abortsWithoutVerifying() async throws {
        let gate = AsyncGate()
        defer { gate.open() }

        var currentAccount = "c1"
        var verifyCallCount = 0
        let context = SubscriptionVerificationContext(
            contractorID: "c1",
            bearerToken: "token-1",
            generation: 1
        )

        let task = Task {
            try await SubscriptionRestoreOperation.performRestore(
                context: context,
                isCurrent: { currentAccount == "c1" },
                sync: {
                    await gate.signalAndPause()
                },
                verify: { _ in
                    verifyCallCount += 1
                    return true
                }
            )
        }

        let entered = await gate.waitForEntry()
        XCTAssertTrue(entered)
        XCTAssertEqual(verifyCallCount, 0)

        // Externally change account while held suspended in sync
        currentAccount = "c2"

        gate.open()
        let result = try await task.value

        XCTAssertFalse(result)
        XCTAssertEqual(verifyCallCount, 0)
    }

    private func httpResponse(
        status: Int,
        headers: [String: String]? = nil
    ) throws -> HTTPURLResponse {
        try XCTUnwrap(
            HTTPURLResponse(
                url: URL(string: "https://example.com/verify")!,
                statusCode: status,
                httpVersion: nil,
                headerFields: headers
            )
        )
    }
}

@MainActor
private final class MutableCounter {
    var value = 0
}

private actor CallCounter {
    private(set) var value = 0

    func increment() {
        value += 1
    }
}
