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
        // Screenshot fixtures must stay deterministic and network-free. Skipping
        // notification and PushKit registration also prevents system permission
        // sheets from obscuring App Store creative.
        if AppStoreScreenshotFixtures.isEnabled {
            return true
        }

        // Request push notification permission
        UNUserNotificationCenter.current().delegate = self
        setupNotificationCategories()

        // Deliberately does NOT prompt here. This used to call
        // requestAuthorization() straight out of didFinishLaunching, which put
        // the system alert on screen at first launch before the user knew what
        // the app was. A denial cannot be undone in-app — it takes a trip to
        // Settings — and it permanently disables the live-call screen, call
        // summaries, and every push the product depends on.
        //
        // Onboarding asks explicitly, in context, via requestPushAuthorization().
        // Here we only re-register an already-granted permission so existing
        // users keep refreshing their device token on every launch.
        UNUserNotificationCenter.current().getNotificationSettings { settings in
            guard settings.authorizationStatus == .authorized
                    || settings.authorizationStatus == .provisional else { return }
            DispatchQueue.main.async {
                UIApplication.shared.registerForRemoteNotifications()
            }
        }

        // Register for VoIP pushes
        let registry = PKPushRegistry(queue: .main)
        registry.delegate = self
        registry.desiredPushTypes = [.voIP]
        voipRegistry = registry

        return true
    }

    /// Ask for notification permission, in context, from onboarding.
    ///
    /// Push is not a nice-to-have for this app: the VoIP push is what raises the
    /// live-call screen, and regular push carries the call summary. Asking at a
    /// moment the user understands is the difference between a working install
    /// and a silent one, so this is called after the user has seen what Kevin
    /// does — never at cold launch.
    ///
    /// Safe to call repeatedly: once a decision exists iOS will not re-prompt,
    /// and an already-granted permission simply re-registers.
    static func requestPushAuthorization(completion: ((Bool) -> Void)? = nil) {
        UNUserNotificationCenter.current().requestAuthorization(
            options: [.alert, .sound, .badge]
        ) { granted, _ in
            DispatchQueue.main.async {
                if granted {
                    UIApplication.shared.registerForRemoteNotifications()
                }
                completion?(granted)
            }
        }
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

        if response.actionIdentifier == "PICK_UP_ACTION" {
            let callSid = userInfo["call_sid"] as? String ?? ""
            let callerPhone = userInfo["caller_phone"] as? String ?? ""
            var callerName = userInfo["caller_name"] as? String ?? ""
            if callerName.isEmpty {
                callerName = lookupContactName(phone: callerPhone)
            }
            Task { @MainActor in
                await CallManager.shared.answerActiveCall(
                    callSid: callSid,
                    callerName: callerName,
                    callerPhone: callerPhone
                )
            }
        }

        completionHandler()
    }

    // MARK: - Notification Categories

    private func setupNotificationCategories() {
        let pickUpAction = UNNotificationAction(
            identifier: "PICK_UP_ACTION",
            title: String(localized: "Pick Up"),
            options: [.foreground]
        )
        let screeningCategory = UNNotificationCategory(
            identifier: "SCREENING_CALL",
            actions: [pickUpAction],
            intentIdentifiers: [],
            options: []
        )
        UNUserNotificationCenter.current().setNotificationCategories([screeningCategory])
    }

    // MARK: - PushKit (VoIP Push)

    func pushRegistry(_ registry: PKPushRegistry, didUpdate pushCredentials: PKPushCredentials, for type: PKPushType) {
        let token = pushCredentials.token.map { String(format: "%02x", $0) }.joined()
        #if DEBUG
        print("VoIP push token: \(token)")
        #endif

        // Register VoIP token with backend
        AppState.shared.voipToken = token
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
