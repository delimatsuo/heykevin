import Foundation
import CallKit
import PushKit
import AVFoundation
import TwilioVoice

class CallManager: NSObject, ObservableObject {
    static let shared = CallManager()
    private let callKitProvider: CXProvider
    private let callController = CXCallController()
    private let audioDevice = DefaultAudioDevice()
    private var activeCall: Call?
    private var activeCallUUID: UUID?
    private var ownership = IncomingCallOwnership()
    private var answeringUUID: UUID?
    private var pendingAccessToken: String?
    private var pendingConferenceName: String?
    private var connectionAuth: CallAuthContext?
    private var connectionSid = ""
    private var connectionScope: CallLifecycleSnapshot?

    private(set) var presentationLease: CallPresentationLease?
    @Published var isOnCall = false
    @Published var isMuted = false
    @Published var isSpeaker = false
    @Published var callerName = ""
    @Published var callerPhone = ""
    @Published var callStartTime: Date?

    override init() {
        let config = CXProviderConfiguration()
        config.maximumCallGroups = 1; config.maximumCallsPerCallGroup = 1
        config.supportsVideo = false; config.supportedHandleTypes = [.phoneNumber, .generic]
        callKitProvider = CXProvider(configuration: config)
        TwilioVoiceSDK.audioDevice = audioDevice
        super.init()
        callKitProvider.setDelegate(self, queue: .main)
    }

    func reportIncomingCall(uuid: UUID, callerPhone: String, callerName: String,
        accessToken: String = "", conferenceName: String = "", callSid: String = "",
        contractorId: String = "", expiresAt: Double = 0, isUrgent: Bool = false,
        completion: (() -> Void)? = nil) {
        let auth = AppState.shared.currentAuthContext()
        let deadline = CallKitOwnershipValidator.calculateDeadline(expiresAt: expiresAt, arrivalTime: Date())
        let context = IncomingCallContext(uuid: uuid, callSid: callSid, auth: auth, deadline: deadline,
            accessToken: accessToken, conferenceName: conferenceName, callerName: callerName,
            callerPhone: callerPhone, isUrgent: isUrgent)
        // Mandatory reporting does not imply adoption. Invalid or duplicate pushes
        // get their own promptly ended report; they never replace the live context.
        let ownerMatches = isUrgent ? (!contractorId.isEmpty && contractorId == auth.contractorId)
            : (contractorId.isEmpty || contractorId == auth.contractorId)
        let canAdopt = ownerMatches && activeCallUUID == nil && AppState.shared.callLifecycleSnapshot.permits(callSid)
            && ownership.adopt(context, currentAuth: auth, now: Date())
        if canAdopt { activeCallUUID = uuid }
        let update = CXCallUpdate()
        update.remoteHandle = CXHandle(type: .generic, value: "Kevin Call")
        update.localizedCallerName = isUrgent ? "Urgent call · Kevin" : (callerName.isEmpty ? callerPhone : callerName)
        update.hasVideo = false; update.supportsDTMF = true
        update.supportsGrouping = false; update.supportsUngrouping = false; update.supportsHolding = false
        callKitProvider.reportNewIncomingCall(with: uuid, update: update) { [weak self] error in
            DispatchQueue.main.async {
                defer { completion?() }
                guard let self else { return }
                guard error == nil, canAdopt, self.activeCallUUID == uuid,
                      self.ownership.usable(uuid, auth: AppState.shared.currentAuthContext(), now: Date()) != nil else {
                    self.callKitProvider.reportCall(with: uuid, endedAt: Date(), reason: .failed)
                    self.ownership.remove(uuid)
                    self.cleanup(uuid: uuid)
                    return
                }
                self.callerName = callerName; self.callerPhone = callerPhone
                AppState.shared.setActiveCall(callSid: callSid, callerPhone: callerPhone, callerName: callerName, authContext: auth)
                DispatchQueue.main.asyncAfter(deadline: .now() + max(0, deadline.timeIntervalSinceNow)) { [weak self] in
                    guard let self, self.activeCallUUID == uuid,
                          self.ownership.contexts[uuid] == context, self.activeCall == nil else { return }
                    self.callKitProvider.reportCall(with: uuid, endedAt: Date(), reason: .unanswered)
                    self.ownership.remove(uuid)
                    self.cleanup(uuid: uuid)
                    // Expiry is presentation only. The backend owns the waiting timeout.
                }
            }
        }
    }

    @MainActor
    func answerPreissuedIfPresent(auth: CallAuthContext, callSid: String) -> Bool? {
        let context: IncomingCallContext
        switch ownership.pickupDisposition(callSid: callSid, auth: auth, now: Date()) {
        case .screened: return nil
        case .unavailable: return false
        case .preissued(let existing): context = existing
        }
        guard auth == AppState.shared.currentAuthContext(), activeCallUUID == context.uuid else { return false }
        if connectionSid == callSid && pendingAccessToken != nil { return true }
        prepareConnection(auth: auth, sid: callSid, token: context.accessToken, conference: context.conferenceName)
        if answeringUUID == context.uuid { return connectToConference(uuid: context.uuid) }
        requestConnection(CXAnswerCallAction(call: context.uuid), uuid: context.uuid)
        return true
    }

    @MainActor
    func connectAcceptedCall(auth: CallAuthContext, callSid: String, accessToken: String, conferenceName: String) -> Bool {
        guard auth == AppState.shared.currentAuthContext(), auth.isValid,
              !accessToken.isEmpty, !conferenceName.isEmpty,
              AppState.shared.callLifecycleSnapshot.permits(callSid) else { return false }
        if ownership.seenCallSids.contains(callSid) {
            guard let uuid = activeCallUUID,
                  let context = ownership.usable(uuid, auth: auth, now: Date()), context.callSid == callSid else { return false }
            prepareConnection(auth: auth, sid: callSid, token: accessToken, conference: conferenceName)
            if answeringUUID == uuid {
                return connectToConference(uuid: uuid)
            }
            // A notification action answers the existing incoming CallKit call.
            // It must never create an outgoing UUID beside the ring.
            requestConnection(CXAnswerCallAction(call: uuid), uuid: uuid)
            return true
        }
        guard activeCallUUID == nil, activeCall == nil else { return false }
        let uuid = UUID(); activeCallUUID = uuid
        prepareConnection(auth: auth, sid: callSid, token: accessToken, conference: conferenceName)
        let action = CXStartCallAction(call: uuid, handle: CXHandle(type: .generic, value: "Kevin Call"))
        action.isVideo = false
        requestConnection(action, uuid: uuid)
        return true
    }

    /// CallKit's sendable completion carries only an actor-isolated relay. The
    /// non-Sendable manager is weakly held and accessed exclusively on MainActor.
    @MainActor private final class TransactionFailureRelay {
        private weak var manager: CallManager?
        private let uuid: UUID
        init(manager: CallManager, uuid: UUID) { self.manager = manager; self.uuid = uuid }
        func fail() { manager?.failConnection(uuid: uuid) }
    }

    @MainActor private func requestConnection(_ action: CXCallAction, uuid: UUID) {
        let relay = TransactionFailureRelay(manager: self, uuid: uuid)
        callController.request(CXTransaction(action: action)) { error in
            guard error != nil else { return }
            Task { @MainActor in relay.fail() }
        }
    }

    private func prepareConnection(auth: CallAuthContext, sid: String, token: String, conference: String) {
        connectionAuth = auth; connectionSid = sid; connectionScope = AppState.shared.callLifecycleSnapshot
        let lease = CallPresentationLease(auth: auth, scope: AppState.shared.callLifecycleSnapshot)
        presentationLease = lease
        AppState.shared.captureScreeningTranscript(for: lease)
        if lease.isValid(auth: AppState.shared.currentAuthContext(), scope: AppState.shared.callLifecycleSnapshot) {
            callerName = AppState.shared.activeCallerName
            callerPhone = AppState.shared.activeCallerPhone
        }
        pendingAccessToken = token; pendingConferenceName = conference
    }
    private func connectionIsCurrent(_ uuid: UUID) -> Bool {
        guard activeCallUUID == uuid, let auth = connectionAuth, auth == AppState.shared.currentAuthContext(),
              connectionScope == AppState.shared.callLifecycleSnapshot else { return false }
        if ownership.seenCallSids.contains(connectionSid) {
            return ownership.usable(uuid, auth: auth, now: Date())?.callSid == connectionSid
        }
        return AppState.shared.callLifecycleSnapshot.permits(connectionSid)
    }
    @discardableResult private func connectToConference(uuid: UUID) -> Bool {
        guard connectionIsCurrent(uuid), activeCall == nil,
              let token = pendingAccessToken, !token.isEmpty,
              let conference = pendingConferenceName, !conference.isEmpty else { return false }
        let options = ConnectOptions(accessToken: token) { $0.params = ["conference": conference] }
        activeCall = TwilioVoiceSDK.connect(options: options, delegate: self)
        return true
    }
    @MainActor func answerActiveCall(callSid: String, callerName: String, callerPhone: String) async {
        _ = await CallActionCoordinator.shared.pickUp(callSid: callSid, callerName: callerName, callerPhone: callerPhone)
    }
    @MainActor func finishMessageRing(callSid: String) {
        guard let uuid = activeCallUUID, ownership.contexts[uuid]?.callSid == callSid, activeCall == nil else { return }
        callKitProvider.reportCall(with: uuid, endedAt: Date(), reason: .declinedElsewhere)
        ownership.remove(uuid); cleanup(uuid: uuid)
    }
    func toggleMute() { guard let call = activeCall else { return }; isMuted.toggle(); call.isMuted = isMuted }
    func toggleSpeaker() { isSpeaker.toggle(); try? AVAudioSession.sharedInstance().overrideOutputAudioPort(isSpeaker ? .speaker : .none) }
    func endCall() {
        guard let uuid = activeCallUUID else { return }
        callController.request(CXTransaction(action: CXEndCallAction(call: uuid))) { _ in }
    }
    private func failConnection(uuid: UUID) {
        guard activeCallUUID == uuid else { return }
        let failedAuth = connectionAuth, failedSid = connectionSid
        if let failedAuth, !failedSid.isEmpty {
            Task { @MainActor in
                CallActionCoordinator.shared.reportConnectionFailure(callSid: failedSid, auth: failedAuth)
            }
        }
        callKitProvider.reportCall(with: uuid, endedAt: Date(), reason: .failed)
        activeCall?.disconnect(); ownership.remove(uuid); cleanup(uuid: uuid)
    }
    private func cleanup(uuid: UUID) {
        guard activeCallUUID == uuid else { return }
        if let lease = presentationLease {
            AppState.shared.clearScreeningTranscript(for: lease)
        }
        presentationLease = nil
        activeCall = nil; activeCallUUID = nil; answeringUUID = nil
        pendingAccessToken = nil; pendingConferenceName = nil; connectionAuth = nil; connectionScope = nil; connectionSid = ""
        isOnCall = false; isMuted = false; isSpeaker = false; callerName = ""; callerPhone = ""; callStartTime = nil
    }

    #if DEBUG
    @MainActor
    func adoptFakeConnectionForTesting(
        auth: CallAuthContext,
        callSid: String,
        callerName: String,
        callerPhone: String,
        startTime: Date = Date()
    ) {
        guard AppStoreScreenshotFixtures.isNativeUIReview else { return }
        // Exercise the production snapshot hook without requesting CallKit or Twilio.
        prepareConnection(auth: auth, sid: callSid, token: "", conference: "")
        self.callerName = callerName
        self.callerPhone = callerPhone
        self.callStartTime = startTime
        self.isOnCall = true
        self.isMuted = false
        self.isSpeaker = false
    }
    #endif
}

extension CallManager: CXProviderDelegate {
    func providerDidReset(_ provider: CXProvider) {
        guard let uuid = activeCallUUID else { return }
        activeCall?.disconnect(); ownership.remove(uuid); cleanup(uuid: uuid)
    }
    func provider(_ provider: CXProvider, perform action: CXAnswerCallAction) {
        Task { @MainActor in
            let uuid = action.callUUID
            guard self.activeCallUUID == uuid,
                  let context = self.ownership.usable(uuid, auth: AppState.shared.currentAuthContext(), now: Date()) else {
                action.fail(); return
            }
            self.answeringUUID = uuid
            let accepted = await CallActionCoordinator.shared.pickUp(callSid: context.callSid, authContext: context.auth)
            guard accepted, self.activeCallUUID == uuid,
                  self.ownership.usable(uuid, auth: AppState.shared.currentAuthContext(), now: Date()) == context else {
                action.fail(); self.failConnection(uuid: uuid); return
            }
            // Notification and CallKit entrypoints may share an already accepted operation.
            if self.activeCall == nil && !self.connectToConference(uuid: uuid) { action.fail(); self.failConnection(uuid: uuid); return }
            action.fulfill()
        }
    }
    func provider(_ provider: CXProvider, perform action: CXStartCallAction) {
        guard activeCallUUID == action.callUUID, ownership.contexts[action.callUUID] == nil,
              connectToConference(uuid: action.callUUID) else { action.fail(); failConnection(uuid: action.callUUID); return }
        action.fulfill()
    }
    func provider(_ provider: CXProvider, perform action: CXEndCallAction) {
        let uuid = action.callUUID
        guard activeCallUUID == uuid else { action.fulfill(); return }
        let context = ownership.contexts[uuid]
        let wasConnected = activeCall != nil
        activeCall?.disconnect(); ownership.remove(uuid); cleanup(uuid: uuid)
        action.fulfill()
        if !wasConnected, let context, context.auth == AppState.shared.currentAuthContext(), Date() < context.deadline {
            Task { @MainActor in
                _ = await CallActionCoordinator.shared.takeMessage(callSid: context.callSid, authContext: context.auth)
            }
        }
    }
    func provider(_ provider: CXProvider, didActivate audioSession: AVAudioSession) { audioDevice.isEnabled = true }
    func provider(_ provider: CXProvider, didDeactivate audioSession: AVAudioSession) { audioDevice.isEnabled = false }
}

extension CallManager: CallDelegate {
    func callDidStartRinging(call: Call) { }
    func callDidConnect(call: Call) {
        DispatchQueue.main.async {
            guard self.activeCall === call, let uuid = self.activeCallUUID else { return }
            guard self.connectionIsCurrent(uuid) else {
                self.failConnection(uuid: uuid); return
            }
            if self.ownership.contexts[uuid] == nil { self.callKitProvider.reportOutgoingCall(with: uuid, connectedAt: Date()) }
            self.isOnCall = true; self.callStartTime = Date()
        }
    }
    func callDidDisconnect(call: Call, error: Error?) {
        DispatchQueue.main.async {
            guard self.activeCall === call, let uuid = self.activeCallUUID else { return }
            self.callKitProvider.reportCall(with: uuid, endedAt: Date(), reason: .remoteEnded)
            self.ownership.remove(uuid); self.cleanup(uuid: uuid)
        }
    }
    func callDidFailToConnect(call: Call, error: Error) {
        DispatchQueue.main.async {
            guard self.activeCall === call, let uuid = self.activeCallUUID else { return }
            self.failConnection(uuid: uuid)
        }
    }
}
