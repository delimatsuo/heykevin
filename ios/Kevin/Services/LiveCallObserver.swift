import Foundation
import Combine

/// Root-owned single observer coordinating active call discovery and status polling.
/// Replaces view-owned timers and prevents duplicate network requests across tabs/sheets.
@MainActor
final class LiveCallObserver: ObservableObject {
    static let shared = LiveCallObserver()

    typealias AuthProvider = @MainActor () -> CallAuthContext
    typealias ScopeProvider = @MainActor () -> CallLifecycleSnapshot
    typealias GetActiveCallHandler = (CallAuthContext) async -> ActiveCallInfo?
    typealias GetCallActionHandler = (String, CallAuthContext) async throws -> CallActionResult?
    typealias ApplyActiveCallEffect = @MainActor (ActiveCallInfo, CallAuthContext) -> Void
    typealias ApplyStatusEffect = @MainActor (CallActionResult, CallAuthContext) -> Void
    typealias ClearActiveCallEffect = @MainActor (String, CallAuthContext) -> Void

    private let authProvider: AuthProvider
    private let scopeProvider: ScopeProvider
    private let getActiveCallHandler: GetActiveCallHandler
    private let getCallActionHandler: GetCallActionHandler
    private let applyActiveCallEffect: ApplyActiveCallEffect
    private let applyStatusEffect: ApplyStatusEffect
    private let clearActiveCallEffect: ClearActiveCallEffect
    private let autoSchedule: Bool

    @Published private(set) var isRunning: Bool = false
    @Published private(set) var isInFlight: Bool = false
    @Published private(set) var lastPollDate: Date? = nil

    private var pollTimer: Timer?
    private var currentRequestRevision: Int = 0
    private var activeFlightToken: Int = 0
    private var flightCounter: Int = 0
    private var isPendingRefresh: Bool = false

    init(
        authProvider: @escaping AuthProvider = { AppState.shared.currentAuthContext() },
        scopeProvider: @escaping ScopeProvider = { AppState.shared.callLifecycleSnapshot },
        getActiveCall: @escaping GetActiveCallHandler = { auth in await APIClient.shared.getActiveCall(authContext: auth) },
        getCallAction: @escaping GetCallActionHandler = { sid, auth in
            try await APIClient.shared.getCallAction(
                callSid: sid,
                contractorId: auth.contractorId,
                bearerToken: auth.bearerToken,
                sessionGeneration: auth.generation
            )
        },
        applyActiveCall: @escaping ApplyActiveCallEffect = { info, auth in
            guard AppState.shared.currentAuthContext() == auth else { return }
            AppState.shared.setActiveCall(callSid: info.callSid, callerPhone: info.callerPhone, callerName: info.callerName, authContext: auth)
            AppState.shared.updateActiveCallTranscript(text: info.transcript, authContext: auth, callSid: info.callSid)
            AppState.shared.showActiveCall = true
        },
        applyStatus: @escaping ApplyStatusEffect = { status, auth in
            guard AppState.shared.currentAuthContext() == auth else { return }
            CallActionCoordinator.shared.observeStatus(status, auth: auth)
            if let text = status.transcript {
                AppState.shared.updateActiveCallTranscript(text: text, authContext: auth, callSid: status.callSid)
            }
            AppState.shared.updateActiveCallReason(reason: status.screeningReason, authContext: auth, callSid: status.callSid)
        },
        clearActiveCall: @escaping ClearActiveCallEffect = { sid, auth in
            guard AppState.shared.currentAuthContext() == auth else { return }
            if AppState.shared.activeCallSid == sid {
                AppState.shared.clearActiveCall()
            }
        },
        autoSchedule: Bool = true
    ) {
        self.authProvider = authProvider
        self.scopeProvider = scopeProvider
        self.getActiveCallHandler = getActiveCall
        self.getCallActionHandler = getCallAction
        self.applyActiveCallEffect = applyActiveCall
        self.applyStatusEffect = applyStatus
        self.clearActiveCallEffect = clearActiveCall
        self.autoSchedule = autoSchedule
    }

    #if DEBUG
    private var isScreenshotFixture: Bool {
        AppStoreScreenshotFixtures.isEnabled
    }
    #else
    private var isScreenshotFixture: Bool { false }
    #endif

    // MARK: - Lifecycle Control

    func start() {
        guard !isScreenshotFixture else { return }
        guard !isRunning else { return }
        isRunning = true
        isPendingRefresh = false
        schedulePollTimer()
        requestRefresh()
    }

    func stop() {
        isRunning = false
        isPendingRefresh = false
        currentRequestRevision += 1
        pollTimer?.invalidate()
        pollTimer = nil
    }

    func handleAuthChange() {
        currentRequestRevision += 1
        if isRunning {
            requestRefresh()
        }
    }

    func requestRefresh() {
        guard !isScreenshotFixture else { return }
        guard isRunning else { return }
        if isInFlight {
            isPendingRefresh = true
        } else {
            Task { @MainActor [weak self] in
                await self?.checkNow()
            }
        }
    }

    private func schedulePollTimer() {
        guard autoSchedule else { return }
        pollTimer?.invalidate()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self = self, self.isRunning else { return }
                let scope = self.scopeProvider()
                // Timer ticks ONLY poll nonempty active scope; never idle-discovery loops
                guard !scope.callSid.isEmpty else { return }
                await self.checkNow()
            }
        }
    }

    // MARK: - Coordinated Discovery and Polling

    @discardableResult
    func checkNow() async -> Bool {
        guard isRunning else { return false }
        guard !isScreenshotFixture else { return false }
        let requestAuth = authProvider()
        let requestScope = scopeProvider()
        guard requestAuth.isValid else { return false }
        guard !isInFlight else { return false }

        flightCounter += 1
        let thisFlightToken = flightCounter
        activeFlightToken = thisFlightToken
        isInFlight = true

        currentRequestRevision += 1
        let capturedRevision = currentRequestRevision
        defer {
            if activeFlightToken == thisFlightToken {
                isInFlight = false
                lastPollDate = Date()
                if isRunning && isPendingRefresh {
                    isPendingRefresh = false
                    Task { @MainActor [weak self] in
                        await self?.checkNow()
                    }
                }
            }
        }

        if !requestScope.callSid.isEmpty {
            // Polling known active call
            do {
                let statusResult = try await getCallActionHandler(requestScope.callSid, requestAuth)

                // GUARD: Check revision and auth/scope match
                guard capturedRevision == self.currentRequestRevision,
                      self.authProvider() == requestAuth,
                      self.scopeProvider() == requestScope else {
                    return false
                }

                if let status = statusResult, status.validNavigation(callSid: requestScope.callSid, contractorId: requestAuth.contractorId) {
                    if status.isEnded {
                        clearActiveCallEffect(requestScope.callSid, requestAuth)
                        return true
                    }
                    applyStatusEffect(status, requestAuth)
                    return true
                }
            } catch {
                // Network / transport errors: retain call on unknown status (fail safe)
                return false
            }
        } else {
            // Discovery of active call
            if let activeInfo = await getActiveCallHandler(requestAuth), !activeInfo.callSid.isEmpty {
                // GUARD: Check revision and auth/scope match
                guard capturedRevision == self.currentRequestRevision,
                      self.authProvider() == requestAuth,
                      self.scopeProvider() == requestScope else {
                    return false
                }

                applyActiveCallEffect(activeInfo, requestAuth)
                return true
            }
        }

        return false
    }
}
