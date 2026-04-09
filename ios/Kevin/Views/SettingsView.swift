import SwiftUI

private func debugLog(_ message: String) {
    #if DEBUG
    print(message)
    #endif
}

struct SettingsView: View {
    @EnvironmentObject var appState: AppState
    @State private var showPaywall = false
    @State private var showDeleteAccountAlert = false
    @State private var showAboutDebug = false
    @State private var showKnowledgeEditor = false
    @State private var knowledgeText = ""
    @State private var websiteURL = ""
    @State private var isImporting = false
    @State private var importMessage = ""
    @State private var syncMessage = ""
    @State private var showModeChangeAlert = false
    @State private var isSaving = false
    @State private var saveError = ""
    @State private var knowledgeLengthWarning = ""
    @State private var businessHoursStart = Calendar.current.date(from: DateComponents(hour: 8, minute: 0)) ?? Date()
    @State private var businessHoursEnd = Calendar.current.date(from: DateComponents(hour: 17, minute: 0)) ?? Date()

    private var kevinNumber: String {
        appState.kevinNumber.isEmpty ? "16504222677" : appState.kevinNumber
    }

    var body: some View {
        NavigationStack {
            Form {
                // MARK: - Subscription Status

                Section {
                    HStack {
                        Text("Plan")
                        Spacer()
                        Text(subscriptionStatusLabel)
                            .foregroundStyle(subscriptionStatusColor)
                    }

                    Button {
                        showPaywall = true
                    } label: {
                        HStack {
                            Text(appState.subscriptionStatus == "trial" ? String(localized: "View Plans") : String(localized: "Subscribe to Kevin AI"))
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
                                    .foregroundStyle(.tertiary)
                            }
                        }
                        .foregroundStyle(.primary)
                    }
                } header: {
                    Text("Subscription")
                }
                .sheet(isPresented: $showPaywall) {
                    PaywallView(canDismiss: true)
                        .environmentObject(appState)
                }

                // MARK: - Kevin Status

                Section {
                    HStack {
                        Text(String(localized: "Kevin Number"))
                        Spacer()
                        Text(formattedKevinNumber)
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                    }
                } header: {
                    Text(String(localized: "Kevin"))
                }

                // MARK: - Profile (adapts to mode)

                if appState.isPersonalMode {
                    Section {
                        HStack {
                            Text(String(localized: "Name"))
                            Spacer()
                            Text(appState.userName)
                                .foregroundStyle(.secondary)
                        }

                        Button {
                            showModeChangeAlert = true
                        } label: {
                            HStack {
                                Text(String(localized: "Mode"))
                                Spacer()
                                Text(appState.isPersonalMode ? String(localized: "Personal Assistant") : String(localized: "Business Assistant"))
                                    .foregroundStyle(appState.isPersonalMode ? .purple : .blue)
                                Image(systemName: "arrow.triangle.2.circlepath")
                                    .foregroundStyle(.tertiary)
                                    .font(.caption)
                            }
                        }
                        .foregroundStyle(.primary)

                        Button {
                            Task {
                                let result = await ContactSyncManager.shared.syncContacts(
                                    contractorId: appState.contractorId
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
                    } header: {
                        Text(String(localized: "My Profile"))
                    } footer: {
                        Text(String(localized: "Contacts from your iPhone ring through. Unknown callers are screened by Kevin."))
                    }
                } else {
                    Section {
                        HStack {
                            Text(String(localized: "Name"))
                            Spacer()
                            Text(appState.userName)
                                .foregroundStyle(.secondary)
                        }

                        HStack {
                            Text(String(localized: "Business"))
                            Spacer()
                            Text(appState.businessName.isEmpty ? String(localized: "Not set") : appState.businessName)
                                .foregroundStyle(appState.businessName.isEmpty ? .tertiary : .secondary)
                        }

                        Button {
                            showModeChangeAlert = true
                        } label: {
                            HStack {
                                Text(String(localized: "Mode"))
                                Spacer()
                                Text(String(localized: "Business Assistant"))
                                    .foregroundStyle(.blue)
                                Image(systemName: "arrow.triangle.2.circlepath")
                                    .foregroundStyle(.tertiary)
                                    .font(.caption)
                            }
                        }
                        .foregroundStyle(.primary)
                    } header: {
                        Text(String(localized: "My Business"))
                    }

                    // MARK: - Business Hours

                    Section {
                        DatePicker(String(localized: "Open"), selection: $businessHoursStart, displayedComponents: .hourAndMinute)
                            .onChange(of: businessHoursStart) { _, _ in saveBusinessHours() }
                        DatePicker(String(localized: "Close"), selection: $businessHoursEnd, displayedComponents: .hourAndMinute)
                            .onChange(of: businessHoursEnd) { _, _ in saveBusinessHours() }
                    } header: {
                        Text(String(localized: "Business Hours"))
                    } footer: {
                        Text(String(localized: "Outside these hours, Kevin will tell callers you're closed and take a message."))
                    }

                    // MARK: - Services & Pricing

                    Section {
                        NavigationLink {
                            ServicesView()
                        } label: {
                            HStack {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(String(localized: "Services & Pricing"))
                                        .font(.subheadline)
                                    Text(String(localized: "Add your services so Kevin can quote estimates"))
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                            }
                        }
                    }

                    // MARK: - Knowledge Base

                    Section {
                        Button {
                            showKnowledgeEditor = true
                        } label: {
                            HStack {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(String(localized: "Business Knowledge"))
                                        .font(.subheadline)
                                    Text(String(localized: "Tell Kevin about your business so he can answer questions"))
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                                Spacer()
                                Image(systemName: "chevron.right")
                                    .foregroundStyle(.tertiary)
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
                }

                // MARK: - Contact Screening

                Section {
                    Toggle(String(localized: "Known contacts ring through"), isOn: $appState.ringThroughContacts)
                        .onChange(of: appState.ringThroughContacts) { _, newValue in
                            Task {
                                isSaving = true
                                saveError = ""
                                await updateRingThrough(newValue)
                                isSaving = false
                            }
                        }

                    Toggle(String(localized: "Block spam with disconnect tone"), isOn: $appState.sitToneEnabled)
                        .onChange(of: appState.sitToneEnabled) { _, newValue in
                            Task {
                                isSaving = true
                                saveError = ""
                                await updateSitToneEnabled(newValue)
                                isSaving = false
                            }
                        }

                    Toggle(String(localized: "Auto-reply text to callers"), isOn: $appState.autoReplySms)
                        .onChange(of: appState.autoReplySms) { _, newValue in
                            Task {
                                isSaving = true
                                saveError = ""
                                await updateContractorSetting("auto_reply_sms", newValue)
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

                    if !saveError.isEmpty {
                        Text(saveError)
                            .font(.caption)
                            .foregroundStyle(.red)
                    }

                } header: {
                    Text(String(localized: "Call Screening"))
                } footer: {
                    Text(String(localized: "Urgent call alerts ring your phone immediately when a caller reports an emergency like flooding, fire, or gas leak."))
                }
                .disabled(isSaving)

                // MARK: - Integrations

                if !appState.isPersonalMode {
                    Section {
                        // Jobber row
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(String(localized: "Jobber"))
                                    .font(.subheadline.weight(.medium))
                                Text(String(localized: "Schedule checking, job creation, customer lookup"))
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
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
                                Text(String(localized: "Availability checking, appointment booking"))
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
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
                        Text(String(localized: "Connect Jobber to let Kevin look up customers and create jobs automatically. Connect Google Calendar to check availability and book appointments."))
                    }
                }

                // MARK: - Call Forwarding

                Section {
                    Button {
                        // *61* = forward on no answer (same as onboarding)
                        dialCode("*61*\(dialNumber)%23")
                    } label: {
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(String(localized: "Activate Kevin"))
                                    .font(.subheadline.weight(.medium))
                                Text(String(localized: "Forward missed calls to Kevin"))
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            Image(systemName: "phone.arrow.right")
                                .foregroundStyle(.green)
                        }
                    }

                    Button(role: .destructive) {
                        // ##61# = cancel no-answer forwarding (GSM standard)
                        dialCode("%23%2361%23")
                    } label: {
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(String(localized: "Deactivate Kevin"))
                                    .font(.subheadline.weight(.medium))
                                Text(String(localized: "Stop forwarding, calls ring normally"))
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            Image(systemName: "xmark.circle")
                                .foregroundStyle(.red)
                        }
                    }
                } header: {
                    Text(String(localized: "Call Forwarding"))
                } footer: {
                    Text(String(localized: "Tapping opens your phone dialer. Tap Call to confirm. Verizon users: use *71 to activate and *73 to deactivate instead."))
                }

                // MARK: - Account

                Section {
                    Button(role: .destructive) {
                        showDeleteAccountAlert = true
                    } label: {
                        Text(String(localized: "Delete Account"))
                    }
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

                // MARK: - Legal

                Section {
                    Link(destination: URL(string: "https://heykevin.one/privacy")!) {
                        HStack {
                            Text(String(localized: "Privacy Policy"))
                            Spacer()
                            Image(systemName: "arrow.up.right.square")
                                .foregroundStyle(.tertiary)
                        }
                    }
                    .foregroundStyle(.primary)

                    Link(destination: URL(string: "https://heykevin.one/terms")!) {
                        HStack {
                            Text(String(localized: "Terms of Service"))
                            Spacer()
                            Image(systemName: "arrow.up.right.square")
                                .foregroundStyle(.tertiary)
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
                            .foregroundStyle(.secondary)
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
            .sheet(isPresented: $showKnowledgeEditor) {
                KnowledgeEditorView(knowledgeText: $knowledgeText)
            }
            .alert(String(localized: "Change Mode"), isPresented: $showModeChangeAlert) {
                Button(String(localized: "Switch Mode")) {
                    appState.pendingModeChange = true
                    appState.isOnboarded = false
                }
                Button(String(localized: "Cancel"), role: .cancel) {}
            } message: {
                Text(String(localized: "This will take you through the setup again so you can switch between Personal and Business mode. Your Kevin number will be kept."))
            }
            .task {
                await loadKnowledge()
                await checkJobberStatus()
                await checkGoogleCalendarStatus()
            }
        }
    }

    private var formattedKevinNumber: String {
        PhoneFormatter.format(kevinNumber)
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

    private var subscriptionStatusLabel: String {
        switch appState.subscriptionStatus {
        case "trial": return "Free Trial"
        case "active": return "Active — \(tierLabel)"
        case "expired": return "Expired"
        case "cancelled": return "Cancelled"
        default: return appState.subscriptionStatus.isEmpty ? "Free Trial" : appState.subscriptionStatus.capitalized
        }
    }

    private var subscriptionStatusColor: Color {
        switch appState.subscriptionStatus {
        case "trial": return .blue
        case "active": return .green
        case "expired", "cancelled": return .red
        default: return .blue
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
            let mode = contractor["mode"] as? String ?? "business"
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

    private func deleteAccount() async {
        guard !appState.contractorId.isEmpty else { return }
        do {
            let encodedId = appState.contractorId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? appState.contractorId
            let url = URL(string: "\(appState.backendURL)/api/contractors/\(encodedId)")!
            var request = URLRequest(url: url)
            request.httpMethod = "DELETE"
            request.timeoutInterval = 15
            APIClient.shared.authorize(&request)
            let (_, _) = try await URLSession.shared.data(for: request)
        } catch {
            debugLog("Delete account failed: \(error)")
        }
        // Clear local state regardless of server response
        await MainActor.run {
            appState.contractorId = ""
            appState.kevinNumber = ""
            appState.isOnboarded = false
            APIClient.shared.contractorToken = ""
        }
    }
}

// MARK: - Knowledge Editor

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
                                    .foregroundStyle(.secondary)
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

                // Text editor
                ZStack(alignment: .topLeading) {
                    TextEditor(text: $knowledgeText)
                        .font(.system(.subheadline, design: .monospaced))
                        .scrollContentBackground(.hidden)

                    if knowledgeText.isEmpty {
                        Text(placeholder)
                            .font(.system(.subheadline, design: .monospaced))
                            .foregroundStyle(.tertiary)
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
                    .foregroundStyle(.tertiary)
                    .padding(.horizontal)
                    .padding(.top, 4)
                    .padding(.bottom, 8)
            }
            .onChange(of: knowledgeText) { _, newValue in
                if newValue.count > 10_000 {
                    knowledgeText = String(newValue.prefix(10_000))
                    knowledgeLengthWarning = String(localized: "Knowledge text truncated to 10,000 characters.")
                } else {
                    knowledgeLengthWarning = ""
                }
            }
            .navigationTitle(String(localized: "Services & Pricing"))
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
                    rawText: transcript
                ) {
                    await MainActor.run {
                        if knowledgeText.isEmpty {
                            knowledgeText = knowledge
                        } else {
                            knowledgeText += "\n\n" + knowledge
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
