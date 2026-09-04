import SwiftUI
import AuthenticationServices

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
    @State private var forwardingInstructions: ForwardingInstructions?
    private var forwardingCountry: String { ForwardingCountry.resolve(accountCountry: appState.countryCode) }
    @State private var showPaywall = false
    // Business street address and city: captured here only when a
    // provisioning attempt fails for an address reason (the server has
    // already resolved the account country by then; the client never
    // guesses it). Seeded from AppState so a value the user already saved
    // in Settings — or entered on a previous provisioning attempt — is
    // never asked for twice.
    @State private var regulatoryAddress = AppState.shared.businessAddress
    @State private var regulatoryCity = AppState.shared.businessCity
    @State private var regulatoryAddressError = ""

    private let businessProductID = "com.kevin.callscreen.business.monthly"

    enum OnboardingStep {
        case welcome, signIn, phoneEntry, modeSelect, businessInfo, contactsPermission, personalInfo, provisioning, forwarding, done
    }

    let serviceTypes = ["plumbing", "electrical", "hvac", "general"]

    var body: some View {
        NavigationStack {
            ZStack {
                Color(.systemGroupedBackground)
                    .ignoresSafeArea()
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
        VStack(spacing: 0) {
            Spacer()

            KevinMark(size: 96)
                .padding(.bottom, HKSpace.xxl)

            VStack(spacing: HKSpace.md) {
                (Text(String(localized: "Hey, I'm "))
                    + Text("Kevin").foregroundColor(.hkBlue)
                    + Text("."))
                    .font(.system(size: 34, weight: .bold))
                    .tracking(-0.7)
                    .multilineTextAlignment(.center)
                    .lineSpacing(2)

                Text(String(localized: "I answer your calls, screen them in real time, and text you who's calling and why."))
                    .font(.system(size: 16))
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .lineSpacing(3)
                    .padding(.horizontal, HKSpace.md)
            }

            Spacer()

            VStack(alignment: .leading, spacing: HKSpace.md) {
                welcomeFeature(
                    icon: "waveform",
                    title: String(localized: "Live transcripts as the call happens"),
                    sub: String(localized: "Read who's calling and why before you decide to pick up.")
                )
                welcomeFeature(
                    icon: "shield.lefthalf.filled",
                    title: String(localized: "Robocalls handled silently"),
                    sub: String(localized: "Kevin filters spam so your phone only rings for real leads.")
                )
                welcomeFeature(
                    icon: "person.crop.circle.badge.checkmark",
                    title: String(localized: "Contacts ring through"),
                    sub: String(localized: "People you know skip Kevin and reach you directly.")
                )
            }
            .padding(.horizontal, HKSpace.sm)

            Spacer()

            VStack(spacing: HKSpace.sm) {
                Button {
                    step = .signIn
                } label: {
                    Text(String(localized: "Get started"))
                }
                .buttonStyle(HKDarkPrimaryButtonStyle())

                Button {
                    step = .signIn
                } label: {
                    Text(String(localized: "Already a member? Sign in"))
                        .font(.system(size: 14))
                        .foregroundStyle(.secondary)
                        .padding(.vertical, 8)
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.bottom, HKSpace.sm)
    }

    @ViewBuilder
    private func welcomeFeature(icon: String, title: String, sub: String) -> some View {
        HStack(alignment: .top, spacing: HKSpace.md) {
            ZStack {
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .fill(Color.hkBlue.opacity(0.10))
                    .frame(width: 32, height: 32)
                Image(systemName: icon)
                    .font(.system(size: 15, weight: .medium))
                    .foregroundStyle(.hkBlue)
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.system(size: 14, weight: .medium))
                    .foregroundStyle(.primary)
                Text(sub)
                    .font(.system(size: 13))
                    .foregroundStyle(.secondary)
                    .lineSpacing(1)
            }
            Spacer(minLength: 0)
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

            // Carrier-required SMS consent disclosure. US A2P 10DLC review needs a
            // verifiable opt-in: the recipient must be shown who is texting them,
            // what for, how often, that rates apply, and how to stop — at the point
            // they hand over the number. Its absence is why the first campaign
            // registration was rejected (error 30909, unverifiable Call to Action).
            // Wording here must stay in sync with the registered campaign's
            // message flow and with heykevin.one/terms.
            VStack(spacing: 8) {
                Text(String(localized: "By continuing you agree to receive service text messages from Hey Kevin at this number — call summaries, voicemail alerts, and account notices. Message frequency varies with your call volume. Message and data rates may apply. Reply STOP to cancel, HELP for help."))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)

                HStack(spacing: 12) {
                    Link(String(localized: "Terms"), destination: URL(string: "https://heykevin.one/terms")!)
                        .font(.caption)
                    Link(String(localized: "Privacy Policy"), destination: URL(string: "https://heykevin.one/privacy")!)
                        .font(.caption)
                }
            }
            .padding(.horizontal)

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
        VStack(spacing: 0) {
            HKProgressBar(current: 2, total: 5)
                .padding(.bottom, HKSpace.xl)

            VStack(spacing: HKSpace.lg) {
                KevinMark(size: 64)

                VStack(spacing: 6) {
                    Text(String(localized: "How do you want\nKevin to work?"))
                        .font(.system(size: 28, weight: .bold))
                        .tracking(-0.6)
                        .multilineTextAlignment(.center)
                        .lineSpacing(2)

                    Text(String(localized: "Pick the role you need. You can change this later, or run both on separate numbers."))
                        .font(.system(size: 14))
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                        .lineSpacing(2)
                        .padding(.horizontal, HKSpace.sm)
                }
            }
            .padding(.bottom, HKSpace.lg)

            VStack(spacing: HKSpace.md) {
                modeCard(
                    mode: "personal",
                    title: String(localized: "Personal"),
                    price: "$9.99",
                    desc: String(localized: "Screen unknown callers. Saved contacts ring through to you."),
                    tags: [
                        String(localized: "Block robocalls"),
                        String(localized: "Live transcripts"),
                        String(localized: "Text replies"),
                    ]
                )

                modeCard(
                    mode: "business",
                    title: String(localized: "Business"),
                    price: "$49.99",
                    desc: String(localized: "A full receptionist. Smart intake, business hours, and a knowledge base for FAQs."),
                    tags: [
                        String(localized: "Smart intake"),
                        String(localized: "Business hours"),
                        String(localized: "After-hours mode"),
                        String(localized: "Knowledge base"),
                    ]
                )
            }

            Spacer()

            VStack(spacing: HKSpace.xs) {
                Button {
                    if selectedMode == "personal" {
                        step = .personalInfo
                    } else {
                        step = .businessInfo
                    }
                } label: {
                    Text(selectedMode == "personal"
                         ? String(localized: "Continue with Personal")
                         : String(localized: "Continue with Business"))
                }
                .buttonStyle(HKDarkPrimaryButtonStyle())

                Text(String(localized: "14-day free trial. Cancel anytime."))
                    .font(.system(size: 12))
                    .foregroundStyle(.tertiary)
                    .padding(.top, 4)
            }
        }
    }

    @ViewBuilder
    private func modeCard(mode: String, title: String, price: String, desc: String, tags: [String]) -> some View {
        let selected = selectedMode == mode
        Button {
            selectedMode = mode
        } label: {
            VStack(alignment: .leading, spacing: HKSpace.sm) {
                HStack(alignment: .firstTextBaseline) {
                    Text(title)
                        .font(.system(size: 18, weight: .bold))
                        .tracking(-0.3)
                        .foregroundStyle(.primary)
                    Spacer()
                    HStack(alignment: .firstTextBaseline, spacing: 0) {
                        Text(price)
                            .font(.system(size: 15, weight: .semibold))
                            .foregroundStyle(.primary)
                        Text(String(localized: "/mo"))
                            .font(.system(size: 13, weight: .medium))
                            .foregroundStyle(.secondary)
                    }
                }

                Text(desc)
                    .font(.system(size: 13))
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.leading)
                    .lineSpacing(2)
                    .fixedSize(horizontal: false, vertical: true)

                FlowLayout(spacing: 6, lineSpacing: 6) {
                    ForEach(tags, id: \.self) { tag in
                        Text(tag)
                            .font(.system(size: 11, weight: .medium))
                            .foregroundStyle(selected ? Color.hkBlue : Color.secondary)
                            .padding(.horizontal, HKSpace.sm)
                            .padding(.vertical, 4)
                            .background(
                                Capsule()
                                    .fill(selected ? Color.hkBlue.opacity(0.10) : Color(.systemGray6))
                            )
                    }
                }
                .padding(.top, 2)
            }
            .padding(HKSpace.lg)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .fill(selected ? Color.hkBlue.opacity(0.05) : Color(.secondarySystemGroupedBackground))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .stroke(selected ? Color.hkBlue : Color(.systemGray5), lineWidth: selected ? 1.5 : 1)
            )
            .overlay(alignment: .topTrailing) {
                Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                    .font(.system(size: 20))
                    .foregroundStyle(selected ? Color.hkBlue : Color(.systemGray3))
                    .padding(HKSpace.md)
            }
        }
        .buttonStyle(.plain)
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

                if RegulatoryAddress.needsAddressCapture(errorMessage: errorMessage) {
                    VStack(spacing: 12) {
                        TextField(String(localized: "Business Address"), text: $regulatoryAddress)
                            .textFieldStyle(.roundedBorder)
                            .textContentType(.fullStreetAddress)

                        TextField(String(localized: "City"), text: $regulatoryCity)
                            .textFieldStyle(.roundedBorder)
                            .textContentType(.addressCity)

                        if !regulatoryAddressError.isEmpty {
                            Text(regulatoryAddressError)
                                .foregroundStyle(.red)
                                .font(.caption)
                        }
                    }
                    .padding(.vertical, 4)
                }

                Button(String(localized: "Try Again")) {
                    if RegulatoryAddress.needsAddressCapture(errorMessage: errorMessage) {
                        Task { await retryProvisioningWithAddress() }
                    } else {
                        errorMessage = ""
                        Task { await provision(mode: selectedMode) }
                    }
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

    private var forwardingCodes: ForwardingCodes {
        ForwardingDialCodes.codes(
            countryCode: forwardingCountry,
            instructions: forwardingInstructions,
            number: kevinNumber,
            isVerizon: isVerizon
        )
    }

    private var forwardingCountryName: String {
        Locale.current.localizedString(forRegionCode: forwardingCountry) ?? forwardingCountry
    }

    // MARK: - Forwarding Setup

    private var forwardingStep: some View {
        VStack(spacing: 20) {
            Text(String(localized: "Set Up Call Forwarding"))
                .font(.title.bold())

            Text(String(localized: "Forward your missed calls to Kevin.\nYour phone rings first — Kevin catches what you miss."))
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)

            // Carrier choice comes FIRST: the dial buttons below derive their
            // codes from it. When this sat below the buttons, a Verizon user
            // following the screen top-to-bottom dialed GSM codes their network
            // silently ignores before ever reaching the picker (review finding
            // on PR #143). Outside North America the picker has no meaning —
            // the codes come from the server for the device's country, or the
            // standard GSM codes when the server is unreachable.
            Group {
                if !ForwardingCountry.isNANP(forwardingCountry) {
                    Text(String(localized: "Using the call forwarding codes for \(forwardingCountryName)"))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    VStack(alignment: .leading, spacing: 6) {
                        Text(String(localized: "Your carrier"))
                            .font(.subheadline.weight(.medium))
                        Picker(String(localized: "Your carrier"), selection: $isVerizon) {
                            Text(String(localized: "AT&T, T-Mobile, other")).tag(false)
                            Text(String(localized: "Verizon")).tag(true)
                        }
                        .pickerStyle(.segmented)
                        .onChange(of: isVerizon) { _, newValue in
                            appState.isVerizonCarrier = newValue
                        }
                        Text(String(localized: "Verizon uses different codes — pick first, then dial."))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .task(id: forwardingCountry) {
                guard !ForwardingCountry.isNANP(forwardingCountry) else { return }
                // Keep good instructions if a re-run is cancelled or fails;
                // overwriting with nil would silently revert the dialed codes.
                if let fetched = await APIClient.shared.getForwardingInstructions(countryCode: forwardingCountry) {
                    forwardingInstructions = fetched
                }
            }

            VStack(spacing: 12) {
                // Step 1: Clear existing forwarding
                Button {
                    if let url = ForwardingDialCodes.telURL(forwardingCodes.clearExisting) {
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
                    if let url = ForwardingDialCodes.telURL(forwardingCodes.activate) {
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

                // There is deliberately no "test" button here. The previous
                // version dialed the Kevin number directly, which reaches Kevin
                // whether or not forwarding is configured — so it confirmed
                // success for users who had set nothing up. A real test requires
                // someone calling the user's own number and the call diverting,
                // which the device cannot stage for itself.
            }

            // iOS Live Voicemail answers calls on-device before the carrier's
            // no-answer timer fires, so the forward never triggers. Apple's own
            // guidance is to turn it off when carrier forwarding misbehaves.
            // There is no URL scheme that deep-links here — Settings can only be
            // opened to our own app's pane — so these are plain instructions.
            VStack(alignment: .leading, spacing: 6) {
                Label(String(localized: "Turn off Live Voicemail first"), systemImage: "exclamationmark.triangle.fill")
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(.orange)
                Text(String(localized: "If Live Voicemail is on, your iPhone answers before Kevin can. Open Settings, find Phone, then Live Voicemail, and switch it off."))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding()
            .background(Color.orange.opacity(0.10))
            .clipShape(RoundedRectangle(cornerRadius: 12))

            Text(String(localized: "Your Kevin number: \(kevinNumber)"))
                .font(.subheadline.monospacedDigit())
                .foregroundStyle(.secondary)

            Spacer()

            Button {
                recordForwardingOutcome(didSetUp: true)
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
                recordForwardingOutcome(didSetUp: false)
                completeOnboarding()
            }
            .font(.subheadline)
            .foregroundStyle(.secondary)
        }
    }

    // MARK: - Done

    private var doneStep: some View {
        // Final onboarding step. canDismiss=false enforces the subscription
        // decision: the user must complete the StoreKit purchase (which grants
        // Apple's 2-week introductory free trial) or tap Restore Purchases.
        // No "Maybe later" or "Done" bypass — we don't want users skipping
        // payment entry and silently churning when the server-side trial
        // expires 14 days later.
        PaywallView(canDismiss: false, isOnboarding: true)
            .environmentObject(appState)
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

        // 2. Look up by Apple User ID on backend. Retries with a refreshed
        //    Apple identity token if the first call returns 401 (token expired).
        if !appState.appleUserId.isEmpty {
            let lookup = await callWithFreshAppleTokenOnAuthFailure { [appState] in
                try await APIClient.shared.findContractorByAppleId(
                    appleUserId: appState.appleUserId,
                    appleIdentityToken: appState.appleIdentityToken
                )
            }
            switch lookup {
            case .success(let result):
                if let result = result, let contractorId = result["contractor_id"] as? String {
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
            case .authFailed:
                await handleBootstrapAuthFailure()
                return
            }
        }

        // 3. No account found — new user, collect their phone number before
        // choosing a mode so account creation can bind owner_phone/country.
        await MainActor.run { step = .phoneEntry }
    }

    private func restoreFromProfile(_ profile: [String: Any]) async {
        let name = profile["owner_name"] as? String ?? ""
        let biz = profile["business_name"] as? String ?? ""
        let mode = profile["effective_mode"] as? String ?? profile["mode"] as? String ?? "personal"
        let number = profile["twilio_number"] as? String ?? ""
        let subUUID = profile["subscription_uuid"] as? String ?? ""
        // Audit F-2: previously this method propagated everything except
        // subscription state, so a returning user whose server-side status
        // was "expired" reinstalled the app, signed in, and landed in the
        // main UI with the local default "trial". The expired-paywall in
        // ContentView never fired because iOS still believed the trial was
        // active. Read both server-authoritative fields here and apply them
        // before flipping isOnboarded.
        let subStatus = profile["subscription_status"] as? String ?? ""
        let subTier = profile["subscription_tier"] as? String ?? ""

        await MainActor.run {
            if !name.isEmpty { appState.userName = name }
            if !biz.isEmpty { appState.businessName = biz }
            appState.mode = (mode == "personal") ? "personal" : "business"
            if !subUUID.isEmpty { appState.subscriptionUUID = subUUID }
            if !subStatus.isEmpty { appState.subscriptionStatus = subStatus }
            if !subTier.isEmpty { appState.subscriptionTier = subTier }
            // The forwarding step keys its dial codes on the account country.
            if let country = SettingsCountry.accountCountry(from: profile) {
                appState.countryCode = country
            }
        }

        // If account has no Kevin number, provision one before completing restore
        if number.isEmpty {
            await MainActor.run { step = .provisioning }
            await provision(mode: (mode == "personal") ? "personal" : "business")
            return
        }

        await MainActor.run {
            appState.kevinNumber = number
            // Even an expired account completes "onboarding" here; the
            // ContentView gate (subscriptionStatus == "expired") then
            // immediately presents the forced paywall on the first frame.
            // This is the right shape: onboarding is "I have an account",
            // ContentView's gate is "I have a paid subscription".
            appState.isOnboarded = true
        }

        // Sync contacts in background only if user has previously consented to upload
        if appState.contactsUploadConsent {
            _ = await ContactSyncManager.shared.syncContacts(contractorId: appState.contractorId, force: true)
        }
    }

    private func restoreOrContinue() async {
        // Try to find existing contractor via phone number. Auto-refreshes the
        // Apple identity token and retries on 401 (token expired in flight).
        let outcome = await callWithFreshAppleTokenOnAuthFailure { [appState, phoneNumber, ownerName] in
            try await APIClient.shared.createContractor(
                ownerName: ownerName,
                businessName: "",
                serviceType: "general",
                ownerPhone: phoneNumber,
                appleUserId: appState.appleUserId,
                appleIdentityToken: appState.appleIdentityToken
            )
        }

        switch outcome {
        case .authFailed:
            await handleBootstrapAuthFailure()
            return
        case .success(let result):
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
    }

    private func provision(mode: String) async {
        // Resolve names: prefer the form-state values (when the user just typed them
        // during onboarding) but fall back to the values already on appState so we
        // don't overwrite a restored profile with empty strings, which the backend
        // rejects as "Failed to update profile."
        let resolvedOwnerName: String = {
            let local = ownerName.trimmingCharacters(in: .whitespaces)
            return local.isEmpty ? appState.userName : local
        }()
        let resolvedBusinessName: String = {
            let local = businessName.trimmingCharacters(in: .whitespaces)
            return local.isEmpty ? appState.businessName : local
        }()

        let isPersonal = mode == "personal"
        let bizName: String = {
            if isPersonal {
                return resolvedOwnerName.isEmpty
                    ? String(localized: "My Kevin number")
                    : "\(resolvedOwnerName)'s phone"
            }
            return resolvedBusinessName
        }()
        let svcType = isPersonal ? "personal" : serviceType

        isLoading = true
        errorMessage = ""

        // Reuse existing contractor if we have one, otherwise create new
        var contractorId = appState.contractorId

        // Fast-path: contractor already exists and the profile already has a Kevin
        // number. Persist the selected mode before finishing onboarding, but avoid
        // overwriting restored profile fields with potentially-stale form values.
        if !contractorId.isEmpty {
            if let profile = await APIClient.shared.getContractorProfile(contractorId: contractorId),
               let existing = profile["twilio_number"] as? String,
               !existing.isEmpty {
                do {
                    let updated = try await APIClient.shared.patchContractor(
                        contractorId,
                        body: ["mode": mode]
                    )
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
                kevinNumber = existing
                appState.kevinNumber = existing
                if !resolvedOwnerName.isEmpty { appState.userName = resolvedOwnerName }
                if !resolvedBusinessName.isEmpty { appState.businessName = resolvedBusinessName }
                appState.mode = mode
                if let subUUID = profile["subscription_uuid"] as? String, !subUUID.isEmpty {
                    appState.subscriptionUUID = subUUID
                }
                if let country = SettingsCountry.accountCountry(from: profile) {
                    appState.countryCode = country
                }
                step = .forwarding
                isLoading = false
                return
            }
        }

        if contractorId.isEmpty {
            // No existing contractor — create one (with Apple User ID for dedup).
            // Retries with a refreshed Apple identity token on 401.
            let createOutcome = await callWithFreshAppleTokenOnAuthFailure { [appState, phoneNumber] in
                try await APIClient.shared.createContractor(
                    ownerName: resolvedOwnerName,
                    businessName: bizName,
                    serviceType: svcType,
                    mode: mode,
                    ownerPhone: phoneNumber,
                    appleUserId: appState.appleUserId,
                    appleIdentityToken: appState.appleIdentityToken,
                    businessAddress: appState.businessAddress,
                    businessCity: appState.businessCity
                )
            }
            let result: [String: Any]?
            switch createOutcome {
            case .authFailed:
                isLoading = false
                await handleBootstrapAuthFailure()
                return
            case .success(let value):
                result = value
            }
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
                var updateBody: [String: Any] = ["mode": mode]
                if !resolvedOwnerName.isEmpty { updateBody["owner_name"] = resolvedOwnerName }
                if !bizName.isEmpty { updateBody["business_name"] = bizName }
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
                if !resolvedOwnerName.isEmpty { appState.userName = resolvedOwnerName }
                if !bizName.isEmpty { appState.businessName = bizName }
            }
        } else {
            // Existing contractor — update profile info. Only patch fields we have
            // values for so we don't overwrite restored profile data with empties.
            var updateBody: [String: Any] = ["mode": mode]
            if !resolvedOwnerName.isEmpty { updateBody["owner_name"] = resolvedOwnerName }
            if !bizName.isEmpty { updateBody["business_name"] = bizName }
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
            if !resolvedOwnerName.isEmpty { appState.userName = resolvedOwnerName }
            if !bizName.isEmpty { appState.businessName = bizName }
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
            if let country = SettingsCountry.accountCountry(from: profile) {
                appState.countryCode = country
            }
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
                // Provisioning is where the server finally resolves the account
                // country from the phone; adopt it before the forwarding step.
                if let country = SettingsCountry.accountCountry(from: provResult ?? [:]) {
                    appState.countryCode = country
                }
            } else {
                let message = provResult?["message"] as? String
                errorMessage = message ?? String(localized: "Failed to provision number. Please try again.")
                isLoading = false
                return
            }
        }

        // Sync contacts only if the user gave explicit upload consent
        if appState.contactsUploadConsent {
            let syncResult = await ContactSyncManager.shared.syncContacts(contractorId: contractorId, force: true)
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

        // Ask for push permission here, not at cold launch. The number now
        // exists and the user has seen what Kevin does, so the system alert
        // arrives with context. A denial is unrecoverable in-app and disables
        // the live-call screen and call summaries entirely.
        AppDelegate.requestPushAuthorization()
        step = .forwarding
        isLoading = false
    }

    /// Validates the address captured on the provisioning-failure screen,
    /// saves it, and only then retries provisioning. If the create call
    /// itself failed (`contractorId` empty), there is nothing to patch yet —
    /// skip straight to the existing retry path, which will send the address
    /// via `createContractor` this time.
    @MainActor
    private func retryProvisioningWithAddress() async {
        let result = RegulatoryAddress.validate(address: regulatoryAddress, city: regulatoryCity)
        guard result == .valid else {
            regulatoryAddressError = regulatoryAddressErrorMessage(for: result)
            return
        }
        regulatoryAddressError = ""
        appState.businessAddress = regulatoryAddress
        appState.businessCity = regulatoryCity

        let contractorId = appState.contractorId
        if !contractorId.isEmpty {
            let saved = await APIClient.shared.updateBusinessAddress(
                contractorId: contractorId,
                address: regulatoryAddress,
                city: regulatoryCity
            )
            guard saved else {
                regulatoryAddressError = String(localized: "Failed to save address. Please try again.")
                return
            }
        }
        errorMessage = ""
        await provision(mode: selectedMode)
    }

    @MainActor
    private func prepareBusinessDraftProfile() async -> Bool {
        isLoading = true
        errorMessage = ""
        defer { isLoading = false }

        if appState.contractorId.isEmpty {
            let outcome = await callWithFreshAppleTokenOnAuthFailure { [appState, phoneNumber, ownerName, businessName, serviceType] in
                try await APIClient.shared.createContractor(
                    ownerName: ownerName,
                    businessName: businessName,
                    serviceType: serviceType,
                    mode: "personal",
                    ownerPhone: phoneNumber,
                    appleUserId: appState.appleUserId,
                    appleIdentityToken: appState.appleIdentityToken
                )
            }
            let result: [String: Any]?
            switch outcome {
            case .authFailed:
                await handleBootstrapAuthFailure()
                return false
            case .success(let value):
                result = value
            }
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

    /// Record which exit the user took from the forwarding step.
    ///
    /// Previously "I'm All Set" and "Skip for now" were indistinguishable — both
    /// called `completeOnboarding()` and wrote nothing — so a user who skipped
    /// setup looked exactly like one who completed it, and nobody could be nudged.
    ///
    /// This records *intent*, not truth: the device cannot verify that forwarding
    /// is live. Ground truth arrives server-side as `forwarding_last_seen_at`,
    /// derived from Twilio's `ForwardedFrom` on a genuinely forwarded call. The
    /// gap between the two is the activation funnel we could not previously see.
    ///
    /// Fire-and-forget: a failed PATCH must never block onboarding completion.
    private func recordForwardingOutcome(didSetUp: Bool) {
        let contractorId = appState.contractorId
        guard !contractorId.isEmpty else { return }
        let field = didSetUp ? "forwarding_self_reported_at" : "forwarding_skipped_at"
        let payload: [String: Any] = [
            field: Date().timeIntervalSince1970,
            "forwarding_carrier_family": isVerizon ? "verizon" : "gsm",
        ]
        Task {
            _ = try? await APIClient.shared.patchContractor(contractorId, body: payload)
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

    // MARK: - Apple Identity Token Refresh

    /// Outcome of an unauthenticated bootstrap call wrapped by
    /// ``callWithFreshAppleTokenOnAuthFailure``.
    private enum BootstrapCallOutcome<T> {
        /// The call succeeded (possibly after one or more refresh-and-retry
        /// attempts). The associated value mirrors the underlying API
        /// response, so `nil` is still possible for non-401 soft failures.
        case success(T?)
        /// Authentication failed even after refreshing the Apple identity
        /// token. The caller should surface a clear "sign in expired" error
        /// and route the user back to the sign-in step.
        case authFailed
    }

    /// Invoke an unauthenticated bootstrap call (lookup-by-apple-id, create
    /// contractor). On `BootstrapAuthError.unauthenticated` (HTTP 401), request
    /// a fresh Apple identity token via ``AppleIdentityRefresher`` and retry,
    /// up to a total of three attempts. Other errors surface as
    /// `.success(nil)` to preserve the original soft-fail behaviour.
    private func callWithFreshAppleTokenOnAuthFailure<T>(
        maxAttempts: Int = 3,
        _ body: @escaping () async throws -> T?
    ) async -> BootstrapCallOutcome<T> {
        var attempt = 0
        while attempt < maxAttempts {
            attempt += 1
            do {
                let value = try await body()
                return .success(value)
            } catch BootstrapAuthError.unauthenticated {
                // Token rejected by backend — try to refresh and loop.
                guard attempt < maxAttempts else { break }
                let refreshed = await refreshAppleIdentityToken()
                if !refreshed {
                    return .authFailed
                }
                // Loop and retry with the new token now stored on appState.
            } catch {
                // Non-auth failures fall through to the soft-fail contract.
                return .success(nil)
            }
        }
        return .authFailed
    }

    /// Drive a fresh Sign-in with Apple credential request and update
    /// `appState.appleIdentityToken` (and `appState.appleUserId` if it
    /// changed). Returns `true` on success.
    @MainActor
    private func refreshAppleIdentityToken() async -> Bool {
        do {
            let result = try await AppleIdentityRefresher.shared.refreshIdentityToken(
                existingUserId: appState.appleUserId
            )
            if !result.appleUserId.isEmpty, result.appleUserId != appState.appleUserId {
                appState.appleUserId = result.appleUserId
            }
            appState.appleIdentityToken = result.identityToken
            return true
        } catch {
            return false
        }
    }

    /// Surface a clear error to the user after we've exhausted Apple identity
    /// token refresh attempts, and route them back to the sign-in step so they
    /// can re-authenticate manually via `SignInWithAppleButton`.
    @MainActor
    private func handleBootstrapAuthFailure() async {
        errorMessage = String(localized: "Sign in expired. Please tap Sign in with Apple again to continue.")
        appState.appleIdentityToken = ""
        isLoading = false
        step = .signIn
    }
}
