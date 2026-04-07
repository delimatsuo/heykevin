import PushKit
import UIKit

/// Handles VoIP push notifications from APNs.
/// When a push arrives, it triggers CallKit to show the native incoming call screen.
class VoIPPushHandler: NSObject, PKPushRegistryDelegate {
    private var registry: PKPushRegistry?

    func registerForVoIPPush() {
        registry = PKPushRegistry(queue: .main)
        registry?.delegate = self
        registry?.desiredPushTypes = [.voIP]
    }

    // MARK: - PKPushRegistryDelegate

    func pushRegistry(
        _ registry: PKPushRegistry,
        didUpdate pushCredentials: PKPushCredentials,
        for type: PKPushType
    ) {
        guard type == .voIP else { return }

        // Convert device token to hex string
        let token = pushCredentials.token.map { String(format: "%02x", $0) }.joined()
        print("VoIP device token: \(token)")

        // Send token to our backend
        Task {
            await APIClient.shared.registerDevice(voipToken: token)
        }
    }

    func pushRegistry(
        _ registry: PKPushRegistry,
        didReceiveIncomingPushWith payload: PKPushPayload,
        for type: PKPushType,
        completion: @escaping () -> Void
    ) {
        guard type == .voIP else {
            completion()
            return
        }

        // Extract caller info from push payload
        let data = payload.dictionaryPayload
        let callerPhone = data["caller_phone"] as? String ?? "Unknown"
        let callerName = data["caller_name"] as? String ?? ""
        let reason = data["reason"] as? String ?? ""
        let callSid = data["call_sid"] as? String ?? ""
        let conferenceName = data["conference_name"] as? String ?? ""

        print("VoIP push received: \(callerPhone) - \(callerName) - \(reason)")

        // Generate a UUID for this call
        let uuid = UUID()

        // Store call info for when user accepts
        AppState.shared.pendingCallSid = callSid
        AppState.shared.pendingConferenceName = conferenceName

        // Report incoming call to CallKit — shows native UI
        CallManager.shared.reportIncomingCall(
            uuid: uuid,
            callerPhone: callerPhone,
            callerName: callerName,
            reason: reason
        ) { error in
            if let error = error {
                print("Failed to report call: \(error)")
            }
            completion()
        }
    }

    func pushRegistry(
        _ registry: PKPushRegistry,
        didInvalidatePushTokenFor type: PKPushType
    ) {
        print("VoIP push token invalidated")
    }
}
