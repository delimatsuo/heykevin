import XCTest
import UserNotifications
@testable import Kevin

@MainActor
final class PushRegistrationRecoveryTests: XCTestCase {
    func testReturningWithAllowedNotificationsRequestsRegularRegistration() {
        for status in [UNAuthorizationStatus.authorized, .provisional] {
            var requests = 0
            var registrations = 0
            AppDelegate.recoverPushRegistrationOnActive(
                status: status,
                requestAuthorization: { requests += 1 },
                registerForRemoteNotifications: { registrations += 1 }
            )
            XCTAssertEqual(requests, 0)
            XCTAssertEqual(registrations, 1)
        }
    }

    func testOnboardedUserWithoutPermissionStillGetsThePrompt() {
        var requests = 0
        var registrations = 0
        AppDelegate.recoverPushRegistrationOnActive(
            status: .notDetermined,
            requestAuthorization: { requests += 1 },
            registerForRemoteNotifications: { registrations += 1 }
        )
        XCTAssertEqual(requests, 1)
        XCTAssertEqual(registrations, 0)
    }
}
