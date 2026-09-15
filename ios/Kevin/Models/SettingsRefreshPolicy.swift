import Foundation

/// Snapshot of draft and transient editing values held locally in SettingsHost.
struct SettingsDraftSnapshot: Equatable, Sendable {
    var knowledgeText: String
    var regulatoryAddress: String
    var regulatoryCity: String
    var businessHoursStart: String // "HH:mm" formatted
    var businessHoursEnd: String   // "HH:mm" formatted
    var isKnowledgeEditorOpen: Bool
    var isKnowledgeDirty: Bool

    init(
        knowledgeText: String = "",
        regulatoryAddress: String = "",
        regulatoryCity: String = "",
        businessHoursStart: String = "08:00",
        businessHoursEnd: String = "17:00",
        isKnowledgeEditorOpen: Bool = false,
        isKnowledgeDirty: Bool = false
    ) {
        self.knowledgeText = knowledgeText
        self.regulatoryAddress = regulatoryAddress
        self.regulatoryCity = regulatoryCity
        self.businessHoursStart = businessHoursStart
        self.businessHoursEnd = businessHoursEnd
        self.isKnowledgeEditorOpen = isKnowledgeEditorOpen
        self.isKnowledgeDirty = isKnowledgeDirty
    }
}

/// Baseline of values confirmed by server or successfully saved locally.
struct SettingsConfirmedBaseline: Equatable, Sendable {
    var knowledgeText: String
    var regulatoryAddress: String
    var regulatoryCity: String
    var businessHoursStart: String
    var businessHoursEnd: String
    var smartInterruption: Bool
    var ringThroughContacts: Bool
    var sitToneEnabled: Bool
    var countryCode: String
    var mode: String

    init(
        knowledgeText: String = "",
        regulatoryAddress: String = "",
        regulatoryCity: String = "",
        businessHoursStart: String = "08:00",
        businessHoursEnd: String = "17:00",
        smartInterruption: Bool = true,
        ringThroughContacts: Bool = true,
        sitToneEnabled: Bool = false,
        countryCode: String = "",
        mode: String = "business"
    ) {
        self.knowledgeText = knowledgeText
        self.regulatoryAddress = regulatoryAddress
        self.regulatoryCity = regulatoryCity
        self.businessHoursStart = businessHoursStart
        self.businessHoursEnd = businessHoursEnd
        self.smartInterruption = smartInterruption
        self.ringThroughContacts = ringThroughContacts
        self.sitToneEnabled = sitToneEnabled
        self.countryCode = countryCode
        self.mode = mode
    }
}

/// Projection of all guarded settings state: confirmed baseline, draft snapshot, and AppState/UI selections.
struct SettingsGuardedStateProjection: Equatable, Sendable {
    var baseline: SettingsConfirmedBaseline
    var drafts: SettingsDraftSnapshot
    var appStateBusinessAddress: String
    var appStateBusinessCity: String
    var appStateSmartInterruption: Bool
    var appStateRingThroughContacts: Bool
    var appStateSitToneEnabled: Bool
    var appStateCountryCode: String
    var appStateMode: String
    var countrySelection: String
    var smartInterruptionSelection: Bool

    init(
        baseline: SettingsConfirmedBaseline = SettingsConfirmedBaseline(),
        drafts: SettingsDraftSnapshot = SettingsDraftSnapshot(),
        appStateBusinessAddress: String = "",
        appStateBusinessCity: String = "",
        appStateSmartInterruption: Bool = true,
        appStateRingThroughContacts: Bool = true,
        appStateSitToneEnabled: Bool = false,
        appStateCountryCode: String = "",
        appStateMode: String = "business",
        countrySelection: String = "",
        smartInterruptionSelection: Bool = true
    ) {
        self.baseline = baseline
        self.drafts = drafts
        self.appStateBusinessAddress = appStateBusinessAddress
        self.appStateBusinessCity = appStateBusinessCity
        self.appStateSmartInterruption = appStateSmartInterruption
        self.appStateRingThroughContacts = appStateRingThroughContacts
        self.appStateSitToneEnabled = appStateSitToneEnabled
        self.appStateCountryCode = appStateCountryCode
        self.appStateMode = appStateMode
        self.countrySelection = countrySelection
        self.smartInterruptionSelection = smartInterruptionSelection
    }
}

/// Per-field decisions for hydrating a server profile without overwriting active drafts or fenced writes.
struct SettingsHydrationDecision: Equatable, Sendable {
    var shouldUpdateKnowledge: Bool
    var shouldUpdateRegulatoryAddress: Bool
    var shouldUpdateRegulatoryCity: Bool
    var shouldUpdateBusinessHours: Bool
    var shouldUpdateSmartInterruption: Bool
    var shouldUpdateScreenAllCalls: Bool
    var shouldUpdateSitTone: Bool
    var shouldUpdateCountry: Bool
    var shouldUpdateMode: Bool

    init(
        shouldUpdateKnowledge: Bool = false,
        shouldUpdateRegulatoryAddress: Bool = false,
        shouldUpdateRegulatoryCity: Bool = false,
        shouldUpdateBusinessHours: Bool = false,
        shouldUpdateSmartInterruption: Bool = false,
        shouldUpdateScreenAllCalls: Bool = false,
        shouldUpdateSitTone: Bool = false,
        shouldUpdateCountry: Bool = false,
        shouldUpdateMode: Bool = false
    ) {
        self.shouldUpdateKnowledge = shouldUpdateKnowledge
        self.shouldUpdateRegulatoryAddress = shouldUpdateRegulatoryAddress
        self.shouldUpdateRegulatoryCity = shouldUpdateRegulatoryCity
        self.shouldUpdateBusinessHours = shouldUpdateBusinessHours
        self.shouldUpdateSmartInterruption = shouldUpdateSmartInterruption
        self.shouldUpdateScreenAllCalls = shouldUpdateScreenAllCalls
        self.shouldUpdateSitTone = shouldUpdateSitTone
        self.shouldUpdateCountry = shouldUpdateCountry
        self.shouldUpdateMode = shouldUpdateMode
    }
}

/// Field-scoped mutation revisions tracking user saves for specific profile properties.
struct SettingsFieldMutationRevisions: Equatable, Sendable {
    enum Field: Sendable {
        case knowledge
        case regulatoryAddress
        case country
        case mode
    }

    var knowledge: UInt64
    var regulatoryAddress: UInt64
    var country: UInt64
    var mode: UInt64

    init(
        knowledge: UInt64 = 0,
        regulatoryAddress: UInt64 = 0,
        country: UInt64 = 0,
        mode: UInt64 = 0
    ) {
        self.knowledge = knowledge
        self.regulatoryAddress = regulatoryAddress
        self.country = country
        self.mode = mode
    }

    mutating func increment(field: Field) {
        switch field {
        case .knowledge:
            knowledge &+= 1
        case .regulatoryAddress:
            regulatoryAddress &+= 1
        case .country:
            country &+= 1
        case .mode:
            mode &+= 1
        }
    }
}

typealias SettingsMutableField = SettingsFieldMutationRevisions.Field

/// Pure policy rules for SettingsHost hydration, coalescing, and draft protection.
enum SettingsRefreshPolicy {

    static let defaultBusinessHoursStart = "08:00"
    static let defaultBusinessHoursEnd = "17:00"

    static func defaultBusinessHoursStartDate(calendar: Calendar = .current) -> Date {
        calendar.date(from: DateComponents(hour: 8, minute: 0)) ?? Date()
    }

    static func defaultBusinessHoursEndDate(calendar: Calendar = .current) -> Date {
        calendar.date(from: DateComponents(hour: 17, minute: 0)) ?? Date()
    }

    /// Resets draft snapshot and confirmed baseline to fresh defaults upon an authentication switch.
    static func resetStateOnAuthChange(
        businessAddress: String = "",
        businessCity: String = "",
        smartInterruption: Bool = true,
        ringThroughContacts: Bool = true,
        sitToneEnabled: Bool = false,
        countryCode: String = "",
        mode: String = "business"
    ) -> (baseline: SettingsConfirmedBaseline, drafts: SettingsDraftSnapshot) {
        let baseline = SettingsConfirmedBaseline(
            knowledgeText: "",
            regulatoryAddress: businessAddress,
            regulatoryCity: businessCity,
            businessHoursStart: defaultBusinessHoursStart,
            businessHoursEnd: defaultBusinessHoursEnd,
            smartInterruption: smartInterruption,
            ringThroughContacts: ringThroughContacts,
            sitToneEnabled: sitToneEnabled,
            countryCode: countryCode,
            mode: mode
        )
        let drafts = SettingsDraftSnapshot(
            knowledgeText: "",
            regulatoryAddress: businessAddress,
            regulatoryCity: businessCity,
            businessHoursStart: defaultBusinessHoursStart,
            businessHoursEnd: defaultBusinessHoursEnd,
            isKnowledgeEditorOpen: false,
            isKnowledgeDirty: false
        )
        return (baseline, drafts)
    }

    /// Evaluates which fields from a server profile payload may safely hydrate local state.
    static func evaluateHydration(
        baseline: SettingsConfirmedBaseline,
        currentDrafts: SettingsDraftSnapshot,
        urgentFencePermits: Bool,
        screenAllFencePermits: Bool,
        sitToneFencePermits: Bool,
        hoursFencePermits: Bool,
        isSavingRegulatoryAddress: Bool,
        isSavingCountry: Bool,
        isSwitchingMode: Bool,
        knowledgeRevisionMatches: Bool = true,
        regulatoryAddressRevisionMatches: Bool = true,
        countryRevisionMatches: Bool = true,
        modeRevisionMatches: Bool = true
    ) -> SettingsHydrationDecision {
        // Knowledge: preserve draft dirty before AND during load even if editor later closes
        let knowledgeDirty = currentDrafts.isKnowledgeDirty
            || currentDrafts.isKnowledgeEditorOpen
            || (currentDrafts.knowledgeText != baseline.knowledgeText)
            || !knowledgeRevisionMatches
        let updateKnowledge = !knowledgeDirty

        // Regulatory address & city: do not overwrite if user has typed a draft or is currently saving
        let addressDirty = isSavingRegulatoryAddress
            || (currentDrafts.regulatoryAddress != baseline.regulatoryAddress)
            || !regulatoryAddressRevisionMatches
        let updateAddress = !addressDirty

        let cityDirty = isSavingRegulatoryAddress
            || (currentDrafts.regulatoryCity != baseline.regulatoryCity)
            || !regulatoryAddressRevisionMatches
        let updateCity = !cityDirty

        // Business hours: do not overwrite if fence does not permit (saving/dirty/failed unsaved)
        let hoursDirty = !hoursFencePermits
            || (currentDrafts.businessHoursStart != baseline.businessHoursStart || currentDrafts.businessHoursEnd != baseline.businessHoursEnd)
        let updateHours = hoursFencePermits && !hoursDirty

        return SettingsHydrationDecision(
            shouldUpdateKnowledge: updateKnowledge,
            shouldUpdateRegulatoryAddress: updateAddress,
            shouldUpdateRegulatoryCity: updateCity,
            shouldUpdateBusinessHours: updateHours,
            shouldUpdateSmartInterruption: urgentFencePermits,
            shouldUpdateScreenAllCalls: screenAllFencePermits,
            shouldUpdateSitTone: sitToneFencePermits,
            shouldUpdateCountry: !isSavingCountry && countryRevisionMatches,
            shouldUpdateMode: !isSwitchingMode && modeRevisionMatches
        )
    }

    /// Applies incoming profile payload values to guarded state according to the hydration decision.
    /// Preserves confirmed baseline, AppState, and local drafts whenever hydration is denied.
    static func applyGuardedProfile(
        contractor: [String: Any],
        decision: SettingsHydrationDecision,
        state: inout SettingsGuardedStateProjection
    ) {
        if decision.shouldUpdateMode {
            let mode = contractor["effective_mode"] as? String ?? contractor["mode"] as? String ?? "personal"
            let resolved = (mode == "personal") ? "personal" : "business"
            state.appStateMode = resolved
            state.baseline.mode = resolved
        }

        if decision.shouldUpdateCountry {
            if let country = SettingsCountry.accountCountry(from: contractor) {
                state.appStateCountryCode = country
                state.countrySelection = country
                state.baseline.countryCode = country
            }
        }

        if decision.shouldUpdateKnowledge {
            if let serverKnowledge = contractor["knowledge"] as? String {
                state.baseline.knowledgeText = serverKnowledge
                state.drafts.knowledgeText = serverKnowledge
                state.drafts.isKnowledgeDirty = false
            }
        }

        if decision.shouldUpdateRegulatoryAddress {
            let bizAddress = contractor["business_address"] as? String ?? ""
            if !bizAddress.isEmpty {
                state.baseline.regulatoryAddress = bizAddress
                state.appStateBusinessAddress = bizAddress
                state.drafts.regulatoryAddress = bizAddress
            }
        }

        if decision.shouldUpdateRegulatoryCity {
            let bizCity = contractor["business_city"] as? String ?? ""
            if !bizCity.isEmpty {
                state.baseline.regulatoryCity = bizCity
                state.appStateBusinessCity = bizCity
                state.drafts.regulatoryCity = bizCity
            }
        }

        if decision.shouldUpdateSmartInterruption {
            let smartInterruption = contractor["smart_interruption"] as? Bool ?? true
            state.baseline.smartInterruption = smartInterruption
            state.appStateSmartInterruption = smartInterruption
            state.smartInterruptionSelection = smartInterruption
        }

        if decision.shouldUpdateScreenAllCalls {
            let ringThrough = contractor["ring_through_contacts"] as? Bool ?? true
            state.baseline.ringThroughContacts = ringThrough
            state.appStateRingThroughContacts = ringThrough
        }

        if decision.shouldUpdateSitTone {
            let sitTone = contractor["sit_tone_enabled"] as? Bool ?? false
            state.baseline.sitToneEnabled = sitTone
            state.appStateSitToneEnabled = sitTone
        }

        if decision.shouldUpdateBusinessHours {
            if let startStr = contractor["business_hours_start"] as? String,
               let endStr = contractor["business_hours_end"] as? String {
                state.baseline.businessHoursStart = startStr
                state.baseline.businessHoursEnd = endStr
                state.drafts.businessHoursStart = startStr
                state.drafts.businessHoursEnd = endStr
            }
        }
    }

    /// Formats Date into "HH:mm" using the en_US_POSIX locale.
    static func formatHours(date: Date, timeZone: TimeZone = .current) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = timeZone
        formatter.dateFormat = "HH:mm"
        return formatter.string(from: date)
    }

    /// Parses "HH:mm" string into Date using the en_US_POSIX locale.
    static func parseHours(string: String, timeZone: TimeZone = .current) -> Date? {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = timeZone
        formatter.dateFormat = "HH:mm"
        return formatter.date(from: string)
    }
}

/// Mutually exclusive paywall routes in SettingsHost.
enum SettingsPaywallDestination: Equatable, Sendable {
    case assistant
    case account
}

/// Production hydrator executing profile hydration safely on MainActor.
@MainActor
struct SettingsProfileHydrator {
    struct CapturedRevisions: Equatable, Sendable {
        var urgentRevision: Int
        var screenAllRevision: Int
        var sitToneRevision: Int
        var hoursRevision: Int
        var fieldMutationRevisions: SettingsFieldMutationRevisions

        init(
            urgentRevision: Int = 0,
            screenAllRevision: Int = 0,
            sitToneRevision: Int = 0,
            hoursRevision: Int = 0,
            fieldMutationRevisions: SettingsFieldMutationRevisions = SettingsFieldMutationRevisions()
        ) {
            self.urgentRevision = urgentRevision
            self.screenAllRevision = screenAllRevision
            self.sitToneRevision = sitToneRevision
            self.hoursRevision = hoursRevision
            self.fieldMutationRevisions = fieldMutationRevisions
        }
    }

    struct FencePermits: Equatable, Sendable {
        var urgentFencePermits: Bool
        var screenAllFencePermits: Bool
        var sitToneFencePermits: Bool
        var hoursFencePermits: Bool
        var isSavingRegulatoryAddress: Bool
        var isSavingCountry: Bool
        var isSwitchingMode: Bool

        init(
            urgentFencePermits: Bool = true,
            screenAllFencePermits: Bool = true,
            sitToneFencePermits: Bool = true,
            hoursFencePermits: Bool = true,
            isSavingRegulatoryAddress: Bool = false,
            isSavingCountry: Bool = false,
            isSwitchingMode: Bool = false
        ) {
            self.urgentFencePermits = urgentFencePermits
            self.screenAllFencePermits = screenAllFencePermits
            self.sitToneFencePermits = sitToneFencePermits
            self.hoursFencePermits = hoursFencePermits
            self.isSavingRegulatoryAddress = isSavingRegulatoryAddress
            self.isSavingCountry = isSavingCountry
            self.isSwitchingMode = isSwitchingMode
        }
    }

    static func hydrate(
        capturedAuth: CallAuthContext,
        capturedRevisions: CapturedRevisions,
        fetchProfile: () async -> [String: Any]?,
        ownsOperation: () -> Bool,
        currentDraftProvider: () -> SettingsDraftSnapshot,
        baselineProvider: () -> SettingsConfirmedBaseline,
        fenceProvider: (CapturedRevisions) -> FencePermits,
        currentFieldRevisions: () -> SettingsFieldMutationRevisions,
        applyProfile: ([String: Any], SettingsHydrationDecision) -> Void
    ) async -> Bool {
        // 1. Await the profile fetch (do NOT evaluate drafts before await)
        guard let profile = await fetchProfile() else {
            return false
        }

        // 2. Check ownership after fetch completes
        guard ownsOperation() else {
            return false
        }

        // 3. Obtain fresh current drafts, baseline, fence permissions, and field mutation revisions
        let currentDrafts = currentDraftProvider()
        let baseline = baselineProvider()
        let fencePermits = fenceProvider(capturedRevisions)
        let currentRevs = currentFieldRevisions()
        let knowledgeMatches = (capturedRevisions.fieldMutationRevisions.knowledge == currentRevs.knowledge)
        let addressMatches = (capturedRevisions.fieldMutationRevisions.regulatoryAddress == currentRevs.regulatoryAddress)
        let countryMatches = (capturedRevisions.fieldMutationRevisions.country == currentRevs.country)
        let modeMatches = (capturedRevisions.fieldMutationRevisions.mode == currentRevs.mode)

        // 4. Evaluate hydration decisions using pure policy
        let decision = SettingsRefreshPolicy.evaluateHydration(
            baseline: baseline,
            currentDrafts: currentDrafts,
            urgentFencePermits: fencePermits.urgentFencePermits,
            screenAllFencePermits: fencePermits.screenAllFencePermits,
            sitToneFencePermits: fencePermits.sitToneFencePermits,
            hoursFencePermits: fencePermits.hoursFencePermits,
            isSavingRegulatoryAddress: fencePermits.isSavingRegulatoryAddress,
            isSavingCountry: fencePermits.isSavingCountry,
            isSwitchingMode: fencePermits.isSwitchingMode,
            knowledgeRevisionMatches: knowledgeMatches,
            regulatoryAddressRevisionMatches: addressMatches,
            countryRevisionMatches: countryMatches,
            modeRevisionMatches: modeMatches
        )

        // 5. Check ownership again before applying
        guard ownsOperation() else {
            return false
        }

        // 6. Apply profile synchronously
        applyProfile(profile, decision)
        return true
    }
}

/// Production deletion confirmation loader managing asynchronous profile inspection and presentation ticket ownership.
@MainActor
final class SettingsDeletionConfirmationLoader {
    private var activeTicket: UInt64 = 0
    private var nextTicket: UInt64 = 1
    private(set) var lookupTask: Task<Void, Never>?

    init() {}

    var isLoading: Bool {
        lookupTask != nil
    }

    func requestConfirmation(
        auth: CallAuthContext,
        isAccountPresented: @escaping () -> Bool,
        currentAuth: @escaping () -> CallAuthContext,
        cachedStatus: String,
        cachedTier: String,
        fetchProfile: @escaping @Sendable (CallAuthContext) async -> [String: Any]?,
        onReady: @escaping (AccountDeletionFirstStep) -> Void
    ) {
        guard auth.isValid else { return }

        lookupTask?.cancel()
        lookupTask = nil

        let ticket = nextTicket
        nextTicket &+= 1
        activeTicket = ticket

        lookupTask = Task { @MainActor [weak self] in
            let profile = await fetchProfile(auth)

            guard let self = self else { return }
            guard !Task.isCancelled else { return }
            guard self.activeTicket == ticket else { return }
            guard currentAuth() == auth else { return }
            guard isAccountPresented() else { return }

            let resolved = AccountDeletionFlow.resolve(
                freshStatus: profile?["subscription_status"] as? String,
                freshTier: profile?["subscription_tier"] as? String,
                cachedStatus: cachedStatus,
                cachedTier: cachedTier
            )

            let step = AccountDeletionFlow.firstStep(
                subscriptionStatus: resolved.status,
                subscriptionTier: resolved.tier
            )

            guard !Task.isCancelled else { return }
            guard self.activeTicket == ticket else { return }
            guard currentAuth() == auth else { return }
            guard isAccountPresented() else { return }

            self.lookupTask = nil
            onReady(step)
        }
    }

    func cancel() {
        lookupTask?.cancel()
        lookupTask = nil
        activeTicket = 0
    }
}

/// Coordinator controlling load ownership, coalescing, and draft protection for SettingsHost.
@MainActor
final class SettingsLoadCoordinator {
    struct LoadPlan: Equatable, Sendable {
        let shouldFetchProfile: Bool
        let shouldCheckIntegrations: Bool
        let flightToken: UInt64
        let authContext: CallAuthContext
    }

    private(set) var currentAuth: CallAuthContext?
    private(set) var hasLoadedProfile: Bool = false
    private(set) var activeFlightToken: UInt64 = 0
    private(set) var nextFlightToken: UInt64 = 1
    private(set) var isFlightActive: Bool = false
    private(set) var fieldMutationRevisions = SettingsFieldMutationRevisions()

    init() {}

    /// Record a user edit or mutation for a specific field to increment its revision.
    func recordMutation(field: SettingsFieldMutationRevisions.Field) {
        fieldMutationRevisions.increment(field: field)
    }

    /// Reset state when authentication context changes.
    func handleAuthChange(newAuth: CallAuthContext) {
        if currentAuth != newAuth {
            currentAuth = newAuth
            hasLoadedProfile = false
            isFlightActive = false
            activeFlightToken = 0
            fieldMutationRevisions = SettingsFieldMutationRevisions()
        }
    }

    /// Begin a load request. Returns a LoadPlan if started, or nil if coalesced with an active flight.
    func beginLoad(
        auth: CallAuthContext,
        isBusinessMode: Bool,
        isForegroundOrTabSwitch: Bool = false,
        forceProfileReload: Bool = false
    ) -> LoadPlan? {
        guard auth.isValid else { return nil }

        if currentAuth != auth {
            handleAuthChange(newAuth: auth)
        }

        if isFlightActive {
            // Coalesce: request is already in-flight for this auth context
            return nil
        }

        let shouldFetchProfile = forceProfileReload || !hasLoadedProfile
        let shouldCheckIntegrations = isBusinessMode

        isFlightActive = true
        let token = nextFlightToken
        nextFlightToken &+= 1
        activeFlightToken = token

        return LoadPlan(
            shouldFetchProfile: shouldFetchProfile,
            shouldCheckIntegrations: shouldCheckIntegrations,
            flightToken: token,
            authContext: auth
        )
    }

    /// Finish a load flight. Returns true if the flight is still owned and matches the current auth.
    func finishLoad(
        flightToken: UInt64,
        auth: CallAuthContext,
        profileFetchSucceeded: Bool
    ) -> Bool {
        guard currentAuth == auth, activeFlightToken == flightToken else {
            // Stale finish or auth changed — do not clear or mutate active flight
            return false
        }
        isFlightActive = false
        if profileFetchSucceeded {
            hasLoadedProfile = true
        }
        return true
    }

    /// Invalidate any active flight and clear state.
    func invalidate() {
        isFlightActive = false
        activeFlightToken = 0
        hasLoadedProfile = false
        currentAuth = nil
        fieldMutationRevisions = SettingsFieldMutationRevisions()
    }
}

/// An immutable token identifying an active account deletion operation owned by a specific authorization context.
struct AccountDeletionToken: Equatable, Sendable {
    let id: UInt64
    let auth: CallAuthContext
}

/// Operation fence coordinating single-flight account deletion execution, cross-sheet persistence, and auth ownership.
struct AccountDeletionFence: Equatable, Sendable {
    private(set) var currentAuth: CallAuthContext? = nil
    private(set) var activeToken: UInt64? = nil
    private var nextToken: UInt64 = 1

    var isPending: Bool {
        activeToken != nil
    }

    mutating func begin(auth: CallAuthContext) -> AccountDeletionToken? {
        guard auth.isValid else { return nil }
        if currentAuth != auth {
            reset(for: auth)
        }
        guard activeToken == nil else { return nil }
        let token = nextToken
        nextToken &+= 1
        activeToken = token
        currentAuth = auth
        return AccountDeletionToken(id: token, auth: auth)
    }

    mutating func finish(token: AccountDeletionToken, auth: CallAuthContext) -> Bool {
        guard currentAuth == auth, activeToken == token.id, token.auth == auth else {
            return false
        }
        activeToken = nil
        return true
    }

    mutating func reset(for newAuth: CallAuthContext? = nil) {
        currentAuth = newAuth
        activeToken = nil
    }
}
