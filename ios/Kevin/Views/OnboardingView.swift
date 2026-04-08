import SwiftUI
import AuthenticationServices

struct OnboardingView: View {
    @EnvironmentObject var appState: AppState
    @State private var step: OnboardingStep = .welcome
    @State private var businessName = ""
    @State private var ownerName = ""
    @State private var serviceType = "general"
    @State private var selectedMode = "business"
    @State private var isLoading = false
    @State private var kevinNumber = ""
    @State private var errorMessage = ""
    @State private var contactsSynced = 0
    @State private var acceptedTerms = false
    @State private var phoneNumber = ""
    @State private var showPaywall = false

    enum OnboardingStep {
        case welcome, signIn, phoneEntry, modeSelect, businessInfo, contactsPermission, personalInfo, provisioning, forwarding, done
    }

    let serviceTypes = ["plumbing", "electrical", "hvac", "general"]

    var body: some View {
        NavigationStack {
            VStack {
                switch step {
                case .welcome:
                    welcomeStep
                case .signIn:
                    signInStep
                case .phoneEntry:
                    phoneEntryStep
                case .modeSelect:
                    modeSelectStep
                case .businessInfo:
                    businessInfoStep
                case .contactsPermission:
                    contactsPermissionStep
                case .personalInfo:
                    personalInfoStep
                case .provisioning:
                    provisioningStep
                case .forwarding:
                    forwardingStep
                case .done:
                    doneStep
                }
            }
            .padding()
        }
    }

    // MARK: - Welcome

    private var welcomeStep: some View {
        VStack(spacing: 24) {
            Spacer()

            ZStack {
                Circle()
                    .fill(
                        LinearGradient(
                            colors: [.blue, .purple, .pink],
                            startPoint: .topLeading,
                            endPoint: .bottomTrailing
                        )
                    )
                    .frame(width: 100, height: 100)
                Text("K")
                    .font(.system(size: 48, weight: .bold))
                    .foregroundStyle(.white)
            }

            Text(String(localized: "Hey Kevin"))
                .font(.largeTitle.bold())

            Text(String(localized: "Your AI phone assistant.\nNever miss an important call again."))
                .font(.title3)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)

            Spacer()

            Button {
                step = .signIn
            } label: {
                Text(String(localized: "Get Started"))
                    .font(.headline)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 16)
            }
            .buttonStyle(.borderedProminent)
            .tint(.blue)
            .clipShape(RoundedRectangle(cornerRadius: 14))
        }
    }

    // MARK: - Sign In with Apple

    private var signInStep: some View {
        VStack(spacing: 24) {
            Spacer()

            Text(String(localized: "Sign In"))
                .font(.title.bold())

            Text(String(localized: "Create your account to get started."))
                .foregroundStyle(.secondary)

            SignInWithAppleButton(.signUp) { request in
                request.requestedScopes = [.fullName, .email]
            } onCompletion: { result in
                handleSignIn(result)
            }
            .signInWithAppleButtonStyle(.black)
            .frame(height: 50)
            .clipShape(RoundedRectangle(cornerRadius: 12))
            .disabled(!acceptedTerms)
            .opacity(acceptedTerms ? 1.0 : 0.5)

            // Terms acceptance
            HStack(alignment: .top, spacing: 10) {
                Button {
                    acceptedTerms.toggle()
                } label: {
                    Image(systemName: acceptedTerms ? "checkmark.square.fill" : "square")
                        .foregroundStyle(acceptedTerms ? .blue : .secondary)
                        .font(.title3)
                }

                Text(String(localized: "I agree to the ")) +
                Text("[\(String(localized: "Terms of Service"))](https://heykevin.one/terms)")
                    .foregroundColor(.blue) +
                Text(String(localized: " and ")) +
                Text("[\(String(localized: "Privacy Policy"))](https://heykevin.one/privacy)")
                    .foregroundColor(.blue)
            }
            .font(.caption)
            .foregroundStyle(.secondary)

            if !errorMessage.isEmpty {
                Text(errorMessage)
                    .foregroundStyle(.red)
                    .font(.caption)
            }

            Spacer()
        }
    }

    // MARK: - Phone Entry

    private var phoneEntryStep: some View {
        VStack(spacing: 24) {
            Spacer()

            Text(String(localized: "Your Phone Number"))
                .font(.title.bold())

            Text(String(localized: "Kevin needs your number to identify your account across devices."))
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)

            TextField(String(localized: "(650) 555-1234"), text: $phoneNumber)
                .textFieldStyle(.roundedBorder)
                .textContentType(.telephoneNumber)
                .keyboardType(.phonePad)
                .font(.title3)
                .multilineTextAlignment(.center)
                .padding(.vertical)

            Spacer()

            Button {
                isLoading = true
                errorMessage = ""
                Task {
                    await restoreOrContinue()
                    isLoading = false
                }
            } label: {
                if isLoading {
                    ProgressView()
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 16)
                } else {
                    Text(String(localized: "Continue"))
                        .font(.headline)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 16)
                }
            }
            .buttonStyle(.borderedProminent)
            .disabled(phoneNumber.filter { $0.isNumber }.count < 10 || isLoading)
            .clipShape(RoundedRectangle(cornerRadius: 14))

            if !errorMessage.isEmpty {
                Text(errorMessage)
                    .foregroundStyle(.red)
                    .font(.caption)
            }
        }
    }

    // MARK: - Mode Selection

    private var modeSelectStep: some View {
        VStack(spacing: 24) {
            Spacer()

            Text(String(localized: "How will you use Kevin?"))
                .font(.title.bold())

            VStack(spacing: 16) {
                Button {
                    selectedMode = "personal"
                    step = .personalInfo
                } label: {
                    HStack(spacing: 16) {
                        Image(systemName: "person.fill")
                            .font(.title2)
                            .frame(width: 44, height: 44)
                            .background(Circle().fill(.purple.opacity(0.2)))
                            .foregroundStyle(.purple)
                        VStack(alignment: .leading, spacing: 4) {
                            Text(String(localized: "Personal Assistant"))
                                .font(.headline)
                            Text(String(localized: "Screen unknown callers. Known contacts ring through."))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        if selectedMode == "personal" {
                            Image(systemName: "checkmark.circle.fill")
                                .foregroundStyle(.purple)
                        }
                    }
                    .padding()
                    .background(Color(.systemGray6))
                    .clipShape(RoundedRectangle(cornerRadius: 14))
                }
                .buttonStyle(.plain)

                Button {
                    selectedMode = "business"
                    step = .businessInfo
                } label: {
                    HStack(spacing: 16) {
                        Image(systemName: "briefcase.fill")
                            .font(.title2)
                            .frame(width: 44, height: 44)
                            .background(Circle().fill(.blue.opacity(0.2)))
                            .foregroundStyle(.blue)
                        VStack(alignment: .leading, spacing: 4) {
                            Text(String(localized: "Business Assistant"))
                                .font(.headline)
                            Text(String(localized: "Answer calls for your business, take messages, give estimates."))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        if selectedMode == "business" {
                            Image(systemName: "checkmark.circle.fill")
                                .foregroundStyle(.blue)
                        }
                    }
                    .padding()
                    .background(Color(.systemGray6))
                    .clipShape(RoundedRectangle(cornerRadius: 14))
                }
                .buttonStyle(.plain)
            }

            Spacer()
        }
    }

    // MARK: - Contacts Permission

    private var contactsPermissionStep: some View {
        VStack(spacing: 24) {
            Spacer()

            Image(systemName: "person.crop.circle.badge.checkmark")
                .font(.system(size: 56))
                .foregroundStyle(.blue)

            Text(String(localized: "Recognize Your Contacts"))
                .font(.title.bold())

            Text(String(localized: "Kevin can recognize callers in your contacts so their calls ring through without screening."))
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)

            Spacer()

            Button {
                Task {
                    let granted = await ContactSyncManager.shared.requestAccess()
                    if granted {
                        // Sync will happen after provisioning when contractorId is available
                    }
                    step = .provisioning
                    await provision(mode: "business")
                }
            } label: {
                Text(String(localized: "Allow Contact Access"))
                    .font(.headline)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 16)
            }
            .buttonStyle(.borderedProminent)
            .clipShape(RoundedRectangle(cornerRadius: 14))

            Button(String(localized: "Skip")) {
                step = .provisioning
                Task { await provision(mode: "business") }
            }
            .font(.subheadline)
            .foregroundStyle(.secondary)
        }
    }

    // MARK: - Personal Info

    private var personalInfoStep: some View {
        VStack(spacing: 24) {
            Text(String(localized: "About You"))
                .font(.title.bold())

            Text(String(localized: "Kevin will use your name to greet callers."))
                .foregroundStyle(.secondary)

            TextField(String(localized: "Your Name"), text: $ownerName)
                .textFieldStyle(.roundedBorder)
                .textContentType(.name)
                .padding(.vertical)

            Text(String(localized: "Kevin will sync your iPhone contacts so known callers ring through automatically."))
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)

            Spacer()

            Button {
                step = .provisioning
                Task { await provision(mode: "personal") }
            } label: {
                Text(String(localized: "Continue"))
                    .font(.headline)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 16)
            }
            .buttonStyle(.borderedProminent)
            .disabled(ownerName.isEmpty)
            .clipShape(RoundedRectangle(cornerRadius: 14))
        }
    }

    // MARK: - Business Info

    private var businessInfoStep: some View {
        VStack(spacing: 24) {
            Text(String(localized: "About Your Business"))
                .font(.title.bold())

            Text(String(localized: "Kevin will use this to greet callers."))
                .foregroundStyle(.secondary)

            VStack(spacing: 16) {
                TextField(String(localized: "Your Name"), text: $ownerName)
                    .textFieldStyle(.roundedBorder)
                    .textContentType(.name)

                TextField(String(localized: "Business Name"), text: $businessName)
                    .textFieldStyle(.roundedBorder)
                    .textContentType(.organizationName)
            }
            .padding(.vertical)

            Spacer()

            Button {
                step = .contactsPermission
            } label: {
                Text(String(localized: "Continue"))
                    .font(.headline)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 16)
            }
            .buttonStyle(.borderedProminent)
            .disabled(ownerName.isEmpty || businessName.isEmpty)
            .clipShape(RoundedRectangle(cornerRadius: 14))
        }
    }

    // MARK: - Provisioning

    private var provisioningStep: some View {
        VStack(spacing: 24) {
            Spacer()

            ProgressView()
                .scaleEffect(1.5)

            Text(String(localized: "Setting up your Kevin number..."))
                .font(.title3)

            Text(String(localized: "This takes a few seconds."))
                .foregroundStyle(.secondary)

            if !errorMessage.isEmpty {
                Text(errorMessage)
                    .foregroundStyle(.red)
                    .font(.caption)

                Button(String(localized: "Try Again")) {
                    errorMessage = ""
                    Task { await provision(mode: selectedMode) }
                }

                Button(String(localized: "Start Over")) {
                    step = .welcome
                    errorMessage = ""
                }
                .font(.subheadline)
                .foregroundStyle(.secondary)
            }

            Spacer()
        }
    }

    // MARK: - Forwarding Setup

    private var forwardingStep: some View {
        VStack(spacing: 20) {
            Text(String(localized: "Set Up Call Forwarding"))
                .font(.title.bold())

            Text(String(localized: "Forward your missed calls to Kevin.\nYour phone rings first — Kevin catches what you miss."))
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)

            VStack(spacing: 12) {
                // Step 1: Clear existing forwarding
                Button {
                    if let url = URL(string: "tel:%23%2321%23") {
                        UIApplication.shared.open(url)
                    }
                } label: {
                    HStack {
                        Text("1")
                            .font(.caption.bold())
                            .frame(width: 24, height: 24)
                            .background(Circle().fill(.blue))
                            .foregroundStyle(.white)
                        Text(String(localized: "Clear existing forwarding"))
                            .font(.subheadline)
                        Spacer()
                        Image(systemName: "phone.arrow.right")
                    }
                    .padding()
                    .background(Color(.systemGray6))
                    .clipShape(RoundedRectangle(cornerRadius: 12))
                }
                .buttonStyle(.plain)

                // Step 2: Set Kevin forwarding
                Button {
                    let number = kevinNumber.filter { $0.isNumber }
                    if let url = URL(string: "tel:*61*\(number)%23") {
                        UIApplication.shared.open(url)
                    }
                } label: {
                    HStack {
                        Text("2")
                            .font(.caption.bold())
                            .frame(width: 24, height: 24)
                            .background(Circle().fill(.blue))
                            .foregroundStyle(.white)
                        Text(String(localized: "Forward missed calls to Kevin"))
                            .font(.subheadline)
                        Spacer()
                        Image(systemName: "phone.arrow.right")
                    }
                    .padding()
                    .background(Color(.systemGray6))
                    .clipShape(RoundedRectangle(cornerRadius: 12))
                }
                .buttonStyle(.plain)

                // Step 3: Test
                Button {
                    let digits = kevinNumber.filter { $0.isNumber }
                    if let url = URL(string: "tel:\(digits)") {
                        UIApplication.shared.open(url)
                    }
                } label: {
                    HStack {
                        Text("3")
                            .font(.caption.bold())
                            .frame(width: 24, height: 24)
                            .background(Circle().fill(.green))
                            .foregroundStyle(.white)
                        Text(String(localized: "Test it — call your Kevin number"))
                            .font(.subheadline)
                        Spacer()
                        Image(systemName: "phone.fill")
                    }
                    .padding()
                    .background(Color(.systemGray6))
                    .clipShape(RoundedRectangle(cornerRadius: 12))
                }
                .buttonStyle(.plain)
            }

            Text(String(localized: "Your Kevin number: \(kevinNumber)"))
                .font(.subheadline.monospacedDigit())
                .foregroundStyle(.secondary)

            Spacer()

            Button {
                completeOnboarding()
            } label: {
                Text(String(localized: "I'm All Set"))
                    .font(.headline)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 16)
            }
            .buttonStyle(.borderedProminent)
            .clipShape(RoundedRectangle(cornerRadius: 14))

            Button(String(localized: "Skip for now")) {
                completeOnboarding()
            }
            .font(.subheadline)
            .foregroundStyle(.secondary)
        }
    }

    // MARK: - Done

    private var doneStep: some View {
        VStack(spacing: 24) {
            Spacer()

            Image(systemName: "checkmark.circle.fill")
                .font(.system(size: 64))
                .foregroundStyle(.green)

            Text(String(localized: "You're All Set!"))
                .font(.title.bold())

            Text(String(localized: "Kevin is ready to answer your calls."))
                .foregroundStyle(.secondary)

            Spacer()

            Button {
                showPaywall = true
            } label: {
                Text(String(localized: "Start Free Trial"))
                    .font(.headline)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 16)
            }
            .buttonStyle(.borderedProminent)
            .clipShape(RoundedRectangle(cornerRadius: 14))
            .sheet(isPresented: $showPaywall, onDismiss: {
                appState.isOnboarded = true
            }) {
                PaywallView()
                    .environmentObject(appState)
            }
        }
    }

    // MARK: - Logic

    private func handleSignIn(_ result: Result<ASAuthorization, Error>) {
        switch result {
        case .success(let auth):
            if let credential = auth.credential as? ASAuthorizationAppleIDCredential {
                let userId = credential.user
                let fullName = credential.fullName
                let name = [fullName?.givenName, fullName?.familyName]
                    .compactMap { $0 }
                    .joined(separator: " ")

                if !name.isEmpty {
                    ownerName = name
                }

                // Store Apple user ID
                appState.appleUserId = userId

                // Send identity token to backend for verification
                if let tokenData = credential.identityToken,
                   let token = String(data: tokenData, encoding: .utf8) {
                    appState.appleIdentityToken = token
                }

                // Try to restore existing account
                Task {
                    isLoading = true
                    await tryRestore()
                    isLoading = false
                }
            }
        case .failure(let error):
            errorMessage = error.localizedDescription
        }
    }

    private func tryRestore() async {
        // If user explicitly triggered mode change, skip restore and go to mode select
        if appState.pendingModeChange {
            appState.pendingModeChange = false
            await MainActor.run { step = .modeSelect }
            return
        }

        // 1. Check if contractorId is already saved (Keychain, migrated from UserDefaults)
        if !appState.contractorId.isEmpty {
            if let profile = await APIClient.shared.getContractorProfile(contractorId: appState.contractorId) {
                let active = profile["active"] as? Bool ?? false
                if active {
                    await restoreFromProfile(profile)
                    return
                }
            }
        }

        // 2. Look up by Apple User ID on backend
        if !appState.appleUserId.isEmpty {
            if let result = await APIClient.shared.findContractorByAppleId(appleUserId: appState.appleUserId, appleIdentityToken: appState.appleIdentityToken) {
                if let contractorId = result["contractor_id"] as? String {
                    appState.contractorId = contractorId
                    // Save API token returned by lookup (login flow)
                    if let apiToken = result["api_token"] as? String, !apiToken.isEmpty {
                        APIClient.shared.contractorToken = apiToken
                    }
                    if let profile = await APIClient.shared.getContractorProfile(contractorId: contractorId) {
                        await restoreFromProfile(profile)
                        return
                    }
                }
            }
        }

        // 3. No account found — new user, continue with onboarding
        await MainActor.run { step = .modeSelect }
    }

    private func restoreFromProfile(_ profile: [String: Any]) async {
        let name = profile["owner_name"] as? String ?? ""
        let biz = profile["business_name"] as? String ?? ""
        let mode = profile["mode"] as? String ?? "business"
        let number = profile["twilio_number"] as? String ?? ""

        await MainActor.run {
            if !name.isEmpty { appState.userName = name }
            if !biz.isEmpty { appState.businessName = biz }
            appState.mode = (mode == "personal") ? "personal" : "business"
            if !number.isEmpty { appState.kevinNumber = number }
            appState.isOnboarded = true
        }

        // Sync contacts in background
        _ = await ContactSyncManager.shared.syncContacts(contractorId: appState.contractorId)
    }

    private func restoreOrContinue() async {
        // Try to find existing contractor via phone number
        let result = await APIClient.shared.createContractor(
            ownerName: ownerName,
            businessName: "",
            serviceType: "general",
            ownerPhone: phoneNumber,
            appleUserId: appState.appleUserId,
            appleIdentityToken: appState.appleIdentityToken
        )

        if let contractorId = result?["contractor_id"] as? String,
           let isExisting = result?["existing"] as? Bool, isExisting {
            // Existing account found — restore it
            appState.contractorId = contractorId
            if let profile = await APIClient.shared.getContractorProfile(contractorId: contractorId) {
                await restoreFromProfile(profile)
            } else {
                await MainActor.run { appState.isOnboarded = true }
            }
        } else {
            // New user — continue with onboarding
            await MainActor.run { step = .modeSelect }
        }
    }

    private func provision(mode: String) async {
        let isPersonal = mode == "personal"
        let bizName = isPersonal ? "\(ownerName)'s phone" : businessName
        let svcType = isPersonal ? "personal" : serviceType

        isLoading = true
        errorMessage = ""

        // Reuse existing contractor if we have one, otherwise create new
        var contractorId = appState.contractorId

        if contractorId.isEmpty {
            // No existing contractor — create one (with Apple User ID for dedup)
            let result = await APIClient.shared.createContractor(
                ownerName: ownerName,
                businessName: bizName,
                serviceType: svcType,
                ownerPhone: phoneNumber,
                appleUserId: appState.appleUserId,
                appleIdentityToken: appState.appleIdentityToken
            )
            contractorId = result?["contractor_id"] as? String ?? ""
            if contractorId.isEmpty {
                errorMessage = String(localized: "Failed to create profile. Please try again.")
                isLoading = false
                return
            }
            appState.contractorId = contractorId
            // Store per-contractor API token if returned
            if let apiToken = result?["api_token"] as? String, !apiToken.isEmpty {
                APIClient.shared.contractorToken = apiToken
            }
        } else {
            // Existing contractor — update profile info
            _ = await APIClient.shared.updateContractorMode(contractorId: contractorId, mode: mode)
            let updateBody: [String: Any] = [
                "owner_name": ownerName,
                "business_name": bizName,
                "mode": mode,
            ]
            do {
                _ = try await APIClient.shared.patchContractor(contractorId, body: updateBody)
            } catch {
                #if DEBUG
                print("Update profile info failed: \(error)")
                #endif
            }
            appState.userName = ownerName
            appState.businessName = bizName
        }

        appState.mode = mode

        // Check if contractor already has a Twilio number
        if let profile = await APIClient.shared.getContractorProfile(contractorId: contractorId),
           let existingNumber = profile["twilio_number"] as? String,
           !existingNumber.isEmpty {
            // Reuse existing number
            kevinNumber = existingNumber
            appState.kevinNumber = kevinNumber
        } else {
            // Provision new Twilio number
            if let provResult = await APIClient.shared.provisionNumber(contractorId: contractorId) {
                kevinNumber = provResult["phone_number"] as? String ?? ""
                appState.kevinNumber = kevinNumber
            } else {
                errorMessage = String(localized: "Failed to provision number. Please try again.")
                isLoading = false
                return
            }
        }

        // Sync contacts
        if isPersonal {
            let granted = await ContactSyncManager.shared.requestAccess()
            if granted {
                let syncResult = await ContactSyncManager.shared.syncContacts(contractorId: contractorId)
                if case .success(let synced, _) = syncResult {
                    contactsSynced = synced
                }
            }
        } else {
            _ = await ContactSyncManager.shared.syncContacts(contractorId: contractorId)
        }

        // Clear identity token after successful provisioning
        appState.appleIdentityToken = ""

        step = .forwarding
        isLoading = false
    }

    private func completeOnboarding() {
        // Note: isOnboarded is set after paywall is dismissed (in doneStep sheet onDismiss)
        step = .done
    }
}
