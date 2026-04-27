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
    @State private var isVerizon = AppState.shared.isVerizonCarrier
    @State private var showPaywall = false
    @State private var didPrepareInitialStep = false

    private let businessProductID = "com.kevin.callscreen.business.monthly"

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
        .task {
            await prepareInitialStep()
        }
        .sheet(isPresented: $showPaywall) {
            PaywallView(
                canDismiss: true,
                isOnboarding: false,
                preferredProductID: businessProductID,
                onSubscribed: {
                    Task { await activateBusinessAfterPurchase() }
                },
                showsTrialSkip: false
            )
            .environmentObject(appState)
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
        ScrollView {
            VStack(spacing: 20) {
                Image(systemName: "person.crop.circle.badge.checkmark")
                    .font(.system(size: 56))
                    .foregroundStyle(.blue)
                    .padding(.top, 24)

                Text(String(localized: "Recognize Your Contacts"))
                    .font(.title.bold())
                    .multilineTextAlignment(.center)

                Text(String(localized: "To recognize callers by name and let trusted contacts ring through without AI screening, Hey Kevin needs to upload your contacts to our secure server."))
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal)

                VStack(alignment: .leading, spacing: 14) {
                    disclosureRow(icon: "lock.shield.fill", color: .blue,
                                  title: String(localized: "Uploaded securely"),
                                  body: String(localized: "Sent over an encrypted connection and stored on servers only you can access with your account."))
                    disclosureRow(icon: "person.2.fill", color: .green,
                                  title: String(localized: "Used only to identify your callers"),
                                  body: String(localized: "We match incoming caller numbers against your contacts so known callers can ring through directly."))
                    disclosureRow(icon: "hand.raised.fill", color: .purple,
                                  title: String(localized: "Never shared or sold"),
                                  body: String(localized: "Your contacts are never used for advertising, shared with third parties, or sold."))
                    disclosureRow(icon: "trash.fill", color: .red,
                                  title: String(localized: "Deleted with your account"),
                                  body: String(localized: "Remove your account and your contacts are permanently deleted from our servers."))
                }
                .padding()
                .background(Color(.systemGray6))
                .clipShape(RoundedRectangle(cornerRadius: 14))
                .padding(.horizontal)

                HStack(spacing: 12) {
                    Link(String(localized: "Privacy Policy"),
                         destination: URL(string: "https://heykevin.one/privacy")!)
                        .font(.caption)
                    Link(String(localized: "Terms of Use"),
                         destination: URL(string: "https://heykevin.one/terms")!)
                        .font(.caption)
                }

                Button {
                    Task {
                        let granted = await ContactSyncManager.shared.requestAccess()
                        if granted {
                            appState.contactsUploadConsent = true
                        }
                        step = .provisioning
                        await provision(mode: selectedMode)
                    }
                } label: {
                    Text(String(localized: "Allow & Upload Contacts"))
                        .font(.headline)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 16)
                }
                .buttonStyle(.borderedProminent)
                .clipShape(RoundedRectangle(cornerRadius: 14))
                .padding(.horizontal)

                Button(String(localized: "Not now")) {
                    appState.contactsUploadConsent = false
                    step = .provisioning
                    Task { await provision(mode: selectedMode) }
                }
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .padding(.bottom, 24)
            }
        }
    }

    @ViewBuilder
    private func disclosureRow(icon: String, color: Color, title: String, body: String) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: icon)
                .foregroundStyle(color)
                .font(.title3)
                .frame(width: 28)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.subheadline.bold())
                Text(body)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
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

            Spacer()

            Button {
                selectedMode = "personal"
                step = .contactsPermission
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
                selectedMode = "business"
                if appState.hasBusinessEntitlement {
                    step = .contactsPermission
                } else {
                    Task {
                        let prepared = await prepareBusinessDraftProfile()
                        if prepared {
                            showPaywall = true
                        }
                    }
                }
            } label: {
                Text(appState.hasBusinessEntitlement
                     ? String(localized: "Continue")
                     : String(localized: "Start Business Trial"))
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
                    let code = isVerizon ? "tel:*73" : "tel:%23%2321%23"
                    if let url = URL(string: code) {
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
                    let code = isVerizon ? "tel:*71\(number)" : "tel:*61*\(number)%23"
                    if let url = URL(string: code) {
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

            if !isVerizon {
                Button {
                    isVerizon = true
                    appState.isVerizonCarrier = true
                } label: {
                    Text(String(localized: "Verizon customer? Tap here"))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .underline()
                }
            } else {
                Button {
                    isVerizon = false
                    appState.isVerizonCarrier = false
                } label: {
                    Text(String(localized: "✓ Using Verizon codes — tap to switch back"))
                        .font(.caption)
                        .foregroundStyle(.blue)
                }
            }

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
        // Skip the intermediate "You're All Set" screen.
        // Go straight to PaywallView as the final onboarding step.
        PaywallView(canDismiss: true, isOnboarding: true)
            .environmentObject(appState)
    }

    // MARK: - Logic

    @MainActor
    private func prepareInitialStep() async {
        guard !didPrepareInitialStep else { return }
        didPrepareInitialStep = true

        guard appState.pendingModeChange else { return }

        // Mode changes are launched from Settings for an existing, authenticated
        // account. Do not ask for Sign in with Apple again just to patch mode.
        guard !appState.contractorId.isEmpty else {
            appState.pendingModeChange = false
            return
        }

        appState.pendingModeChange = false
        ownerName = appState.userName
        businessName = appState.businessName
        if !appState.serviceType.isEmpty {
            serviceType = appState.serviceType
        }
        selectedMode = appState.mode == "personal" ? "business" : "personal"
        kevinNumber = appState.kevinNumber
        step = .modeSelect

        if let profile = await APIClient.shared.getContractorProfile(contractorId: appState.contractorId) {
            let name = profile["owner_name"] as? String ?? ""
            let biz = profile["business_name"] as? String ?? ""
            let svc = profile["service_type"] as? String ?? ""
            let mode = profile["effective_mode"] as? String ?? profile["mode"] as? String ?? appState.mode
            let number = profile["twilio_number"] as? String ?? ""

            if !name.isEmpty {
                ownerName = name
                appState.userName = name
            }
            if !biz.isEmpty {
                businessName = biz
                appState.businessName = biz
            }
            if !svc.isEmpty {
                serviceType = svc
                appState.serviceType = svc
            }
            let normalizedMode = mode == "personal" ? "personal" : "business"
            appState.mode = normalizedMode
            selectedMode = normalizedMode == "personal" ? "business" : "personal"
            if !number.isEmpty {
                kevinNumber = number
                appState.kevinNumber = number
            }
        }
    }

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
        let mode = profile["effective_mode"] as? String ?? profile["mode"] as? String ?? "personal"
        let number = profile["twilio_number"] as? String ?? ""
        let subUUID = profile["subscription_uuid"] as? String ?? ""

        await MainActor.run {
            if !name.isEmpty { appState.userName = name }
            if !biz.isEmpty { appState.businessName = biz }
            appState.mode = (mode == "personal") ? "personal" : "business"
            if !subUUID.isEmpty { appState.subscriptionUUID = subUUID }
        }

        // If account has no Kevin number, provision one before completing restore
        if number.isEmpty {
            await MainActor.run { step = .provisioning }
            await provision(mode: (mode == "personal") ? "personal" : "business")
            return
        }

        await MainActor.run {
            appState.kevinNumber = number
            appState.isOnboarded = true
        }

        // Sync contacts in background only if user has previously consented to upload
        if appState.contactsUploadConsent {
            _ = await ContactSyncManager.shared.syncContacts(contractorId: appState.contractorId)
        }
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
                mode: mode,
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

            // If the create endpoint restored an existing account by phone,
            // persist the selected mode/profile before continuing.
            if result?["existing"] as? Bool == true {
                let updateBody: [String: Any] = [
                    "owner_name": ownerName,
                    "business_name": bizName,
                    "mode": mode,
                ]
                do {
                    let updated = try await APIClient.shared.patchContractor(contractorId, body: updateBody)
                    if !updated {
                        errorMessage = mode == "business"
                            ? String(localized: "Business mode requires an active Business subscription. Restore purchases or choose Personal.")
                            : String(localized: "Failed to update profile. Please try again.")
                        isLoading = false
                        return
                    }
                } catch {
                    errorMessage = String(localized: "Failed to update profile. Please try again.")
                    isLoading = false
                    return
                }
                appState.userName = ownerName
                appState.businessName = bizName
            }
        } else {
            // Existing contractor — update profile info
            let updateBody: [String: Any] = [
                "owner_name": ownerName,
                "business_name": bizName,
                "mode": mode,
            ]
            do {
                let updated = try await APIClient.shared.patchContractor(contractorId, body: updateBody)
                if !updated {
                    errorMessage = mode == "business"
                        ? String(localized: "Business mode requires an active Business subscription. Restore purchases or choose Personal.")
                        : String(localized: "Failed to update profile. Please try again.")
                    isLoading = false
                    return
                }
            } catch {
                errorMessage = String(localized: "Failed to update profile. Please try again.")
                isLoading = false
                return
            }
            appState.userName = ownerName
            appState.businessName = bizName
        }

        appState.mode = mode

        // Check if contractor already has a Twilio number. During mode changes,
        // keep the current Kevin number even if the profile fetch is transiently stale.
        if let profile = await APIClient.shared.getContractorProfile(contractorId: contractorId),
           let existingNumber = profile["twilio_number"] as? String,
           !existingNumber.isEmpty {
            // Reuse existing number
            kevinNumber = existingNumber
            appState.kevinNumber = kevinNumber
        } else if !appState.kevinNumber.isEmpty {
            kevinNumber = appState.kevinNumber
        } else {
            // Provision new Twilio number
            let provResult = await APIClient.shared.provisionNumber(contractorId: contractorId)
            if provResult?["status"] as? String == "ok",
               let phoneNumber = provResult?["phone_number"] as? String,
               !phoneNumber.isEmpty {
                kevinNumber = phoneNumber
                appState.kevinNumber = kevinNumber
            } else {
                let message = provResult?["message"] as? String
                errorMessage = message ?? String(localized: "Failed to provision number. Please try again.")
                isLoading = false
                return
            }
        }

        // Sync contacts only if the user gave explicit upload consent
        if appState.contactsUploadConsent {
            let syncResult = await ContactSyncManager.shared.syncContacts(contractorId: contractorId)
            if case .success(let synced, _) = syncResult {
                contactsSynced = synced
            }
        }

        // Clear identity token after successful provisioning
        appState.appleIdentityToken = ""

        // Load subscription_uuid from backend profile
        if let profile = await APIClient.shared.getContractorProfile(contractorId: contractorId) {
            let subUUID = profile["subscription_uuid"] as? String ?? ""
            await MainActor.run {
                if !subUUID.isEmpty { appState.subscriptionUUID = subUUID }
            }
        }

        step = .forwarding
        isLoading = false
    }

    @MainActor
    private func prepareBusinessDraftProfile() async -> Bool {
        isLoading = true
        errorMessage = ""
        defer { isLoading = false }

        if appState.contractorId.isEmpty {
            let result = await APIClient.shared.createContractor(
                ownerName: ownerName,
                businessName: businessName,
                serviceType: serviceType,
                mode: "personal",
                ownerPhone: phoneNumber,
                appleUserId: appState.appleUserId,
                appleIdentityToken: appState.appleIdentityToken
            )
            guard let contractorId = result?["contractor_id"] as? String, !contractorId.isEmpty else {
                errorMessage = String(localized: "Failed to prepare your business profile. Please try again.")
                return false
            }
            appState.contractorId = contractorId
            if let apiToken = result?["api_token"] as? String, !apiToken.isEmpty {
                APIClient.shared.contractorToken = apiToken
            }
            do {
                let updated = try await APIClient.shared.patchContractor(contractorId, body: [
                    "owner_name": ownerName,
                    "business_name": businessName,
                    "service_type": serviceType,
                ])
                if !updated {
                    errorMessage = String(localized: "Failed to save your business profile. Please try again.")
                    return false
                }
            } catch {
                errorMessage = String(localized: "Failed to save your business profile. Please try again.")
                return false
            }
        } else {
            do {
                let updated = try await APIClient.shared.patchContractor(appState.contractorId, body: [
                    "owner_name": ownerName,
                    "business_name": businessName,
                    "service_type": serviceType,
                ])
                if !updated {
                    errorMessage = String(localized: "Failed to save your business profile. Please try again.")
                    return false
                }
            } catch {
                errorMessage = String(localized: "Failed to save your business profile. Please try again.")
                return false
            }
        }

        appState.userName = ownerName
        appState.businessName = businessName
        appState.serviceType = serviceType

        if let profile = await APIClient.shared.getContractorProfile(contractorId: appState.contractorId) {
            let subUUID = profile["subscription_uuid"] as? String ?? ""
            if !subUUID.isEmpty {
                appState.subscriptionUUID = subUUID
            }
        }

        return true
    }

    @MainActor
    private func activateBusinessAfterPurchase() async {
        guard appState.hasBusinessEntitlement else {
            errorMessage = String(localized: "Business purchase is still being verified. Tap Restore Purchases or try again.")
            return
        }
        guard !appState.contractorId.isEmpty else {
            errorMessage = String(localized: "Set up your Kevin account before activating Business mode.")
            return
        }

        do {
            let updated = try await APIClient.shared.patchContractor(appState.contractorId, body: [
                "owner_name": ownerName,
                "business_name": businessName,
                "service_type": serviceType,
                "mode": "business",
            ])
            if updated {
                selectedMode = "business"
                appState.mode = "business"
                appState.userName = ownerName
                appState.businessName = businessName
                appState.serviceType = serviceType
                showPaywall = false
                step = .contactsPermission
            } else {
                errorMessage = String(localized: "Business mode requires an active Business subscription. Restore purchases or choose Personal.")
            }
        } catch {
            errorMessage = String(localized: "Failed to activate Business mode. Please try again.")
        }
    }

    private func completeOnboarding() {
        if appState.subscriptionStatus == "active" {
            appState.isOnboarded = true
        } else {
            // Note: isOnboarded is set after paywall is dismissed (in doneStep sheet onDismiss)
            step = .done
        }
    }
}
