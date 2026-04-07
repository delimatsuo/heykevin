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
    private var activeCall: Call?  // Twilio Voice Call
    private var activeCallUUID: UUID?
    private var pendingAccessToken: String?
    private var pendingConferenceName: String?
    private var pendingConnect = false  // Connect after CallKit activates audio

    // Published state for in-app call UI
    @Published var isOnCall = false
    @Published var isMuted = false
    @Published var isSpeaker = false
    @Published var callerName = ""
    @Published var callerPhone = ""
    @Published var callStartTime: Date? = nil

    override init() {
        let config = CXProviderConfiguration()
        config.maximumCallGroups = 1
        config.maximumCallsPerCallGroup = 1
        config.supportsVideo = false
        config.supportedHandleTypes = [.phoneNumber, .generic]
        config.iconTemplateImageData = nil
        callKitProvider = CXProvider(configuration: config)

        // Register Twilio's audio device BEFORE any calls are made
        TwilioVoiceSDK.audioDevice = audioDevice

        super.init()
        callKitProvider.setDelegate(self, queue: nil)
    }

    /// Report an incoming call (called from VoIP push handler)
    func reportIncomingCall(
        uuid: UUID,
        callerPhone: String,
        callerName: String,
        accessToken: String,
        conferenceName: String,
        completion: (() -> Void)? = nil
    ) {
        pendingAccessToken = accessToken
        pendingConferenceName = conferenceName
        activeCallUUID = uuid

        DispatchQueue.main.async {
            self.callerName = callerName
            self.callerPhone = callerPhone
        }

        let update = CXCallUpdate()
        update.remoteHandle = CXHandle(type: .phoneNumber, value: callerPhone)
        update.localizedCallerName = callerName.isEmpty ? callerPhone : callerName
        update.hasVideo = false
        update.supportsDTMF = true
        update.supportsGrouping = false
        update.supportsUngrouping = false
        update.supportsHolding = false

        callKitProvider.reportNewIncomingCall(with: uuid, update: update) { error in
            #if DEBUG
            if let error = error {
                print("Failed to report incoming call: \(error.localizedDescription)")
            } else {
                print("Incoming call reported to CallKit")
            }
            #endif
            completion?()
        }
    }

    /// Connect to conference via Twilio Voice SDK
    private func connectToConference() {
        guard let token = pendingAccessToken, let conf = pendingConferenceName else {
            #if DEBUG
            print("No access token or conference name — cannot connect")
            #endif
            return
        }

        #if DEBUG
        print("Connecting to Twilio conference: \(conf)")
        #endif

        let connectOptions = ConnectOptions(accessToken: token) { builder in
            builder.params = ["conference": conf]
        }

        activeCall = TwilioVoiceSDK.connect(options: connectOptions, delegate: self)
    }

    /// Connect directly to a conference (user tapped Pick Up in the app)
    func connectDirectly(accessToken: String, conferenceName: String) {
        pendingAccessToken = accessToken
        pendingConferenceName = conferenceName
        pendingConnect = true

        // Start an outgoing call via CallKit — this triggers the audio session activation
        let uuid = UUID()
        activeCallUUID = uuid
        let handle = CXHandle(type: .generic, value: "Kevin Call")
        let startAction = CXStartCallAction(call: uuid, handle: handle)
        startAction.isVideo = false

        let transaction = CXTransaction(action: startAction)
        callController.request(transaction) { [weak self] error in
            if let error = error {
                #if DEBUG
                print("CallKit start action failed: \(error)")
                #endif
                // CallKit failed — try connecting directly without it
                self?.connectToConference()
            }
        }
    }

    func toggleMute() {
        guard let call = activeCall else { return }
        isMuted.toggle()
        call.isMuted = isMuted
    }

    func toggleSpeaker() {
        isSpeaker.toggle()
        let session = AVAudioSession.sharedInstance()
        try? session.overrideOutputAudioPort(isSpeaker ? .speaker : .none)
    }

    /// End the current call
    func endCall() {
        activeCall?.disconnect()
        if let uuid = activeCallUUID {
            let action = CXEndCallAction(call: uuid)
            let transaction = CXTransaction(action: action)
            callController.request(transaction) { error in
                #if DEBUG
                if let error = error {
                    print("End call failed: \(error)")
                }
                #endif
            }
        }
        cleanup()
    }

    private func cleanup() {
        activeCall = nil
        activeCallUUID = nil
        pendingAccessToken = nil
        pendingConferenceName = nil
        pendingConnect = false
        DispatchQueue.main.async {
            self.isOnCall = false
            self.isMuted = false
            self.isSpeaker = false
            self.callerName = ""
            self.callerPhone = ""
            self.callStartTime = nil
        }
    }
}

// MARK: - CXProviderDelegate (CallKit)

extension CallManager: CXProviderDelegate {
    func providerDidReset(_ provider: CXProvider) {
        activeCall?.disconnect()
        cleanup()
    }

    func provider(_ provider: CXProvider, perform action: CXAnswerCallAction) {
        // User answered an incoming call (slide-to-answer via CallKit)
        connectToConference()
        action.fulfill()
    }

    func provider(_ provider: CXProvider, perform action: CXStartCallAction) {
        // CallKit acknowledged our outgoing call — now connect to Twilio
        #if DEBUG
        print("CallKit: start call action fulfilled — connecting to Twilio")
        #endif

        if pendingConnect {
            pendingConnect = false
            connectToConference()
        }

        action.fulfill()
    }

    func provider(_ provider: CXProvider, perform action: CXEndCallAction) {
        let wasConnected = activeCall != nil
        activeCall?.disconnect()
        cleanup()
        action.fulfill()

        // If the call was never connected, this is a decline of an incoming call.
        // Notify the backend so it can stop ringing and redirect the caller to Kevin.
        if !wasConnected {
            let callSid = AppState.shared.activeCallSid
            if !callSid.isEmpty {
                Task {
                    _ = await APIClient.shared.sendCallAction(callSid: callSid, action: "decline")
                }
                DispatchQueue.main.async {
                    AppState.shared.clearActiveCall()
                }
            }
        }
    }

    func provider(_ provider: CXProvider, didActivate audioSession: AVAudioSession) {
        #if DEBUG
        print("CallKit: audio session activated")
        #endif
        audioDevice.isEnabled = true
    }

    func provider(_ provider: CXProvider, didDeactivate audioSession: AVAudioSession) {
        #if DEBUG
        print("CallKit: audio session deactivated")
        #endif
        audioDevice.isEnabled = false
    }
}

// MARK: - CallDelegate (Twilio Voice SDK)

extension CallManager: CallDelegate {
    func callDidStartRinging(call: Call) {
        #if DEBUG
        print("Twilio call ringing")
        #endif
    }

    func callDidConnect(call: Call) {
        #if DEBUG
        print("Twilio call connected — audio should be flowing")
        #endif
        if let uuid = activeCallUUID {
            callKitProvider.reportOutgoingCall(with: uuid, connectedAt: Date())
        }
        DispatchQueue.main.async {
            self.isOnCall = true
            self.callStartTime = Date()
        }
    }

    func callDidDisconnect(call: Call, error: Error?) {
        #if DEBUG
        print("Twilio call disconnected: \(error?.localizedDescription ?? "clean")")
        #endif
        if let uuid = activeCallUUID {
            callKitProvider.reportCall(with: uuid, endedAt: Date(), reason: .remoteEnded)
        }
        cleanup()
    }

    func callDidFailToConnect(call: Call, error: Error) {
        #if DEBUG
        print("Twilio call FAILED: \(error.localizedDescription)")
        #endif
        if let uuid = activeCallUUID {
            callKitProvider.reportCall(with: uuid, endedAt: Date(), reason: .failed)
        }
        cleanup()
    }
}
