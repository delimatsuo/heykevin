import Foundation
import UIKit
import UserNotifications

struct PushRegistrationSnapshot: Equatable, Sendable {
    let auth: CallAuthContext
    let pushToken: String
    let voipToken: String
}

/// Serial submissions keep an older request from overwriting a newer token on
/// the server. A failure ends the flight; only a new event requests a retry.
@MainActor
final class PushRegistrationCoordinator {
    static let shared = PushRegistrationCoordinator()
    private let permissionStatus: () async -> UNAuthorizationStatus
    private let requestAuthorization: () async -> Bool
    private let registerWithAPNs: @MainActor () -> Void
    private let submit: (String, String, CallAuthContext) async -> Bool
    private let currentAuth: () -> CallAuthContext
    private let sessionState: () -> AccountSessionState
    private let updateTokens: (String, String) -> Void
    private let acknowledge: (Bool) -> Void
    private(set) var cachedPushToken = ""
    private(set) var cachedVoIPToken = ""
    private(set) var lastRegisteredSnapshot: PushRegistrationSnapshot?
    private(set) var lastPermissionStatus: UNAuthorizationStatus = .notDetermined
    private(set) var isFlightActive = false
    private var registrationRequested = false
    private var forceRegistration = false
    private var permissionFlightActive = false

    init(permissionStatusProvider: @escaping () async -> UNAuthorizationStatus = { await UNUserNotificationCenter.current().notificationSettings().authorizationStatus },
         requestAuthorizationHandler: @escaping () async -> Bool = {
             (try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge])) ?? false
         },
         remoteNotificationRegisterHandler: @escaping @MainActor () -> Void = { UIApplication.shared.registerForRemoteNotifications() },
         submitRegistrationHandler: @escaping (String, String, CallAuthContext) async -> Bool = { await APIClient.shared.registerDevice(pushToken: $0, voipToken: $1, authContext: $2) },
         currentAuthProvider: @escaping () -> CallAuthContext = { AppState.shared.currentAuthContext() },
         sessionStateProvider: @escaping () -> AccountSessionState = { AppState.shared.sessionState },
         tokenUpdateEffect: @escaping (String, String) -> Void = { push, voip in
             AppState.shared.pushToken = push
             AppState.shared.voipToken = voip
         },
         registrationSuccessEffect: @escaping (Bool) -> Void = { AppState.shared.isRegistered = $0 }) {
        permissionStatus = permissionStatusProvider
        requestAuthorization = requestAuthorizationHandler
        registerWithAPNs = remoteNotificationRegisterHandler
        submit = submitRegistrationHandler
        currentAuth = currentAuthProvider
        sessionState = sessionStateProvider
        updateTokens = tokenUpdateEffect
        acknowledge = registrationSuccessEffect
    }

    func handleSceneActive() async { await reconcilePermissionAndRegistration() }
    func handleAccountReady() async { await reconcilePermissionAndRegistration() }

    private func reconcilePermissionAndRegistration() async {
        if !permissionFlightActive {
            permissionFlightActive = true
            let status = await permissionStatus()
            lastPermissionStatus = status
            let auth = currentAuth()
            if sessionState().isReady && auth.isValid {
                switch status {
                case .notDetermined:
                    let granted = await requestAuthorization()
                    if currentAuth() == auth && sessionState().isReady && granted {
                        lastPermissionStatus = .authorized
                        registerWithAPNs()
                    }
                case .authorized, .provisional: registerWithAPNs()
                default: break
                }
            }
            permissionFlightActive = false
        }
        await reconcileRegistration(force: true)
    }

    func handlePushToken(_ token: String) async {
        guard !token.isEmpty else { return }
        if cachedPushToken != token { invalidateAcknowledgement() }
        cachedPushToken = token
        updateTokens(cachedPushToken, cachedVoIPToken)
        await reconcileRegistration()
    }

    func handleVoIPToken(_ token: String) async {
        guard !token.isEmpty else { return }
        if cachedVoIPToken != token { invalidateAcknowledgement() }
        cachedVoIPToken = token
        updateTokens(cachedPushToken, cachedVoIPToken)
        await reconcileRegistration()
    }

    func handleAuthChange() {
        invalidateAcknowledgement()
        // A flight always reads the latest context, never a queued old owner.
        if isFlightActive { registrationRequested = true }
    }

    private func invalidateAcknowledgement() {
        lastRegisteredSnapshot = nil
        acknowledge(false)
    }

    private func snapshot() -> PushRegistrationSnapshot? {
        let auth = currentAuth()
        guard auth.isValid, sessionState().isReady,
              !cachedPushToken.isEmpty || !cachedVoIPToken.isEmpty else { return nil }
        return PushRegistrationSnapshot(auth: auth, pushToken: cachedPushToken, voipToken: cachedVoIPToken)
    }

    private func reconcileRegistration(force: Bool = false) async {
        guard snapshot() != nil else { return }
        registrationRequested = true
        forceRegistration = forceRegistration || force
        guard !isFlightActive else { return }
        isFlightActive = true
        defer { isFlightActive = false }
        while registrationRequested {
            registrationRequested = false
            let forced = forceRegistration
            forceRegistration = false
            guard let request = snapshot() else { break }
            if !forced && lastRegisteredSnapshot == request { continue }
            let success = await submit(request.pushToken, request.voipToken, request.auth)
            let latest = snapshot()
            if success && latest == request {
                lastRegisteredSnapshot = request
                acknowledge(true)
            }
            if let latest, latest != request {
                registrationRequested = true
            }
            // With no new event and no changed snapshot, failure stops here.
        }
    }
}
