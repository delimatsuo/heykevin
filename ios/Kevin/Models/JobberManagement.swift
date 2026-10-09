import Foundation
import Combine

// MARK: - API Client Protocol

protocol JobberAPIClientProtocol: Sendable {
    func getJobberConnectURL(auth: CallAuthContext) async throws -> URL
    func getJobberStatus(auth: CallAuthContext) async throws -> JobberStatusPayload
    func disconnectJobber(auth: CallAuthContext) async throws -> JobberDisconnectPayload
}

// MARK: - Errors

enum JobberManagementError: Error, LocalizedError, Equatable {
    case unauthenticated
    case invalidAuthContext
    case httpError(statusCode: Int)
    case malformedResponse
    case inconsistentCaptureState
    case identityMismatch
    case providerMismatch
    case credentialDeletionUnacknowledged
    case invalidAuthorizeURL
    case operationInProgress
    case staleLease

    var errorDescription: String? {
        switch self {
        case .unauthenticated, .invalidAuthContext:
            return String(localized: "Authentication required.")
        case .httpError(let code):
            return String(localized: "Request failed with status \(code).")
        case .malformedResponse, .inconsistentCaptureState, .identityMismatch, .providerMismatch, .credentialDeletionUnacknowledged, .invalidAuthorizeURL:
            return String(localized: "Received an invalid response from the server.")
        case .operationInProgress:
            return String(localized: "An operation is already in progress.")
        case .staleLease:
            return String(localized: "Operation lease expired.")
        }
    }
}

// MARK: - Status Models & Parser

enum JobberConnectionState: Equatable, Sendable {
    case unknown
    case notReady
    case connected(leadCaptureEnabled: Bool)
}

struct JobberStatusPayload: Equatable, Sendable {
    let connected: Bool
    let leadCaptureEnabled: Bool
    let connectedAt: Double?

    init(connected: Bool, leadCaptureEnabled: Bool, connectedAt: Double? = nil) {
        self.connected = connected
        self.leadCaptureEnabled = leadCaptureEnabled
        self.connectedAt = connectedAt
    }
}

enum JobberStatusParser {
    static func parse(data: Data, response: HTTPURLResponse) throws -> JobberStatusPayload {
        guard response.statusCode == 200 else {
            throw JobberManagementError.httpError(statusCode: response.statusCode)
        }
        guard let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            throw JobberManagementError.malformedResponse
        }

        guard let rawConnected = json["connected"],
              let connected = parseExactBool(rawConnected) else {
            throw JobberManagementError.malformedResponse
        }

        guard let rawLeadCapture = json["lead_capture_enabled"],
              let leadCaptureEnabled = parseExactBool(rawLeadCapture) else {
            throw JobberManagementError.malformedResponse
        }

        if !connected && leadCaptureEnabled {
            throw JobberManagementError.inconsistentCaptureState
        }

        var connectedAt: Double? = nil
        if let rawConnectedAt = json["connected_at"] {
            if let num = rawConnectedAt as? NSNumber, CFGetTypeID(num) != CFBooleanGetTypeID() {
                let d = num.doubleValue
                if d.isFinite && d > 0 {
                    connectedAt = d
                }
            }
        }

        return JobberStatusPayload(
            connected: connected,
            leadCaptureEnabled: leadCaptureEnabled,
            connectedAt: connectedAt
        )
    }

    private static func parseExactBool(_ value: Any?) -> Bool? {
        guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else {
            return nil
        }
        return number.boolValue
    }
}

// MARK: - Disconnect Models & Parser

enum JobberRevocationStatus: String, Equatable, Sendable {
    case providerConfirmed = "provider_confirmed"
    case providerRejected = "provider_rejected"
    case transportErrorUnknown = "transport_error_unknown"
    case notAttemptedUnavailableToken = "not_attempted_unavailable_token"
    case unknown = "unknown"
}

struct JobberDisconnectPayload: Equatable, Sendable {
    let status: String
    let contractorId: String
    let provider: String
    let credentialDeletionStatus: String
    let revocationStatus: JobberRevocationStatus

    var isProviderConfirmed: Bool {
        revocationStatus == .providerConfirmed
    }

    init(
        status: String,
        contractorId: String,
        provider: String,
        credentialDeletionStatus: String,
        revocationStatus: JobberRevocationStatus
    ) {
        self.status = status
        self.contractorId = contractorId
        self.provider = provider
        self.credentialDeletionStatus = credentialDeletionStatus
        self.revocationStatus = revocationStatus
    }
}

enum JobberDisconnectParser {
    private static let validCredentialDeletionStatuses: Set<String> = [
        "executed",
        "partial_reconciled",
        "legacy_reconciled"
    ]

    static func parse(
        data: Data,
        response: HTTPURLResponse,
        expectedContractorId: String
    ) throws -> JobberDisconnectPayload {
        guard response.statusCode == 200 else {
            throw JobberManagementError.httpError(statusCode: response.statusCode)
        }
        guard let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            throw JobberManagementError.malformedResponse
        }

        guard let status = json["status"] as? String, status == "disconnected" else {
            throw JobberManagementError.malformedResponse
        }

        guard let contractorId = json["contractor_id"] as? String,
              contractorId == expectedContractorId,
              !contractorId.isEmpty else {
            throw JobberManagementError.identityMismatch
        }

        guard let provider = json["provider"] as? String, provider == "jobber" else {
            throw JobberManagementError.providerMismatch
        }

        guard let credentialDeletion = json["credential_deletion"] as? [String: Any],
              let credDelStatus = credentialDeletion["status"] as? String,
              validCredentialDeletionStatuses.contains(credDelStatus) else {
            throw JobberManagementError.credentialDeletionUnacknowledged
        }

        var rawNestedStatus: String? = nil
        if let providerRevocation = json["provider_revocation"] as? [String: Any],
           let nested = providerRevocation["status"] as? String {
            rawNestedStatus = nested
        }

        let rawTopLevelStatus = json["revocation_status"] as? String

        if let nested = rawNestedStatus, let top = rawTopLevelStatus {
            if nested != top {
                throw JobberManagementError.malformedResponse
            }
        }

        let revStatus: JobberRevocationStatus
        if let raw = rawNestedStatus {
            switch raw {
            case "provider_confirmed":
                revStatus = .providerConfirmed
            case "provider_rejected":
                revStatus = .providerRejected
            case "transport_error_unknown":
                revStatus = .transportErrorUnknown
            case "not_attempted_unavailable_token":
                revStatus = .notAttemptedUnavailableToken
            default:
                revStatus = .unknown
            }
        } else {
            revStatus = .unknown
        }

        return JobberDisconnectPayload(
            status: status,
            contractorId: contractorId,
            provider: provider,
            credentialDeletionStatus: credDelStatus,
            revocationStatus: revStatus
        )
    }
}

// MARK: - Authorize URL Validation & Connect Parser

enum JobberAuthorizeURLValidator {
    static func validate(_ urlString: String) -> URL? {
        guard let components = URLComponents(string: urlString) else { return nil }
        guard components.scheme == "https" else { return nil }
        guard components.host == "api.getjobber.com" else { return nil }
        guard components.path == "/api/oauth/authorize" else { return nil }
        guard components.user == nil, components.password == nil else { return nil }
        guard components.port == nil else { return nil }
        guard components.fragment == nil else { return nil }
        return components.url
    }
}

enum JobberConnectResponseParser {
    static func parse(data: Data, response: HTTPURLResponse) throws -> URL {
        guard response.statusCode == 200 else {
            throw JobberManagementError.httpError(statusCode: response.statusCode)
        }
        guard let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let authorizeUrlStr = json["authorize_url"] as? String,
              let validUrl = JobberAuthorizeURLValidator.validate(authorizeUrlStr) else {
            throw JobberManagementError.invalidAuthorizeURL
        }
        return validUrl
    }
}

// MARK: - Deep Link Validator

enum JobberDeepLinkValidator {
    static let canonicalURLString = "heykevin://integrations/jobber"

    static func isValidJobberDeepLink(_ url: URL) -> Bool {
        guard url.absoluteString == canonicalURLString else { return false }
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return false }
        guard components.scheme == "heykevin" else { return false }
        guard components.host == "integrations" else { return false }
        guard components.path == "/jobber" else { return false }
        guard components.user == nil, components.password == nil else { return false }
        guard components.port == nil else { return false }
        guard components.query == nil else { return false }
        guard components.fragment == nil else { return false }
        return true
    }
}

// MARK: - Operation Leases & Management Model

enum JobberOperationKind: Equatable, Sendable {
    case refreshStatus
    case connect
    case disconnect
}

struct JobberOperationLease: Identifiable, Equatable, Sendable {
    let id: UUID
    let auth: CallAuthContext
    let operation: JobberOperationKind

    init(id: UUID = UUID(), auth: CallAuthContext, operation: JobberOperationKind) {
        self.id = id
        self.auth = auth
        self.operation = operation
    }

    func isValid(currentAuth: CallAuthContext, activeLeaseId: UUID?) -> Bool {
        auth == currentAuth && currentAuth.isValid && id == activeLeaseId
    }
}

private final class JobberOpenerResolver: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<JobberManagementModel.OpenerOutcome, Never>?
    private var outcome: JobberManagementModel.OpenerOutcome?
    private var openerTask: Task<Void, Never>?
    private var timeoutTask: Task<Void, Never>?
    private var isResolved = false

    func registerTasks(opener: Task<Void, Never>, timeout: Task<Void, Never>) {
        lock.lock()
        self.openerTask = opener
        self.timeoutTask = timeout
        let shouldCancel = isResolved
        lock.unlock()

        if shouldCancel {
            opener.cancel()
            timeout.cancel()
        }
    }

    func resolve(_ result: JobberManagementModel.OpenerOutcome) {
        lock.lock()
        guard !isResolved else {
            lock.unlock()
            return
        }
        isResolved = true
        self.outcome = result
        let cont = self.continuation
        self.continuation = nil
        let opTask = self.openerTask
        let tmTask = self.timeoutTask
        self.openerTask = nil
        self.timeoutTask = nil
        lock.unlock()

        opTask?.cancel()
        tmTask?.cancel()
        cont?.resume(returning: result)
    }

    func setContinuation(_ cont: CheckedContinuation<JobberManagementModel.OpenerOutcome, Never>) {
        lock.lock()
        if isResolved, let outcome = self.outcome {
            lock.unlock()
            cont.resume(returning: outcome)
        } else {
            self.continuation = cont
            lock.unlock()
        }
    }

    func cancel() {
        resolve(.cancelled)
    }
}

@MainActor
final class JobberManagementModel: ObservableObject {
    typealias LiveAuthProvider = @MainActor () -> CallAuthContext?
    typealias BrowserOpener = @MainActor (URL) async throws -> Bool

    fileprivate enum OpenerOutcome: Sendable {
        case completed(Bool)
        case threw(Error)
        case timedOut
        case cancelled
    }

    @Published private(set) var connectionState: JobberConnectionState = .unknown
    @Published private(set) var isBusy: Bool = false
    @Published private(set) var activeOperation: JobberOperationKind? = nil
    @Published private(set) var unconfirmedRevocationNotice: String? = nil
    @Published private(set) var statusErrorMessage: String? = nil
    @Published private(set) var browserPromptMessage: String? = nil
    @Published private(set) var lastConnectedAt: Double? = nil
    @Published private(set) var activeLease: JobberOperationLease? = nil

    private let currentAuthProvider: LiveAuthProvider
    private var currentBoundAuth: CallAuthContext?
    private var pendingDeferredStatusAuth: CallAuthContext?

    static let fixedUnconfirmedRevocationNotice = String(
        localized: "Jobber access was removed from Kevin, but could not be confirmed with Jobber. Please check your Installed Apps in Jobber to ensure access is removed."
    )
    static let fixedStatusErrorMessage = String(
        localized: "Couldn't refresh Jobber status. Please check your connection and try again."
    )
    static let fixedConnectErrorMessage = String(
        localized: "Couldn't start Jobber connection. Please try again."
    )
    static let fixedDisconnectErrorMessage = String(
        localized: "Couldn't disconnect Jobber. Tap Refresh to check current status."
    )
    static let fixedBrowserPromptMessage = String(
        localized: "Finish connecting in your browser, then return to Kevin and tap Refresh."
    )

    init(
        currentAuthProvider: @escaping LiveAuthProvider = {
            let appState = AppState.shared
            guard appState.sessionState == .ready, appState.isOnboarded, !appState.isPersonalMode else { return nil }
            let auth = appState.currentAuthContext()
            return auth.isValid ? auth : nil
        }
    ) {
        self.currentAuthProvider = currentAuthProvider
        if let live = currentAuthProvider(), live.isValid {
            self.currentBoundAuth = live
        }
    }

    private func resetBoundState(newAuth: CallAuthContext?) {
        activeLease = nil
        pendingDeferredStatusAuth = nil
        isBusy = false
        activeOperation = nil
        connectionState = .unknown
        unconfirmedRevocationNotice = nil
        statusErrorMessage = nil
        browserPromptMessage = nil
        lastConnectedAt = nil
        currentBoundAuth = (newAuth?.isValid == true) ? newAuth : nil
    }

    func handleAuthChange(newAuth: CallAuthContext) {
        let live = currentAuthProvider()
        let normalizedLive = (live?.isValid == true) ? live : nil
        if normalizedLive != currentBoundAuth {
            resetBoundState(newAuth: normalizedLive)
        }
    }

    func refreshStatus(
        auth: CallAuthContext,
        client: JobberAPIClientProtocol? = nil
    ) async {
        let live = currentAuthProvider()
        let normalizedLive = (live?.isValid == true) ? live : nil
        if normalizedLive != currentBoundAuth {
            resetBoundState(newAuth: normalizedLive)
        }
        guard let currentLive = normalizedLive, currentLive == auth else {
            return
        }

        if isBusy {
            pendingDeferredStatusAuth = auth
            return
        }

        let lease = JobberOperationLease(auth: currentLive, operation: .refreshStatus)
        activeLease = lease
        isBusy = true
        activeOperation = .refreshStatus
        statusErrorMessage = nil
        browserPromptMessage = nil

        let effectiveClient = client ?? APIClient.shared

        do {
            let payload = try await effectiveClient.getJobberStatus(auth: auth)
            let liveOnCompletion = currentAuthProvider()
            let normalizedLiveOnCompletion = (liveOnCompletion?.isValid == true) ? liveOnCompletion : nil
            if normalizedLiveOnCompletion != currentBoundAuth {
                resetBoundState(newAuth: normalizedLiveOnCompletion)
            }
            if let currentLiveOnCompletion = normalizedLiveOnCompletion,
               currentLiveOnCompletion == lease.auth,
               activeLease?.id == lease.id {
                connectionState = payload.connected
                    ? .connected(leadCaptureEnabled: payload.leadCaptureEnabled)
                    : .notReady
                lastConnectedAt = payload.connectedAt
                statusErrorMessage = nil
            }
        } catch {
            let liveOnCompletion = currentAuthProvider()
            let normalizedLiveOnCompletion = (liveOnCompletion?.isValid == true) ? liveOnCompletion : nil
            if normalizedLiveOnCompletion != currentBoundAuth {
                resetBoundState(newAuth: normalizedLiveOnCompletion)
            }
            if let currentLiveOnCompletion = normalizedLiveOnCompletion,
               currentLiveOnCompletion == lease.auth,
               activeLease?.id == lease.id {
                statusErrorMessage = Self.fixedStatusErrorMessage
            }
        }

        if activeLease?.id == lease.id {
            activeLease = nil
            isBusy = false
            activeOperation = nil
            drainDeferredStatusIfNeeded(client: effectiveClient)
        }
    }

    func connect(
        auth: CallAuthContext,
        client: JobberAPIClientProtocol? = nil,
        timeout: TimeInterval = 10.0,
        openURL: @escaping BrowserOpener
    ) async {
        let live = currentAuthProvider()
        let normalizedLive = (live?.isValid == true) ? live : nil
        if normalizedLive != currentBoundAuth {
            resetBoundState(newAuth: normalizedLive)
        }
        guard let currentLive = normalizedLive, currentLive == auth else {
            return
        }
        guard !isBusy else { return }

        let lease = JobberOperationLease(auth: currentLive, operation: .connect)
        activeLease = lease
        isBusy = true
        activeOperation = .connect
        statusErrorMessage = nil
        browserPromptMessage = nil

        let effectiveClient = client ?? APIClient.shared

        do {
            let authorizeURL = try await effectiveClient.getJobberConnectURL(auth: auth)

            let liveBeforeOpen = currentAuthProvider()
            let normalizedLiveBeforeOpen = (liveBeforeOpen?.isValid == true) ? liveBeforeOpen : nil
            if normalizedLiveBeforeOpen != currentBoundAuth {
                resetBoundState(newAuth: normalizedLiveBeforeOpen)
            }
            guard let currentLiveBeforeOpen = normalizedLiveBeforeOpen,
                  currentLiveBeforeOpen == lease.auth,
                  activeLease?.id == lease.id else {
                if activeLease?.id == lease.id {
                    activeLease = nil
                    isBusy = false
                    activeOperation = nil
                    drainDeferredStatusIfNeeded(client: effectiveClient)
                }
                return
            }

            guard !Task.isCancelled else {
                if activeLease?.id == lease.id {
                    statusErrorMessage = Self.fixedConnectErrorMessage
                    browserPromptMessage = nil
                    activeLease = nil
                    isBusy = false
                    activeOperation = nil
                    drainDeferredStatusIfNeeded(client: effectiveClient)
                }
                return
            }

            let resolver = JobberOpenerResolver()

            let openerTask = Task { @MainActor in
                guard !Task.isCancelled else {
                    resolver.resolve(.cancelled)
                    return
                }
                do {
                    let success = try await openURL(authorizeURL)
                    resolver.resolve(.completed(success))
                } catch {
                    resolver.resolve(.threw(error))
                }
            }

            let timeoutNanos = UInt64(max(0, timeout) * 1_000_000_000)
            let timeoutTask = Task {
                try? await Task.sleep(nanoseconds: timeoutNanos)
                resolver.resolve(.timedOut)
            }

            resolver.registerTasks(opener: openerTask, timeout: timeoutTask)

            let outcome: OpenerOutcome = await withTaskCancellationHandler {
                await withCheckedContinuation { continuation in
                    resolver.setContinuation(continuation)
                }
            } onCancel: {
                resolver.cancel()
            }

            let liveAfterOpen = currentAuthProvider()
            let normalizedLiveAfterOpen = (liveAfterOpen?.isValid == true) ? liveAfterOpen : nil
            if normalizedLiveAfterOpen != currentBoundAuth {
                resetBoundState(newAuth: normalizedLiveAfterOpen)
            }
            guard let currentLiveAfterOpen = normalizedLiveAfterOpen,
                  currentLiveAfterOpen == lease.auth,
                  activeLease?.id == lease.id else {
                if activeLease?.id == lease.id {
                    activeLease = nil
                    isBusy = false
                    activeOperation = nil
                    drainDeferredStatusIfNeeded(client: effectiveClient)
                }
                return
            }

            switch outcome {
            case .completed(let success):
                if success && !Task.isCancelled {
                    browserPromptMessage = Self.fixedBrowserPromptMessage
                    statusErrorMessage = nil
                } else {
                    statusErrorMessage = Self.fixedConnectErrorMessage
                    browserPromptMessage = nil
                }
            case .threw, .timedOut, .cancelled:
                statusErrorMessage = Self.fixedConnectErrorMessage
                browserPromptMessage = nil
            }
        } catch {
            let liveAfterError = currentAuthProvider()
            let normalizedLiveAfterError = (liveAfterError?.isValid == true) ? liveAfterError : nil
            if normalizedLiveAfterError != currentBoundAuth {
                resetBoundState(newAuth: normalizedLiveAfterError)
            }
            if let currentLiveAfterError = normalizedLiveAfterError,
               currentLiveAfterError == lease.auth,
               activeLease?.id == lease.id {
                statusErrorMessage = Self.fixedConnectErrorMessage
                browserPromptMessage = nil
            }
        }

        if activeLease?.id == lease.id {
            activeLease = nil
            isBusy = false
            activeOperation = nil
            drainDeferredStatusIfNeeded(client: effectiveClient)
        }
    }

    func disconnect(
        auth: CallAuthContext,
        client: JobberAPIClientProtocol? = nil
    ) async {
        let live = currentAuthProvider()
        let normalizedLive = (live?.isValid == true) ? live : nil
        if normalizedLive != currentBoundAuth {
            resetBoundState(newAuth: normalizedLive)
        }
        guard let currentLive = normalizedLive, currentLive == auth else {
            return
        }
        guard !isBusy else { return }

        let lease = JobberOperationLease(auth: currentLive, operation: .disconnect)
        activeLease = lease
        isBusy = true
        activeOperation = .disconnect
        statusErrorMessage = nil
        browserPromptMessage = nil

        let effectiveClient = client ?? APIClient.shared

        do {
            let payload = try await effectiveClient.disconnectJobber(auth: auth)
            let liveOnCompletion = currentAuthProvider()
            let normalizedLiveOnCompletion = (liveOnCompletion?.isValid == true) ? liveOnCompletion : nil
            if normalizedLiveOnCompletion != currentBoundAuth {
                resetBoundState(newAuth: normalizedLiveOnCompletion)
            }
            if let currentLiveOnCompletion = normalizedLiveOnCompletion,
               currentLiveOnCompletion == lease.auth,
               activeLease?.id == lease.id {
                connectionState = .notReady
                lastConnectedAt = nil
                if payload.isProviderConfirmed {
                    unconfirmedRevocationNotice = nil
                } else {
                    unconfirmedRevocationNotice = Self.fixedUnconfirmedRevocationNotice
                }
                statusErrorMessage = nil
            }
        } catch {
            let liveOnCompletion = currentAuthProvider()
            let normalizedLiveOnCompletion = (liveOnCompletion?.isValid == true) ? liveOnCompletion : nil
            if normalizedLiveOnCompletion != currentBoundAuth {
                resetBoundState(newAuth: normalizedLiveOnCompletion)
            }
            if let currentLiveOnCompletion = normalizedLiveOnCompletion,
               currentLiveOnCompletion == lease.auth,
               activeLease?.id == lease.id {
                statusErrorMessage = Self.fixedDisconnectErrorMessage
            }
        }

        if activeLease?.id == lease.id {
            activeLease = nil
            isBusy = false
            activeOperation = nil
            drainDeferredStatusIfNeeded(client: effectiveClient)
        }
    }

    private func drainDeferredStatusIfNeeded(client: JobberAPIClientProtocol) {
        guard let deferredAuth = pendingDeferredStatusAuth else { return }
        pendingDeferredStatusAuth = nil

        let live = currentAuthProvider()
        let normalizedLive = (live?.isValid == true) ? live : nil
        if normalizedLive != currentBoundAuth {
            resetBoundState(newAuth: normalizedLive)
        }
        guard let currentLive = normalizedLive, currentLive == deferredAuth else {
            return
        }

        Task { [weak self] in
            await self?.refreshStatus(auth: deferredAuth, client: client)
        }
    }

    func clearStatusError() {
        statusErrorMessage = nil
    }

    func clearBrowserPrompt() {
        browserPromptMessage = nil
    }
}
