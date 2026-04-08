import UIKit
import UserNotifications
import PushKit
import Contacts

class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate, PKPushRegistryDelegate {

    private var voipRegistry: PKPushRegistry?

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        // Request push notification permission
        UNUserNotificationCenter.current().delegate = self
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { granted, error in
            if granted {
                #if DEBUG
                print("Push notifications authorized")
                #endif
                DispatchQueue.main.async {
                    UIApplication.shared.registerForRemoteNotifications()
                }
            } else {
                #if DEBUG
                print("Push notifications denied: \(error?.localizedDescription ?? "")")
                #endif
            }
        }

        // Register for VoIP pushes
        let registry = PKPushRegistry(queue: .main)
        registry.delegate = self
        registry.desiredPushTypes = [.voIP]
        voipRegistry = registry

        return true
    }

    // Got device token for regular push notifications
    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        let token = deviceToken.map { String(format: "%02x", $0) }.joined()
        #if DEBUG
        print("Push device token: \(token)")
        #endif

        // Save to app state so it's visible in Settings
        DispatchQueue.main.async {
            AppState.shared.pushToken = token
        }

        // Register with backend
        Task {
            await APIClient.shared.registerDevice(pushToken: token)
        }
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        #if DEBUG
        print("Failed to register for push: \(error.localizedDescription)")
        #endif
    }

    // Handle push notification when app is in foreground
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        let userInfo = notification.request.content.userInfo
        handleIncomingCallNotification(userInfo)
        completionHandler([.banner, .sound])
    }

    // Handle push notification tap
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        let userInfo = response.notification.request.content.userInfo
        handleIncomingCallNotification(userInfo)
        completionHandler()
    }

    // MARK: - PushKit (VoIP Push)

    func pushRegistry(_ registry: PKPushRegistry, didUpdate pushCredentials: PKPushCredentials, for type: PKPushType) {
        let token = pushCredentials.token.map { String(format: "%02x", $0) }.joined()
        #if DEBUG
        print("VoIP push token: \(token)")
        #endif

        // Register VoIP token with backend
        Task {
            await APIClient.shared.registerDevice(pushToken: AppState.shared.pushToken, voipToken: token)
        }
    }

    func pushRegistry(_ registry: PKPushRegistry, didReceiveIncomingPushWith payload: PKPushPayload, for type: PKPushType, completion: @escaping () -> Void) {
        guard type == .voIP else {
            completion()
            return
        }

        let data = payload.dictionaryPayload
        let callSid = data["call_sid"] as? String ?? ""
        let callerPhone = data["caller_phone"] as? String ?? ""
        var callerName = data["caller_name"] as? String ?? ""
        let accessToken = data["access_token"] as? String ?? ""
        let conferenceName = data["conference_name"] as? String ?? ""

        // Look up caller name from iPhone contacts if not provided
        if callerName.isEmpty {
            callerName = lookupContactName(phone: callerPhone)
        }

        let uuid = UUID()

        // MUST report to CallKit immediately (Apple requirement)
        CallManager.shared.reportIncomingCall(
            uuid: uuid,
            callerPhone: callerPhone,
            callerName: callerName,
            accessToken: accessToken,
            conferenceName: conferenceName
        ) {
            // Call completion AFTER CallKit is set up
            completion()
        }

        // VoIP pushes are for direct calls (ring-through / urgency).
        // CallKit handles the call UI — don't show the in-app Live screen.
    }

    func pushRegistry(_ registry: PKPushRegistry, didInvalidatePushTokenFor type: PKPushType) {
        #if DEBUG
        print("VoIP push token invalidated")
        #endif
    }

    // MARK: - Regular Push Notification Handling

    private func handleIncomingCallNotification(_ userInfo: [AnyHashable: Any]) {
        let callSid = userInfo["call_sid"] as? String ?? ""
        let callerPhone = userInfo["caller_phone"] as? String ?? ""
        var callerName = userInfo["caller_name"] as? String ?? ""

        // Look up caller name from iPhone contacts if not provided
        if callerName.isEmpty {
            callerName = lookupContactName(phone: callerPhone)
        }

        if !callSid.isEmpty {
            DispatchQueue.main.async {
                AppState.shared.setActiveCall(
                    callSid: callSid,
                    callerPhone: callerPhone,
                    callerName: callerName
                )
                AppState.shared.showActiveCall = true
            }
        }
    }

    // MARK: - iPhone Contact Lookup

    private func lookupContactName(phone: String) -> String {
        guard !phone.isEmpty else { return "" }

        let store = CNContactStore()
        let status = CNContactStore.authorizationStatus(for: .contacts)
        guard status == .authorized else { return "" }

        // Normalize: strip everything except digits
        let digits = phone.filter { $0.isNumber }
        guard digits.count >= 7 else { return "" }

        // Search by phone number
        do {
            let predicate = CNContact.predicateForContacts(matching: CNPhoneNumber(stringValue: phone))
            let contacts = try store.unifiedContacts(matching: predicate, keysToFetch: [
                CNContactGivenNameKey as CNKeyDescriptor,
                CNContactFamilyNameKey as CNKeyDescriptor,
            ])
            if let contact = contacts.first {
                let name = "\(contact.givenName) \(contact.familyName)".trimmingCharacters(in: .whitespaces)
                if !name.isEmpty {
                    return name
                }
            }
        } catch {
            #if DEBUG
            print("Contact lookup failed: \(error.localizedDescription)")
            #endif
        }

        return ""
    }
}
