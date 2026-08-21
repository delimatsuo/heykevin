import SwiftUI
import UserNotifications

private func debugLog(_ message: String) {
    #if DEBUG
    print(message)
    #endif
}

struct SettingsView: View {
    @EnvironmentObject var appState: AppState
    @Environment(\.scenePhase) private var scenePhase
    @State private var showPaywall = false
    @State private var showDeleteAccountAlert = false
    @State private var showDeleteAccountError = false
    @State private var isDeletingAccount = false
    @State private var showSubscriptionWarningAlert = false
    @State private var confirmDeleteTask: Task<Void, Never>?

    /// Delay before presenting a follow-up alert: flipping a second alert's
    /// isPresented during another's ~300ms dismissal can silently drop it.
    /// Shared by the continue-deleting and deletion-error paths.
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
    @State private var knowledgeLengthWarning = ""
    @State private var pushPermission: UNAuthorizationStatus = .notDetermined
    @State private var isProvisioningNumber = false
    @State private var businessHoursStart = Calendar.current.date(from: DateComponents(hour: 8, minute: 0)) ?? Date()
    @State private var businessHoursEnd = Calendar.current.date(from: DateComponents(hour: 17, minute: 0)) ?? Date()

    private var kevinNumber: String {
        appState.kevinNumber
    }

    var body: some View {
        NavigationStack {
            Form {
                // MARK: - Kevin Status

                setupStatusSection

                // MARK: - Account & Plan
                //
                // The one place billing lives. "Plan" is what the user pays for;
                // how Kevin answers calls is a separate concept and lives in the
                // sections below — keeping them apart is deliberate.

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
                        // Deliberately not status-colored: a green "Business"
                        // reads as "OK" and re-conflates plan with status, which
                        // is the confusion this section exists to remove.
                        Text(planLabel)
                            .foregroundStyle(Color.secondary)
                    }

                    Button {
                        showPaywall = true
                    } label: {
                        HStack {
                            Text(viewPlansLabel)
                                .foregroundStyle(.blue)
                            Spacer()
                            Image(systemName: "arrow.right.circle.fill")
                                .foregroundStyle(.blue)
                        }
                    }

                    if appState.subscriptionStatus == "active" {
                        Button {
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
                    }
                } header: {
                    Text(String(localized: "Account & Plan"))
                }
                .sheet(isPresented: $showPaywall) {
                    PaywallView(canDismiss: true)
                        .environmentObject(appState)
                }

                // MARK: - How Kevin Answers
                //
                // Answering behavior for both modes. The old "Mode" status row
                // read like a second plan ("Active — Business" vs "Business
                // Assistant"); it is now a directional action instead, so plan
                // and behavior can't be confused.

                Section {
                    Toggle(String(localized: "Block spam with disconnect tone"), isOn: $appState.sitToneEnabled)
                        .onChange(of: appState.sitToneEnabled) { _, newValue in
                            Task {
                                isSaving = true
                                saveError = ""
                                await updateSitToneEnabled(newValue)
                                isSaving = false
                            }
                        }

                    Toggle(String(localized: "Alert me for urgent calls"), isOn: $appState.smartInterruption)
                        .onChange(of: appState.smartInterruption) { _, newValue in
                            Task {
                                isSaving = true
                                saveError = ""
                                await updateContractorSetting("smart_interruption", newValue)
                                isSaving = false
                            }
                        }

                    if appState.isPersonalMode {
                        Button {
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
                    }

                    Button {
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
                    .disabled(isSwitchingMode)

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
                         ? String(localized: "Kevin screens unknown callers; contacts from your iPhone ring through. Urgent calls (flooding, fire, gas leak) ring you immediately when alerts are on.")
                         : String(localized: "Kevin answers as your business receptionist, using the business setup below. Urgent calls (flooding, fire, gas leak) ring you immediately when alerts are on."))
                }
                .disabled(isSaving)

                // MARK: - Your Business (business mode only)
                //
                // Everything Kevin needs to know about the customer's business,
                // grouped in one contiguous block — kept apart from the product
                // settings above. Personal-mode users never see any of this.

                if !appState.isPersonalMode {
                    Section {
                        HStack {
                            Text(String(localized: "Business"))
                            Spacer()
                            Text(appState.businessName.isEmpty ? String(localized: "Not set") : appState.businessName)
                                .foregroundStyle(appState.businessName.isEmpty ? .tertiary : .secondary)
                        }

                        DatePicker(String(localized: "Open"), selection: $businessHoursStart, displayedComponents: .hourAndMinute)
                            .onChange(of: businessHoursStart) { _, _ in saveBusinessHours() }
                        DatePicker(String(localized: "Close"), selection: $businessHoursEnd, displayedComponents: .hourAndMinute)
                            .onChange(of: businessHoursEnd) { _, _ in saveBusinessHours() }
                    } header: {
                        Text(String(localized: "Your Business"))
                    } footer: {
                        Text(String(localized: "Outside these hours, Kevin will tell callers you're closed and take a message."))
                    }

                    // MARK: - Knowledge Base
                    //
                    // Services and knowledge are the same job — teaching Kevin
                    // about the business — so they share one card. They open
                    // differently (push vs sheet), so both titles are styled
                    // primary with a trailing chevron to read as one list.

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

                            if isImporting {
                                ProgressView()
                                    .scaleEffect(0.8)
                            } else {
                                Button(String(localized: "Import")) {
                                    Task { await importWebsite() }
                                }
                                .disabled(websiteURL.isEmpty)
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

                    // MARK: - Integrations

                    Section {
                        // Jobber row
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
                            }
                        }

                        // Google Calendar row
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
                            }
                        }
                    } header: {
                        Text(String(localized: "Integrations"))
                    } footer: {
                        Text(String(localized: "Connect Jobber to let Kevin look up customers and create jobs automatically. Connect Google Calendar so Kevin can offer your open times and send you appointment requests to confirm."))
                    }
                }

                // MARK: - Call Forwarding

                Section {
                    Toggle(isOn: $appState.isVerizonCarrier) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(String(localized: "I'm a Verizon customer"))
                                .font(.subheadline.weight(.medium))
                            Text(String(localized: "Uses *71 to activate and *73 to deactivate"))
                                .font(.caption)
                                .foregroundStyle(Color.secondary)
                        }
                    }

                    Button {
                        // Activate — match the code by carrier.
                        // Verizon: *71<number> (no-answer forward). GSM: *61*<number># (no-answer forward).
                        let code = appState.isVerizonCarrier
                            ? "*71\(dialNumber)"
                            : "*61*\(dialNumber)%23"
                        dialCode(code)
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

                    Button(role: .destructive) {
                        // Deactivate — must match the activate code exactly.
                        // Verizon: *73 (cancels forwarding). GSM: ##61# (cancels no-answer).
                        let code = appState.isVerizonCarrier ? "*73" : "%23%2361%23"
                        dialCode(code)
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

                    Button(role: .destructive) {
                        // ##002# clears every forwarding type (unconditional, busy,
                        // no-answer, not-reachable) on GSM networks. Useful when
                        // prior forwarding from another source is still active.
                        dialCode("%23%23002%23")
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
                    .disabled(appState.isVerizonCarrier)
                } header: {
                    Text(String(localized: "Call Forwarding"))
                } footer: {
                    if appState.kevinNumber.isEmpty {
                        Text(String(localized: "You need a Kevin number before setting up forwarding. Please contact support."))
                            .foregroundStyle(.orange)
                    } else {
                        Text(String(localized: "Tapping opens your phone dialer. Tap Call to confirm. If you're on Verizon, turn on the toggle above so the correct codes are used."))
                    }
                }
                .disabled(appState.kevinNumber.isEmpty)

                // MARK: - Account

                Section {
                    Button(role: .destructive) {
                        // Fetch the authoritative subscription state before
                        // deciding which alert to show — the cached status
                        // defaults to "trial" and a stale cache would skip
                        // the billing warning for an active subscriber.
                        Task {
                            let profile = await APIClient.shared.getContractorProfile(
                                contractorId: appState.contractorId
                            )
                            let resolved = AccountDeletionFlow.resolve(
                                freshStatus: profile?["subscription_status"] as? String,
                                freshTier: profile?["subscription_tier"] as? String,
                                cachedStatus: appState.subscriptionStatus,
                                cachedTier: appState.subscriptionTier
                            )
                            await MainActor.run {
                                switch AccountDeletionFlow.firstStep(
                                    subscriptionStatus: resolved.status,
                                    subscriptionTier: resolved.tier
                                ) {
                                case .warnActiveSubscription:
                                    showSubscriptionWarningAlert = true
                                case .confirmDelete:
                                    showDeleteAccountAlert = true
                                }
                            }
                        }
                    } label: {
                        if isDeletingAccount {
                            HStack {
                                Text(String(localized: "Deleting Account…"))
                                Spacer()
                                ProgressView()
                            }
                        } else {
                            Text(String(localized: "Delete Account"))
                        }
                    }
                    .disabled(isDeletingAccount || confirmDeleteTask != nil)
                } footer: {
                    Text(String(localized: "Releases your Kevin number and deletes all data. You will need to disable call forwarding manually."))
                }
                .alert(String(localized: "Delete Account"), isPresented: $showDeleteAccountAlert) {
                    Button(String(localized: "Delete"), role: .destructive) {
                        Task { await deleteAccount() }
                    }
                    Button(String(localized: "Cancel"), role: .cancel) {}
                } message: {
                    Text(String(localized: "This will permanently delete your Kevin account and release your Kevin number. Make sure to deactivate call forwarding first."))
                }
                .alert(String(localized: "Subscription Still Active"), isPresented: $showSubscriptionWarningAlert) {
                    Button(String(localized: "Manage Subscription")) {
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
                                // Re-check inside the MainActor hop: a cancel
                                // landing during the suspension (onDisappear)
                                // must not pop the destructive confirmation
                                // on a later reappearance.
                                guard !Task.isCancelled else { return }
                                confirmDeleteTask = nil
                                showDeleteAccountAlert = true
                            }
                        }
                    }
                    Button(String(localized: "Cancel"), role: .cancel) {}
                } message: {
                    Text(String(localized: "Deleting your account does not cancel your Apple subscription, and you would keep being charged. Cancel it under Manage Subscription first."))
                }
                .alert(String(localized: "Couldn't Delete Account"), isPresented: $showDeleteAccountError) {
                    Button(String(localized: "OK"), role: .cancel) {}
                } message: {
                    Text(String(localized: "The server couldn't complete the deletion, so your account is unchanged. Please check your connection and try again."))
                }

                // MARK: - Legal

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

                    Link(destination: URL(string: "https://heykevin.one/terms")!) {
                        HStack {
                            Text(String(localized: "Terms of Service"))
                            Spacer()
                            Image(systemName: "arrow.up.right.square")
                                .foregroundStyle(Color(uiColor: .tertiaryLabel))
                        }
                    }
                    .foregroundStyle(.primary)
                } header: {
                    Text(String(localized: "Legal"))
                }

                // MARK: - About

                Section {
                    HStack {
                        Text(String(localized: "Version"))
                        Spacer()
                        Text("1.0.0")
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
            .navigationTitle(String(localized: "Settings"))
            .onDisappear { cancelPendingDeleteConfirmation() }
            .sheet(isPresented: $showKnowledgeEditor) {
                KnowledgeEditorView(knowledgeText: $knowledgeText)
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
            .task {
                await loadKnowledge()
                await checkJobberStatus()
                await checkGoogleCalendarStatus()
                await refreshPushPermission()
            }
            .onChange(of: scenePhase) { _, phase in
                if phase == .active {
                    Task {
                        await checkJobberStatus()
                        await checkGoogleCalendarStatus()
                        await refreshPushPermission()
                    }
                }
            }
        }
    }

    private var formattedKevinNumber: String {
        PhoneFormatter.format(kevinNumber)
    }

    // MARK: - Kevin Status

    private var setupStatusSection: some View {
        let numberOK = !appState.kevinNumber.isEmpty
        let pushOK = pushPermission == .authorized || pushPermission == .provisional
        let subOK = appState.subscriptionStatus == "trial" || appState.subscriptionStatus == "active"
        let allGood = numberOK && pushOK && subOK && appState.forwardingActivated

        return Section {
            if allGood {
                Label("Kevin is set up and ready", systemImage: "checkmark.circle.fill")
                    .foregroundStyle(.green)
                    .font(.subheadline.weight(.medium))
            }

            // Kevin number — always visible; this is the number calls forward
            // to (and the number businesses hand out).
            if numberOK {
                HStack(spacing: 12) {
                    // Invisible stand-in for SetupRow's status icon. This row
                    // has nothing to warn about, but without the gutter its
                    // text sits flush-left while every row below is indented,
                    // leaving the card with a ragged left edge.
                    Image(systemName: "checkmark.circle.fill")
                        .font(.title3)
                        .hidden()

                    Text(String(localized: "Kevin Number"))
                    Spacer()
                    Text(formattedKevinNumber)
                        .foregroundStyle(Color.secondary)
                        .textSelection(.enabled)
                }
            } else {
                SetupRow(
                    title: "Kevin Number",
                    ok: false,
                    okLabel: formattedKevinNumber,
                    failLabel: "No number assigned"
                ) {
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
                        AnyView(Text("Get Number").font(.caption.weight(.medium)).foregroundStyle(.blue))
                    }
                }
            }

            // The checklist below shows only while something still needs
            // attention; once every step passes it collapses to the ready
            // row above, keeping the screen short for set-up users.
            if !allGood {
                // Call forwarding — track activation locally (carrier state not queryable)
                HStack(spacing: 12) {
                    Image(systemName: appState.forwardingActivated ? "checkmark.circle.fill" : "exclamationmark.circle.fill")
                        .foregroundStyle(appState.forwardingActivated ? Color.green : Color.orange)
                        .font(.title3)

                    VStack(alignment: .leading, spacing: 2) {
                        Text("Call Forwarding")
                            .font(.subheadline)
                        Text(appState.forwardingActivated ? "Activated" : "Missed calls must route to Kevin")
                            .font(.caption)
                            .foregroundStyle(appState.forwardingActivated ? Color.secondary : Color.orange)
                    }
                    Spacer()
                    Button {
                        if !appState.kevinNumber.isEmpty {
                            // Match the code to the carrier. Hardcoding the GSM code
                            // here sent Verizon users a code their network ignores —
                            // and then showed them a green checkmark for it.
                            let code = appState.isVerizonCarrier
                                ? "*71\(dialNumber)"
                                : "*61*\(dialNumber)%23"
                            dialCode(code)
                            // Set the optimistic flag only after dialing is attempted.
                            // This still records intent rather than fact — the device
                            // cannot read forwarding state — but it no longer claims
                            // success before anything has happened. Ground truth is
                            // the server's forwarding_last_seen_at, derived from
                            // Twilio's ForwardedFrom on a real forwarded call.
                            UserDefaults.standard.set(appState.kevinNumber, forKey: "forwardingActivatedFor")
                            appState.forwardingActivated = true
                        }
                    } label: {
                        Text(appState.forwardingActivated ? "Re-activate" : "Unverified")
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

                // Push notifications.
                //
                // The action depends on whether iOS has a decision on file:
                //
                // - notDetermined: nobody has ever asked. Settings shows no
                //   Notifications row for an app in this state, so sending the user
                //   there strands them on a pane with Siri, Search, and Language and
                //   no way to enable anything. Ask for permission instead.
                // - denied: the row exists, so deep-link straight to it with
                //   openNotificationSettingsURLString (iOS 15.4+). The general
                //   openSettingsURLString only opens the app's top-level pane and
                //   makes the user hunt.
                SetupRow(
                    title: "Push Notifications",
                    ok: pushOK,
                    okLabel: "Enabled",
                    failLabel: pushPermission == .denied ? "Blocked in iOS Settings" : "Not enabled"
                ) {
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

                // Subscription state as a setup step; the Account & Plan
                // section below is where plans are viewed and managed.
                SetupRow(
                    title: "Subscription",
                    ok: subOK,
                    // Both labels come from planLabel so this row and the
                    // Account & Plan row can never contradict each other; the
                    // check/warning icon already carries the pass-fail signal.
                    okLabel: planLabel,
                    failLabel: planLabel
                ) {
                    showPaywall = true
                } actionLabel: {
                    AnyView(Text("Subscribe").font(.caption.weight(.medium)).foregroundStyle(.blue))
                }
            }
        } header: {
            Text(allGood ? String(localized: "Kevin") : String(localized: "Setup Status"))
        }
    }

    private func refreshPushPermission() async {
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        await MainActor.run { pushPermission = settings.authorizationStatus }
    }

    private func provisionNumberFromSettings() async {
        guard !appState.contractorId.isEmpty else { return }
        do {
            let url = URL(string: "\(APIClient.shared.baseURL)/api/contractors/\(appState.contractorId)/provision-number")!
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.timeoutInterval = 30
            APIClient.shared.authorize(&request)
            let (data, _) = try await URLSession.shared.data(for: request)
            if let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
               let number = json["phone_number"] as? String, !number.isEmpty {
                await MainActor.run { appState.kevinNumber = number }
            }
        } catch {
            debugLog("Provision from settings failed: \(error)")
        }
    }

    private var dialNumber: String {
        // Strip + and any non-digit characters for carrier codes
        let digits = kevinNumber.filter { $0.isNumber }
        // Ensure it starts with 1 for US numbers
        if digits.count == 10 {
            return "1\(digits)"
        }
        return digits
    }

    private func dialCode(_ code: String) {
        if let url = URL(string: "tel:\(code)") {
            UIApplication.shared.open(url)
        }
    }

    // MARK: - Subscription computed properties

    /// Value for the "Plan" row. Shows the tier by name when active — the old
    /// "Active — Business" wording read like a mode and collided with the
    /// business-assistant concept elsewhere on this screen.
    private var planLabel: String {
        switch appState.subscriptionStatus {
        case "trial": return "Free Trial"
        case "active": return tierLabel
        case "expired": return "Expired"
        case "cancelled": return "Cancelled"
        default: return appState.subscriptionStatus.isEmpty ? "Free Trial" : appState.subscriptionStatus.capitalized
        }
    }

    /// Label for the paywall entry point, phrased for the current state:
    /// browsing during trial, changing while active, subscribing otherwise.
    private var viewPlansLabel: String {
        switch appState.subscriptionStatus {
        case "trial": return String(localized: "View Plans")
        case "active": return String(localized: "Change Plan")
        default: return String(localized: "Subscribe to Kevin AI")
        }
    }

    private var tierLabel: String {
        switch appState.subscriptionTier {
        case "personal": return "Personal"
        case "business": return "Business"
        case "businessPro": return "Business Pro"
        default: return "Kevin AI"
        }
    }

    private func loadKnowledge() async {
        guard !appState.contractorId.isEmpty else { return }
        if let contractor = await APIClient.shared.getContractorProfile(contractorId: appState.contractorId) {
            knowledgeText = contractor["knowledge"] as? String ?? ""
            let name = contractor["owner_name"] as? String ?? ""
            let biz = contractor["business_name"] as? String ?? ""
            let svc = contractor["service_type"] as? String ?? ""
            let mode = contractor["effective_mode"] as? String ?? contractor["mode"] as? String ?? "personal"
            let ringThrough = contractor["ring_through_contacts"] as? Bool ?? true
            await MainActor.run {
                if !name.isEmpty { appState.userName = name }
                if !biz.isEmpty { appState.businessName = biz }
                if !svc.isEmpty { appState.serviceType = svc }
                appState.mode = (mode == "personal") ? "personal" : "business"
                appState.ringThroughContacts = ringThrough
                let sitTone = contractor["sit_tone_enabled"] as? Bool ?? false
                appState.sitToneEnabled = sitTone
                let autoReply = contractor["auto_reply_sms"] as? Bool ?? false
                appState.autoReplySms = autoReply

                // Load business hours
                let formatter = DateFormatter()
                formatter.dateFormat = "HH:mm"
                if let startStr = contractor["business_hours_start"] as? String,
                   let startDate = formatter.date(from: startStr) {
                    businessHoursStart = startDate
                }
                if let endStr = contractor["business_hours_end"] as? String,
                   let endDate = formatter.date(from: endStr) {
                    businessHoursEnd = endDate
                }

                // Load subscription state
                let subStatus = contractor["subscription_status"] as? String ?? ""
                let subTier = contractor["subscription_tier"] as? String ?? ""
                if !subStatus.isEmpty { appState.subscriptionStatus = subStatus }
                if !subTier.isEmpty { appState.subscriptionTier = subTier }
            }
        }
    }

    private func updateRingThrough(_ value: Bool) async {
        guard !appState.contractorId.isEmpty else { return }
        do {
            _ = try await APIClient.shared.patchContractor(appState.contractorId, body: ["ring_through_contacts": value])
        } catch {
            debugLog("Update ring through failed: \(error)")
            await MainActor.run { saveError = String(localized: "Failed to save setting. Please try again.") }
        }
    }

    private func updateSitToneEnabled(_ value: Bool) async {
        guard !appState.contractorId.isEmpty else { return }
        do {
            _ = try await APIClient.shared.patchContractor(appState.contractorId, body: ["sit_tone_enabled": value])
        } catch {
            debugLog("Update SIT tone setting failed: \(error)")
            await MainActor.run { saveError = String(localized: "Failed to save setting. Please try again.") }
        }
    }

    private func saveBusinessHours() {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm"
        let start = formatter.string(from: businessHoursStart)
        let end = formatter.string(from: businessHoursEnd)
        Task {
            await updateContractorSetting("business_hours_start", start)
            await updateContractorSetting("business_hours_end", end)
        }
    }

    /// Switches Personal <-> Business mode with a direct PATCH.
    ///
    /// This used to hand off to `pendingModeChange` + `isOnboarded = false`,
    /// re-running the full onboarding wizard (mode select, business info entry,
    /// a contacts-permission re-prompt, a provisioning spinner) just to flip one
    /// field on an account that already has a Kevin number. That multi-screen
    /// detour was the reported bug: Cloud Run logs showed mode-switch attempts
    /// never produced a single network call, meaning the flow was going nowhere
    /// before it ever reached the point that would persist anything. The
    /// backend capability this needs — PATCH mode, entitlement-checked — already
    /// existed and is exactly what onboarding's own "fast path" falls back to
    /// for a contractor that already has a number. This calls it directly.
    /// `@MainActor` is explicit here, matching the other save helpers in this
    /// file: every line below mutates observable state, and a mutation landing
    /// off the main thread would leave the row showing the old mode — the exact
    /// symptom this fix exists to remove.
    @MainActor
    private func switchMode() async {
        guard !appState.contractorId.isEmpty else { return }
        let targetMode = appState.isPersonalMode ? "business" : "personal"

        isSwitchingMode = true
        modeChangeError = ""

        // Ask the server rather than trusting the cached subscription tier. A 403
        // is a real answer ("needs Business"), so it routes to the paywall; the
        // local tier is only a UI cache and can be stale, which would otherwise
        // paywall someone who is already entitled.
        switch await APIClient.shared.updateContractorMode(contractorId: appState.contractorId, mode: targetMode) {
        case .success:
            appState.mode = targetMode
        case .entitlementRequired:
            showPaywall = true
        case .failed:
            modeChangeError = String(localized: "Could not switch mode. Please try again.")
        }

        isSwitchingMode = false
    }

    private func updateContractorSetting(_ key: String, _ value: Any) async {
        guard !appState.contractorId.isEmpty else { return }
        do {
            _ = try await APIClient.shared.patchContractor(appState.contractorId, body: [key: value])
        } catch {
            debugLog("Update \(key) failed: \(error)")
            await MainActor.run { saveError = String(localized: "Failed to save setting. Please try again.") }
        }
    }

    private func connectJobber() async {
        guard !appState.contractorId.isEmpty else { return }
        do {
            if let authorizeURL = try await APIClient.shared.getIntegrationConnectURL("jobber", contractorId: appState.contractorId) {
                guard let url = URL(string: authorizeURL),
                      let scheme = url.scheme, scheme == "https",
                      let host = url.host,
                      host == "getjobber.com" || host.hasSuffix(".getjobber.com") else {
                    debugLog("Invalid OAuth URL rejected")
                    return
                }
                await MainActor.run { UIApplication.shared.open(url) }
            }
        } catch {
            debugLog("Connect Jobber failed: \(error)")
        }
    }

    private func disconnectJobber() async {
        guard !appState.contractorId.isEmpty else { return }
        do {
            _ = try await APIClient.shared.disconnectIntegration("jobber", contractorId: appState.contractorId)
            await MainActor.run {
                appState.jobberConnected = false
            }
        } catch {
            debugLog("Disconnect Jobber failed: \(error)")
        }
    }

    private func checkJobberStatus() async {
        guard !appState.contractorId.isEmpty else { return }
        do {
            let connected = try await APIClient.shared.checkIntegrationStatus("jobber", contractorId: appState.contractorId)
            await MainActor.run {
                appState.jobberConnected = connected
            }
        } catch {
            debugLog("Check Jobber status failed: \(error)")
        }
    }

    // MARK: - Google Calendar

    private func connectGoogleCalendar() async {
        guard !appState.contractorId.isEmpty else { return }
        do {
            if let authorizeURL = try await APIClient.shared.getIntegrationConnectURL("google-calendar", contractorId: appState.contractorId) {
                guard let url = URL(string: authorizeURL),
                      let scheme = url.scheme, scheme == "https",
                      let host = url.host,
                      host == "google.com" || host.hasSuffix(".google.com") else {
                    debugLog("Invalid OAuth URL rejected")
                    return
                }
                await MainActor.run { UIApplication.shared.open(url) }
            }
        } catch {
            debugLog("Connect Google Calendar failed: \(error)")
        }
    }

    private func disconnectGoogleCalendar() async {
        guard !appState.contractorId.isEmpty else { return }
        do {
            _ = try await APIClient.shared.disconnectIntegration("google-calendar", contractorId: appState.contractorId)
            await MainActor.run {
                appState.googleCalendarConnected = false
            }
        } catch {
            debugLog("Disconnect Google Calendar failed: \(error)")
        }
    }

    private func checkGoogleCalendarStatus() async {
        guard !appState.contractorId.isEmpty else { return }
        do {
            let connected = try await APIClient.shared.checkIntegrationStatus("google-calendar", contractorId: appState.contractorId)
            await MainActor.run {
                appState.googleCalendarConnected = connected
            }
        } catch {
            debugLog("Check Google Calendar status failed: \(error)")
        }
    }

    private func importWebsite() async {
        guard !websiteURL.isEmpty, !appState.contractorId.isEmpty else { return }
        isImporting = true
        importMessage = ""

        var url = websiteURL
        if !url.hasPrefix("http") {
            url = "https://\(url)"
        }

        if let result = await APIClient.shared.importWebsite(contractorId: appState.contractorId, url: url) {
            if result["status"] as? String == "ok" {
                knowledgeText = result["knowledge"] as? String ?? ""
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
        confirmDeleteTask?.cancel()
        confirmDeleteTask = nil
    }

    private func deleteAccount() async {
        guard !appState.contractorId.isEmpty, !isDeletingAccount else { return }
        await MainActor.run { isDeletingAccount = true }
        var outcome = AccountDeletionOutcome.failed
        do {
            let encodedId = appState.contractorId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? appState.contractorId
            let url = URL(string: "\(appState.backendURL)/api/contractors/\(encodedId)")!
            var request = URLRequest(url: url)
            request.httpMethod = "DELETE"
            request.timeoutInterval = 15
            APIClient.shared.authorize(&request)
            let (data, response) = try await URLSession.shared.data(for: request)
            outcome = AccountDeletionResponseParser.parse(response: response, data: data)
        } catch {
            debugLog("Delete account failed: \(error)")
        }
        // Only clear local state when the server confirmed the deletion (or
        // reported it already gone). Clearing it on failure strands the user:
        // logged out locally while the account stays active and billing.
        if outcome == .failed {
            // Let the confirmation alert finish dismissing before presenting
            // the error alert — flipping a second alert's isPresented during
            // another's dismissal can silently drop it (fast failures like
            // airplane mode land inside the ~300ms dismissal window).
            try? await Task.sleep(nanoseconds: alertRedismissalDelay)
        }
        await MainActor.run {
            isDeletingAccount = false
            switch outcome {
            case .deleted:
                appState.contractorId = ""
                appState.kevinNumber = ""
                appState.isOnboarded = false
                APIClient.shared.contractorToken = ""
            case .failed:
                showDeleteAccountError = true
            }
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
                // Voice record option
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

                // Text editor
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

                // Length warning
                if !knowledgeLengthWarning.isEmpty {
                    Text(knowledgeLengthWarning)
                        .font(.caption)
                        .foregroundStyle(.orange)
                        .padding(.horizontal)
                        .padding(.top, 4)
                }

                // Tip
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
                    .disabled(isSaving || knowledgeText.isEmpty)
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
            // Transcribe locally using Apple Speech Recognition
            let transcript = await transcribeLocally(url: url)
            if let transcript = transcript, !transcript.isEmpty {
                // Send text to Claude for structuring into knowledge doc
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
            // Clean up temporary audio file
            try? FileManager.default.removeItem(at: url)
            await MainActor.run { isTranscribing = false }
        }
    }

    private func transcribeLocally(url: URL) async -> String? {
        let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
        guard let recognizer = recognizer, recognizer.isAvailable else { return nil }

        // Request authorization
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
        guard !appState.contractorId.isEmpty else { return }
        isSaving = true
        await APIClient.shared.updateKnowledge(contractorId: appState.contractorId, knowledge: knowledgeText)
        isSaving = false
        dismiss()
    }
}

import AVFoundation
import Speech

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
