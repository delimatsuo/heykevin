import SwiftUI
import UserNotifications
import AVFoundation
import Speech

private func debugLog(_ message: String) {
    #if DEBUG
    print(message)
    #endif
}

/// User-facing text for a failed `RegulatoryAddress.validate` result.
private func regulatoryAddressErrorMessage(for result: RegulatoryAddress.ValidationResult) -> String {
    switch result {
    case .valid:
        return ""
    case .missingAddress:
        return String(localized: "Business address is required for your country.")
    case .missingCity:
        return String(localized: "City is required for your country.")
    case .addressTooLong:
        return String(localized: "Business address must be 500 characters or fewer.")
    case .cityTooLong:
        return String(localized: "City must be 100 characters or fewer.")
    }
}

/// Persistent single state owner for all Settings and Assistant configuration.
/// Supplies the Kevin tab (assistantScreen) as a stable AnyView to the root and
/// owns the presentation of the Account sheet.
struct SettingsHost<Root: View>: View {
    @EnvironmentObject var appState: AppState
    @Environment(\.scenePhase) private var scenePhase

    @Binding var isAccountPresented: Bool
    var shouldScrollToGoogleCalendar: Binding<Bool>? = nil
    var onDismissAccount: (() -> Void)? = nil
    let assistantIsSelected: Bool
    let onOpenCall: (CallPresentationLease) -> Void
    let root: (AnyView) -> Root

    // MARK: - State

    @State private var activePaywallDestination: SettingsPaywallDestination? = nil
    @State private var loadCoordinator = SettingsLoadCoordinator()
    @State private var confirmedBaseline = SettingsConfirmedBaseline(
        regulatoryAddress: AppState.shared.businessAddress,
        regulatoryCity: AppState.shared.businessCity,
        businessHoursStart: "08:00",
        businessHoursEnd: "17:00",
        smartInterruption: AppState.shared.smartInterruption,
        ringThroughContacts: AppState.shared.ringThroughContacts,
        sitToneEnabled: AppState.shared.sitToneEnabled,
        countryCode: AppState.shared.countryCode,
        mode: AppState.shared.isPersonalMode ? "personal" : "business"
    )
    @State private var isKnowledgeDirty = false
    @State private var deletionLoader = SettingsDeletionConfirmationLoader()

    @State private var showDeleteAccountAlert = false
    @State private var showDeleteAccountError = false
    @State private var accountDeletionFence = AccountDeletionFence()
    @State private var showSubscriptionWarningAlert = false
    @State private var confirmDeleteTask: Task<Void, Never>?

    private let alertRedismissalDelay: UInt64 = 700_000_000

    @State private var showAboutDebug = false
    @State private var showKnowledgeEditor = false
    @State private var knowledgeText = ""
    @State private var websiteURL = ""
    @State private var isImporting = false
    @State private var importMessage = ""
    @State private var syncMessage = ""
    @State private var showModeChangeAlert = false
    @State private var isSwitchingMode = false
    @State private var modeChangeError = ""
    @State private var isSaving = false
    @State private var saveError = ""
    @State private var pushPermission: UNAuthorizationStatus = .notDetermined
    @State private var isProvisioningNumber = false
    @State private var businessHoursStart = Calendar.current.date(from: DateComponents(hour: 8, minute: 0)) ?? Date()
    @State private var businessHoursEnd = Calendar.current.date(from: DateComponents(hour: 17, minute: 0)) ?? Date()
    @State private var isSavingBusinessHours = false
    @State private var businessHoursSaveError = ""
    @State private var forwardingInstructions: ForwardingInstructions?
    @State private var countrySelection = SettingsCountryFlow.displayedSelection(accountCountry: "")
    @State private var isSavingCountry = false
    @State private var countrySaveError = ""
    @State private var smartInterruptionSelection = AppState.shared.smartInterruption
    @State private var isSavingSmartInterruption = false
    @State private var smartInterruptionSaveError = ""

    // Fences
    @State private var urgentPreferenceFence = PreferenceWriteFence()
    @State private var screenAllCallsFence = PreferenceWriteFence()
    @State private var sitToneFence = PreferenceWriteFence()
    @State private var businessHoursFence = PreferenceWriteFence()

    // Screen all calls state
    @State private var isSavingScreenAllCalls = false
    @State private var screenAllCallsSaveError = ""

    // SIT Tone save state
    @State private var isSavingSitTone = false
    @State private var sitToneSaveError = ""

    // Regulatory address drafts
    @State private var regulatoryAddressDraft = AppState.shared.businessAddress
    @State private var regulatoryCityDraft = AppState.shared.businessCity
    @State private var isSavingRegulatoryAddress = false
    @State private var regulatoryAddressError = ""

    @State private var pendingCallLeaseToOpen: CallPresentationLease? = nil

    private var forwardingCountry: String { ForwardingCountry.resolve(accountCountry: appState.countryCode) }
    private var kevinNumber: String { appState.kevinNumber }

    private var isFixtureMode: Bool {
        #if DEBUG
        return AppStoreScreenshotFixtures.isEnabled
        #else
        return false
        #endif
    }

    private var assistantPaywallBinding: Binding<Bool> {
        Binding(
            get: { activePaywallDestination == .assistant },
            set: { isPresented in
                if !isPresented && activePaywallDestination == .assistant {
                    activePaywallDestination = nil
                }
            }
        )
    }

    private var accountPaywallBinding: Binding<Bool> {
        Binding(
            get: { activePaywallDestination == .account },
            set: { isPresented in
                if !isPresented && activePaywallDestination == .account {
                    activePaywallDestination = nil
                }
            }
        )
    }

    private func presentPaywall(from destination: SettingsPaywallDestination) {
        #if DEBUG
        if isFixtureMode { return }
        #endif
        activePaywallDestination = destination
    }

    init(
        isAccountPresented: Binding<Bool>,
        shouldScrollToGoogleCalendar: Binding<Bool>? = nil,
        onDismissAccount: (() -> Void)? = nil,
        assistantIsSelected: Bool,
        onOpenCall: @escaping (CallPresentationLease) -> Void,
        root: @escaping (AnyView) -> Root
    ) {
        self._isAccountPresented = isAccountPresented
        self.shouldScrollToGoogleCalendar = shouldScrollToGoogleCalendar
        self.onDismissAccount = onDismissAccount
        self.assistantIsSelected = assistantIsSelected
        self.onOpenCall = onOpenCall
        self.root = root
    }

    var body: some View {
        root(AnyView(assistantScreenView))
            .sheet(isPresented: $isAccountPresented, onDismiss: {
                cancelPendingDeleteConfirmation()
                if activePaywallDestination == .account {
                    activePaywallDestination = nil
                }
                onDismissAccount?()
                if let lease = pendingCallLeaseToOpen {
                    pendingCallLeaseToOpen = nil
                    let currentAuth = appState.currentAuthContext()
                    let currentScope = appState.callLifecycleSnapshot
                    if lease.isValid(auth: currentAuth, scope: currentScope) {
                        onOpenCall(lease)
                    }
                }
            }) {
                NavigationStack {
                    accountSheetView
                }
            }
            .task {
                await coalesceLoad()
            }
            .onChange(of: scenePhase) { _, phase in
                if phase == .active {
                    Task { await coalesceLoad(isForegroundOrTabSwitch: true) }
                }
            }
            .onChange(of: assistantIsSelected) { _, isSelected in
                if isSelected {
                    Task { await coalesceLoad(isForegroundOrTabSwitch: true) }
                }
            }
            .onChange(of: appState.countryCode) { _, newCode in
                countrySelection = SettingsCountryFlow.displayedSelection(accountCountry: newCode)
            }
            .onChange(of: appState.currentAuthContext()) { _, newAuth in
                handleAuthChange(newAuth: newAuth)
            }
    }

    // MARK: - Coalesced Loading

    private func coalesceLoad(isForegroundOrTabSwitch: Bool = false) async {
        #if DEBUG
        if isFixtureMode {
            return
        }
        #endif
        let auth = appState.currentAuthContext()
        guard auth.isValid else { return }

        guard let plan = loadCoordinator.beginLoad(
            auth: auth,
            isBusinessMode: !appState.isPersonalMode,
            isForegroundOrTabSwitch: isForegroundOrTabSwitch
        ) else {
            return
        }

        var profileSuccess = false
        if plan.shouldFetchProfile {
            profileSuccess = await loadProfileAndHydrate(capturedAuth: plan.authContext)
        } else {
            profileSuccess = true
        }

        if plan.shouldCheckIntegrations {
            await checkIntegrationsStatus(capturedAuth: plan.authContext)
        }

        await refreshPushPermission()

        _ = loadCoordinator.finishLoad(
            flightToken: plan.flightToken,
            auth: plan.authContext,
            profileFetchSucceeded: profileSuccess
        )
    }

    private func handleAuthChange(newAuth: CallAuthContext) {
        activePaywallDestination = nil
        cancelPendingDeleteConfirmation()
        loadCoordinator.handleAuthChange(newAuth: newAuth)

        urgentPreferenceFence = PreferenceWriteFence()
        screenAllCallsFence = PreferenceWriteFence()
        sitToneFence = PreferenceWriteFence()
        businessHoursFence = PreferenceWriteFence()

        isSavingScreenAllCalls = false
        screenAllCallsSaveError = ""
        isSavingSitTone = false
        sitToneSaveError = ""
        isSavingSmartInterruption = false
        smartInterruptionSaveError = ""
        isSavingCountry = false
        countrySaveError = ""
        isSavingRegulatoryAddress = false
        regulatoryAddressError = ""
        isSavingBusinessHours = false
        businessHoursSaveError = ""
        isSwitchingMode = false
        modeChangeError = ""
        isImporting = false
        importMessage = ""
        syncMessage = ""
        isProvisioningNumber = false
        accountDeletionFence.reset(for: newAuth)
        showDeleteAccountAlert = false
        showDeleteAccountError = false
        showSubscriptionWarningAlert = false

        let reset = SettingsRefreshPolicy.resetStateOnAuthChange(
            businessAddress: appState.businessAddress,
            businessCity: appState.businessCity,
            smartInterruption: appState.smartInterruption,
            ringThroughContacts: appState.ringThroughContacts,
            sitToneEnabled: appState.sitToneEnabled,
            countryCode: appState.countryCode,
            mode: appState.isPersonalMode ? "personal" : "business"
        )
        confirmedBaseline = reset.baseline
        knowledgeText = reset.drafts.knowledgeText
        isKnowledgeDirty = reset.drafts.isKnowledgeDirty
        regulatoryAddressDraft = reset.drafts.regulatoryAddress
        regulatoryCityDraft = reset.drafts.regulatoryCity
        businessHoursStart = SettingsRefreshPolicy.defaultBusinessHoursStartDate()
        businessHoursEnd = SettingsRefreshPolicy.defaultBusinessHoursEndDate()
        countrySelection = SettingsCountryFlow.displayedSelection(accountCountry: appState.countryCode)
        smartInterruptionSelection = appState.smartInterruption

        Task {
            await coalesceLoad()
        }
    }

    // MARK: - Assistant Screen (Kevin Tab)

    private var assistantScreenView: some View {
        ScrollViewReader { proxy in
            Form {
                if let lease = appState.ownedActiveCallLease {
                    Section {
                        CompactReturnToCallCard(lease: lease) { validLease in
                            onOpenCall(validLease)
                        }
                    }
                    .listRowInsets(EdgeInsets())
                    .listRowBackground(Color.clear)
                }

                setupStatusSection
                    .disabled(isFixtureMode)
                    .accessibilityIdentifier("settings.section.setup")

                howKevinAnswersSection
                    .disabled(isFixtureMode)
                    .accessibilityIdentifier("settings.section.howKevinAnswers")

                if !appState.isPersonalMode {
                    yourBusinessSection
                        .disabled(isFixtureMode)
                        .accessibilityIdentifier("settings.section.yourBusiness")
                    knowledgeBaseSection
                        .disabled(isFixtureMode)
                        .accessibilityIdentifier("settings.section.knowledgeBase")
                    integrationsSection
                        .disabled(isFixtureMode)
                        .accessibilityIdentifier("settings.section.integrations")
                }

                callForwardingSection
                    .disabled(isFixtureMode)
                    .accessibilityIdentifier("settings.section.callForwarding")
            }
            .navigationTitle(String(localized: "Kevin"))
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        isAccountPresented = true
                    } label: {
                        HStack(spacing: 4) {
                            Image(systemName: "person.crop.circle")
                            Text(String(localized: "Settings"))
                        }
                        .frame(minWidth: 44, minHeight: 44)
                        .contentShape(Rectangle())
                    }
                    .accessibilityIdentifier("nav.settings")
                    .accessibilityLabel(String(localized: "Settings"))
                }
            }
            .onChange(of: shouldScrollToGoogleCalendar?.wrappedValue ?? false) { _, shouldScroll in
                if shouldScroll {
                    withAnimation {
                        proxy.scrollTo("google_calendar_section", anchor: .top)
                    }
                    shouldScrollToGoogleCalendar?.wrappedValue = false
                }
            }
            .sheet(isPresented: $showKnowledgeEditor, onDismiss: {
                if knowledgeText != confirmedBaseline.knowledgeText {
                    isKnowledgeDirty = true
                }
            }) {
                KnowledgeEditorView(knowledgeText: $knowledgeText)
            }
            .sheet(isPresented: assistantPaywallBinding) {
                PaywallView(canDismiss: true)
                    .environmentObject(appState)
            }
            .alert(String(localized: "Change How Kevin Answers"), isPresented: $showModeChangeAlert) {
                Button(String(localized: "Switch")) {
                    Task { await switchMode() }
                }
                Button(String(localized: "Cancel"), role: .cancel) {}
            } message: {
                Text(appState.isPersonalMode
                     ? String(localized: "Kevin will become your business receptionist: smart intake questions, business hours, and a knowledge base for FAQs. Your Kevin number will be kept.")
                     : String(localized: "Kevin will switch to personal screening: unknown callers are screened, saved contacts ring through. Your business setup and Kevin number will be kept."))
            }
        }
    }

    // MARK: - Account Sheet View

    private var accountSheetView: some View {
        Form {
            if let lease = appState.ownedActiveCallLease {
                Section {
                    CompactReturnToCallCard(lease: lease) { validLease in
                        pendingCallLeaseToOpen = validLease
                        isAccountPresented = false
                    }
                }
                .listRowInsets(EdgeInsets())
                .listRowBackground(Color.clear)
            }

            accountAndPlanSection
                .disabled(isFixtureMode)
                .accessibilityIdentifier("settings.section.accountAndPlan")

            deleteAccountSection
                .disabled(isFixtureMode)
                .accessibilityIdentifier("settings.section.deleteAccount")

            feedbackSection
                .disabled(isFixtureMode)
                .accessibilityIdentifier("settings.section.feedback")

            legalSection
                .disabled(isFixtureMode)
                .accessibilityIdentifier("settings.section.legal")

            aboutSection
                .disabled(isFixtureMode)
                .accessibilityIdentifier("settings.section.about")
        }
        .navigationTitle(String(localized: "Account"))
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .confirmationAction) {
                Button(String(localized: "Done")) {
                    isAccountPresented = false
                }
                .font(.headline)
                .accessibilityIdentifier("settings.done")
            }
        }
        .sheet(isPresented: accountPaywallBinding) {
            PaywallView(canDismiss: true)
                .environmentObject(appState)
        }
    }

    // MARK: - Sections: Kevin Tab

    private var setupStatusSection: some View {
        let numberOK = !appState.kevinNumber.isEmpty
        let pushOK = pushPermission == .authorized || pushPermission == .provisional
        let subOK = appState.subscriptionStatus == "trial" || appState.subscriptionStatus == "active"
        let allGood = numberOK && pushOK && subOK && appState.forwardingActivated

        return Section {
            if allGood {
                Label(String(localized: "Setup steps completed"), systemImage: "checkmark.circle.fill")
                    .foregroundStyle(.green)
                    .font(.subheadline.weight(.medium))
            }

            if numberOK {
                HStack(spacing: 12) {
                    Image(systemName: "checkmark.circle.fill")
                        .font(.title3)
                        .hidden()

                    Text(String(localized: "Kevin Number"))
                    Spacer()
                    Text(PhoneFormatter.format(kevinNumber))
                        .foregroundStyle(Color.secondary)
                        .textSelection(.enabled)
                }
            } else {
                SetupRow(
                    title: String(localized: "Kevin Number"),
                    ok: false,
                    okLabel: PhoneFormatter.format(kevinNumber),
                    failLabel: String(localized: "No number assigned")
                ) {
                    #if DEBUG
                    if isFixtureMode { return }
                    #endif
                    if isProvisioningNumber { return }
                    isProvisioningNumber = true
                    Task {
                        await provisionNumberFromSettings()
                        isProvisioningNumber = false
                    }
                } actionLabel: {
                    if isProvisioningNumber {
                        AnyView(ProgressView().scaleEffect(0.8))
                    } else {
                        AnyView(Text(String(localized: "Get Number")).font(.caption.weight(.medium)).foregroundStyle(.blue))
                    }
                }
            }

            if !allGood {
                HStack(spacing: 12) {
                    Image(systemName: appState.forwardingActivated ? "checkmark.circle.fill" : "exclamationmark.circle.fill")
                        .foregroundStyle(appState.forwardingActivated ? Color.green : Color.orange)
                        .font(.title3)

                    VStack(alignment: .leading, spacing: 2) {
                        Text(String(localized: "Call Forwarding"))
                            .font(.subheadline)
                        Text(appState.forwardingActivated ? String(localized: "Activation requested") : String(localized: "Missed calls must route to Kevin"))
                            .font(.caption)
                            .foregroundStyle(appState.forwardingActivated ? Color.secondary : Color.orange)
                    }
                    Spacer()
                    Button {
                        #if DEBUG
                        if isFixtureMode { return }
                        #endif
                        if !appState.kevinNumber.isEmpty {
                            dialCode(forwardingCodes.activate)
                            #if !DEBUG
                            UserDefaults.standard.set(appState.kevinNumber, forKey: "forwardingActivatedFor")
                            #endif
                            appState.forwardingActivated = true
                        }
                    } label: {
                        Text(appState.forwardingActivated ? String(localized: "Re-activate") : String(localized: "Set up forwarding"))
                            .font(.caption.weight(.medium))
                            .foregroundStyle(appState.kevinNumber.isEmpty ? Color.secondary : (appState.forwardingActivated ? Color.blue : Color.orange))
                            .padding(.horizontal, 10)
                            .padding(.vertical, 5)
                            .background((appState.forwardingActivated ? Color.blue : Color.orange).opacity(appState.kevinNumber.isEmpty ? 0.05 : 0.15))
                            .clipShape(Capsule())
                    }
                    .buttonStyle(.borderless)
                    .disabled(appState.kevinNumber.isEmpty)
                }

                SetupRow(
                    title: String(localized: "Push Notifications"),
                    ok: pushOK,
                    okLabel: String(localized: "Enabled"),
                    failLabel: pushPermission == .denied ? String(localized: "Blocked in iOS Settings") : String(localized: "Not enabled")
                ) {
                    #if DEBUG
                    if isFixtureMode { return }
                    #endif
                    if pushPermission == .denied {
                        if let url = URL(string: UIApplication.openNotificationSettingsURLString) {
                            UIApplication.shared.open(url)
                        }
                    } else {
                        AppDelegate.requestPushAuthorization { _ in
                            Task { await refreshPushPermission() }
                        }
                    }
                } actionLabel: {
                    AnyView(
                        Text(pushPermission == .denied
                             ? String(localized: "Open Settings")
                             : String(localized: "Enable"))
                            .font(.caption.weight(.medium))
                            .foregroundStyle(.blue)
                    )
                }

                SetupRow(
                    title: String(localized: "Subscription"),
                    ok: subOK,
                    okLabel: planLabel,
                    failLabel: planLabel
                ) {
                    presentPaywall(from: .assistant)
                } actionLabel: {
                    AnyView(Text(String(localized: "Subscribe")).font(.caption.weight(.medium)).foregroundStyle(.blue))
                }
            }
        } header: {
            Text(allGood ? String(localized: "Kevin") : String(localized: "Setup Status"))
        }
    }

    private var howKevinAnswersSection: some View {
        Section {
            // Screen all calls toggle (moved from Recents)
            Toggle(isOn: screenAllCallsBinding) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(String(localized: "Screen all calls"))
                        .font(.subheadline.weight(.medium))
                    Text(!appState.ringThroughContacts
                         ? String(localized: "Kevin screens everyone, including contacts")
                         : String(localized: "Contacts bypass Kevin and ring directly"))
                        .font(.caption)
                        .foregroundStyle(Color.secondary)
                }
            }
            .disabled(isSavingScreenAllCalls || isFixtureMode)
            .accessibilityIdentifier("kevin.screenAllCalls")

            if !screenAllCallsSaveError.isEmpty {
                Text(screenAllCallsSaveError)
                    .font(.caption)
                    .foregroundStyle(.red)
            }

            Toggle(String(localized: "Block spam with disconnect tone"), isOn: sitToneBinding)
                .disabled(isSavingSitTone || isFixtureMode)

            if !sitToneSaveError.isEmpty {
                Text(sitToneSaveError)
                    .font(.caption)
                    .foregroundStyle(.red)
            }

            Toggle(String(localized: "Alert me for urgent calls"), isOn: smartInterruptionBinding)
                .disabled(isSavingSmartInterruption || isFixtureMode)

            if !smartInterruptionSaveError.isEmpty {
                Text(smartInterruptionSaveError)
                    .font(.caption)
                    .foregroundStyle(.red)
            }

            if appState.isPersonalMode {
                Button {
                    #if DEBUG
                    if isFixtureMode {
                        syncMessage = String(localized: "Synced 42 contacts")
                        return
                    }
                    #endif
                    Task {
                        appState.contactsUploadConsent = true
                        let result = await ContactSyncManager.shared.syncContacts(
                            contractorId: appState.contractorId,
                            force: true
                        )
                        switch result {
                        case .success(let synced, _):
                            syncMessage = String(localized: "Synced \(synced) contacts")
                        case .permissionDenied:
                            syncMessage = String(localized: "Contacts permission denied")
                        case .rateLimited:
                            syncMessage = String(localized: "Please wait before syncing again")
                        case .error(let msg):
                            syncMessage = String(localized: "Error: \(msg)")
                        }
                    }
                } label: {
                    HStack {
                        Text(String(localized: "Sync Contacts"))
                            .font(.subheadline)
                        Spacer()
                        if !syncMessage.isEmpty {
                            Text(syncMessage)
                                .font(.caption)
                                .foregroundStyle(syncMessage.contains("Error") || syncMessage.contains("denied") ? .red : .green)
                        }
                        Image(systemName: "arrow.triangle.2.circlepath")
                            .foregroundStyle(.blue)
                    }
                }
                .disabled(isFixtureMode)
            }

            Button {
                #if DEBUG
                if isFixtureMode { return }
                #endif
                showModeChangeAlert = true
            } label: {
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(appState.isPersonalMode
                             ? String(localized: "Use Kevin for Your Business")
                             : String(localized: "Switch to Personal Screening"))
                            .font(.subheadline.weight(.medium))
                            .foregroundStyle(.blue)
                        Text(appState.isPersonalMode
                             ? String(localized: "Receptionist mode: intake questions, business hours, knowledge base. Requires a Business plan.")
                             : String(localized: "Kevin only screens unknown callers and takes messages. Your business setup is kept."))
                            .font(.caption)
                            .foregroundStyle(Color.secondary)
                    }
                    Spacer()
                    if isSwitchingMode {
                        ProgressView()
                    } else {
                        Image(systemName: "arrow.triangle.2.circlepath")
                            .foregroundStyle(Color(uiColor: .tertiaryLabel))
                            .font(.caption)
                    }
                }
            }
            .disabled(isSwitchingMode || isFixtureMode)

            if !modeChangeError.isEmpty {
                Text(modeChangeError)
                    .font(.caption)
                    .foregroundStyle(.red)
            }

            if !saveError.isEmpty {
                Text(saveError)
                    .font(.caption)
                    .foregroundStyle(.red)
            }
        } header: {
            Text(String(localized: "How Kevin Answers"))
        } footer: {
            Text(appState.isPersonalMode
                 ? String(localized: "Choose how Kevin screens your calls. Enable urgent call alerts to be notified about urgent issues.")
                 : String(localized: "Kevin answers using your business setup. Enable urgent call alerts to be notified about urgent issues."))
        }
    }

    private var yourBusinessSection: some View {
        Section {
            HStack {
                Text(String(localized: "Business"))
                Spacer()
                Text(appState.businessName.isEmpty ? String(localized: "Not set") : appState.businessName)
                    .foregroundStyle(appState.businessName.isEmpty ? .tertiary : .secondary)
            }

            HStack {
                DatePicker(String(localized: "Open"), selection: Binding(
                    get: { businessHoursStart },
                    set: { newDate in
                        businessHoursStart = newDate
                        saveBusinessHoursExplicitly()
                    }
                ), displayedComponents: .hourAndMinute)
                .disabled(isSavingBusinessHours || isFixtureMode)
                .accessibilityIdentifier("settings.businessHours.start")

                if isSavingBusinessHours {
                    ProgressView()
                        .scaleEffect(0.8)
                }
            }

            HStack {
                DatePicker(String(localized: "Close"), selection: Binding(
                    get: { businessHoursEnd },
                    set: { newDate in
                        businessHoursEnd = newDate
                        saveBusinessHoursExplicitly()
                    }
                ), displayedComponents: .hourAndMinute)
                .disabled(isSavingBusinessHours || isFixtureMode)
                .accessibilityIdentifier("settings.businessHours.end")

                if isSavingBusinessHours {
                    ProgressView()
                        .scaleEffect(0.8)
                }
            }

            if !businessHoursSaveError.isEmpty {
                HStack {
                    Text(businessHoursSaveError)
                        .font(.caption)
                        .foregroundStyle(.red)
                    Spacer()
                    Button(String(localized: "Retry")) {
                        saveBusinessHoursExplicitly()
                    }
                    .font(.caption.weight(.medium))
                    .accessibilityIdentifier("settings.businessHours.retry")
                }
            }
        } header: {
            Text(String(localized: "Your Business"))
        } footer: {
            Text(String(localized: "Outside these hours, Kevin will tell callers you're closed and take a message."))
        }
    }

    private var knowledgeBaseSection: some View {
        Section {
            NavigationLink {
                ServicesView()
            } label: {
                VStack(alignment: .leading, spacing: 2) {
                    Text(String(localized: "Services & Pricing"))
                        .font(.subheadline)
                    Text(String(localized: "Add your services so Kevin can quote estimates"))
                        .font(.caption)
                        .foregroundStyle(Color.secondary)
                }
            }

            Button {
                #if DEBUG
                if isFixtureMode { return }
                #endif
                showKnowledgeEditor = true
            } label: {
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(String(localized: "Business Knowledge"))
                            .font(.subheadline)
                            .foregroundStyle(Color.primary)
                        Text(String(localized: "Tell Kevin about your business so he can answer questions"))
                            .font(.caption)
                            .foregroundStyle(Color.secondary)
                    }
                    Spacer()
                    Image(systemName: "chevron.right")
                        .font(.footnote.weight(.semibold))
                        .foregroundStyle(Color(uiColor: .tertiaryLabel))
                }
            }

            HStack {
                TextField(String(localized: "Website URL"), text: $websiteURL)
                    .textContentType(.URL)
                    .keyboardType(.URL)
                    .autocapitalization(.none)
                    .font(.subheadline)
                    .disabled(isFixtureMode)

                if isImporting {
                    ProgressView()
                        .scaleEffect(0.8)
                } else {
                    Button(String(localized: "Import")) {
                        Task { await importWebsite() }
                    }
                    .disabled(websiteURL.isEmpty || isFixtureMode)
                }
            }

            if !importMessage.isEmpty {
                Text(importMessage)
                    .font(.caption)
                    .foregroundStyle(importMessage.contains("Failed") ? .red : .green)
            }
        } header: {
            Text(String(localized: "Knowledge Base"))
        } footer: {
            Text(String(localized: "Kevin uses this info to answer caller questions about your services, pricing, and hours."))
        }
    }

    private var integrationsSection: some View {
        Section {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(String(localized: "Jobber"))
                        .font(.subheadline.weight(.medium))
                    Text(String(localized: "Schedule checking, job creation, customer lookup"))
                        .font(.caption)
                        .foregroundStyle(Color.secondary)
                }
                Spacer()
                if appState.jobberConnected {
                    Button(role: .destructive) {
                        Task { await disconnectJobber() }
                    } label: {
                        Text(String(localized: "Disconnect"))
                            .font(.caption)
                    }
                    .buttonStyle(.borderless)
                    .disabled(isFixtureMode)
                } else {
                    Button {
                        Task { await connectJobber() }
                    } label: {
                        Text(String(localized: "Connect"))
                            .font(.caption.weight(.medium))
                            .foregroundStyle(.blue)
                            .padding(.horizontal, 10)
                            .padding(.vertical, 5)
                            .background(Color.blue.opacity(0.12))
                            .clipShape(Capsule())
                    }
                    .buttonStyle(.borderless)
                    .disabled(isFixtureMode)
                }
            }

            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(String(localized: "Google Calendar"))
                        .font(.subheadline.weight(.medium))
                    Text(String(localized: "Availability checking, appointment requests"))
                        .font(.caption)
                        .foregroundStyle(Color.secondary)
                }
                Spacer()
                if appState.googleCalendarConnected {
                    Button(role: .destructive) {
                        Task { await disconnectGoogleCalendar() }
                    } label: {
                        Text(String(localized: "Disconnect"))
                            .font(.caption)
                    }
                    .buttonStyle(.borderless)
                    .disabled(isFixtureMode)
                } else {
                    Button {
                        Task { await connectGoogleCalendar() }
                    } label: {
                        Text(String(localized: "Connect"))
                            .font(.caption.weight(.medium))
                            .foregroundStyle(.blue)
                            .padding(.horizontal, 10)
                            .padding(.vertical, 5)
                            .background(Color.blue.opacity(0.12))
                            .clipShape(Capsule())
                    }
                    .buttonStyle(.borderless)
                    .disabled(isFixtureMode)
                }
            }
            .id("google_calendar_section")
        } header: {
            Text(String(localized: "Integrations"))
        } footer: {
            Text(String(localized: "Connect Jobber to let Kevin look up customers and create jobs automatically. Connect Google Calendar so Kevin can offer your open times and send you appointment requests to confirm."))
        }
    }

    private var callForwardingSection: some View {
        Section {
            Group {
                if !ForwardingCountry.isNANP(forwardingCountry) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(String(localized: "Carrier codes"))
                            .font(.subheadline.weight(.medium))
                        Text(String(localized: "Using the call forwarding codes for \(forwardingCountryName)"))
                            .font(.caption)
                            .foregroundStyle(Color.secondary)
                    }
                } else {
                    Toggle(isOn: $appState.isVerizonCarrier) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(String(localized: "I'm a Verizon customer"))
                                .font(.subheadline.weight(.medium))
                            Text(String(localized: "Uses *71 to activate and *73 to deactivate"))
                                .font(.caption)
                                .foregroundStyle(Color.secondary)
                        }
                    }
                }
            }
            .task(id: forwardingCountry) {
                #if DEBUG
                if isFixtureMode { return }
                #endif
                guard !ForwardingCountry.isNANP(forwardingCountry) else { return }
                if let fetched = await APIClient.shared.getForwardingInstructions(countryCode: forwardingCountry) {
                    forwardingInstructions = fetched
                }
            }

            Button {
                dialCode(forwardingCodes.activate)
            } label: {
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(String(localized: "Activate Kevin"))
                            .font(.subheadline.weight(.medium))
                        Text(String(localized: "Forward missed calls to Kevin"))
                            .font(.caption)
                            .foregroundStyle(Color.secondary)
                    }
                    Spacer()
                    Image(systemName: "phone.arrow.right")
                        .foregroundStyle(.green)
                }
            }
            .disabled(isFixtureMode)

            Button(role: .destructive) {
                dialCode(forwardingCodes.deactivate)
            } label: {
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(String(localized: "Deactivate Kevin"))
                            .font(.subheadline.weight(.medium))
                        Text(String(localized: "Stop forwarding, calls ring normally"))
                            .font(.caption)
                            .foregroundStyle(Color.secondary)
                    }
                    Spacer()
                    Image(systemName: "xmark.circle")
                        .foregroundStyle(.red)
                }
            }
            .disabled(isFixtureMode)

            Button(role: .destructive) {
                if let code = forwardingCodes.clearAll { dialCode(code) }
            } label: {
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(String(localized: "Clear All Forwarding"))
                            .font(.subheadline.weight(.medium))
                        Text(String(localized: "Nuclear option — clears every forwarding type at once"))
                            .font(.caption)
                            .foregroundStyle(Color.secondary)
                    }
                    Spacer()
                    Image(systemName: "exclamationmark.octagon")
                        .foregroundStyle(.red)
                }
            }
            .disabled(forwardingCodes.clearAll == nil || isFixtureMode)
        } header: {
            Text(String(localized: "Call Forwarding"))
        } footer: {
            if appState.kevinNumber.isEmpty {
                Text(String(localized: "You need a Kevin number before setting up forwarding. Please contact support."))
                    .foregroundStyle(.orange)
            } else {
                Text(ForwardingCountry.isNANP(forwardingCountry)
                    ? String(localized: "Tapping opens your phone dialer. Tap Call to confirm. If you're on Verizon, turn on the toggle above so the correct codes are used.")
                    : String(localized: "Tapping opens your phone dialer. Tap Call to confirm."))
            }
        }
        .disabled(appState.kevinNumber.isEmpty)
    }

    // MARK: - Sections: Account Sheet

    private var accountAndPlanSection: some View {
        Section {
            HStack {
                Text(String(localized: "Name"))
                Spacer()
                Text(appState.userName)
                    .foregroundStyle(Color.secondary)
            }

            HStack {
                Text(String(localized: "Plan"))
                Spacer()
                Text(planLabel)
                    .foregroundStyle(Color.secondary)
            }

            Picker(selection: countryBinding) {
                ForEach(SettingsCountry.supported, id: \.self) { code in
                    Text(SettingsCountry.displayName(code)).tag(code)
                }
            } label: {
                VStack(alignment: .leading, spacing: 2) {
                    Text(String(localized: "Country"))
                    Text(String(localized: "Sets the call forwarding codes Kevin shows you."))
                        .font(.caption)
                        .foregroundStyle(Color.secondary)
                }
            }
            .disabled(isSavingCountry || isFixtureMode)

            if !countrySaveError.isEmpty {
                Text(countrySaveError)
                    .font(.caption)
                    .foregroundStyle(.red)
            }

            if RegulatoryAddress.requiresAddress(countryCode: appState.countryCode) {
                TextField(String(localized: "Business Address"), text: $regulatoryAddressDraft)
                    .textContentType(.fullStreetAddress)
                    .font(.subheadline)
                    .disabled(isFixtureMode)
                TextField(String(localized: "City"), text: $regulatoryCityDraft)
                    .textContentType(.addressCity)
                    .font(.subheadline)
                    .disabled(isFixtureMode)

                if !regulatoryAddressError.isEmpty {
                    Text(regulatoryAddressError)
                        .font(.caption)
                        .foregroundStyle(.red)
                }

                Button {
                    saveRegulatoryAddress()
                } label: {
                    if isSavingRegulatoryAddress {
                        ProgressView()
                    } else {
                        Text(String(localized: "Save Address"))
                    }
                }
                .disabled(isSavingRegulatoryAddress || isFixtureMode)
            }

            Button {
                presentPaywall(from: .account)
            } label: {
                HStack {
                    Text(viewPlansLabel)
                        .foregroundStyle(.blue)
                    Spacer()
                    Image(systemName: "arrow.right.circle.fill")
                        .foregroundStyle(.blue)
                }
            }
            .disabled(isFixtureMode)
            .accessibilityIdentifier("settings.viewPlans")

            if appState.subscriptionStatus == "active" {
                Button {
                    #if DEBUG
                    if isFixtureMode { return }
                    #endif
                    if let url = URL(string: "https://apps.apple.com/account/subscriptions") {
                        UIApplication.shared.open(url)
                    }
                } label: {
                    HStack {
                        Text(String(localized: "Manage Subscription"))
                        Spacer()
                        Image(systemName: "arrow.up.right.square")
                            .foregroundStyle(Color(uiColor: .tertiaryLabel))
                    }
                }
                .foregroundStyle(.primary)
                .disabled(isFixtureMode)
            }
        } header: {
            Text(String(localized: "Account & Plan"))
        }
    }

    private var deleteAccountSection: some View {
        Section {
            Button(role: .destructive) {
                #if DEBUG
                if isFixtureMode { return }
                #endif
                let auth = appState.currentAuthContext()
                guard auth.isValid else { return }
                deletionLoader.requestConfirmation(
                    auth: auth,
                    isAccountPresented: { isAccountPresented },
                    currentAuth: { appState.currentAuthContext() },
                    cachedStatus: appState.subscriptionStatus,
                    cachedTier: appState.subscriptionTier,
                    fetchProfile: { targetAuth in
                        await APIClient.shared.getContractorProfile(
                            contractorId: targetAuth.contractorId,
                            bearerToken: targetAuth.bearerToken
                        )
                    },
                    onReady: { step in
                        switch step {
                        case .warnActiveSubscription:
                            showSubscriptionWarningAlert = true
                        case .confirmDelete:
                            showDeleteAccountAlert = true
                        }
                    }
                )
            } label: {
                if accountDeletionFence.isPending {
                    HStack {
                        Text(String(localized: "Deleting Account…"))
                        Spacer()
                        ProgressView()
                    }
                } else {
                    Text(String(localized: "Delete Account"))
                }
            }
            .disabled(accountDeletionFence.isPending || deletionLoader.isLoading || confirmDeleteTask != nil || isFixtureMode)
        } footer: {
            Text(String(localized: "Releases your Kevin number. Your data is permanently deleted within 30 days. You will need to disable call forwarding manually."))
        }
        .alert(String(localized: "Delete Account"), isPresented: $showDeleteAccountAlert) {
            Button(String(localized: "Delete"), role: .destructive) {
                Task { await deleteAccount() }
            }
            Button(String(localized: "Cancel"), role: .cancel) {}
        } message: {
            Text(String(localized: "This will delete your Kevin account and release your Kevin number. All your data is permanently deleted within 30 days. Make sure to deactivate call forwarding first."))
        }
        .alert(String(localized: AccountDeletionFlow.warningTitle), isPresented: $showSubscriptionWarningAlert) {
            Button(String(localized: "Manage Subscription")) {
                #if DEBUG
                if isFixtureMode { return }
                #endif
                if let url = URL(string: "https://apps.apple.com/account/subscriptions") {
                    UIApplication.shared.open(url)
                }
            }
            Button(String(localized: "Continue Deleting"), role: .destructive) {
                confirmDeleteTask?.cancel()
                confirmDeleteTask = Task {
                    try? await Task.sleep(nanoseconds: alertRedismissalDelay)
                    guard !Task.isCancelled else { return }
                    await MainActor.run {
                        guard !Task.isCancelled else { return }
                        confirmDeleteTask = nil
                        showDeleteAccountAlert = true
                    }
                }
            }
            Button(String(localized: "Cancel"), role: .cancel) {}
        } message: {
            Text(String(localized: AccountDeletionFlow.warningBody))
        }
        .alert(String(localized: "Couldn't Delete Account"), isPresented: $showDeleteAccountError) {
            Button(String(localized: "OK"), role: .cancel) {}
        } message: {
            Text(String(localized: "The server couldn't complete the deletion, so your account is unchanged. Please check your connection and try again."))
        }
    }

    private var feedbackSection: some View {
        Section {
            Button {
                #if DEBUG
                if isFixtureMode { return }
                #endif
                FeedbackSupport.sendFeedback(contractorId: appState.contractorId)
            } label: {
                HStack {
                    Text(String(localized: "Send Feedback"))
                    Spacer()
                    Image(systemName: "envelope")
                        .foregroundStyle(Color(uiColor: .tertiaryLabel))
                }
            }
            .foregroundStyle(.primary)
            .disabled(isFixtureMode)
        } header: {
            Text(String(localized: "Feedback & Support"))
        }
    }

    private var legalSection: some View {
        Section {
            Link(destination: URL(string: "https://heykevin.one/privacy")!) {
                HStack {
                    Text(String(localized: "Privacy Policy"))
                    Spacer()
                    Image(systemName: "arrow.up.right.square")
                        .foregroundStyle(Color(uiColor: .tertiaryLabel))
                }
            }
            .foregroundStyle(.primary)
            .disabled(isFixtureMode)

            Link(destination: URL(string: "https://heykevin.one/terms")!) {
                HStack {
                    Text(String(localized: "Terms of Service"))
                    Spacer()
                    Image(systemName: "arrow.up.right.square")
                        .foregroundStyle(Color(uiColor: .tertiaryLabel))
                }
            }
            .foregroundStyle(.primary)
            .disabled(isFixtureMode)
        } header: {
            Text(String(localized: "Legal"))
        }
    }

    private var aboutSection: some View {
        Section {
            HStack {
                Text(String(localized: "Version"))
                Spacer()
                Text(AppVersionService.marketingVersion())
                    .foregroundStyle(Color.secondary)
            }

            #if DEBUG
            DisclosureGroup(String(localized: "Debug"), isExpanded: $showAboutDebug) {
                if appState.pushToken.isEmpty {
                    Text(String(localized: "Push: Not registered"))
                        .foregroundStyle(.red)
                } else {
                    Text(String(localized: "Push: \(appState.pushToken.prefix(16))..."))
                        .font(.system(.caption2, design: .monospaced))
                        .textSelection(.enabled)
                }
                if !appState.contractorId.isEmpty {
                    Text(String(localized: "ID: \(appState.contractorId)"))
                        .font(.system(.caption2, design: .monospaced))
                        .textSelection(.enabled)
                }
            }
            .font(.subheadline)
            #endif
        } header: {
            Text(String(localized: "About"))
        }
    }

    // MARK: - Bindings and Actions

    private var screenAllCallsBinding: Binding<Bool> {
        Binding(
            get: { !appState.ringThroughContacts },
            set: { userSelectedScreenAll in
                guard !isSavingScreenAllCalls else { return }
                let targetRingThrough = !userSelectedScreenAll
                saveScreenAllCalls(targetRingThrough)
            }
        )
    }

    private func saveScreenAllCalls(_ newRingThrough: Bool) {
        let auth = appState.currentAuthContext()
        guard auth.isValid, !isSavingScreenAllCalls, let operation = screenAllCallsFence.beginSave() else { return }
        screenAllCallsSaveError = ""
        isSavingScreenAllCalls = true
        let previous = appState.ringThroughContacts
        Task { @MainActor in
            #if DEBUG
            if isFixtureMode {
                _ = screenAllCallsFence.finish(operation)
                isSavingScreenAllCalls = false
                appState.ringThroughContacts = newRingThrough
                confirmedBaseline.ringThroughContacts = newRingThrough
                return
            }
            #endif
            let success: Bool
            do {
                success = try await APIClient.shared.patchContractor(
                    auth.contractorId,
                    body: ["ring_through_contacts": newRingThrough],
                    bearerToken: auth.bearerToken
                )
            } catch {
                success = false
            }
            guard screenAllCallsFence.finish(operation) else { return }
            isSavingScreenAllCalls = false
            guard appState.currentAuthContext() == auth else { return }
            if success {
                appState.ringThroughContacts = newRingThrough
                confirmedBaseline.ringThroughContacts = newRingThrough
            } else {
                appState.ringThroughContacts = previous
                screenAllCallsSaveError = String(localized: "Failed to save setting. Please try again.")
            }
        }
    }

    private var sitToneBinding: Binding<Bool> {
        Binding(
            get: { appState.sitToneEnabled },
            set: { newValue in
                guard !isSavingSitTone else { return }
                saveSitTone(newValue)
            }
        )
    }

    private func saveSitTone(_ newValue: Bool) {
        let auth = appState.currentAuthContext()
        guard auth.isValid, !isSavingSitTone, let operation = sitToneFence.beginSave() else { return }
        isSavingSitTone = true
        sitToneSaveError = ""
        let previous = appState.sitToneEnabled
        Task { @MainActor in
            #if DEBUG
            if isFixtureMode {
                _ = sitToneFence.finish(operation)
                isSavingSitTone = false
                appState.sitToneEnabled = newValue
                confirmedBaseline.sitToneEnabled = newValue
                return
            }
            #endif
            let success: Bool
            do {
                success = try await APIClient.shared.patchContractor(
                    auth.contractorId,
                    body: ["sit_tone_enabled": newValue],
                    bearerToken: auth.bearerToken
                )
            } catch {
                success = false
            }
            guard sitToneFence.finish(operation) else { return }
            isSavingSitTone = false
            guard appState.currentAuthContext() == auth else { return }
            if success {
                appState.sitToneEnabled = newValue
                confirmedBaseline.sitToneEnabled = newValue
            } else {
                appState.sitToneEnabled = previous
                sitToneSaveError = String(localized: "Failed to save setting. Please try again.")
            }
        }
    }

    private var smartInterruptionBinding: Binding<Bool> {
        Binding(
            get: { smartInterruptionSelection },
            set: { picked in
                guard !isSavingSmartInterruption, picked != smartInterruptionSelection else { return }
                smartInterruptionSelection = picked
                saveSmartInterruption(picked)
            }
        )
    }

    private func saveSmartInterruption(_ newValue: Bool) {
        let auth = appState.currentAuthContext()
        guard auth.isValid, !isSavingSmartInterruption, let operation = urgentPreferenceFence.beginSave() else { return }
        smartInterruptionSaveError = ""
        isSavingSmartInterruption = true
        let previous = appState.smartInterruption
        Task { @MainActor in
            #if DEBUG
            if isFixtureMode {
                _ = urgentPreferenceFence.finish(operation)
                isSavingSmartInterruption = false
                appState.smartInterruption = newValue
                smartInterruptionSelection = newValue
                confirmedBaseline.smartInterruption = newValue
                return
            }
            #endif
            let success: Bool
            do {
                success = try await APIClient.shared.patchContractor(
                    auth.contractorId,
                    body: ["smart_interruption": newValue],
                    bearerToken: auth.bearerToken
                )
            } catch {
                success = false
            }
            guard urgentPreferenceFence.finish(operation) else { return }
            isSavingSmartInterruption = false
            guard appState.currentAuthContext() == auth else { return }
            if success {
                appState.smartInterruption = newValue
                smartInterruptionSelection = newValue
                confirmedBaseline.smartInterruption = newValue
            } else {
                smartInterruptionSelection = previous
                smartInterruptionSaveError = String(localized: "Failed to save setting. Please try again.")
            }
        }
    }

    private var countryBinding: Binding<String> {
        Binding(
            get: { countrySelection },
            set: { picked in
                countrySelection = picked
                if SettingsCountryFlow.shouldWrite(picked: picked, accountCountry: appState.countryCode) {
                    saveCountry(picked)
                }
            }
        )
    }

    private func saveCountry(_ code: String) {
        let auth = appState.currentAuthContext()
        guard auth.isValid, !isSavingCountry else { return }
        loadCoordinator.recordMutation(field: .country)
        countrySaveError = ""
        isSavingCountry = true
        Task {
            #if DEBUG
            if isFixtureMode {
                isSavingCountry = false
                appState.countryCode = code
                confirmedBaseline.countryCode = code
                return
            }
            #endif
            let returned = await APIClient.shared.updateCountryCode(
                contractorId: auth.contractorId,
                countryCode: code
            )
            await MainActor.run {
                isSavingCountry = false
                guard appState.currentAuthContext() == auth else { return }
                if SettingsCountryFlow.isConfirmed(requested: code, returned: returned) {
                    appState.countryCode = code
                    confirmedBaseline.countryCode = code
                } else {
                    countrySelection = SettingsCountryFlow.displayedSelection(accountCountry: appState.countryCode)
                    countrySaveError = String(localized: "Failed to save setting. Please try again.")
                }
            }
        }
    }

    private func saveRegulatoryAddress() {
        let auth = appState.currentAuthContext()
        guard auth.isValid, !isSavingRegulatoryAddress else { return }
        let result = RegulatoryAddress.validate(address: regulatoryAddressDraft, city: regulatoryCityDraft)
        guard result == .valid else {
            regulatoryAddressError = regulatoryAddressErrorMessage(for: result)
            return
        }
        loadCoordinator.recordMutation(field: .regulatoryAddress)
        regulatoryAddressError = ""
        isSavingRegulatoryAddress = true
        let address = regulatoryAddressDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        let city = regulatoryCityDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        Task {
            #if DEBUG
            if isFixtureMode {
                isSavingRegulatoryAddress = false
                appState.businessAddress = address
                appState.businessCity = city
                regulatoryAddressDraft = address
                regulatoryCityDraft = city
                confirmedBaseline.regulatoryAddress = address
                confirmedBaseline.regulatoryCity = city
                return
            }
            #endif
            let success = await APIClient.shared.updateBusinessAddress(
                contractorId: auth.contractorId,
                address: address,
                city: city
            )
            await MainActor.run {
                isSavingRegulatoryAddress = false
                guard appState.currentAuthContext() == auth else { return }
                if success {
                    appState.businessAddress = address
                    appState.businessCity = city
                    regulatoryAddressDraft = address
                    regulatoryCityDraft = city
                    confirmedBaseline.regulatoryAddress = address
                    confirmedBaseline.regulatoryCity = city
                } else {
                    regulatoryAddressError = String(localized: "Failed to save setting. Please try again.")
                }
            }
        }
    }

    private func saveBusinessHoursExplicitly() {
        let auth = appState.currentAuthContext()
        guard auth.isValid, !isSavingBusinessHours, let operation = businessHoursFence.beginSave() else { return }
        businessHoursSaveError = ""
        isSavingBusinessHours = true
        let startStr = SettingsRefreshPolicy.formatHours(date: businessHoursStart)
        let endStr = SettingsRefreshPolicy.formatHours(date: businessHoursEnd)

        Task { @MainActor in
            #if DEBUG
            if isFixtureMode {
                _ = businessHoursFence.finish(operation)
                isSavingBusinessHours = false
                confirmedBaseline.businessHoursStart = startStr
                confirmedBaseline.businessHoursEnd = endStr
                return
            }
            #endif
            let success: Bool
            do {
                success = try await APIClient.shared.patchContractor(
                    auth.contractorId,
                    body: [
                        "business_hours_start": startStr,
                        "business_hours_end": endStr
                    ],
                    bearerToken: auth.bearerToken
                )
            } catch {
                success = false
            }
            guard businessHoursFence.finish(operation) else { return }
            isSavingBusinessHours = false
            guard appState.currentAuthContext() == auth else { return }
            if success {
                confirmedBaseline.businessHoursStart = startStr
                confirmedBaseline.businessHoursEnd = endStr
                businessHoursSaveError = ""
            } else {
                businessHoursSaveError = String(localized: "Failed to save business hours. Please try again.")
            }
        }
    }

    private func switchMode() async {
        let auth = appState.currentAuthContext()
        guard auth.isValid else { return }
        let targetMode = appState.isPersonalMode ? "business" : "personal"
        loadCoordinator.recordMutation(field: .mode)
        isSwitchingMode = true
        modeChangeError = ""

        #if DEBUG
        if isFixtureMode {
            isSwitchingMode = false
            appState.mode = targetMode
            confirmedBaseline.mode = targetMode
            return
        }
        #endif

        switch await APIClient.shared.updateContractorMode(contractorId: auth.contractorId, mode: targetMode) {
        case .success:
            appState.mode = targetMode
            confirmedBaseline.mode = targetMode
        case .entitlementRequired:
            presentPaywall(from: .assistant)
        case .failed:
            modeChangeError = String(localized: "Could not switch mode. Please try again.")
        }
        isSwitchingMode = false
    }

    private func refreshPushPermission() async {
        #if DEBUG
        if isFixtureMode {
            await MainActor.run { pushPermission = .authorized }
            return
        }
        #endif
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        await MainActor.run { pushPermission = settings.authorizationStatus }
    }

    private func provisionNumberFromSettings() async {
        let auth = appState.currentAuthContext()
        guard auth.isValid else { return }
        #if DEBUG
        if isFixtureMode { return }
        #endif
        do {
            let url = URL(string: "\(APIClient.shared.baseURL)/api/contractors/\(auth.contractorId)/provision-number")!
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.timeoutInterval = 30
            if !auth.bearerToken.isEmpty {
                request.setValue("Bearer \(auth.bearerToken)", forHTTPHeaderField: "Authorization")
            } else {
                APIClient.shared.authorize(&request)
            }
            let (data, _) = try await URLSession.shared.data(for: request)
            if let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
               let number = json["phone_number"] as? String, !number.isEmpty {
                let country = SettingsCountry.accountCountry(from: json)
                await MainActor.run {
                    guard appState.currentAuthContext() == auth else { return }
                    appState.kevinNumber = number
                    if let country {
                        appState.countryCode = country
                        countrySelection = country
                        confirmedBaseline.countryCode = country
                    }
                }
            }
        } catch {
            debugLog("Provision from settings failed: \(error)")
        }
    }

    private func loadProfileAndHydrate(capturedAuth: CallAuthContext) async -> Bool {
        let capturedRevisions = SettingsProfileHydrator.CapturedRevisions(
            urgentRevision: urgentPreferenceFence.revision,
            screenAllRevision: screenAllCallsFence.revision,
            sitToneRevision: sitToneFence.revision,
            hoursRevision: businessHoursFence.revision,
            fieldMutationRevisions: loadCoordinator.fieldMutationRevisions
        )

        return await SettingsProfileHydrator.hydrate(
            capturedAuth: capturedAuth,
            capturedRevisions: capturedRevisions,
            fetchProfile: {
                await APIClient.shared.getContractorProfile(
                    contractorId: capturedAuth.contractorId,
                    bearerToken: capturedAuth.bearerToken
                )
            },
            ownsOperation: { [self] in
                appState.currentAuthContext() == capturedAuth
            },
            currentDraftProvider: { [self] in
                SettingsDraftSnapshot(
                    knowledgeText: knowledgeText,
                    regulatoryAddress: regulatoryAddressDraft,
                    regulatoryCity: regulatoryCityDraft,
                    businessHoursStart: SettingsRefreshPolicy.formatHours(date: businessHoursStart),
                    businessHoursEnd: SettingsRefreshPolicy.formatHours(date: businessHoursEnd),
                    isKnowledgeEditorOpen: showKnowledgeEditor,
                    isKnowledgeDirty: isKnowledgeDirty
                )
            },
            baselineProvider: { [self] in
                confirmedBaseline
            },
            fenceProvider: { [self] revisions in
                SettingsProfileHydrator.FencePermits(
                    urgentFencePermits: urgentPreferenceFence.permitsLoad(revisions.urgentRevision),
                    screenAllFencePermits: screenAllCallsFence.permitsLoad(revisions.screenAllRevision),
                    sitToneFencePermits: sitToneFence.permitsLoad(revisions.sitToneRevision),
                    hoursFencePermits: businessHoursFence.permitsLoad(revisions.hoursRevision),
                    isSavingRegulatoryAddress: isSavingRegulatoryAddress,
                    isSavingCountry: isSavingCountry,
                    isSwitchingMode: isSwitchingMode
                )
            },
            currentFieldRevisions: { [self] in
                loadCoordinator.fieldMutationRevisions
            },
            applyProfile: { [self] contractor, decision in
                applyHydratedProfile(contractor: contractor, decision: decision)
            }
        )
    }

    private func applyHydratedProfile(contractor: [String: Any], decision: SettingsHydrationDecision) {
        let name = contractor["owner_name"] as? String ?? ""
        let biz = contractor["business_name"] as? String ?? ""
        let svc = contractor["service_type"] as? String ?? ""
        if !name.isEmpty { appState.userName = name }
        if !biz.isEmpty { appState.businessName = biz }
        if !svc.isEmpty { appState.serviceType = svc }

        let subStatus = contractor["subscription_status"] as? String ?? ""
        let subTier = contractor["subscription_tier"] as? String ?? ""
        if !subStatus.isEmpty { appState.subscriptionStatus = subStatus }
        if !subTier.isEmpty { appState.subscriptionTier = subTier }

        let autoReply = contractor["auto_reply_sms"] as? Bool ?? false
        appState.autoReplySms = autoReply

        var projection = SettingsGuardedStateProjection(
            baseline: confirmedBaseline,
            drafts: SettingsDraftSnapshot(
                knowledgeText: knowledgeText,
                regulatoryAddress: regulatoryAddressDraft,
                regulatoryCity: regulatoryCityDraft,
                businessHoursStart: SettingsRefreshPolicy.formatHours(date: businessHoursStart),
                businessHoursEnd: SettingsRefreshPolicy.formatHours(date: businessHoursEnd),
                isKnowledgeEditorOpen: showKnowledgeEditor,
                isKnowledgeDirty: isKnowledgeDirty
            ),
            appStateBusinessAddress: appState.businessAddress,
            appStateBusinessCity: appState.businessCity,
            appStateSmartInterruption: appState.smartInterruption,
            appStateRingThroughContacts: appState.ringThroughContacts,
            appStateSitToneEnabled: appState.sitToneEnabled,
            appStateCountryCode: appState.countryCode,
            appStateMode: appState.mode,
            countrySelection: countrySelection,
            smartInterruptionSelection: smartInterruptionSelection
        )

        SettingsRefreshPolicy.applyGuardedProfile(
            contractor: contractor,
            decision: decision,
            state: &projection
        )

        confirmedBaseline = projection.baseline
        knowledgeText = projection.drafts.knowledgeText
        isKnowledgeDirty = projection.drafts.isKnowledgeDirty
        regulatoryAddressDraft = projection.drafts.regulatoryAddress
        regulatoryCityDraft = projection.drafts.regulatoryCity
        appState.businessAddress = projection.appStateBusinessAddress
        appState.businessCity = projection.appStateBusinessCity
        appState.smartInterruption = projection.appStateSmartInterruption
        smartInterruptionSelection = projection.smartInterruptionSelection
        appState.ringThroughContacts = projection.appStateRingThroughContacts
        appState.sitToneEnabled = projection.appStateSitToneEnabled
        appState.countryCode = projection.appStateCountryCode
        countrySelection = projection.countrySelection
        appState.mode = projection.appStateMode

        if decision.shouldUpdateBusinessHours {
            if let startDate = SettingsRefreshPolicy.parseHours(string: projection.drafts.businessHoursStart) {
                businessHoursStart = startDate
            }
            if let endDate = SettingsRefreshPolicy.parseHours(string: projection.drafts.businessHoursEnd) {
                businessHoursEnd = endDate
            }
        }
    }

    private func checkIntegrationsStatus(capturedAuth: CallAuthContext) async {
        guard !appState.isPersonalMode else { return }
        guard capturedAuth.isValid else { return }

        // Jobber
        do {
            let connected = try await APIClient.shared.checkIntegrationStatus(
                "jobber",
                contractorId: capturedAuth.contractorId,
                bearerToken: capturedAuth.bearerToken
            )
            await MainActor.run {
                guard appState.currentAuthContext() == capturedAuth else { return }
                appState.jobberConnected = connected
            }
        } catch {
            debugLog("Check Jobber status failed: \(error)")
        }

        // Google Calendar
        do {
            let connected = try await APIClient.shared.checkIntegrationStatus(
                "google-calendar",
                contractorId: capturedAuth.contractorId,
                bearerToken: capturedAuth.bearerToken
            )
            await MainActor.run {
                guard appState.currentAuthContext() == capturedAuth else { return }
                appState.googleCalendarConnected = connected
            }
        } catch {
            debugLog("Check Google Calendar status failed: \(error)")
        }
    }

    private func connectJobber() async {
        let auth = appState.currentAuthContext()
        guard auth.isValid else { return }
        #if DEBUG
        if isFixtureMode { return }
        #endif
        do {
            if let authorizeURL = try await APIClient.shared.getIntegrationConnectURL("jobber", contractorId: auth.contractorId) {
                guard let url = URL(string: authorizeURL),
                      let scheme = url.scheme, scheme == "https",
                      let host = url.host,
                      host == "getjobber.com" || host.hasSuffix(".getjobber.com") else {
                    return
                }
                await MainActor.run {
                    guard appState.currentAuthContext() == auth else { return }
                    UIApplication.shared.open(url)
                }
            }
        } catch {
            debugLog("Connect Jobber failed: \(error)")
        }
    }

    private func disconnectJobber() async {
        let auth = appState.currentAuthContext()
        guard auth.isValid else { return }
        #if DEBUG
        if isFixtureMode {
            appState.jobberConnected = false
            return
        }
        #endif
        do {
            _ = try await APIClient.shared.disconnectIntegration("jobber", contractorId: auth.contractorId)
            await MainActor.run {
                guard appState.currentAuthContext() == auth else { return }
                appState.jobberConnected = false
            }
        } catch {
            debugLog("Disconnect Jobber failed: \(error)")
        }
    }

    private func connectGoogleCalendar() async {
        let auth = appState.currentAuthContext()
        guard auth.isValid else { return }
        #if DEBUG
        if isFixtureMode { return }
        #endif
        do {
            if let authorizeURL = try await APIClient.shared.getIntegrationConnectURL("google-calendar", contractorId: auth.contractorId) {
                guard let url = URL(string: authorizeURL),
                      let scheme = url.scheme, scheme == "https",
                      let host = url.host,
                      host == "google.com" || host.hasSuffix(".google.com") else {
                    return
                }
                await MainActor.run {
                    guard appState.currentAuthContext() == auth else { return }
                    UIApplication.shared.open(url)
                }
            }
        } catch {
            debugLog("Connect Google Calendar failed: \(error)")
        }
    }

    private func disconnectGoogleCalendar() async {
        let auth = appState.currentAuthContext()
        guard auth.isValid else { return }
        #if DEBUG
        if isFixtureMode {
            appState.googleCalendarConnected = false
            return
        }
        #endif
        do {
            _ = try await APIClient.shared.disconnectIntegration("google-calendar", contractorId: auth.contractorId)
            await MainActor.run {
                guard appState.currentAuthContext() == auth else { return }
                appState.googleCalendarConnected = false
            }
        } catch {
            debugLog("Disconnect Google Calendar failed: \(error)")
        }
    }

    private func importWebsite() async {
        let auth = appState.currentAuthContext()
        guard !websiteURL.isEmpty, auth.isValid else { return }
        loadCoordinator.recordMutation(field: .knowledge)
        isImporting = true
        importMessage = ""

        #if DEBUG
        if isFixtureMode {
            isImporting = false
            importMessage = String(localized: "Imported successfully!")
            return
        }
        #endif

        var url = websiteURL
        if !url.hasPrefix("http") {
            url = "https://\(url)"
        }

        if let result = await APIClient.shared.importWebsite(contractorId: auth.contractorId, url: url) {
            guard appState.currentAuthContext() == auth else { return }
            if result["status"] as? String == "ok" {
                let importedKnowledge = result["knowledge"] as? String ?? ""
                knowledgeText = importedKnowledge
                confirmedBaseline.knowledgeText = importedKnowledge
                isKnowledgeDirty = false
                importMessage = String(localized: "Imported successfully!")
            } else {
                let msg = result["message"] as? String ?? String(localized: "Unknown error")
                importMessage = String(localized: "Failed: \(msg)")
            }
        } else {
            importMessage = String(localized: "Failed to connect")
        }
        isImporting = false
    }

    private func cancelPendingDeleteConfirmation() {
        deletionLoader.cancel()
        confirmDeleteTask?.cancel()
        confirmDeleteTask = nil
        showDeleteAccountAlert = false
        showSubscriptionWarningAlert = false
        showDeleteAccountError = false
    }

    private func deleteAccount() async {
        let auth = appState.currentAuthContext()
        guard let token = accountDeletionFence.begin(auth: auth) else { return }
        var outcome = AccountDeletionOutcome.failed
        do {
            let encodedId = auth.contractorId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? auth.contractorId
            let url = URL(string: "\(appState.backendURL)/api/contractors/\(encodedId)")!
            var request = URLRequest(url: url)
            request.httpMethod = "DELETE"
            request.timeoutInterval = 15
            if !auth.bearerToken.isEmpty {
                request.setValue("Bearer \(auth.bearerToken)", forHTTPHeaderField: "Authorization")
            } else {
                APIClient.shared.authorize(&request)
            }
            let (data, response) = try await URLSession.shared.data(for: request)
            outcome = AccountDeletionResponseParser.parse(response: response, data: data)
        } catch {
            debugLog("Delete account failed: \(error)")
        }

        if outcome == .failed {
            try? await Task.sleep(nanoseconds: alertRedismissalDelay)
        }
        await MainActor.run {
            let currentAuth = appState.currentAuthContext()
            guard accountDeletionFence.finish(token: token, auth: currentAuth) else { return }
            switch outcome {
            case .deleted:
                appState.contractorId = ""
                appState.kevinNumber = ""
                appState.countryCode = ""
                appState.isOnboarded = false
                APIClient.shared.contractorToken = ""
            case .failed:
                showDeleteAccountError = true
            }
        }
    }

    // MARK: - Forwarding Helpers

    private var forwardingCodes: ForwardingCodes {
        ForwardingDialCodes.codes(
            countryCode: forwardingCountry,
            instructions: forwardingInstructions,
            number: dialNumber,
            isVerizon: appState.isVerizonCarrier
        )
    }

    private var forwardingCountryName: String {
        Locale.current.localizedString(forRegionCode: forwardingCountry) ?? forwardingCountry
    }

    private var dialNumber: String {
        let digits = kevinNumber.filter { $0.isNumber }
        if digits.count == 10 {
            return "1\(digits)"
        }
        return digits
    }

    private func dialCode(_ code: String) {
        #if DEBUG
        if isFixtureMode { return }
        #endif
        if let url = ForwardingDialCodes.telURL(code) {
            UIApplication.shared.open(url)
        }
    }

    // MARK: - Computed Properties

    private var planLabel: String {
        switch appState.subscriptionStatus {
        case "trial": return String(localized: "Free Trial")
        case "active": return tierLabel
        case "expired": return String(localized: "Expired")
        case "cancelled": return String(localized: "Cancelled")
        default: return appState.subscriptionStatus.isEmpty ? String(localized: "Free Trial") : appState.subscriptionStatus.capitalized
        }
    }

    private var viewPlansLabel: String {
        switch appState.subscriptionStatus {
        case "trial": return String(localized: "View Plans")
        case "active": return String(localized: "Change Plan")
        default: return String(localized: "Subscribe to Kevin AI")
        }
    }

    private var tierLabel: String {
        switch appState.subscriptionTier {
        case "personal": return String(localized: "Personal")
        case "business": return String(localized: "Business")
        case "businessPro": return String(localized: "Business Pro")
        default: return String(localized: "Kevin AI")
        }
    }
}

/// Backward compatibility wrapper for SettingsView.
struct SettingsView: View {
    @EnvironmentObject var appState: AppState

    var body: some View {
        SettingsHost(
            isAccountPresented: .constant(false),
            assistantIsSelected: true,
            onOpenCall: { _ in }
        ) { assistantView in
            assistantView
        }
    }
}

// MARK: - Knowledge Editor

private enum KnowledgeVoiceMode: String, CaseIterable, Identifiable {
    case add
    case replace

    var id: String { rawValue }

    var title: String {
        switch self {
        case .add: return String(localized: "Add")
        case .replace: return String(localized: "Replace")
        }
    }
}

struct KnowledgeEditorView: View {
    @Environment(\.dismiss) var dismiss
    @EnvironmentObject var appState: AppState
    @Binding var knowledgeText: String
    @State private var isSaving = false
    @State private var isRecording = false
    @State private var audioRecorder: AVAudioRecorder?
    @State private var isTranscribing = false
    @State private var recordingURL: URL?
    @State private var knowledgeLengthWarning = ""
    @State private var voiceMode: KnowledgeVoiceMode = .add
    @State private var pendingKnowledge = ""
    @State private var showKnowledgeDraft = false
    @State private var showClearKnowledge = false

    private var isFixtureMode: Bool {
        #if DEBUG
        return AppStoreScreenshotFixtures.isEnabled
        #else
        return false
        #endif
    }

    private let placeholder = """
## Services
- Faucet repair ($150-350)
- Water heater install ($800-2500)
- Drain cleaning ($150-250)

## NOT Offered
- Commercial plumbing

## Hours
Mon-Fri 7am-6pm, Sat 8am-2pm

## Service Area
San Jose, Santa Clara, Campbell

## Pricing
- Service call fee: $89
"""

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                HStack(spacing: 12) {
                    Button {
                        if isRecording {
                            stopRecording()
                        } else {
                            startRecording()
                        }
                    } label: {
                        HStack(spacing: 8) {
                            Image(systemName: isRecording ? "stop.circle.fill" : "mic.circle.fill")
                                .font(.title2)
                                .foregroundStyle(isRecording ? .red : .blue)
                            VStack(alignment: .leading, spacing: 1) {
                                Text(isRecording ? String(localized: "Tap to stop") : String(localized: "Describe your business"))
                                    .font(.subheadline.weight(.medium))
                                Text(isRecording ? String(localized: "Recording...") : String(localized: "Talk and Kevin will learn"))
                                    .font(.caption)
                                    .foregroundStyle(Color.secondary)
                            }
                        }
                    }
                    .buttonStyle(.plain)
                    .disabled(isFixtureMode)

                    if isTranscribing {
                        ProgressView()
                            .scaleEffect(0.8)
                    }
                }
                .padding()
                .background(Color(.systemGray6))
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .padding(.horizontal)
                .padding(.top, 8)

                Picker(String(localized: "Voice Update Mode"), selection: $voiceMode) {
                    ForEach(KnowledgeVoiceMode.allCases) { mode in
                        Text(mode.title).tag(mode)
                    }
                }
                .pickerStyle(.segmented)
                .padding(.horizontal)
                .padding(.top, 8)
                .disabled(isFixtureMode)

                ZStack(alignment: .topLeading) {
                    TextEditor(text: $knowledgeText)
                        .font(.system(.subheadline, design: .monospaced))
                        .scrollContentBackground(.hidden)

                    if knowledgeText.isEmpty {
                        Text(placeholder)
                            .font(.system(.subheadline, design: .monospaced))
                            .foregroundStyle(Color(uiColor: .tertiaryLabel))
                            .padding(.top, 8)
                            .padding(.leading, 5)
                            .allowsHitTesting(false)
                    }
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .overlay(
                    RoundedRectangle(cornerRadius: 10)
                        .stroke(Color(.systemGray4), lineWidth: 1)
                        .padding(.horizontal, 8)
                )
                .padding(.top, 8)

                if !knowledgeLengthWarning.isEmpty {
                    Text(knowledgeLengthWarning)
                        .font(.caption)
                        .foregroundStyle(.orange)
                        .padding(.horizontal)
                        .padding(.top, 4)
                }

                Text(String(localized: "Type your services, or tap the mic to describe them by voice. Kevin uses this to answer caller questions."))
                    .font(.caption)
                    .foregroundStyle(Color(uiColor: .tertiaryLabel))
                    .padding(.horizontal)
                    .padding(.top, 4)
                    .padding(.bottom, 8)

                if !knowledgeText.isEmpty {
                    Button(role: .destructive) {
                        showClearKnowledge = true
                    } label: {
                        Label(String(localized: "Clear Business Knowledge"), systemImage: "trash")
                    }
                    .font(.subheadline)
                    .padding(.bottom, 12)
                    .disabled(isFixtureMode)
                }
            }
            .onChange(of: knowledgeText) { _, newValue in
                if newValue.count > 10_000 {
                    knowledgeText = String(newValue.prefix(10_000))
                    knowledgeLengthWarning = String(localized: "Knowledge text truncated to 10,000 characters.")
                } else {
                    knowledgeLengthWarning = ""
                }
            }
            .navigationTitle(String(localized: "Business Knowledge"))
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button(String(localized: "Cancel")) { dismiss() }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button(String(localized: "Save")) {
                        Task { await saveKnowledge() }
                    }
                    .fontWeight(.semibold)
                    .disabled(isSaving || knowledgeText.isEmpty || isFixtureMode)
                }
            }
            .confirmationDialog(
                String(localized: "Clear all business knowledge?"),
                isPresented: $showClearKnowledge,
                titleVisibility: .visible
            ) {
                Button(String(localized: "Clear All"), role: .destructive) {
                    knowledgeText = ""
                }
                Button(String(localized: "Cancel"), role: .cancel) {}
            } message: {
                Text(String(localized: "This clears the knowledge Kevin uses to answer business questions. It is not saved until you tap Save."))
            }
            .sheet(isPresented: $showKnowledgeDraft) {
                NavigationStack {
                    TextEditor(text: $pendingKnowledge)
                        .font(.system(.subheadline, design: .monospaced))
                        .padding()
                        .navigationTitle(String(localized: "Review Changes"))
                        .navigationBarTitleDisplayMode(.inline)
                        .toolbar {
                            ToolbarItem(placement: .topBarLeading) {
                                Button(String(localized: "Discard")) {
                                    showKnowledgeDraft = false
                                }
                            }
                            ToolbarItem(placement: .topBarTrailing) {
                                Button(String(localized: "Apply")) {
                                    knowledgeText = pendingKnowledge
                                    showKnowledgeDraft = false
                                }
                                .fontWeight(.semibold)
                                .disabled(pendingKnowledge.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                            }
                        }
                }
            }
        }
    }

    private func startRecording() {
        #if DEBUG
        if isFixtureMode { return }
        #endif
        let session = AVAudioSession.sharedInstance()
        do {
            try session.setCategory(.record, mode: .default)
            try session.setActive(true)
        } catch {
            debugLog("Audio session error: \(error)")
            return
        }

        let url = FileManager.default.temporaryDirectory.appendingPathComponent("kevin_training.m4a")
        recordingURL = url

        let recSettings: [String: Any] = [
            AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
            AVSampleRateKey: 16000,
            AVNumberOfChannelsKey: 1,
            AVEncoderAudioQualityKey: AVAudioQuality.high.rawValue,
        ]

        do {
            audioRecorder = try AVAudioRecorder(url: url, settings: recSettings)
            audioRecorder?.record()
            isRecording = true
        } catch {
            debugLog("Recording error: \(error)")
        }
    }

    private func stopRecording() {
        audioRecorder?.stop()
        isRecording = false

        guard let url = recordingURL else { return }
        isTranscribing = true

        Task {
            let transcript = await transcribeLocally(url: url)
            if let transcript = transcript, !transcript.isEmpty {
                if let knowledge = await APIClient.shared.structureKnowledge(
                    contractorId: appState.contractorId,
                    rawText: transcript,
                    existingKnowledge: knowledgeText,
                    mode: voiceMode.rawValue
                ) {
                    await MainActor.run {
                        let trimmed = knowledge.trimmingCharacters(in: .whitespacesAndNewlines)
                        if trimmed.isEmpty {
                            knowledgeLengthWarning = String(localized: "No business details were detected in that recording.")
                        } else if voiceMode == .replace || knowledgeText.isEmpty {
                            pendingKnowledge = trimmed
                            showKnowledgeDraft = true
                        } else {
                            pendingKnowledge = knowledgeText + "\n\n" + trimmed
                            showKnowledgeDraft = true
                        }
                    }
                }
            }
            try? FileManager.default.removeItem(at: url)
            await MainActor.run { isTranscribing = false }
        }
    }

    private func transcribeLocally(url: URL) async -> String? {
        let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
        guard let recognizer = recognizer, recognizer.isAvailable else { return nil }

        let authStatus = await withCheckedContinuation { continuation in
            SFSpeechRecognizer.requestAuthorization { status in
                continuation.resume(returning: status)
            }
        }
        guard authStatus == .authorized else { return nil }

        let request = SFSpeechURLRecognitionRequest(url: url)
        request.shouldReportPartialResults = false

        return await withCheckedContinuation { continuation in
            recognizer.recognitionTask(with: request) { result, error in
                if let result = result, result.isFinal {
                    continuation.resume(returning: result.bestTranscription.formattedString)
                } else if error != nil {
                    continuation.resume(returning: nil)
                }
            }
        }
    }

    private func saveKnowledge() async {
        let auth = appState.currentAuthContext()
        guard auth.isValid else { return }
        isSaving = true
        #if DEBUG
        if isFixtureMode {
            isSaving = false
            dismiss()
            return
        }
        #endif
        await APIClient.shared.updateKnowledge(contractorId: auth.contractorId, knowledge: knowledgeText)
        isSaving = false
        dismiss()
    }
}

// MARK: - SetupRow

private struct SetupRow<ActionLabel: View>: View {
    let title: String
    let ok: Bool
    let okLabel: String
    let failLabel: String
    let action: () -> Void
    @ViewBuilder let actionLabel: () -> ActionLabel

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: ok ? "checkmark.circle.fill" : "exclamationmark.circle.fill")
                .foregroundStyle(ok ? .green : .orange)
                .font(.title3)

            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.subheadline)
                Text(ok ? okLabel : failLabel)
                    .font(.caption)
                    .foregroundStyle(ok ? Color.secondary : Color.orange)
            }

            Spacer()

            if !ok {
                Button(action: action) {
                    actionLabel()
                }
                .buttonStyle(.borderless)
            }
        }
    }
}
