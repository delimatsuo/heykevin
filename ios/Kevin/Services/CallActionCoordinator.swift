import Foundation
import Combine

extension Notification.Name {
    static let callSessionEpochDidChange = Notification.Name("CallSessionEpochDidChangeNotification")
}

/// Invalidates an authorization lease even when credentials change A -> B -> A.
final class CallSessionEpoch: @unchecked Sendable {
    static let shared = CallSessionEpoch()
    static let didChangeNotification = Notification.Name.callSessionEpochDidChange
    private let lock = NSRecursiveLock()
    private var value = 0
    var generation: Int { synchronized { value } }
    @discardableResult func advance() -> Int {
        let newGen = synchronized {
            value += 1
            return value
        }
        DispatchQueue.main.async {
            NotificationCenter.default.post(name: Self.didChangeNotification, object: nil)
        }
        return newGen
    }
    func credentialChanged(from old: String, to new: String) { if old != new { advance() } }
    func synchronized<T>(_ body: () -> T) -> T {
        lock.lock(); defer { lock.unlock() }; return body()
    }
}

struct CallAuthContext: Hashable, Equatable, Sendable {
    let contractorId: String
    let bearerToken: String
    let generation: Int
    var isValid: Bool { !contractorId.isEmpty && !bearerToken.isEmpty }
    func matches(contractorId: String, bearerToken: String, generation: Int) -> Bool {
        self == CallAuthContext(contractorId: contractorId, bearerToken: bearerToken, generation: generation)
    }
}

struct CallLifecycleSnapshot: Equatable, Sendable {
    let callSid: String
    let revision: Int
    func permits(_ sid: String) -> Bool { callSid.isEmpty || callSid == sid }
}

/// The sheet belongs to the call that was presented, not whatever becomes active later.
struct CallPresentationLease: Equatable {
    let auth: CallAuthContext
    let scope: CallLifecycleSnapshot
    func isValid(auth currentAuth: CallAuthContext, scope currentScope: CallLifecycleSnapshot) -> Bool {
        auth == currentAuth && currentAuth.isValid && scope == currentScope && !scope.callSid.isEmpty
    }
    func mayClear(auth currentAuth: CallAuthContext, scope currentScope: CallLifecycleSnapshot) -> Bool {
        auth == currentAuth && scope == currentScope && !scope.callSid.isEmpty
    }
}

/// Shared production/test fence: a save invalidates all earlier profile reads.
struct PreferenceWriteFence {
    private(set) var revision = 0
    private(set) var pending: UUID?
    mutating func beginSave() -> UUID? {
        guard pending == nil else { return nil }
        revision += 1
        let id = UUID(); pending = id; return id
    }
    func permitsLoad(_ capturedRevision: Int) -> Bool { revision == capturedRevision && pending == nil }
    mutating func finish(_ id: UUID) -> Bool {
        guard pending == id else { return false }
        pending = nil; return true
    }
}

enum CallActionState: Equatable, Sendable {
    case idle, accepting(operationId: String), accepted(accessToken: String, conferenceName: String), directAccepted
    case messageRequested(operationId: String), takingMessage, ended, error(String)
    case unresolved(action: String, operationId: String)
}

struct CallKitOwnershipValidator {
    static func shouldAcceptRing(payloadContractorId: String, activeContractorId: String, deadline: Date, now: Date = Date()) -> Bool {
        !activeContractorId.isEmpty && (payloadContractorId.isEmpty || payloadContractorId == activeContractorId) && now < deadline
    }
    static func calculateDeadline(expiresAt: Double, arrivalTime: Date, maxWaitSeconds: TimeInterval = 30) -> Date {
        let cap = arrivalTime.addingTimeInterval(maxWaitSeconds)
        return expiresAt > 0 ? min(Date(timeIntervalSince1970: expiresAt), cap) : cap
    }
}

struct IncomingCallContext: Equatable {
    let uuid: UUID
    let callSid: String
    let auth: CallAuthContext
    let deadline: Date
    let accessToken: String
    let conferenceName: String
    let callerName: String
    let callerPhone: String
    let isUrgent: Bool
    var hasPreissuedToken: Bool { !accessToken.isEmpty && !conferenceName.isEmpty && !isUrgent }
}

enum IncomingPickupDisposition: Equatable {
    case screened
    case preissued(IncomingCallContext)
    case unavailable
}

/// Tombstones stop a late accepted urgent result becoming a new outgoing call.
struct IncomingCallOwnership {
    private(set) var contexts: [UUID: IncomingCallContext] = [:]
    private(set) var seenCallSids: Set<String> = []
    private(set) var activeUUID: UUID?
    mutating func adopt(_ context: IncomingCallContext, currentAuth: CallAuthContext, now: Date) -> Bool {
        guard activeUUID == nil, context.auth == currentAuth, currentAuth.isValid,
              now < context.deadline, !context.callSid.isEmpty,
              !seenCallSids.contains(context.callSid), context.isUrgent || context.hasPreissuedToken else { return false }
        contexts[context.uuid] = context
        seenCallSids.insert(context.callSid)
        activeUUID = context.uuid
        return true
    }
    func usable(_ uuid: UUID, auth: CallAuthContext, now: Date) -> IncomingCallContext? {
        guard activeUUID == uuid, let context = contexts[uuid], context.auth == auth,
              auth.isValid, now < context.deadline else { return nil }
        return context
    }
    func pickupDisposition(callSid: String, auth: CallAuthContext, now: Date) -> IncomingPickupDisposition {
        guard seenCallSids.contains(callSid) else { return .screened }
        guard let uuid = activeUUID, let context = usable(uuid, auth: auth, now: now), context.callSid == callSid else { return .unavailable }
        return context.hasPreissuedToken ? .preissued(context) : .screened
    }
    func preissuedContext(callSid: String, auth: CallAuthContext, now: Date) -> IncomingCallContext? {
        guard let uuid = activeUUID, let context = usable(uuid, auth: auth, now: now),
              context.callSid == callSid, context.hasPreissuedToken else { return nil }
        return context
    }
    @discardableResult mutating func remove(_ uuid: UUID) -> Bool {
        contexts.removeValue(forKey: uuid)
        guard activeUUID == uuid else { return false }
        activeUUID = nil; return true
    }
}

@MainActor
final class CallActionCoordinator: ObservableObject {
    static let shared = CallActionCoordinator()
    typealias SendActionHandler = @MainActor (CallAuthContext, String, String, String, String) async throws -> CallActionResult
    typealias GetStatusHandler = @MainActor (CallAuthContext, String, String) async throws -> CallActionResult?
    typealias ConnectHandler = @MainActor (CallAuthContext, String, String, String) -> Bool

    @Published private(set) var currentStates: [String: CallActionState] = [:]
    @Published private(set) var urgentStatus: [String: Bool] = [:]
    @Published private(set) var isReconciling: [String: Bool] = [:]
    @Published private(set) var errors: [String: String] = [:]
    @Published private var serverBlocked: Set<String> = []
    @Published private var serverTakingMessage: Set<String> = []
    private let sendAction: SendActionHandler
    private let getStatus: GetStatusHandler
    private let connect: ConnectHandler
    private let directAnswer: @MainActor (CallAuthContext, String) -> Bool?
    private let currentAuth: @MainActor () -> CallAuthContext
    private let currentCall: @MainActor () -> CallLifecycleSnapshot
    private let onMessage: @MainActor (String) -> Void
    private let pollAttempts: Int
    private var observedAuth: CallAuthContext?
    private var navigationRequest = 0

    private final class Operation {
        let id = UUID().uuidString
        let auth: CallAuthContext
        let scope: CallLifecycleSnapshot
        let sid: String
        let action: String
        var task: Task<Bool, Never>?
        var complete = false
        var direct = false
        var retryable = false
        init(auth: CallAuthContext, scope: CallLifecycleSnapshot, sid: String, action: String) {
            self.auth = auth; self.scope = scope; self.sid = sid; self.action = action
        }
    }
    private var operations: [String: Operation] = [:]

    init(sendAction: SendActionHandler? = nil, getStatus: GetStatusHandler? = nil,
         connect: ConnectHandler? = nil,
         directAnswer: (@MainActor (CallAuthContext, String) -> Bool?)? = nil,
         currentAuth: (@MainActor () -> CallAuthContext)? = nil,
         currentCall: (@MainActor () -> CallLifecycleSnapshot)? = nil,
         onMessage: (@MainActor (String) -> Void)? = nil, pollAttempts: Int = 3) {
        self.directAnswer = directAnswer ?? { auth, sid in CallManager.shared.answerPreissuedIfPresent(auth: auth, callSid: sid) }
        self.currentAuth = currentAuth ?? { AppState.shared.currentAuthContext() }
        self.currentCall = currentCall ?? { AppState.shared.callLifecycleSnapshot }
        self.sendAction = sendAction ?? { auth, sid, action, op, message in
            try await APIClient.shared.sendCallAction(callSid: sid, action: action, operationId: op,
                message: message, contractorId: auth.contractorId, bearerToken: auth.bearerToken, sessionGeneration: auth.generation)
        }
        self.getStatus = getStatus ?? { auth, sid, op in
            try await APIClient.shared.getCallAction(callSid: sid, operationId: op,
                contractorId: auth.contractorId, bearerToken: auth.bearerToken, sessionGeneration: auth.generation)
        }
        self.connect = connect ?? { auth, sid, token, conference in
            CallManager.shared.connectAcceptedCall(auth: auth, callSid: sid, accessToken: token, conferenceName: conference)
        }
        self.onMessage = onMessage ?? { sid in
            if AppState.shared.activeCallSid == sid { AppState.shared.callIgnored = true }
            CallManager.shared.finishMessageRing(callSid: sid)
        }
        self.pollAttempts = max(1, pollAttempts)
    }

    private func synchronizeSession() {
        let auth = currentAuth()
        guard observedAuth != auth else { return }
        operations.values.forEach { $0.task?.cancel() }
        operations.removeAll(); currentStates.removeAll(); errors.removeAll()
        urgentStatus.removeAll(); isReconciling.removeAll(); serverBlocked.removeAll(); serverTakingMessage.removeAll(); observedAuth = auth
    }
    private func owns(_ op: Operation) -> Bool {
        operations[op.sid] === op && currentAuth() == op.auth && currentCall() == op.scope && op.scope.permits(op.sid) && !Task.isCancelled
    }
    func state(for sid: String) -> CallActionState { observedAuth == currentAuth() ? (currentStates[sid] ?? .idle) : .idle }
    func isAcceptPending(for sid: String) -> Bool {
        switch state(for: sid) { case .accepting, .accepted, .directAccepted, .unresolved(action: "accept", operationId: _): return true; default: return false }
    }
    func isDeclinePending(for sid: String) -> Bool {
        switch state(for: sid) { case .messageRequested, .unresolved(action: "decline", operationId: _): return true; default: return false }
    }
    func isActionPending(for sid: String) -> Bool {
        observedAuth == currentAuth() && (isAcceptPending(for: sid) || isDeclinePending(for: sid) || serverBlocked.contains(sid))
    }
    func canCheckStatus(for sid: String) -> Bool {
        guard observedAuth == currentAuth(), let op = operations[sid] else { return false }
        return !op.complete && op.task == nil && owns(op)
    }
    func isTakingMessage(for sid: String) -> Bool { observedAuth == currentAuth() && (state(for: sid) == .takingMessage || serverTakingMessage.contains(sid)) }
    func isUrgent(for sid: String) -> Bool { observedAuth == currentAuth() && (urgentStatus[sid] ?? false) }
    func errorMessage(for sid: String) -> String? { observedAuth == currentAuth() ? errors[sid] : nil }
    func setUrgent(_ value: Bool, for sid: String) { synchronizeSession(); urgentStatus[sid] = value }
    func observeStatus(_ status: CallActionResult, auth: CallAuthContext) {
        synchronizeSession()
        guard currentAuth() == auth, currentCall().callSid == status.callSid,
              status.validNavigation(callSid: status.callSid, contractorId: auth.contractorId) else { return }
        urgentStatus[status.callSid] = status.isUrgent
        let terminalDirectFailure: Bool
        if let op = operations[status.callSid], op.direct && op.complete,
           case .error = currentStates[status.callSid] { terminalDirectFailure = true }
        else { terminalDirectFailure = false }
        if status.actionStatus == "ready" && !terminalDirectFailure { serverBlocked.remove(status.callSid) }
        else { serverBlocked.insert(status.callSid) }
        if status.actionStatus == "taking_message" {
            serverTakingMessage.insert(status.callSid)
            onMessage(status.callSid)
        }
        // This projection is not reconciliation of our retained operation.
    }
    func reportConnectionFailure(callSid sid: String, auth: CallAuthContext) {
        guard let op = operations[sid], op.auth == auth, owns(op), op.action == "accept" else { return }
        // The backend may already have accepted. Retain its operation and only
        // reconcile through an explicit GET; no replacement POST is safe.
        op.complete = op.direct
        currentStates[sid] = op.direct ? .error("The direct call could not connect.") : .unresolved(action: op.action, operationId: op.id)
        if op.direct {
            serverBlocked.insert(sid)
            errors[sid] = "The direct call could not connect. This incoming call is no longer answerable."
        } else {
            errors[sid] = "The call could not connect. Check status before trying again."
        }
    }
    func resetState(for sid: String) {
        // Keep unresolved operation identity. Resetting presentation cannot authorize a second POST.
        if let op = operations[sid], !op.complete { return }
        operations.removeValue(forKey: sid); currentStates.removeValue(forKey: sid)
    }
    func pickUp(callSid: String, callerName: String = "", callerPhone: String = "", authContext: CallAuthContext? = nil) async -> Bool {
        await perform(sid: callSid, action: "accept", auth: authContext)
    }
    func takeMessage(callSid: String, authContext: CallAuthContext? = nil) async -> Bool {
        await perform(sid: callSid, action: "decline", auth: authContext)
    }
    private func perform(sid: String, action: String, auth supplied: CallAuthContext?) async -> Bool {
        synchronizeSession()
        let auth = supplied ?? currentAuth(), scope = currentCall()
        guard auth.isValid, auth == currentAuth(), !sid.isEmpty, scope.permits(sid) else { return false }
        if let existing = operations[sid] {
            guard owns(existing) else { return false }
            if existing.complete && existing.retryable {
                operations.removeValue(forKey: sid); errors.removeValue(forKey: sid)
                return await perform(sid: sid, action: action, auth: auth)
            }
            guard existing.action == action else { return false }
            if let task = existing.task { return await task.value }
            if existing.complete { return currentStates[sid] == .takingMessage || currentStates[sid] == .directAccepted || { if case .accepted = currentStates[sid] { return true }; return false }() }
            return await checkStatus(callSid: sid)
        }
        let op = Operation(auth: auth, scope: scope, sid: sid, action: action)
        operations[sid] = op
        currentStates[sid] = action == "accept" ? .accepting(operationId: op.id) : .messageRequested(operationId: op.id)
        if action == "accept", let direct = directAnswer(auth, sid) {
            op.direct = true; op.complete = true
            currentStates[sid] = direct ? .directAccepted : .ended
            if !direct { serverBlocked.insert(sid); errors[sid] = "This incoming call is no longer answerable." }
            return direct
        }
        let task = Task { @MainActor in
            defer { if self.operations[sid] === op { op.task = nil } }
            do {
                let result = try await self.sendAction(auth, sid, action, op.id, "")
                guard self.owns(op) else { return false }
                if let resolved = self.consume(result, op: op, allowPreparationRetry: true) { return resolved }
            } catch { guard self.owns(op) else { return false } }
            return await self.reconcile(op)
        }
        op.task = task
        return await task.value
    }
    func checkStatus(callSid sid: String) async -> Bool {
        synchronizeSession()
        guard let op = operations[sid], owns(op), !op.complete else { return false }
        if let task = op.task { return await task.value }
        let task = Task { @MainActor in
            defer { if self.operations[sid] === op { op.task = nil } }
            return await self.reconcile(op)
        }
        op.task = task
        return await task.value
    }
    /// nil means unknown: retain the operation and reconcile by GET, never another POST.
    private func consume(_ result: CallActionResult, op: Operation, allowPreparationRetry: Bool = false) -> Bool? {
        guard owns(op) else { return false }
        guard result.matchesAction(callSid: op.sid, contractorId: op.auth.contractorId, operationId: op.id, action: op.action) else { return nil }
        if result.isPreparationFailure && allowPreparationRetry {
            op.complete = true; op.retryable = true
            errors[op.sid] = "The call could not be prepared. Try again."
            currentStates[op.sid] = .error(errors[op.sid]!); return false
        }
        if result.isAccepted {
            guard owns(op), connect(op.auth, op.sid, result.accessToken!, result.conferenceName!) else {
                errors[op.sid] = "Call is no longer available to connect."
                currentStates[op.sid] = .ended; op.complete = true; return false
            }
            currentStates[op.sid] = .accepted(accessToken: result.accessToken!, conferenceName: result.conferenceName!)
            errors.removeValue(forKey: op.sid); op.complete = true; return true
        }
        if result.isTakingMessage {
            guard owns(op) else { return false }
            currentStates[op.sid] = .takingMessage; errors.removeValue(forKey: op.sid); op.complete = true
            onMessage(op.sid); return true
        }
        if result.isEnded {
            serverBlocked.insert(op.sid); errors[op.sid] = "This call has ended."
            currentStates[op.sid] = .ended; op.complete = true; return false
        }
        if result.isConflict {
            serverBlocked.insert(op.sid)
            errors[op.sid] = "Another action owns this call. Open the call to review its status."
            currentStates[op.sid] = .error(errors[op.sid]!); op.complete = true; return false
        }
        return nil
    }
    private func reconcile(_ op: Operation) async -> Bool {
        guard owns(op) else { return false }
        isReconciling[op.sid] = true
        defer { if operations[op.sid] === op { isReconciling[op.sid] = false } }
        for _ in 0..<pollAttempts {
            guard owns(op) else { return false }
            do {
                let status = try await getStatus(op.auth, op.sid, op.id)
                guard owns(op) else { return false }
                if let status, let resolved = consume(status, op: op) { return resolved }
            } catch { guard owns(op) else { return false } }
        }
        guard owns(op) else { return false }
        currentStates[op.sid] = .unresolved(action: op.action, operationId: op.id)
        errors[op.sid] = "Outcome not confirmed. Check status before choosing another action."
        return false
    }

    @discardableResult
    func validateAndNavigate(callSid: String, fallbackCallerName: String = "", fallbackCallerPhone: String = "", authContext: CallAuthContext? = nil) async -> Bool {
        synchronizeSession()
        let auth = authContext ?? currentAuth(), scope = currentCall()
        guard auth.isValid, auth == currentAuth(), !callSid.isEmpty else { return false }
        navigationRequest += 1; let request = navigationRequest
        var result: CallActionResult?
        do { result = try await getStatus(auth, callSid, "") } catch { }
        guard currentAuth() == auth, navigationRequest == request else { return false }
        let sameScope = currentCall() == scope && scope.permits(callSid)
        if let status = result, status.validNavigation(callSid: callSid, contractorId: auth.contractorId) {
            if status.isActive && sameScope {
                AppState.shared.setActiveCall(callSid: callSid, callerPhone: status.callerPhone ?? "", callerName: status.callerName ?? "", authContext: auth)
                observeStatus(status, auth: auth)
                if let transcript = status.transcript { AppState.shared.updateActiveCallTranscript(text: transcript, authContext: auth, callSid: callSid) }
                AppState.shared.showActiveCall = true; AppState.shared.selectedTab = .live
                return true
            }
            if status.isEnded && sameScope && AppState.shared.activeCallSid == callSid { AppState.shared.clearActiveCall() }
        }
        // A transport failure is unknown, not evidence of a finished call.
        AppState.shared.notificationCallSid = callSid
        AppState.shared.notificationCallMessage = result?.isEnded == true ? "" : "Live status unavailable. The call may still be active."
        AppState.shared.selectedTab = .recents
        return false
    }
}
