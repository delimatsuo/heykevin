import Combine
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


    func testReleaseReconciliation503IsRetryable() throws {
        let response = try httpResponse(
            status: 503,
            headers: ["Retry-After": "60"]
        )
        let data = try JSONSerialization.data(withJSONObject: [
            "status": "retryable",
            "reason": "number_release_reconciliation",
            "retry_after_seconds": 60,
        ])

        XCTAssertEqual(
            SubscriptionVerificationResponseParser.parse(data: data, response: response),
            .retryable(after: 60)
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

    func testOwnedProfileAppliesEachSuppliedStringAndReturnsFreshStatus() {
        let auth = CallAuthContext(contractorId: "c1", bearerToken: "token-1", generation: 1)
        let cases: [(status: Any?, tier: Any?, savedStatus: String, savedTier: String, fresh: String?)] = [
            ("active", "businessPro", "active", "businessPro", "active"),
            ("trial", "personal", "trial", "personal", "trial"),
            ("expired", "none", "expired", "none", "expired"),
            ("cancelled", "business", "cancelled", "business", "cancelled"),
            ("expired", nil, "expired", "businessPro", "expired"),
            (nil, "personal", "active", "personal", nil),
            (nil, nil, "active", "businessPro", nil),
            (NSNull(), "business", "active", "business", nil),
            ("cancelled", NSNull(), "cancelled", "businessPro", "cancelled"),
            (false, "personal", "active", "personal", nil),
            ("trial", ["business"], "trial", "businessPro", "trial"),
            ("", "business", "active", "business", nil),
            ("expired", "", "expired", "businessPro", "expired"),
            (" future ", " ", " future ", " ", " future "),
        ]
        for (index, row) in cases.enumerated() {
            let state = AppState(authProvider: { auth }, inMemory: true)
            state.subscriptionStatus = "active"
            state.subscriptionTier = "businessPro"
            var profile: [String: Any] = ["contractor_id": auth.contractorId]
            profile["subscription_status"] = row.status
            profile["subscription_tier"] = row.tier
            let fresh = state.applySubscriptionProfile(profile, for: auth)
            XCTAssertEqual(fresh, row.fresh, "row \(index)")
            XCTAssertEqual(state.subscriptionStatus, row.savedStatus, "row \(index)")
            XCTAssertEqual(state.subscriptionTier, row.savedTier, "row \(index)")
        }
    }

    func testSubscriptionRefreshRequiresTheExactRequestedProfileID() {
        let auth = CallAuthContext(contractorId: "café", bearerToken: "token-1", generation: 1)
        let state = AppState(authProvider: { auth }, inMemory: true)
        let invalidIDs: [Any?] = [nil, NSNull(), false, 123, "", "other", " café", "cafe\u{301}"]
        for (index, invalidID) in invalidIDs.enumerated() {
            var profile: [String: Any] = ["subscription_status": "active", "subscription_tier": "business"]
            profile["contractor_id"] = invalidID
            XCTAssertNil(state.applySubscriptionProfile(profile, for: auth), "row \(index)")
            XCTAssertEqual(state.subscriptionStatus, "trial", "row \(index)")
            XCTAssertEqual(state.subscriptionTier, "none", "row \(index)")
        }
    }

    func testProfileRequiresCurrentContractorTokenGenerationAndValidAuth() {
        let captured = CallAuthContext(contractorId: "c1", bearerToken: "token-1", generation: 1)
        var current = captured
        let state = AppState(authProvider: { current }, inMemory: true)
        let profile: [String: Any] = [
            "contractor_id": captured.contractorId, "subscription_status": "expired", "subscription_tier": "personal",
        ]
        let staleSessions = [
            CallAuthContext(contractorId: "c2", bearerToken: "token-1", generation: 1),
            CallAuthContext(contractorId: "c1", bearerToken: "token-2", generation: 1),
            CallAuthContext(contractorId: "c1", bearerToken: "token-1", generation: 2),
        ]
        for newSession in staleSessions {
            current = newSession
            XCTAssertNil(state.applySubscriptionProfile(profile, for: captured))
            XCTAssertEqual(state.subscriptionStatus, "trial")
            XCTAssertEqual(state.subscriptionTier, "none")
        }
        current = CallAuthContext(contractorId: captured.contractorId, bearerToken: "", generation: 3)
        XCTAssertNil(state.applySubscriptionProfile(profile, for: current))
        XCTAssertEqual(state.subscriptionStatus, "trial")
        XCTAssertEqual(state.subscriptionTier, "none")
    }

    func testLaunchRefreshChecksReadinessWhenTheWriteRuns() async {
        let auth = CallAuthContext(contractorId: "c1", bearerToken: "token-1", generation: 1)
        let state = AppState(authProvider: { auth }, inMemory: true)
        let profile: [String: Any] = [
            "contractor_id": auth.contractorId, "subscription_status": "active", "subscription_tier": "personal",
        ]
        state.sessionState = .ready
        let queuedWrite = Task { @MainActor in
            state.applySubscriptionProfile(profile, for: auth, requiresReadySession: true)
        }
        // This test holds the main actor until the following await.
        state.sessionState = .needsRecovery
        let fresh = await queuedWrite.value
        XCTAssertNil(fresh)
        XCTAssertEqual(state.subscriptionStatus, "trial")
        XCTAssertEqual(state.subscriptionTier, "none")
        state.sessionState = .ready
        XCTAssertEqual(state.applySubscriptionProfile(profile, for: auth, requiresReadySession: true), "active")
        XCTAssertEqual(state.subscriptionTier, "personal")
    }

    func testRefreshPublishesOnlySubscriptionFieldsInOrderAndPreservesAccountAndCallState() throws {
        let auth = CallAuthContext(contractorId: "c1", bearerToken: "token-1", generation: 1)
        let state = AppState(authProvider: { auth }, inMemory: true)
        state.contractorId = auth.contractorId
        state.appleUserId = "apple-A"
        state.subscriptionUUID = "uuid-A"
        state.contactsUploadConsent = true
        state.isVerizonCarrier = true
        let binding = ServiceBinding(countryCode: "US", provider: "fixture")
        state.serviceBinding = binding
        state.setActiveCall(callSid: "call-A", callerPhone: "", callerName: "", authContext: auth)
        let lease = try XCTUnwrap(state.ownedActiveCallLease)

        var events: [String] = []
        let statusObserver = state.$subscriptionStatus.dropFirst().sink { events.append("status \($0)") }
        let tierObserver = state.$subscriptionTier.dropFirst().sink { events.append("tier \($0)") }
        withExtendedLifetime((statusObserver, tierObserver)) {
            for contractor in ["c1", "c2"] {
                let profile: [String: Any] = [
                    "contractor_id": contractor, "subscription_status": "active", "subscription_tier": "businessPro",
                    "subscription_uuid": "foreign", "service_binding": NSNull(), "contacts_upload_consent": false,
                ]
                let fresh = state.applySubscriptionProfile(profile, for: auth)
                XCTAssertEqual(fresh, contractor == "c1" ? "active" : nil)
                XCTAssertEqual(events, ["status active", "tier businessPro"])
                XCTAssertEqual(state.subscriptionStatus, "active")
                XCTAssertEqual(state.subscriptionTier, "businessPro")
                XCTAssertEqual(state.contractorId, auth.contractorId)
                XCTAssertEqual(state.currentAuthContext(), auth)
                XCTAssertEqual(state.appleUserId, "apple-A")
                XCTAssertEqual(state.subscriptionUUID, "uuid-A")
                XCTAssertTrue(state.contactsUploadConsent)
                XCTAssertTrue(state.isVerizonCarrier)
                XCTAssertEqual(state.serviceBinding, binding)
                XCTAssertEqual(state.ownedActiveCallLease, lease)
            }
        }
    }

    func testPurchaseReconciliationUsesFreshStatusAndPreservesInactiveAcknowledgement() async {
        let context = SubscriptionVerificationContext(contractorID: "c1", bearerToken: "token-1", generation: 4)
        let auth = CallAuthContext(contractorId: "c1", bearerToken: "token-1", generation: 4)
        let cases: [(SubscriptionVerificationOutcome, [String: Any]?, SubscriptionServerResolution, String, String)] = [
            (.active, nil, .retryable(after: 30), "active", "businessPro"),
            (.active, ["contractor_id": "c1", "subscription_tier": "personal"], .retryable(after: 30), "active", "personal"),
            (.active, ["contractor_id": "c2", "subscription_status": "active"], .retryable(after: 30), "active", "businessPro"),
            (.active, ["contractor_id": "c1", "subscription_status": "active"], .active, "active", "businessPro"),
            (.active, ["contractor_id": "c1", "subscription_status": "future"], .inactive, "future", "businessPro"),
            (.inactive, nil, .inactive, "active", "businessPro"),
            (.inactive, ["contractor_id": "c1", "subscription_status": "expired"], .inactive, "expired", "businessPro"),
        ]
        for (acknowledgement, profile, expected, status, tier) in cases {
            let state = AppState(authProvider: { auth }, inMemory: true)
            state.sessionState = .notOnboarded
            state.subscriptionStatus = "active"
            state.subscriptionTier = "businessPro"
            let resolution = await SubscriptionManager.reconcileVerification(
                acknowledgement, context: context, appState: state
            ) { id, token in
                XCTAssertEqual(id, context.contractorID)
                XCTAssertEqual(token, context.bearerToken)
                return profile
            }
            XCTAssertEqual(resolution, expected)
            XCTAssertEqual(state.subscriptionStatus, status)
            XCTAssertEqual(state.subscriptionTier, tier)
        }
    }

    func testHeldPurchaseRefreshRejectsAcknowledgementAfterAccountSwitchAndReturn() async {
        let context = SubscriptionVerificationContext(contractorID: "c1", bearerToken: "token-1", generation: 1)
        var current = CallAuthContext(contractorId: "c1", bearerToken: "token-1", generation: 1)
        let state = AppState(authProvider: { current }, inMemory: true)
        for acknowledgement in [SubscriptionVerificationOutcome.active, .inactive] {
            let gate = AsyncGate()
            defer { gate.open() }
            current = CallAuthContext(contractorId: "c1", bearerToken: "token-1", generation: 1)
            let task = Task {
                await SubscriptionManager.reconcileVerification(
                    acknowledgement, context: context, appState: state
                ) { _, _ in
                    await gate.signalAndPause()
                    if acknowledgement == .inactive { return nil }
                    return ["contractor_id": "c1", "subscription_status": "active", "subscription_tier": "business"]
                }
            }
            let entered = await gate.waitForEntry()
            XCTAssertTrue(entered)
            current = CallAuthContext(contractorId: "c2", bearerToken: "token-2", generation: 2)
            XCTAssertEqual(state.currentAuthContext().contractorId, "c2")
            current = CallAuthContext(contractorId: "c1", bearerToken: "token-1", generation: 3)
            gate.open()
            let resolution = await task.value
            XCTAssertEqual(resolution, .rejected(reason: "account_changed"))
            XCTAssertEqual(state.subscriptionStatus, "trial")
            XCTAssertEqual(state.subscriptionTier, "none")
        }
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

    // MARK: - ContractorProfilePayload & Subscription UUID Tests

    func testContractorProfilePayload_validatesIdentityUnicodeBytesAndFieldPreservation() {
        let invalidPayloads: [[String: Any]] = [
            [:],
            ["contractor_id": ""],
            ["contractor_id": NSNull()],
            ["contractor_id": 123],
            ["contractor_id": true],
        ]
        for invalid in invalidPayloads {
            XCTAssertNil(ContractorProfilePayload(profile: invalid))
            XCTAssertNil(ContractorProfilePayload(profile: invalid, expectedContractorId: "c1"))
        }

        let nfc = "caf\u{00E9}"
        let nfd = "cafe\u{0301}"
        XCTAssertEqual(nfc, nfd, "Swift String equality treats NFC and NFD as equal")
        XCTAssertFalse(nfc.utf8.elementsEqual(nfd.utf8))

        XCTAssertNil(
            ContractorProfilePayload(
                profile: ["contractor_id": nfd],
                expectedContractorId: nfc
            ),
            "Byte-distinct NFD contractor_id must be rejected when expecting NFC"
        )
        XCTAssertNotNil(
            ContractorProfilePayload(
                profile: ["contractor_id": nfc],
                expectedContractorId: nfc
            )
        )
        XCTAssertNil(
            ContractorProfilePayload(
                profile: ["contractor_id": "c1"],
                expectedContractorId: ""
            ),
            "Empty expectedContractorId must fail closed"
        )

        // matchesOptionalContractorId table checks
        XCTAssertFalse(
            ContractorProfilePayload.matchesOptionalContractorId(
                in: [:],
                expectedContractorId: ""
            )
        )
        XCTAssertFalse(
            ContractorProfilePayload.matchesOptionalContractorId(
                in: ["contractor_id": "c1"],
                expectedContractorId: ""
            )
        )
        XCTAssertTrue(
            ContractorProfilePayload.matchesOptionalContractorId(
                in: ["owner_name": "Alice"],
                expectedContractorId: "c1"
            ),
            "Absent contractor_id key is permitted for pure-policy synthetic dicts"
        )
        for badValue: Any in [NSNull(), 123, "", "other", nfd] {
            XCTAssertFalse(
                ContractorProfilePayload.matchesOptionalContractorId(
                    in: ["contractor_id": badValue],
                    expectedContractorId: nfc
                ),
                "Present invalid/mismatched contractor_id (\(badValue)) must fail closed"
            )
        }
        XCTAssertTrue(
            ContractorProfilePayload.matchesOptionalContractorId(
                in: ["contractor_id": nfc],
                expectedContractorId: nfc
            )
        )

        // knowledge: "" preserved as .some("") vs nil when absent; untrimmed status/tier preserved
        let lowercaseUUID = "550e8400-e29b-41d4-a716-446655440000"
        let emptyKnowledgePayload = ContractorProfilePayload(
            profile: [
                "contractor_id": "c1",
                "knowledge": "",
                "subscription_status": " future ",
                "subscription_tier": " enterprise ",
                "subscription_uuid": lowercaseUUID,
                "owner_phone": "",
            ],
            expectedContractorId: "c1"
        )
        XCTAssertEqual(emptyKnowledgePayload?.knowledge, .some(""))
        XCTAssertEqual(emptyKnowledgePayload?.subscriptionStatus, " future ")
        XCTAssertEqual(emptyKnowledgePayload?.subscriptionTier, " enterprise ")
        XCTAssertEqual(emptyKnowledgePayload?.subscriptionUUIDString, lowercaseUUID)
        XCTAssertEqual(emptyKnowledgePayload?.parsedSubscriptionUUID, UUID(uuidString: lowercaseUUID))
        XCTAssertNil(emptyKnowledgePayload?.ownerPhone)

        let absentKnowledgePayload = ContractorProfilePayload(
            profile: ["contractor_id": "c1"],
            expectedContractorId: "c1"
        )
        XCTAssertNil(absentKnowledgePayload?.knowledge)
    }

    func testAPIClientGetContractorProfile_enforcesContractorIdMatchOverHTTP() async throws {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [MockURLProtocol.self]
        let session = URLSession(configuration: config)
        defer {
            session.invalidateAndCancel()
            MockURLProtocol.requestHandler = nil
        }
        let client = APIClient(session: session)

        let nfc = "caf\u{00E9}"
        let nfd = "cafe\u{0301}"

        let rejectedBodies: [(requestedId: String, body: [String: Any])] = [
            ("c1", [:]),
            ("c1", ["contractor_id": ""]),
            ("c1", ["contractor_id": "other"]),
            (nfc, ["contractor_id": nfd]),
        ]

        for testCase in rejectedBodies {
            MockURLProtocol.requestHandler = { request in
                let response = HTTPURLResponse(
                    url: request.url!,
                    statusCode: 200,
                    httpVersion: nil,
                    headerFields: nil
                )!
                let data = try JSONSerialization.data(withJSONObject: testCase.body)
                return (response, data)
            }
            let fetched = await client.getContractorProfile(
                contractorId: testCase.requestedId,
                bearerToken: "token-1"
            )
            XCTAssertNil(fetched, "Expected nil for mismatched/missing contractor_id: \(testCase.body)")
        }

        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer token-1")
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: nil
            )!
            let data = try JSONSerialization.data(withJSONObject: [
                "contractor_id": nfc,
                "owner_name": "Deli",
            ])
            return (response, data)
        }
        let valid = await client.getContractorProfile(
            contractorId: nfc,
            bearerToken: "token-1"
        )
        XCTAssertEqual(valid?["contractor_id"] as? String, nfc)
        XCTAssertEqual(valid?["owner_name"] as? String, "Deli")
    }

    @MainActor
    func testApplyAndResolveServerSubscriptionUUID_fencesSessionPreservesRawCasingAndNeverShortCircuitsCache() async {
        var activeAuth = CallAuthContext(contractorId: "c1", bearerToken: "token-1", generation: 1)
        let state = AppState(authProvider: { activeAuth }, inMemory: true)
        state.contractorId = "c1"
        state.sessionState = .notOnboarded

        let cachedValidUUID = "11111111-2222-3333-4444-555555555555"
        let serverLowercaseUUID = "aa0e8400-e29b-41d4-a716-4466554400bb"
        state.subscriptionUUID = cachedValidUUID

        // Rejects wrong contractor_id and invalid UUID without mutating cached subscriptionUUID
        XCTAssertNil(
            state.applySubscriptionUUID(
                from: ["contractor_id": "other", "subscription_uuid": serverLowercaseUUID],
                for: activeAuth
            )
        )
        XCTAssertEqual(state.subscriptionUUID, cachedValidUUID)

        XCTAssertNil(
            state.applySubscriptionUUID(
                from: ["contractor_id": "c1", "subscription_uuid": "not-a-valid-uuid"],
                for: activeAuth
            )
        )
        XCTAssertEqual(state.subscriptionUUID, cachedValidUUID)

        // Always fetches server profile even when appState.subscriptionUUID already holds a valid UUID,
        // works while sessionState == .notOnboarded, and preserves lowercase hex in appState.subscriptionUUID.
        var fetchCount = 0
        let context = SubscriptionVerificationContext(
            contractorID: "c1",
            bearerToken: "token-1",
            generation: 1
        )
        let resolved = await SubscriptionManager.resolveServerSubscriptionUUID(
            context: context,
            appState: state,
            fetchProfile: { cid, token in
                fetchCount += 1
                XCTAssertEqual(cid, "c1")
                XCTAssertEqual(token, "token-1")
                return [
                    "contractor_id": "c1",
                    "subscription_uuid": serverLowercaseUUID,
                ]
            }
        )
        XCTAssertEqual(fetchCount, 1, "Must always fetch from server even when cached UUID is non-empty")
        XCTAssertEqual(resolved, .resolved(UUID(uuidString: serverLowercaseUUID)!))
        XCTAssertEqual(
            state.subscriptionUUID,
            serverLowercaseUUID,
            "Must store raw validated UUID string without uppercasing hex digits"
        )

        // Invalid server UUID returns .missingOrInvalid (distinct from .accountChanged)
        let invalidOutcome = await SubscriptionManager.resolveServerSubscriptionUUID(
            context: context,
            appState: state,
            fetchProfile: { _, _ in
                ["contractor_id": "c1", "subscription_uuid": "malformed-uuid"]
            }
        )
        XCTAssertEqual(invalidOutcome, .missingOrInvalid)
        XCTAssertEqual(state.subscriptionUUID, serverLowercaseUUID)

        // Stale context before start returns .accountChanged without fetching
        var staleFetchCalled = false
        let staleContext = SubscriptionVerificationContext(
            contractorID: "c1",
            bearerToken: "token-1",
            generation: 99
        )
        let preSwitchOutcome = await SubscriptionManager.resolveServerSubscriptionUUID(
            context: staleContext,
            appState: state,
            fetchProfile: { _, _ in
                staleFetchCalled = true
                return nil
            }
        )
        XCTAssertEqual(preSwitchOutcome, .accountChanged)
        XCTAssertFalse(staleFetchCalled)

        // Account/generation switch during suspension returns .accountChanged and does not overwrite replacement UUID
        let gate = AsyncGate()
        defer { gate.open() }

        let newAccountUUID = "99999999-aaaa-bbbb-cccc-dddddddddddd"
        let task = Task { @MainActor in
            await SubscriptionManager.resolveServerSubscriptionUUID(
                context: context,
                appState: state,
                fetchProfile: { _, _ in
                    await gate.signalAndPause()
                    return [
                        "contractor_id": "c1",
                        "subscription_uuid": "ffffffff-eeee-dddd-cccc-bbbbbbbbbbbb",
                    ]
                }
            )
        }

        let entered = await gate.waitForEntry()
        XCTAssertTrue(entered)

        activeAuth = CallAuthContext(contractorId: "c2", bearerToken: "token-2", generation: 2)
        state.subscriptionUUID = newAccountUUID

        gate.open()
        let midFlightOutcome = await task.value
        XCTAssertEqual(midFlightOutcome, .accountChanged)
        XCTAssertEqual(state.subscriptionUUID, newAccountUUID)
    }

    func testAPIClientSignSubscriptionOffer_usesExplicitContractorIdAndBearerToken() async throws {
        let suite = "Kevin.SignOfferTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        let keychain = RecoveryTestKeychain()
        let persistence = AccountPersistence(
            defaults: defaults,
            keychain: keychain.client,
            epoch: CallSessionEpoch(notificationCenter: NotificationCenter())
        )
        persistence.contractorToken = "wrong-global-token"

        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [MockURLProtocol.self]
        let session = URLSession(configuration: config)
        defer {
            session.invalidateAndCancel()
            MockURLProtocol.requestHandler = nil
            defaults.removePersistentDomain(forName: suite)
        }

        let client = APIClient(session: session, persistence: persistence)
        var capturedHeader: String?
        var capturedBody: [String: Any]?

        MockURLProtocol.requestHandler = { request in
            capturedHeader = request.value(forHTTPHeaderField: "Authorization")
            let bodyData = try Self.readRequestBody(from: request)
            capturedBody = try JSONSerialization.jsonObject(with: bodyData) as? [String: Any]

            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: nil
            )!
            let payload: [String: Any] = [
                "signature": [
                    "keyIdentifier": "KEY123",
                    "nonce": "550e8400-e29b-41d4-a716-446655440000",
                    "signature": Data("sig".utf8).base64EncodedString(),
                    "timestamp": 1_700_000_000_000,
                ]
            ]
            return (response, try JSONSerialization.data(withJSONObject: payload))
        }

        let sig = await client.signSubscriptionOffer(
            productId: "com.kevin.callscreen.business.monthly",
            offerId: "founding_member_75off_business",
            applicationUsername: "explicit-contractor",
            contractorId: "explicit-contractor",
            bearerToken: "explicit-bearer-token"
        )

        XCTAssertEqual(capturedHeader, "Bearer explicit-bearer-token")
        XCTAssertEqual(capturedBody?["contractor_id"] as? String, "explicit-contractor")
        XCTAssertEqual(capturedBody?["application_username"] as? String, "explicit-contractor")
        XCTAssertEqual(capturedBody?["product_id"] as? String, "com.kevin.callscreen.business.monthly")
        XCTAssertEqual(capturedBody?["offer_id"] as? String, "founding_member_75off_business")
        XCTAssertEqual(sig?["keyIdentifier"] as? String, "KEY123")
    }

    private static func readRequestBody(from request: URLRequest) throws -> Data {
        if let body = request.httpBody {
            return body
        }
        let stream = try XCTUnwrap(request.httpBodyStream)
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 1024)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            guard count > 0 else { break }
            data.append(buffer, count: count)
        }
        return data
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
