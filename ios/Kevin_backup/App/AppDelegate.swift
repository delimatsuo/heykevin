import UIKit
import PushKit
import CallKit

class AppDelegate: NSObject, UIApplicationDelegate {
    let callManager = CallManager.shared
    let pushHandler = VoIPPushHandler()

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        // Register for VoIP push notifications
        pushHandler.registerForVoIPPush()
        return true
    }
}
