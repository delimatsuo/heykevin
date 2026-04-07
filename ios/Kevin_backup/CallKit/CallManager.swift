import CallKit
import AVFoundation

/// Manages CallKit integration — shows native incoming call UI and handles call actions.
class CallManager: NSObject, ObservableObject {
    static let shared = CallManager()

    private let provider: CXProvider
    private let callController = CXCallController()

    @Published var activeCallUUID: UUID?
    @Published var activeCallerName: String = ""
    @Published var activeCallerPhone: String = ""
    @Published var activeCallReason: String = ""
    @Published var isCallActive: Bool = false

    // Callback when user accepts the call
    var onCallAccepted: ((UUID) -> Void)?
    // Callback when user declines the call
    var onCallDeclined: ((UUID) -> Void)?

    private override init() {
        let config = CXProviderConfiguration()
        config.localizedName = "Kevin"
        config.supportsVideo = false
        config.maximumCallsPerCallGroup = 1
        config.maximumCallGroups = 1
        config.supportedHandleTypes = [.phoneNumber]
        // Custom ringtone (optional)
        // config.ringtoneSound = "kevin-ringtone.caf"

        provider = CXProvider(configuration: config)
        super.init()
        provider.setDelegate(self, queue: nil)
    }

    /// Report an incoming call — triggers CallKit native UI
    func reportIncomingCall(
        uuid: UUID,
        callerPhone: String,
        callerName: String,
        reason: String,
        completion: @escaping (Error?) -> Void
    ) {
        let update = CXCallUpdate()
        update.remoteHandle = CXHandle(type: .phoneNumber, value: callerPhone)

        // Show caller name + reason in the CallKit UI
        let displayName = callerName.isEmpty ? callerPhone : "\(callerName)"
        update.localizedCallerName = displayName
        update.hasVideo = false
        update.supportsHolding = false
        update.supportsGrouping = false
        update.supportsUngrouping = false
        update.supportsDTMF = false

        activeCallerPhone = callerPhone
        activeCallerName = callerName
        activeCallReason = reason

        provider.reportNewIncomingCall(with: uuid, update: update) { error in
            if let error = error {
                print("Failed to report incoming call: \(error.localizedDescription)")
            } else {
                print("Incoming call reported: \(displayName)")
                DispatchQueue.main.async {
                    self.activeCallUUID = uuid
                    self.isCallActive = true
                }
            }
            completion(error)
        }
    }

    /// End a call
    func endCall(uuid: UUID) {
        let endAction = CXEndCallAction(call: uuid)
        let transaction = CXTransaction(action: endAction)
        callController.request(transaction) { error in
            if let error = error {
                print("Failed to end call: \(error.localizedDescription)")
            }
            DispatchQueue.main.async {
                self.isCallActive = false
                self.activeCallUUID = nil
            }
        }
    }
}

// MARK: - CXProviderDelegate
extension CallManager: CXProviderDelegate {

    func providerDidReset(_ provider: CXProvider) {
        // Clean up any active calls
        DispatchQueue.main.async {
            self.isCallActive = false
            self.activeCallUUID = nil
        }
    }

    func provider(_ provider: CXProvider, perform action: CXAnswerCallAction) {
        // User tapped "Accept" — connect via Twilio Voice SDK
        print("Call accepted by user")

        // Configure audio session for VoIP
        let audioSession = AVAudioSession.sharedInstance()
        do {
            try audioSession.setCategory(.playAndRecord, mode: .voiceChat, options: [.allowBluetooth])
            try audioSession.setActive(true)
        } catch {
            print("Audio session error: \(error)")
        }

        onCallAccepted?(action.callUUID)
        action.fulfill()
    }

    func provider(_ provider: CXProvider, perform action: CXEndCallAction) {
        // User tapped "Decline" or call ended
        print("Call ended/declined")

        onCallDeclined?(action.callUUID)

        DispatchQueue.main.async {
            self.isCallActive = false
            self.activeCallUUID = nil
        }

        action.fulfill()
    }

    func provider(_ provider: CXProvider, didActivate audioSession: AVAudioSession) {
        // Audio session is active — enable Twilio audio
        print("Audio session activated")
        // TwilioCallService will enable audio here
    }

    func provider(_ provider: CXProvider, didDeactivate audioSession: AVAudioSession) {
        print("Audio session deactivated")
    }
}
