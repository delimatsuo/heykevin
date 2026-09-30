import Foundation
import AdServices

/// Coordinates Apple Ads attribution token generation and backend recording.
///
/// Ensures attribution collection is strictly bounded:
/// - Verifies backend eligibility before requesting any attribution token
/// - Generates `AAAttribution.attributionToken()` off the main thread in memory
/// - Applies CallAuthContext generation fences before and after every suspension
/// - Active task owned by full CallAuthContext; new context cancels prior task
/// - Retries at most three times with minimum 5-second spacing
/// - Discards flight immediately on account change/signout
@MainActor
final class AcquisitionCoordinator {
    static let shared = AcquisitionCoordinator()

    private let checkEligibility: (String) async throws -> Bool
    private let requestAttributionToken: () async throws -> String
    private let postAttributionToken: (String, String) async throws -> AttributionPostResult
    private let sleepSeconds: (Double) async throws -> Void
    private let currentAuth: () -> CallAuthContext

    private var activeContext: CallAuthContext?
    private var activeTask: Task<Void, Error>?
    private var activeFlightId: UUID?

    init(
        eligibilityChecker: @escaping (String) async throws -> Bool = {
            await APIClient.shared.getAcquisitionStatus(bearerToken: $0)
        },
        tokenRequester: @escaping () async throws -> String = {
            try await Task.detached(priority: .utility) {
                if #available(iOS 14.3, *) {
                    return (try? AAAttribution.attributionToken()) ?? ""
                }
                return ""
            }.value
        },
        attributionPoster: @escaping (String, String) async throws -> AttributionPostResult = {
            try await APIClient.shared.postAppleAdsAttribution(token: $0, bearerToken: $1)
        },
        sleepHandler: @escaping (Double) async throws -> Void = {
            try await Task.sleep(nanoseconds: UInt64($0 * 1_000_000_000))
        },
        authProvider: @escaping () -> CallAuthContext = {
            AppState.shared.currentAuthContext()
        }
    ) {
        self.checkEligibility = eligibilityChecker
        self.requestAttributionToken = tokenRequester
        self.postAttributionToken = attributionPoster
        self.sleepSeconds = sleepHandler
        self.currentAuth = authProvider
    }

    func handleSceneActive() async {
        await evaluateAndRecord()
    }

    func handleAuthChange() async {
        await evaluateAndRecord()
    }

    func evaluateAndRecord() async {
        guard !Task.isCancelled else { return }
        let auth = currentAuth()
        guard auth.isValid, !auth.contractorId.isEmpty, !auth.bearerToken.isEmpty else {
            cancelActiveFlight()
            return
        }

        if let current = activeContext, current == auth, let task = activeTask, !task.isCancelled {
            await withTaskCancellationHandler {
                _ = try? await task.value
            } onCancel: {
                task.cancel()
            }
            return
        }

        cancelActiveFlight()
        guard !Task.isCancelled else { return }
        let flightId = UUID()
        activeFlightId = flightId
        activeContext = auth

        let task = Task<Void, Error> { @MainActor [weak self] in
            guard let self = self else { return }
            try await self.runAttributionLoop(for: auth)
        }
        activeTask = task

        await withTaskCancellationHandler {
            _ = try? await task.value
        } onCancel: {
            task.cancel()
        }

        if activeFlightId == flightId {
            activeFlightId = nil
            activeTask = nil
            activeContext = nil
        }
    }

    private func cancelActiveFlight() {
        activeTask?.cancel()
        activeTask = nil
        activeContext = nil
        activeFlightId = nil
    }

    private func runAttributionLoop(for auth: CallAuthContext) async throws {
        try Task.checkCancellation()
        guard currentAuth() == auth else { return }

        // Step 1: Check backend eligibility with captured bearer token
        let isEligible = try await checkEligibility(auth.bearerToken)
        try Task.checkCancellation()
        guard currentAuth() == auth, isEligible else { return }

        // Step 2: Generate attribution token off main thread in memory
        let token = try await requestAttributionToken()
        try Task.checkCancellation()
        guard currentAuth() == auth else { return }

        let trimmedToken = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedToken.isEmpty, let tokenBytes = trimmedToken.data(using: .utf8),
              tokenBytes.count >= 1 && tokenBytes.count <= 8192 else {
            return
        }

        // Step 3: Post attribution token with max 3 attempts and 5-second spacing
        for attempt in 1...3 {
            try Task.checkCancellation()
            guard currentAuth() == auth else { return }

            let result: AttributionPostResult
            do {
                result = try await postAttributionToken(trimmedToken, auth.bearerToken)
            } catch {
                if Task.isCancelled { throw CancellationError() }
                if attempt < 3 {
                    try await sleepSeconds(5.0)
                    continue
                }
                return
            }

            try Task.checkCancellation()
            guard currentAuth() == auth else { return }

            switch result.status {
            case .recorded, .alreadyRecorded, .unattributed, .ineligible, .disabled, .exhausted:
                return
            case .retryable:
                if attempt < 3 {
                    let delay = Double(result.retryAfterSeconds ?? 5)
                    let clampedDelay = max(5.0, min(15.0, delay))
                    try await sleepSeconds(clampedDelay)
                }
            case .unknown:
                if attempt < 3 {
                    try await sleepSeconds(5.0)
                }
            }
        }
    }
}
