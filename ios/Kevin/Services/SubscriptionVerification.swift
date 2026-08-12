import Foundation

enum SubscriptionVerificationSource: String, Sendable {
    case purchase
    case transactionUpdate = "transaction_update"
    case launch
    case restore
    case retry
}

enum SubscriptionVerificationOutcome: Equatable, Sendable {
    case active
    case inactive
    case retryable(after: TimeInterval)
    case rejected(reason: String)
}

struct SubscriptionVerificationContext: Equatable, Sendable {
    let contractorID: String
    let bearerToken: String

    var cacheNamespace: String { contractorID }

    func matches(contractorID: String, bearerToken: String) -> Bool {
        self.contractorID == contractorID && self.bearerToken == bearerToken
    }
}

enum SubscriptionServerResolution: Equatable, Sendable {
    case active
    case inactive
    case retryable(after: TimeInterval)
    case rejected(reason: String)

    var shouldFinishTransaction: Bool {
        switch self {
        case .active, .inactive:
            return true
        case .retryable, .rejected:
            return false
        }
    }
}

enum SubscriptionVerificationResponseParser {
    static func parse(data: Data, response: HTTPURLResponse) -> SubscriptionVerificationOutcome {
        let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]

        if response.statusCode == 200 {
            // The previously deployed endpoint used this exact non-terminal
            // envelope when Apple verified a transaction but the server could
            // not apply it. Preserve retry behavior during a server-first
            // rolling rollout; no other 200 error shape is accepted.
            if json?["status"] as? String == "error",
               json?["message"] as? String == "update_failed" {
                return .retryable(after: retryAfter(from: response, json: json))
            }

            guard json?["status"] as? String == "ok" else {
                return .rejected(reason: "invalid_success_response")
            }

            let outcome = json?["outcome"] as? String
            let entitlementActive = json?["entitlement_active"] as? Bool
            if outcome == "active", entitlementActive == true {
                return .active
            }
            if outcome == "inactive", entitlementActive == false {
                return .inactive
            }

            // Backward compatibility is intentionally narrow. Older verified
            // successes used one of these two messages and had neither of the
            // typed fields above. In particular, the historical
            // `verification_skipped` response must never finish a transaction.
            if outcome == nil,
               entitlementActive == nil,
               let message = json?["message"] as? String,
               message == "updated" || message == "already_processed" {
                return .active
            }

            return .rejected(reason: "invalid_success_response")
        }

        if response.statusCode == 429 || (500...599).contains(response.statusCode) {
            return .retryable(after: retryAfter(from: response, json: json))
        }

        if (200...299).contains(response.statusCode) {
            return .retryable(after: 30)
        }

        return .rejected(
            reason: reason(from: json, fallback: "http_\(response.statusCode)")
        )
    }

    private static func retryAfter(
        from response: HTTPURLResponse,
        json: [String: Any]?
    ) -> TimeInterval {
        if let header = response.value(forHTTPHeaderField: "Retry-After"),
           let seconds = TimeInterval(header),
           seconds.isFinite,
           seconds > 0 {
            return seconds
        }
        if let seconds = json?["retry_after_seconds"] as? Double,
           seconds.isFinite,
           seconds > 0 {
            return seconds
        }
        if let seconds = json?["retry_after_seconds"] as? Int, seconds > 0 {
            return TimeInterval(seconds)
        }
        return 30
    }

    private static func reason(from json: [String: Any]?, fallback: String) -> String {
        if let reason = json?["reason"] as? String, !reason.isEmpty { return reason }
        if let detail = json?["detail"] as? String, !detail.isEmpty { return detail }
        if let message = json?["message"] as? String, !message.isEmpty { return message }
        return fallback
    }
}

/// Coalesces overlapping StoreKit callbacks for one contractor/transaction.
/// Terminal responses are cached briefly so launch reconciliation immediately
/// following Transaction.updates does not repeat the same server call.
actor SubscriptionVerificationCoordinator {
    private struct CacheEntry: Sendable {
        let outcome: SubscriptionVerificationOutcome
        let expiresAt: ContinuousClock.Instant
    }

    private let cacheLifetime: TimeInterval
    private let clock = ContinuousClock()
    private var inFlight: [String: Task<SubscriptionVerificationOutcome, Never>] = [:]
    private var cache: [String: CacheEntry] = [:]
    private var retryNotBefore: [String: ContinuousClock.Instant] = [:]

    init(cacheLifetime: TimeInterval = 30) {
        self.cacheLifetime = cacheLifetime
    }

    func result(
        for key: String,
        operation: @escaping @Sendable () async -> SubscriptionVerificationOutcome
    ) async -> SubscriptionVerificationOutcome {
        let now = clock.now
        if let cached = cache[key], cached.expiresAt > now {
            return cached.outcome
        }
        cache[key] = nil

        if let deadline = retryNotBefore[key], deadline > now {
            return .retryable(after: seconds(from: now, to: deadline))
        }
        retryNotBefore[key] = nil

        if let existing = inFlight[key] {
            return await existing.value
        }

        let task = Task { await operation() }
        inFlight[key] = task
        let outcome = await task.value
        inFlight[key] = nil

        let completedAt = clock.now
        if case .retryable(let requestedDelay) = outcome {
            let delay = max(requestedDelay, 0.001)
            retryNotBefore[key] = completedAt.advanced(by: .seconds(delay))
        } else {
            retryNotBefore[key] = nil
            cache[key] = CacheEntry(
                outcome: outcome,
                expiresAt: completedAt.advanced(by: .seconds(cacheLifetime))
            )
        }
        return outcome
    }


    private func seconds(
        from start: ContinuousClock.Instant,
        to end: ContinuousClock.Instant
    ) -> TimeInterval {
        let components = start.duration(to: end).components
        return max(
            0.001,
            Double(components.seconds) + Double(components.attoseconds) / 1e18
        )
    }
}

enum SubscriptionVerificationRetryPolicy {
    static let maximumAttempts = 3
    static let maximumPolicyDelay: TimeInterval = 15 * 60

    /// Positive-only jitter avoids a synchronized retry wave without ever
    /// retrying before the server's Retry-After deadline.
    static func delay(
        requested: TimeInterval,
        attempt: Int,
        jitterUnit: Double
    ) -> TimeInterval {
        // Retry-After is a lower bound owned by the server. Never shorten it;
        // only the client-generated exponential policy is capped.
        let serverFloor = requested.isFinite ? max(requested, 1) : 30
        let policyFloor = min(30 * pow(2, Double(max(attempt, 0))), 120)
        let backedOff = max(serverFloor, min(policyFloor, maximumPolicyDelay))
        let boundedJitterUnit = min(max(jitterUnit, 0), 1)
        let jitterCap = min(backedOff * 0.1, 5)
        return backedOff + jitterCap * boundedJitterUnit
    }
}

/// Testable bounded retry loop used by SubscriptionManager. It returns only a
/// terminal resolution; nil means cancellation or exhaustion, leaving the
/// transaction unfinished for a future launch/restore reconciliation.
@MainActor
enum SubscriptionVerificationRetryRunner {
    static func run(
        initialDelay: TimeInterval,
        maximumAttempts: Int = SubscriptionVerificationRetryPolicy.maximumAttempts,
        jitter: () -> Double = { Double.random(in: 0...1) },
        sleep: (TimeInterval) async throws -> Void = { delay in
            try await Task.sleep(for: .seconds(delay))
        },
        operation: () async -> SubscriptionServerResolution
    ) async -> SubscriptionServerResolution? {
        var requestedDelay = initialDelay

        for attempt in 0..<maximumAttempts {
            let delay = SubscriptionVerificationRetryPolicy.delay(
                requested: requestedDelay,
                attempt: attempt,
                jitterUnit: jitter()
            )
            do {
                try await sleep(delay)
            } catch {
                return nil
            }
            guard !Task.isCancelled else { return nil }

            let resolution = await operation()
            guard !Task.isCancelled else { return nil }
            if case .retryable(let nextDelay) = resolution {
                requestedDelay = nextDelay
                continue
            }
            return resolution
        }

        return nil
    }
}
