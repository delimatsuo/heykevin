import Foundation
import Combine
import UIKit

struct CapturedScreeningTranscript: Equatable, Sendable {
    let lease: CallPresentationLease
    let lines: [String]
    let reason: String
}

enum AppTab {
    case live, recents, settings
}

class AppState: ObservableObject {
    static let shared: AppState = {
        #if DEBUG
        if ProcessInfo.processInfo.environment["KEVIN_UNIT_TESTS"] == "1" {
            return AppState(inMemory: true)
        }
        #endif
        return AppState()
    }()

    private static let keychainBackedKeys = [
        "contractorId",
        "appleUserId",
        "subscriptionStatus",
        "subscriptionTier",
        "subscriptionUUID",
        "contractorApiToken",
    ]

    let backendURL: String = {
        guard let url = Bundle.main.infoDictionary?["BackendURL"] as? String,
              !url.isEmpty,
              !url.contains("$(") else {
            fatalError("BackendURL not set in Info.plist. Check the active build configuration.")
        }

        #if DEBUG || STAGING
        if url == "https://kevin-api-752910912062.us-central1.run.app" {
            fatalError("Non-production build is configured to use the production backend.")
        }
        #endif

        return url
    }()

    // When taking App Store screenshots, avoid persisting any local state changes.
    // This ensures fixture-only values do not leak into normal debug sessions.
    #if DEBUG
    private var inScreenshotFixture: Bool {
        ProcessInfo.processInfo.environment["APP_STORE_SCREENSHOT_SCENARIO"] != nil
    }
    #else
    private var inScreenshotFixture: Bool { false }
    #endif

    // Observable account session gate
    @Published var sessionState: AccountSessionState = .uninitialized

    let inMemory: Bool
    private let authProviderClosure: (() -> CallAuthContext)?
    private let persistence: AccountPersistence
    private let injectedSecureStore: KeychainManager?
    private var secureStore: KeychainManager { injectedSecureStore ?? persistence.keychain }
    private var inMemoryBearerToken = ""
    private var inMemoryGeneration = 0
    private(set) var recoveryCommitPending = false
    private(set) var pendingAccountRecovery: AccountRecoveryCheckpoint?
    private static let recoveryMarkerKey = "accountRecoveryCommit"
    static let contactsUploadConsentKey = "contactsUploadConsent"
    static let contactsUploadConsentOwnerKey = "contactsUploadConsentOwner"
    private var isHydratingInitialStorage = false
    private var isUpdatingConsentState = false
    private(set) var contactsUploadConsentOwner: String? = nil

    private func migrateToKeychain(_ key: String) -> String {
        (injectedSecureStore == nil
            ? persistence.readMigratingLegacy(key)
            : secureStore.readMigratingLegacy(key, defaults: persistence.defaults)).stringValue ?? ""
    }

    init(authProvider: (() -> CallAuthContext)? = nil, inMemory: Bool = false,
         secureStore: KeychainManager? = nil, persistence: AccountPersistence = .live) {
        self.persistence = persistence
        self.inMemory = inMemory
        self.authProviderClosure = authProvider
        self.injectedSecureStore = secureStore

        if !inMemory && !inScreenshotFixture {
            do {
                isHydratingInitialStorage = true
                defer { isHydratingInitialStorage = false }
                let initialContractor = migrateToKeychain("contractorId")
                self.contractorId = initialContractor
                self.isOnboarded = persistence.defaults.bool(forKey: "isOnboarded")
                self.subscriptionStatus = persistence.keychain.retrieve("subscriptionStatus") ?? "trial"
                self.subscriptionTier = persistence.keychain.retrieve("subscriptionTier") ?? "none"
                self.subscriptionUUID = persistence.keychain.retrieve("subscriptionUUID") ?? ""
                self.kevinNumber = persistence.defaults.string(forKey: "kevinNumber") ?? ""
                self.forwardingActivated = persistence.defaults.bool(forKey: "forwardingActivated")
                self.appleUserId = migrateToKeychain("appleUserId")
                self.userName = persistence.defaults.string(forKey: "userName") ?? ""
                self.businessName = persistence.defaults.string(forKey: "businessName") ?? ""
                self.businessAddress = persistence.defaults.string(forKey: "businessAddress") ?? ""
                self.businessCity = persistence.defaults.string(forKey: "businessCity") ?? ""
                self.serviceType = persistence.defaults.string(forKey: "serviceType") ?? ""
                self.mode = persistence.defaults.string(forKey: "kevinMode") ?? "business"
                self.ringThroughContacts = persistence.defaults.object(forKey: "ringThroughContacts") as? Bool ?? true
                self.isVerizonCarrier = persistence.defaults.bool(forKey: "isVerizonCarrier")
                self.sitToneEnabled = persistence.defaults.object(forKey: "sitToneEnabled") as? Bool ?? false
                self.autoReplySms = persistence.defaults.object(forKey: "autoReplySms") as? Bool ?? false
                self.smartInterruption = persistence.defaults.object(forKey: "smartInterruption") as? Bool ?? true
                self.kevinLanguage = persistence.defaults.string(forKey: "kevinLanguage") ?? "auto"
                self.countryCode = persistence.defaults.string(forKey: "countryCode") ?? ""
                self.readCallIds = loadReadCallIds(for: initialContractor)
                self.jobberConnected = persistence.defaults.bool(forKey: "jobberConnected")
                self.googleCalendarConnected = persistence.defaults.bool(forKey: "googleCalendarConnected")
            }
            reconcilePersistedConsent()
        }

        if !inMemory {
            persistence.epoch.notificationCenter.addObserver(
                self,
                selector: #selector(handleAuthEpochChanged),
                name: CallSessionEpoch.didChangeNotification,
                object: nil
            )
            persistence.epoch.notificationCenter.addObserver(
                self,
                selector: #selector(handleProtectedDataBecameAvailable),
                name: UIApplication.protectedDataDidBecomeAvailableNotification,
                object: nil
            )
        }
    }

    deinit {
        persistence.epoch.notificationCenter.removeObserver(self)
    }

    @objc private func handleProtectedDataBecameAvailable() {
        Task { @MainActor [weak self] in self?.refreshSecureStorageForActiveUse() }
    }

    @objc private func handleAuthEpochChanged() {
        if Thread.isMainThread {
            self.applyAuthChange()
        } else {
            DispatchQueue.main.async { [weak self] in
                self?.applyAuthChange()
            }
        }
    }

    func applyAuthChange() {
        isRegistered = false
        let currentAuth = currentAuthContext()
        if let origin = activeCallAuth, origin != currentAuth || !currentAuth.isValid {
            clearActiveCall()
        }
        let current = contractorId
        if !inMemory && !inScreenshotFixture {
            readCallIds = loadReadCallIds(for: current)
        } else {
            readCallIds = []
        }
        unreadCallCount = 0
        notificationCallSid = ""
        notificationCallMessage = ""
        serviceBinding = nil
    }

    @Published var serviceBinding: ServiceBinding? = nil

    // Session generation incremented on logout, contractor change, and credential change
    var sessionGeneration: Int {
        if let custom = authProviderClosure?() {
            return custom.generation
        }
        return inMemory ? inMemoryGeneration : persistence.epoch.generation
    }

    // Onboarding
    @Published var isOnboarded: Bool = false {
        didSet {
            let loggedOut = oldValue && !isOnboarded
            if !inScreenshotFixture && !inMemory {
                DispatchQueue.main.async { self.persistence.defaults.set(self.isOnboarded, forKey: "isOnboarded") }
            }
            if !isOnboarded {
                sessionState = .notOnboarded
            }
            if loggedOut {
                callLifecycleRevision += 1
                if !inMemory && !inScreenshotFixture {
                    persistence.epoch.advance()
                }
            }
        }
    }
    @Published var contractorId: String = "" {
        didSet {
            let changed = oldValue != contractorId
            if !isHydratingInitialStorage && !inScreenshotFixture && !inMemory {
                if contractorId.isEmpty {
                    _ = KeychainManager.removeNonemptyLegacyValue(forKey: "contractorId", in: persistence.defaults)
                    persistence.clearMigratedLegacyKey("contractorId")
                    persistence.keychain.delete("contractorId")
                } else {
                    if persistence.keychain.save("contractorId", value: contractorId) {
                        _ = KeychainManager.removeNonemptyLegacyValue(forKey: "contractorId", in: persistence.defaults)
                    }
                }
            }
            if changed {
                let newTrimmed = contractorId.trimmingCharacters(in: .whitespacesAndNewlines)
                if (contactsUploadConsent || contactsUploadConsentOwner != nil)
                    && (newTrimmed.isEmpty || contactsUploadConsentOwner != newTrimmed) {
                    revokeContactsUploadConsent()
                }
                if inMemory { inMemoryGeneration += 1 }
                callLifecycleRevision += 1
                if !inMemory && !inScreenshotFixture {
                    readCallIds = loadReadCallIds(for: contractorId)
                } else {
                    readCallIds = []
                }
                unreadCallCount = 0
                notificationCallSid = ""
                notificationCallMessage = ""
                serviceBinding = nil
                if !inMemory && !inScreenshotFixture {
                    persistence.epoch.advance()
                }
            }
        }
    }
    @Published var isRegistered: Bool = false

    // Subscription state (Keychain-backed — UI cache only, server is source of truth)
    @Published var subscriptionStatus: String = "trial" {
        didSet {
            if inScreenshotFixture || inMemory { return }
            if subscriptionStatus.isEmpty {
                persistence.keychain.delete("subscriptionStatus")
            } else {
                persistence.keychain.save("subscriptionStatus", value: subscriptionStatus)
            }
        }
    }
    @Published var subscriptionTier: String = "none" {
        didSet {
            if inScreenshotFixture || inMemory { return }
            if subscriptionTier.isEmpty {
                persistence.keychain.delete("subscriptionTier")
            } else {
                persistence.keychain.save("subscriptionTier", value: subscriptionTier)
            }
        }
    }
    @Published var subscriptionUUID: String = "" {
        didSet {
            if inScreenshotFixture || inMemory { return }
            if subscriptionUUID.isEmpty {
                persistence.keychain.delete("subscriptionUUID")
            } else {
                persistence.keychain.save("subscriptionUUID", value: subscriptionUUID)
            }
        }
    }

    var isSubscriptionActive: Bool {
        subscriptionStatus == "trial" || subscriptionStatus == "active"
    }

    /// A tier-only response updates the cache and returns nil to callers verifying a purchase.
    @MainActor
    @discardableResult
    func applySubscriptionProfile(
        _ profile: [String: Any],
        for capturedAuth: CallAuthContext,
        requiresReadySession: Bool = false
    ) -> String? {
        guard capturedAuth.isValid, currentAuthContext() == capturedAuth,
              !requiresReadySession || sessionState == .ready,
              let update = SubscriptionProfileUpdate(profile: profile),
              update.contractorId.utf8.elementsEqual(capturedAuth.contractorId.utf8) else { return nil }

        if let status = update.status { subscriptionStatus = status }
        if let tier = update.tier { subscriptionTier = tier }
        return update.status
    }

    @Published var kevinNumber: String = "" {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { self.persistence.defaults.set(self.kevinNumber, forKey: "kevinNumber") }
            // New number means forwarding needs to be re-activated
            if !kevinNumber.isEmpty {
                let stored = persistence.defaults.string(forKey: "forwardingActivatedFor") ?? ""
                if stored != kevinNumber {
                    DispatchQueue.main.async { self.persistence.defaults.set(false, forKey: "forwardingActivated") }
                }
            }
        }
    }
    @Published var forwardingActivated: Bool = false {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { self.persistence.defaults.set(self.forwardingActivated, forKey: "forwardingActivated") }
        }
    }
    @Published var appleUserId: String = "" {
        didSet {
            if !isHydratingInitialStorage && !inScreenshotFixture && !inMemory {
                if appleUserId.isEmpty {
                    _ = KeychainManager.removeNonemptyLegacyValue(forKey: "appleUserId", in: persistence.defaults)
                    persistence.clearMigratedLegacyKey("appleUserId")
                    persistence.keychain.delete("appleUserId")
                } else {
                    if persistence.keychain.save("appleUserId", value: appleUserId) {
                        _ = KeychainManager.removeNonemptyLegacyValue(forKey: "appleUserId", in: persistence.defaults)
                    }
                }
            }
        }
    }
    @Published var appleIdentityToken: String = ""

    // Device
    @Published var pushToken: String = ""
    @Published var voipToken: String = ""

    // Navigation
    @Published var selectedTab: AppTab = .recents

    // Active call
    private(set) var callLifecycleRevision = 0

    /// Stored producing auth context for the currently active call
    private(set) var activeCallAuth: CallAuthContext? = nil

    /// Optional presentation lease built directly from stored origin auth + raw active SID/revision.
    /// Returns nil immediately when origin != currentAuthContext or invalid, even before queued auth cleanup.
    var ownedActiveCallLease: CallPresentationLease? {
        let currentAuth = currentAuthContext()
        guard let origin = activeCallAuth,
              origin == currentAuth,
              origin.isValid,
              !activeCallSid.isEmpty else {
            return nil
        }
        return CallPresentationLease(
            auth: origin,
            scope: CallLifecycleSnapshot(callSid: activeCallSid, revision: callLifecycleRevision)
        )
    }

    /// Snapshot of call lifecycle. Returns an empty SID with current revision when stale or unowned.
    var callLifecycleSnapshot: CallLifecycleSnapshot {
        if ownedActiveCallLease != nil {
            return CallLifecycleSnapshot(callSid: activeCallSid, revision: callLifecycleRevision)
        } else {
            return CallLifecycleSnapshot(callSid: "", revision: callLifecycleRevision)
        }
    }

    @Published var notificationCallSid = ""
    @Published var notificationCallMessage = ""
    @Published var activeCallSid: String = "" {
        didSet { if oldValue != activeCallSid { callLifecycleRevision += 1 } }
    }
    @Published var activeCallerPhone: String = ""
    @Published var activeCallerName: String = ""
    @Published var activeCallReason: String = ""
    @Published private var capturedScreeningTranscript: CapturedScreeningTranscript? = nil
    @Published var showActiveCall: Bool = false
    @Published var transcriptLines: [TranscriptLine] = []
    @Published var callStartTime: Date? = nil
    @Published var callIgnored: Bool = false
    @Published var isOnCall: Bool = false

    // Settings
    @Published var userName: String = "" {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { self.persistence.defaults.set(self.userName, forKey: "userName") }
        }
    }
    @Published var businessName: String = "" {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { self.persistence.defaults.set(self.businessName, forKey: "businessName") }
        }
    }
    // Business street address and city required for Twilio number
    // provisioning in regulatory countries (RegulatoryAddress.countries).
    // Business contact details, not credentials, so UserDefaults matches
    // businessName rather than the Keychain.
    @Published var businessAddress: String = "" {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { self.persistence.defaults.set(self.businessAddress, forKey: "businessAddress") }
        }
    }
    @Published var businessCity: String = "" {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { self.persistence.defaults.set(self.businessCity, forKey: "businessCity") }
        }
    }
    @Published var serviceType: String = "" {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { self.persistence.defaults.set(self.serviceType, forKey: "serviceType") }
        }
    }
    @Published var mode: String = "business" {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { self.persistence.defaults.set(self.mode, forKey: "kevinMode") }
        }
    }

    var isPersonalMode: Bool { mode == "personal" }
    var hasBusinessEntitlement: Bool {
        (subscriptionStatus == "trial" || subscriptionStatus == "active")
            && (subscriptionTier == "business" || subscriptionTier == "businessPro")
    }
    var hasBusinessProEntitlement: Bool {
        (subscriptionStatus == "trial" || subscriptionStatus == "active")
            && subscriptionTier == "businessPro"
    }

    @Published var ringThroughContacts: Bool = true {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { self.persistence.defaults.set(self.ringThroughContacts, forKey: "ringThroughContacts") }
        }
    }

    // Explicit consent to upload contacts to the server. Separate from iOS contacts permission.
    @Published var contactsUploadConsent: Bool = false {
        didSet {
            guard !isUpdatingConsentState else { return }
            if contactsUploadConsent {
                let trimmed = contractorId.trimmingCharacters(in: .whitespacesAndNewlines)
                if !trimmed.isEmpty && !recoveryCommitPending && pendingAccountRecovery == nil {
                    contactsUploadConsentOwner = trimmed
                    if !inScreenshotFixture && !inMemory {
                        persistence.clearMigratedLegacyKey("contractorId")
                        persistence.defaults.set(true, forKey: Self.contactsUploadConsentKey)
                        persistence.defaults.set(trimmed, forKey: Self.contactsUploadConsentOwnerKey)
                    }
                } else {
                    contactsUploadConsentOwner = nil
                    if !inScreenshotFixture && !inMemory {
                        persistence.defaults.set(false, forKey: Self.contactsUploadConsentKey)
                        persistence.defaults.removeObject(forKey: Self.contactsUploadConsentOwnerKey)
                    }
                }
            } else {
                contactsUploadConsentOwner = nil
                if !inScreenshotFixture && !inMemory {
                    persistence.defaults.set(false, forKey: Self.contactsUploadConsentKey)
                    persistence.defaults.removeObject(forKey: Self.contactsUploadConsentOwnerKey)
                }
            }
        }
    }

    func grantContactsUploadConsent(for accountId: String) {
        let trimmed = accountId.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !recoveryCommitPending, pendingAccountRecovery == nil else {
            revokeContactsUploadConsent()
            return
        }
        isUpdatingConsentState = true
        contactsUploadConsentOwner = trimmed
        if !contactsUploadConsent { contactsUploadConsent = true }
        isUpdatingConsentState = false
        if !inScreenshotFixture && !inMemory {
            persistence.clearMigratedLegacyKey("contractorId")
            persistence.defaults.set(true, forKey: Self.contactsUploadConsentKey)
            persistence.defaults.set(trimmed, forKey: Self.contactsUploadConsentOwnerKey)
        }
    }

    func revokeContactsUploadConsent() {
        isUpdatingConsentState = true
        contactsUploadConsentOwner = nil
        if contactsUploadConsent { contactsUploadConsent = false }
        isUpdatingConsentState = false
        if !inScreenshotFixture && !inMemory {
            persistence.defaults.set(false, forKey: Self.contactsUploadConsentKey)
            persistence.defaults.removeObject(forKey: Self.contactsUploadConsentOwnerKey)
        }
    }

    func hasOwnedContactsUploadConsent(for accountId: String) -> Bool {
        let trimmed = accountId.trimmingCharacters(in: .whitespacesAndNewlines)
        return !trimmed.isEmpty && contactsUploadConsent && contactsUploadConsentOwner == trimmed
    }

    private func reconcilePersistedConsent(pendingRecovery: Bool? = nil) {
        guard !inMemory && !inScreenshotFixture else { return }
        let marker = secureStore.read(Self.recoveryMarkerKey)
        let idRead = secureStore.read("contractorId")
        let tokenRead = secureStore.read("contractorApiToken")
        if case .error = marker {
            isUpdatingConsentState = true
            contactsUploadConsentOwner = nil
            if contactsUploadConsent { contactsUploadConsent = false }
            isUpdatingConsentState = false
            return
        }
        if case .error = idRead {
            isUpdatingConsentState = true
            contactsUploadConsentOwner = nil
            if contactsUploadConsent { contactsUploadConsent = false }
            isUpdatingConsentState = false
            return
        }
        if case .error = tokenRead {
            isUpdatingConsentState = true
            contactsUploadConsentOwner = nil
            if contactsUploadConsent { contactsUploadConsent = false }
            isUpdatingConsentState = false
            return
        }
        let markerPending: Bool
        switch marker {
        case .missing, .value("complete"):
            markerPending = false
        case .value:
            markerPending = true
        case .error:
            return
        }
        let hasPending = recoveryCommitPending || pendingAccountRecovery != nil || (pendingRecovery ?? markerPending)
        let rawConsent = persistence.defaults.bool(forKey: Self.contactsUploadConsentKey)
        let storedOwner = (persistence.defaults.string(forKey: Self.contactsUploadConsentOwnerKey) ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let activeId = contractorId.trimmingCharacters(in: .whitespacesAndNewlines)

        if !rawConsent {
            isUpdatingConsentState = true
            contactsUploadConsentOwner = nil
            if contactsUploadConsent { contactsUploadConsent = false }
            isUpdatingConsentState = false
            if persistence.defaults.object(forKey: Self.contactsUploadConsentOwnerKey) != nil {
                persistence.defaults.removeObject(forKey: Self.contactsUploadConsentOwnerKey)
            }
            return
        }
        if hasPending {
            revokeContactsUploadConsent()
            return
        }
        let validId: Bool
        if case .value(let kcId) = idRead, kcId.trimmingCharacters(in: .whitespacesAndNewlines) == activeId {
            validId = true
        } else {
            validId = false
        }
        if !storedOwner.isEmpty {
            let valid = !activeId.isEmpty && activeId == storedOwner && validId && !persistence.didMigrateLegacyKey("contractorId")
            if valid {
                isUpdatingConsentState = true
                contactsUploadConsentOwner = storedOwner
                if !contactsUploadConsent { contactsUploadConsent = true }
                isUpdatingConsentState = false
            } else {
                revokeContactsUploadConsent()
            }
            return
        }

        if case .value = tokenRead {
            _ = KeychainManager.removeNonemptyLegacyValue(forKey: "contractorApiToken", in: persistence.defaults)
        }
        let validToken: Bool
        if case .value(let tok) = tokenRead, !tok.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            validToken = true
        } else {
            validToken = false
        }
        let valid = isOnboarded
            && !activeId.isEmpty
            && validId
            && validToken
            && !persistence.didMigrateLegacyKey("contractorId")
            && !persistence.didMigrateLegacyKey("contractorApiToken")
        if valid {
            isUpdatingConsentState = true
            contactsUploadConsentOwner = activeId
            if !contactsUploadConsent { contactsUploadConsent = true }
            isUpdatingConsentState = false
            persistence.defaults.set(activeId, forKey: Self.contactsUploadConsentOwnerKey)
        } else {
            revokeContactsUploadConsent()
        }
    }

    // Carrier type for forwarding codes. Verizon uses *71/*73; everyone else uses GSM codes.
    // Set during onboarding forwarding step; editable from Settings.
    @Published var isVerizonCarrier: Bool = false {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { self.persistence.defaults.set(self.isVerizonCarrier, forKey: "isVerizonCarrier") }
        }
    }

    @Published var sitToneEnabled: Bool = false {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { self.persistence.defaults.set(self.sitToneEnabled, forKey: "sitToneEnabled") }
        }
    }
    @Published var autoReplySms: Bool = false {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { self.persistence.defaults.set(self.autoReplySms, forKey: "autoReplySms") }
        }
    }
    @Published var smartInterruption: Bool = true {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { self.persistence.defaults.set(self.smartInterruption, forKey: "smartInterruption") }
        }
    }
    @Published var kevinLanguage: String = "auto" {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { self.persistence.defaults.set(self.kevinLanguage, forKey: "kevinLanguage") }
        }
    }

    // Account country (ISO 3166-1 alpha-2), root-authoritative on the server;
    // mirrored here so forwarding codes key on it. Empty = not yet loaded.
    @Published var countryCode: String = "" {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { self.persistence.defaults.set(self.countryCode, forKey: "countryCode") }
        }
    }

    private func loadReadCallIds(for contractor: String) -> Set<String> {
        guard !contractor.isEmpty else { return [] }
        let arr = persistence.defaults.stringArray(forKey: "readCallIds_\(contractor)") ?? []
        return Set(arr)
    }

    // Unread calls — tracked locally by call ID, scoped per contractor with synchronous persistence
    @Published var readCallIds: Set<String> = [] {
        didSet {
            if inScreenshotFixture || inMemory { return }
            let contractor = contractorId
            guard !contractor.isEmpty else { return }
            persistence.defaults.set(Array(self.readCallIds), forKey: "readCallIds_\(contractor)")
        }
    }

    func markCallAsRead(_ callId: String) {
        readCallIds.insert(callId)
    }

    func isCallUnread(_ call: CallRecord) -> Bool {
        if call.readOnServer { return false }
        return call.hasMessage && !readCallIds.contains(call.id)
    }

    @Published var unreadCallCount: Int = 0

    func updateUnreadCount(calls: [CallRecord]) {
        unreadCallCount = calls.filter { isCallUnread($0) }.count
    }

    func pruneReadCallIds(validIds: Set<String>) {
        readCallIds = readCallIds.intersection(validIds)
    }

    // Re-auth flag — set to true when server returns 401 (token expired/invalid)
    @Published var needsReauth: Bool = false

    // Integrations
    @Published var jobberConnected: Bool = false {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { self.persistence.defaults.set(self.jobberConnected, forKey: "jobberConnected") }
        }
    }
    @Published var googleCalendarConnected: Bool = false {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { self.persistence.defaults.set(self.googleCalendarConnected, forKey: "googleCalendarConnected") }
        }
    }

    /// Current snapshot of authorization context
    func currentAuthContext() -> CallAuthContext {
        if let custom = authProviderClosure?() {
            return custom
        }
        #if DEBUG
        if inScreenshotFixture {
            return CallAuthContext(
                contractorId: contractorId.isEmpty ? "app-store-screenshot-fixture" : contractorId,
                bearerToken: "fixture-token",
                generation: sessionGeneration
            )
        }
        #endif
        return CallAuthContext(
            contractorId: contractorId,
            bearerToken: inMemory ? inMemoryBearerToken : persistence.contractorToken,
            generation: sessionGeneration
        )
    }

    /// Whether there's an active call owned by current auth
    var hasActiveCall: Bool {
        ownedActiveCallLease != nil
    }

    /// Set active call state from producing boundary with required producing auth context
    func setActiveCall(callSid: String, callerPhone: String, callerName: String, authContext: CallAuthContext) {
        guard authContext.isValid, authContext == currentAuthContext(), !callSid.isEmpty else { return }
        let isSameCall = (activeCallSid == callSid && activeCallAuth == authContext)
        if !isSameCall {
            activeCallSid = callSid
            activeCallAuth = authContext
            activeCallerPhone = callerPhone
            activeCallerName = callerName
            activeCallReason = ""
            capturedScreeningTranscript = nil
            transcriptLines = []
            callIgnored = false
            callStartTime = Date()
            isOnCall = false
        } else {
            activeCallerPhone = callerPhone
            activeCallerName = callerName
        }
    }

    /// Captures in-memory frozen screening transcript snapshot for the exact valid presentation lease
    func captureScreeningTranscript(for lease: CallPresentationLease) {
        let currentAuth = currentAuthContext()
        let currentScope = callLifecycleSnapshot
        guard lease.isValid(auth: currentAuth, scope: currentScope) else { return }
        if let existing = capturedScreeningTranscript, existing.lease == lease {
            return
        }
        capturedScreeningTranscript = CapturedScreeningTranscript(
            lease: lease,
            lines: transcriptLines.map(\.text),
            reason: activeCallReason
        )
    }

    /// Retrieves captured screening transcript only if matching a valid presentation lease
    func screeningTranscript(for lease: CallPresentationLease) -> CapturedScreeningTranscript? {
        guard let snapshot = capturedScreeningTranscript,
              snapshot.lease == lease,
              lease.isValid(auth: currentAuthContext(), scope: callLifecycleSnapshot) else {
            return nil
        }
        return snapshot
    }

    /// Clears captured screening transcript scoped to its own presentation lease
    func clearScreeningTranscript(for lease: CallPresentationLease) {
        if let current = capturedScreeningTranscript, current.lease == lease {
            capturedScreeningTranscript = nil
        }
    }

    /// Updates active call reason with origin auth and owned SID validation
    func updateActiveCallReason(reason: String, authContext: CallAuthContext, callSid: String) {
        guard let origin = activeCallAuth,
              origin == authContext,
              origin == currentAuthContext(),
              origin.isValid,
              activeCallSid == callSid,
              !callSid.isEmpty else {
            return
        }
        let trimmed = reason.trimmingCharacters(in: .whitespacesAndNewlines)
        self.activeCallReason = trimmed == "Speaking with Kevin" ? "" : String(trimmed.prefix(160))
    }

    /// Updates active call reason using presentation lease
    func updateActiveCallReason(reason: String, lease: CallPresentationLease) {
        guard lease.isValid(auth: currentAuthContext(), scope: callLifecycleSnapshot) else { return }
        updateActiveCallReason(reason: reason, authContext: lease.auth, callSid: lease.scope.callSid)
    }

    /// Updates active call transcript with origin auth and owned SID validation
    func updateActiveCallTranscript(text: String, authContext: CallAuthContext, callSid: String) {
        guard let origin = activeCallAuth,
              origin == authContext,
              origin == currentAuthContext(),
              origin.isValid,
              activeCallSid == callSid,
              !callSid.isEmpty else {
            return
        }
        self.transcriptLines = text.components(separatedBy: "\n").filter { !$0.isEmpty }.map { TranscriptLine(text: $0) }
    }

    /// Updates active call transcript lines with origin auth and owned SID validation
    func updateActiveCallTranscript(lines: [TranscriptLine], authContext: CallAuthContext, callSid: String) {
        guard let origin = activeCallAuth,
              origin == authContext,
              origin == currentAuthContext(),
              origin.isValid,
              activeCallSid == callSid,
              !callSid.isEmpty else {
            return
        }
        self.transcriptLines = lines
    }

    /// Updates active call transcript using presentation lease
    func updateActiveCallTranscript(text: String, lease: CallPresentationLease) {
        updateActiveCallTranscript(text: text, authContext: lease.auth, callSid: lease.scope.callSid)
    }

    /// Updates active call transcript lines using presentation lease
    func updateActiveCallTranscript(lines: [TranscriptLine], lease: CallPresentationLease) {
        updateActiveCallTranscript(lines: lines, authContext: lease.auth, callSid: lease.scope.callSid)
    }

    /// Clear active call state
    func clearActiveCall() {
        activeCallAuth = nil
        activeCallSid = ""
        activeCallerPhone = ""
        activeCallerName = ""
        activeCallReason = ""
        capturedScreeningTranscript = nil
        showActiveCall = false
        transcriptLines = []
        callStartTime = nil
        callIgnored = false
        isOnCall = false
    }

    /// Check backend for an active call (used on app foreground)
    func checkForActiveCall() {
        Task { @MainActor in
            self.refreshSecureStorageForActiveUse()
            LiveCallObserver.shared.requestRefresh()
        }
    }

    /// A marker is written before any credential replacement. An interrupted
    /// multi-item Keychain commit must remain in recovery on the next launch.
    @MainActor
    func beginAccountRecovery(checkpoint: AccountRecoveryCheckpoint? = nil) {
        let checkpoint = checkpoint ?? pendingAccountRecovery ?? AccountRecoveryCheckpoint(
            contractorId: contractorId, appleUserId: appleUserId, allowsUnfinishedSetup: false)
        recoveryCommitPending = true
        pendingAccountRecovery = checkpoint
        isRegistered = false
        sessionState = checkpoint.allowsUnfinishedSetup && !isOnboarded ? .notOnboarded : .needsRecovery
        if (!inMemory || injectedSecureStore != nil) && !inScreenshotFixture,
           let data = try? JSONEncoder().encode(checkpoint), let marker = String(data: data, encoding: .utf8) {
            _ = secureStore.save(Self.recoveryMarkerKey, value: marker)
        }
    }

    @MainActor
    func persistRecoveredCredentials(_ values: [String: String], checkpoint: AccountRecoveryCheckpoint? = nil) -> Bool {
        let checkpoint = checkpoint ?? AccountRecoveryCheckpoint(contractorId: values["contractorId"] ?? "",
            appleUserId: values["appleUserId"] ?? "", allowsUnfinishedSetup: false)
        recoveryCommitPending = true
        pendingAccountRecovery = checkpoint
        if inMemory && injectedSecureStore == nil { return true }
        guard let data = try? JSONEncoder().encode(checkpoint), let marker = String(data: data, encoding: .utf8),
              secureStore.save(Self.recoveryMarkerKey, value: marker),
              secureStore.read(Self.recoveryMarkerKey) == .value(marker) else { return false }
        for key in values.keys.sorted() {
            guard let value = values[key], secureStore.save(key, value: value),
                  secureStore.read(key) == .value(value) else { return false }
        }
        if !inMemory && !inScreenshotFixture {
            for key in ["contractorId", "contractorApiToken", "appleUserId"] {
                _ = KeychainManager.removeNonemptyLegacyValue(forKey: key, in: persistence.defaults)
                persistence.clearMigratedLegacyKey(key)
            }
        }
        return true
    }

    @MainActor
    func finishAccountRecovery(bearerToken: String) -> Bool {
        if !inMemory || injectedSecureStore != nil {
            guard secureStore.save(Self.recoveryMarkerKey, value: "complete"),
                  secureStore.read(Self.recoveryMarkerKey) == .value("complete") else { return false }
        }
        if inMemory {
            inMemoryBearerToken = bearerToken
            inMemoryGeneration += 1
        } else {
            // The staged Keychain write already replaced the token, so a
            // token setter comparing its new value cannot advance this epoch.
            persistence.epoch.advance()
        }
        recoveryCommitPending = false
        pendingAccountRecovery = nil
        needsReauth = false
        isRegistered = false
        sessionState = isOnboarded ? .ready : .notOnboarded
        return true
    }

    @MainActor
    func refreshSecureStorageForActiveUse() {
        if inScreenshotFixture { sessionState = .ready; return }
        if inMemory && injectedSecureStore == nil {
            if !isOnboarded && !recoveryCommitPending { sessionState = .notOnboarded }
            return
        }

        let marker = secureStore.read(Self.recoveryMarkerKey)
        let storedCheckpoint: AccountRecoveryCheckpoint?
        switch marker {
        case .missing, .value("complete"): storedCheckpoint = nil
        case .value("pending"):
            storedCheckpoint = AccountRecoveryCheckpoint(contractorId: "", appleUserId: "", allowsUnfinishedSetup: false)
        case .value(let value):
            guard let data = value.data(using: .utf8),
                  let checkpoint = try? JSONDecoder().decode(AccountRecoveryCheckpoint.self, from: data),
                  !checkpoint.allowsUnfinishedSetup || (!checkpoint.contractorId.isEmpty && !checkpoint.appleUserId.isEmpty) else {
                sessionState = .storageUnavailable(reason: "Secure storage is temporarily unavailable")
                return
            }
            storedCheckpoint = checkpoint
        case .error:
            sessionState = .storageUnavailable(reason: "Secure storage is temporarily unavailable")
            return
        }
        let pending = recoveryCommitPending ? pendingAccountRecovery : storedCheckpoint
        // A clean installation can also contain an interrupted returning-user
        // commit. Inspect it before permitting ordinary onboarding.
        guard isOnboarded || pending != nil else {
            if !inMemory {
                reconcilePersistedConsent(pendingRecovery: false)
            }
            sessionState = .notOnboarded
            return
        }
        var reads = Dictionary(uniqueKeysWithValues: Self.keychainBackedKeys.map { ($0, secureStore.read($0)) })
        for result in [marker] + Array(reads.values) {
            if case .error = result {
                sessionState = .storageUnavailable(reason: "Secure storage is temporarily unavailable")
                return
            }
        }
        if !inMemory {
            for key in ["contractorId", "contractorApiToken", "appleUserId"] {
                switch reads[key] {
                case .missing:
                    let migrated = injectedSecureStore == nil
                        ? persistence.readMigratingLegacy(key)
                        : secureStore.readMigratingLegacy(key, defaults: persistence.defaults)
                    guard case .error = migrated else { reads[key] = migrated; continue }
                    sessionState = .storageUnavailable(reason: "Secure storage is temporarily unavailable")
                    return
                case .value:
                    _ = KeychainManager.removeNonemptyLegacyValue(forKey: key, in: persistence.defaults)
                case .error, .none:
                    break
                }
            }
        }
        // Surviving identity constrains recovery even if the other credential
        // is missing after an initially unavailable Keychain read.
        let retainedId = pending.flatMap { $0.contractorId.isEmpty ? nil : $0.contractorId } ?? reads["contractorId"]?.stringValue
        let retainedApple = pending.flatMap { $0.appleUserId.isEmpty ? nil : $0.appleUserId } ?? reads["appleUserId"]?.stringValue
        if let cid = retainedId, !cid.isEmpty, contractorId != cid { contractorId = cid }
        if let apple = retainedApple, !apple.isEmpty, appleUserId != apple { appleUserId = apple }
        if !inMemory {
            reconcilePersistedConsent(pendingRecovery: pending != nil)
        }
        if let pending {
            if !recoveryCommitPending {
                // After an interrupted commit, the stored ID may be staged.
                // Cached consent cannot establish its original owner.
                revokeContactsUploadConsent()
            }
            recoveryCommitPending = true
            pendingAccountRecovery = pending
            sessionState = pending.allowsUnfinishedSetup && !isOnboarded ? .notOnboarded : .needsRecovery
            return
        }
        guard let cid = reads["contractorId"]?.stringValue, !cid.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              let token = reads["contractorApiToken"]?.stringValue, !token.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            sessionState = .needsRecovery
            return
        }
        guard secureStore.migrateAccessibility(for: Self.keychainBackedKeys) else {
            sessionState = .storageUnavailable(reason: "Secure storage is temporarily unavailable")
            return
        }
        if inMemory { inMemoryBearerToken = token }
        hydrateRemainingKeychainItems()
        sessionState = .ready
    }

    private func hydrateRemainingKeychainItems() {
        if let value = secureStore.retrieve("appleUserId") { appleUserId = value }
        if let value = secureStore.retrieve("subscriptionUUID") { subscriptionUUID = value }
        if let value = secureStore.retrieve("subscriptionStatus"), !value.isEmpty { subscriptionStatus = value }
        if let value = secureStore.retrieve("subscriptionTier"), !value.isEmpty { subscriptionTier = value }
    }
}
