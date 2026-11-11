import Foundation
import SwiftUI

/// Root-owned `@MainActor` coordinator for foreground reconciliation and
/// account-scoped background-cancellable startup work.
///
/// Owns:
/// - Single-start StoreKit transaction listener activation on `.active`
/// - Per-account cold-start 3s delay + profile refresh + entitlement verification
///   (`completedInitialRefreshAuth`) and warm-foreground active-call refresh
/// - Per-account 2s delayed automatic contact sync with 1-hour foreground throttle
/// - Immediate cancellation of foreground tasks on `.background`, auth switch,
///   session non-readiness, or contacts-consent revocation, while preserving
///   in-flight tasks across transient `.inactive` overlays.
@MainActor
final class AppLifecycleCoordinator {
    static let shared = AppLifecycleCoordinator()

    private let currentAuth: @MainActor () -> CallAuthContext
    private let sessionState: @MainActor () -> AccountSessionState
    private let isOnboarded: @MainActor () -> Bool
    private let hasContactsConsent: @MainActor (String) -> Bool
    private let startTransactionListener: @MainActor () -> Void
    private let onAuthChangeSubscriptionCleanup: @MainActor (CallAuthContext) -> Void
    private let fetchContractorProfile: (String, String) async -> [String: Any]?
    private let applySubscriptionProfile: @MainActor ([String: Any], CallAuthContext) -> String?
    private let verifyCurrentEntitlements: @MainActor (CallAuthContext) async -> Void
    private let checkForActiveCall: @MainActor () -> Void
    private let syncContacts: (CallAuthContext) async -> SyncResult
    private let sleepNanoseconds: (UInt64) async -> Void
    private let now: () -> Date
    private let coldLaunchDelayNanoseconds: UInt64
    private let contactSyncDelayNanoseconds: UInt64
    private let contactSyncThrottleInterval: TimeInterval

    private(set) var isInForeground = false
    private(set) var hasStartedTransactionListener = false
    private(set) var completedInitialRefreshAuth: CallAuthContext?
    private var foregroundRefreshTask: Task<Void, Never>?
    private var foregroundRefreshAuth: CallAuthContext?
    private var foregroundRefreshFlightID: UUID?
    private var contactSyncTask: Task<Void, Never>?
    private var contactSyncAuth: CallAuthContext?
    private var contactSyncFlightID: UUID?
    private(set) var lastContactSyncAt: [String: Date] = [:]

    var hasActiveForegroundRefreshTask: Bool {
        if let task = foregroundRefreshTask {
            return !task.isCancelled
        }
        return false
    }

    var hasActiveContactSyncTask: Bool {
        if let task = contactSyncTask {
            return !task.isCancelled
        }
        return false
    }

    init(
        currentAuth: @escaping @MainActor () -> CallAuthContext = { AppState.shared.currentAuthContext() },
        sessionState: @escaping @MainActor () -> AccountSessionState = { AppState.shared.sessionState },
        isOnboarded: @escaping @MainActor () -> Bool = { AppState.shared.isOnboarded },
        hasContactsConsent: @escaping @MainActor (String) -> Bool = { AppState.shared.hasOwnedContactsUploadConsent(for: $0) },
        startTransactionListener: @escaping @MainActor () -> Void = { SubscriptionManager.shared.startTransactionListener() },
        onAuthChangeSubscriptionCleanup: @escaping @MainActor (CallAuthContext) -> Void = { SubscriptionManager.shared.handleAuthChange(currentAuth: $0) },
        fetchContractorProfile: @escaping (String, String) async -> [String: Any]? = { await APIClient.shared.getContractorProfile(contractorId: $0, bearerToken: $1) },
        applySubscriptionProfile: @escaping @MainActor ([String: Any], CallAuthContext) -> String? = { AppState.shared.applySubscriptionProfile($0, for: $1, requiresReadySession: true) },
        verifyCurrentEntitlements: @escaping @MainActor (CallAuthContext) async -> Void = { auth in
            let ctx = SubscriptionVerificationContext(
                contractorID: auth.contractorId,
                bearerToken: auth.bearerToken,
                generation: auth.generation
            )
            _ = await SubscriptionManager.shared.verifyCurrentEntitlements(source: .launch, context: ctx)
        },
        checkForActiveCall: @escaping @MainActor () -> Void = { LiveCallObserver.shared.requestRefresh() },
        syncContacts: @escaping (CallAuthContext) async -> SyncResult = { auth in
            await ContactSyncManager.shared.syncContacts(contractorId: auth.contractorId, bearerToken: auth.bearerToken)
        },
        sleepNanoseconds: @escaping (UInt64) async -> Void = { ns in try? await Task.sleep(nanoseconds: ns) },
        now: @escaping () -> Date = { Date() },
        coldLaunchDelayNanoseconds: UInt64 = 3_000_000_000,
        contactSyncDelayNanoseconds: UInt64 = 2_000_000_000,
        contactSyncThrottleInterval: TimeInterval = 3600
    ) {
        self.currentAuth = currentAuth
        self.sessionState = sessionState
        self.isOnboarded = isOnboarded
        self.hasContactsConsent = hasContactsConsent
        self.startTransactionListener = startTransactionListener
        self.onAuthChangeSubscriptionCleanup = onAuthChangeSubscriptionCleanup
        self.fetchContractorProfile = fetchContractorProfile
        self.applySubscriptionProfile = applySubscriptionProfile
        self.verifyCurrentEntitlements = verifyCurrentEntitlements
        self.checkForActiveCall = checkForActiveCall
        self.syncContacts = syncContacts
        self.sleepNanoseconds = sleepNanoseconds
        self.now = now
        self.coldLaunchDelayNanoseconds = coldLaunchDelayNanoseconds
        self.contactSyncDelayNanoseconds = contactSyncDelayNanoseconds
        self.contactSyncThrottleInterval = contactSyncThrottleInterval
    }

    func handleScenePhaseChange(_ phase: ScenePhase) {
        switch phase {
        case .active:
            isInForeground = true
            if !hasStartedTransactionListener {
                hasStartedTransactionListener = true
                startTransactionListener()
            }
            reconcileForegroundTasksIfEligible()
        case .inactive:
            // Preserve foreground state and in-flight tasks across transient system overlays
            // (StoreKit sheets, notification/contacts permission dialogs, Control Center).
            break
        case .background:
            isInForeground = false
            cancelForegroundRefreshTask()
            cancelContactSyncTask()
        @unknown default:
            break
        }
    }

    func handleAuthChange() {
        let auth = currentAuth()
        onAuthChangeSubscriptionCleanup(auth)
        if completedInitialRefreshAuth != auth {
            completedInitialRefreshAuth = nil
        }
        if foregroundRefreshAuth != auth || !auth.isValid {
            cancelForegroundRefreshTask()
        }
        if contactSyncAuth != auth || !auth.isValid {
            cancelContactSyncTask()
        }
        reconcileForegroundTasksIfEligible()
    }

    func handleSessionReadinessChange() {
        if !isOnboarded() || sessionState() != .ready || !currentAuth().isValid {
            cancelForegroundRefreshTask()
            cancelContactSyncTask()
            return
        }
        reconcileForegroundTasksIfEligible()
    }

    func handleContactsConsentChange() {
        let auth = currentAuth()
        if !auth.isValid || !hasContactsConsent(auth.contractorId) {
            cancelContactSyncTask()
        }
    }

    private func isEligibleForForegroundWork(_ auth: CallAuthContext) -> Bool {
        isInForeground && isOnboarded() && sessionState() == .ready && auth.isValid && currentAuth() == auth
    }

    private func reconcileForegroundTasksIfEligible() {
        let auth = currentAuth()
        guard isEligibleForForegroundWork(auth) else { return }
        startForegroundRefreshIfNeeded(for: auth)
        startContactSyncIfNeeded(for: auth)
    }

    private func startForegroundRefreshIfNeeded(for auth: CallAuthContext) {
        if let existing = foregroundRefreshTask, !existing.isCancelled, foregroundRefreshAuth == auth {
            return
        }

        cancelForegroundRefreshTask()

        let flightID = UUID()
        foregroundRefreshFlightID = flightID
        foregroundRefreshAuth = auth
        let needsInitialRefresh = (completedInitialRefreshAuth != auth)

        foregroundRefreshTask = Task { @MainActor [weak self] in
            defer {
                if let self, self.foregroundRefreshFlightID == flightID {
                    self.foregroundRefreshTask = nil
                    self.foregroundRefreshAuth = nil
                    self.foregroundRefreshFlightID = nil
                }
            }
            guard let self else { return }

            if needsInitialRefresh {
                await self.sleepNanoseconds(self.coldLaunchDelayNanoseconds)
                guard !Task.isCancelled,
                      self.foregroundRefreshFlightID == flightID,
                      self.isEligibleForForegroundWork(auth) else { return }

                let profile = await self.fetchContractorProfile(auth.contractorId, auth.bearerToken)
                guard !Task.isCancelled,
                      self.foregroundRefreshFlightID == flightID,
                      self.isEligibleForForegroundWork(auth) else { return }

                if let profile {
                    _ = self.applySubscriptionProfile(profile, auth)
                }
                guard !Task.isCancelled,
                      self.foregroundRefreshFlightID == flightID,
                      self.isEligibleForForegroundWork(auth) else { return }

                await self.verifyCurrentEntitlements(auth)
                guard !Task.isCancelled,
                      self.foregroundRefreshFlightID == flightID,
                      self.isEligibleForForegroundWork(auth) else { return }

                self.completedInitialRefreshAuth = auth
            }

            guard !Task.isCancelled,
                  self.foregroundRefreshFlightID == flightID,
                  self.isEligibleForForegroundWork(auth) else { return }
            self.checkForActiveCall()
        }
    }

    private func startContactSyncIfNeeded(for auth: CallAuthContext) {
        guard hasContactsConsent(auth.contractorId) else {
            cancelContactSyncTask()
            return
        }

        if let existing = contactSyncTask, !existing.isCancelled, contactSyncAuth == auth {
            return
        }

        if let lastSync = lastContactSyncAt[auth.contractorId],
           now().timeIntervalSince(lastSync) <= contactSyncThrottleInterval {
            return
        }

        cancelContactSyncTask()

        let flightID = UUID()
        contactSyncFlightID = flightID
        contactSyncAuth = auth

        contactSyncTask = Task { @MainActor [weak self] in
            defer {
                if let self, self.contactSyncFlightID == flightID {
                    self.contactSyncTask = nil
                    self.contactSyncAuth = nil
                    self.contactSyncFlightID = nil
                }
            }
            guard let self else { return }

            await self.sleepNanoseconds(self.contactSyncDelayNanoseconds)
            guard !Task.isCancelled,
                  self.contactSyncFlightID == flightID,
                  self.isEligibleForForegroundWork(auth),
                  self.hasContactsConsent(auth.contractorId) else { return }

            let result = await self.syncContacts(auth)
            guard self.currentAuth() == auth else { return }
            let didSucceed: Bool
            if case .success = result {
                didSucceed = true
            } else {
                didSucceed = false
            }
            if (!Task.isCancelled && self.contactSyncFlightID == flightID) || didSucceed {
                self.lastContactSyncAt[auth.contractorId] = self.now()
            }
        }
    }

    private func cancelForegroundRefreshTask() {
        foregroundRefreshTask?.cancel()
        foregroundRefreshTask = nil
        foregroundRefreshAuth = nil
        foregroundRefreshFlightID = nil
    }

    private func cancelContactSyncTask() {
        contactSyncTask?.cancel()
        contactSyncTask = nil
        contactSyncAuth = nil
        contactSyncFlightID = nil
    }
}
