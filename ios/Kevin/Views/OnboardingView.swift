import SwiftUI
import AuthenticationServices
import StoreKit
import PhotosUI
import UniformTypeIdentifiers

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
    @ObservedObject private var subscriptionManager = SubscriptionManager.shared
    var isRecoveryMode: Bool = false
    @State private var step: OnboardingStep
    @State private var businessName = ""
    @State private var ownerName = ""
    @State private var serviceType = "general"
    @State private var selectedMode = "business"
    @State private var isLoading = false
    @State private var signInAttemptRevision: Int?
    @State private var signupContinuation: SignupContinuation?
    @State private var kevinNumber = ""
    @State private var errorMessage = ""
    @State private var marketCountryCode: String?
    @State private var marketAdmission: SignupAdmissionStatus?
    @State private var marketCheckID = UUID()
    @State private var marketAdmissionDestination: MarketAdmissionDestination = .signIn
    @State private var countrySelectionReturnDestination: CountrySelectionReturnDestination = .welcome
    @State private var newSignupAdmissionCountryCode: String?
    @State private var servicesDraft = OnboardingServicesDraft()
    @State private var selectedTrade: OnboardingTradeCategory = .plumbing
    @State private var businessSubStep: Int = 0
    @State private var servicesSubStep: Int = 0
    @State private var hasSeededTradeServices = false
    @State private var draftServiceName = ""
    @State private var draftServiceMinPrice = ""
    @State private var draftServiceMaxPrice = ""
    @State private var showServicesFileImporter = false
    @State private var selectedPriceListPhoto: PhotosPickerItem?
    @State private var servicesUploadMessage = ""
    @State private var isExtractingPriceList = false
    @State private var didSkipServicesStep = false
    private enum OnboardingInputField: Hashable {
        case personalOwnerName
        case businessOwnerName
        case businessName
        case customService
        case additionalNotes
    }
    @FocusState private var focusedField: OnboardingInputField?
    var onDebugPreviewComplete: (() -> Void)? = nil

    #if DEBUG || STAGING
    init(
        isRecoveryMode: Bool = false,
        initialStep: OnboardingStep = .welcome,
        initialCountryCode: String = SignupCountry.suggestedCountry(),
        initialPhoneNumber: String = "",
        initialErrorMessage: String = "",
        initialMarketCountryCode: String? = nil,
        initialMarketAdmission: SignupAdmissionStatus? = nil,
        onDebugPreviewComplete: (() -> Void)? = nil
    ) {
        self.isRecoveryMode = isRecoveryMode
        self.onDebugPreviewComplete = onDebugPreviewComplete
        let resolvedInitialStep: OnboardingStep = {
            guard !isRecoveryMode else { return .signIn }
            guard initialStep == .welcome,
                  let envStep = ProcessInfo.processInfo.environment["KEVIN_DEBUG_ONBOARDING_STEP"] else {
                return initialStep
            }
            switch envStep {
            case "modeSelect": return .modeSelect
            case "businessInfo": return .businessInfo
            case "businessServices": return .businessServices
            default: return initialStep
            }
        }()
        _step = State(initialValue: resolvedInitialStep)
        _selectedCountryCode = State(initialValue: initialCountryCode)
        _phoneNumber = State(initialValue: initialPhoneNumber)
        _errorMessage = State(initialValue: initialErrorMessage)
        _marketCountryCode = State(initialValue: initialMarketCountryCode)
        _marketAdmission = State(initialValue: initialMarketAdmission)
        _ownerName = State(initialValue: AppState.shared.userName)
        _businessName = State(initialValue: AppState.shared.businessName)
    }
    #else
    init(isRecoveryMode: Bool = false) {
        self.isRecoveryMode = isRecoveryMode
        _step = State(initialValue: isRecoveryMode ? .signIn : .welcome)
    }
    #endif
    @State private var contactsSynced = 0
    @State private var acceptedTerms = false
    @State private var phoneNumber = ""
    @State private var selectedCountryCode: String = SignupCountry.suggestedCountry()
    @State private var isVerizon = AppState.shared.isVerizonCarrier
    @State private var forwardingInstructions: ForwardingInstructions?
    private var forwardingCountry: String {
        let assigned = !kevinNumber.isEmpty ? kevinNumber : appState.kevinNumber
        return ForwardingCountry.resolve(
            serviceBinding: appState.serviceBinding,
            accountCountry: appState.countryCode,
            assignedNumber: assigned,
            hasAssignedNumber: !assigned.isEmpty
        )
    }
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
        case welcome, countrySelection, marketAvailability, signIn, phoneEntry, modeSelect, businessInfo, businessServices, contactsPermission, personalInfo, provisioning, forwarding, done
    }

    private enum MarketAdmissionDestination: Equatable {
        case signIn
        case phoneEntry
    }

    /// Where Back returns from the country picker. This is deliberately
    /// separate from where an *allowed* market check proceeds: a person who
    /// entered through sign-in may proceed to phone entry after a check, but
    /// must not reach phone entry by simply backing out of that check.
    enum CountrySelectionReturnDestination {
        case welcome
        case signIn
        case phoneEntry
    }

    @MainActor
    static func countrySelectionBackStep(
        for destination: CountrySelectionReturnDestination
    ) -> OnboardingStep {
        switch destination {
        case .welcome:
            .welcome
        case .signIn:
            .signIn
        case .phoneEntry:
            .phoneEntry
        }
    }

    let serviceTypes = ["plumbing", "electrical", "hvac", "general"]

    var body: some View {
        NavigationStack {
            ZStack {
                Color.hkWarmCanvas
                    .ignoresSafeArea()
                VStack {
                    switch step {
                    case .welcome:
                        welcomeStep
                    case .countrySelection:
                        countrySelectionStep
                    case .marketAvailability:
                        marketAvailabilityStep
                    case .signIn:
                        signInStep
                    case .phoneEntry:
                        phoneEntryStep
                    case .modeSelect:
                        modeSelectStep
                    case .businessInfo:
                        businessInfoStep
                    case .businessServices:
                        businessServicesStep
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
        .preferredColorScheme(.light)
        .onDisappear {
            marketCheckID = UUID()
            if appState.sessionState != .ready, let revision = signInAttemptRevision,
               AccountRestoreCoordinator.shared.activeAttemptRevision == revision {
                AccountRestoreCoordinator.shared.cancelAttempt()
                signInAttemptRevision = nil
            }
        }
        .sheet(isPresented: $showPaywall) {
            PaywallView(
                purpose: .businessActivation,
                canDismiss: true,
                isOnboarding: false,
                preferredProductID: PaywallPolicy.businessProductID,
                onSubscribed: { authContext in
                    await activateBusinessAfterPurchase(authContext: authContext)
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
                    beginNewSignup()
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

    // MARK: - Mobile Number Country

    private var deviceRegionCountryCode: String? {
        SignupCountry.deviceRegionCode()
    }

    private var marketCountryName: String {
        guard let marketCountryCode else {
            return String(localized: "your mobile-number country")
        }
        return SignupCountry.localizedName(forCode: marketCountryCode)
    }

    private var countrySelectionStep: some View {
        ScrollView {
            VStack(spacing: HKSpace.lg) {
                Spacer(minLength: HKSpace.xl)

                KevinMark(size: 64)

                VStack(spacing: HKSpace.sm) {
                    Text(String(localized: "Where is your mobile number from?"))
                        .font(.system(size: 28, weight: .bold))
                        .multilineTextAlignment(.center)

                    Text(String(localized: "Choose the country that issued the mobile number you’ll use with Kevin. Your device region is only a suggestion."))
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                        .lineSpacing(2)
                }

                if let deviceRegionCountryCode {
                    VStack(alignment: .leading, spacing: HKSpace.sm) {
                        Text(String(localized: "Suggested from this device"))
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(.secondary)
                        countrySelectionButton(
                            countryCode: deviceRegionCountryCode,
                            badgeText: String(localized: "Suggested")
                        )
                    }
                }

                VStack(alignment: .leading, spacing: HKSpace.sm) {
                    Text(String(localized: "Choose a different country"))
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)

                    ForEach(SignupCountry.supported.filter { $0.code != deviceRegionCountryCode }) { country in
                        countrySelectionButton(countryCode: country.code)
                    }
                }

                Text(String(localized: "If your mobile number’s country is not shown, Kevin cannot set up that number yet."))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, HKSpace.md)

                VStack(spacing: HKSpace.sm) {
                    Button {
                        leaveCountrySelection()
                    } label: {
                        Text(String(localized: "Back"))
                            .font(.system(size: 14))
                            .foregroundStyle(.secondary)
                            .padding(.vertical, 8)
                    }
                    .buttonStyle(.plain)

                    if marketAdmissionDestination == .signIn {
                        Button {
                            enterSignIn()
                        } label: {
                            Text(String(localized: "Already a member? Sign in"))
                                .font(.system(size: 14))
                                .foregroundStyle(.secondary)
                                .padding(.vertical, 8)
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
            .padding(.bottom, HKSpace.md)
        }
    }

    private func countrySelectionButton(countryCode: String, badgeText: String? = nil) -> some View {
        let country = SignupCountry.forCode(countryCode)
        let countryName = country?.localizedName() ?? SignupCountry.localizedName(forCode: countryCode)

        return Button {
            checkMarketAvailability(for: countryCode)
        } label: {
            HStack(spacing: HKSpace.md) {
                if let country {
                    Text(country.flagEmoji)
                        .font(.title2)
                } else {
                    Image(systemName: "globe")
                        .font(.title3)
                        .foregroundStyle(.hkBlue)
                        .frame(width: 24)
                }

                VStack(alignment: .leading, spacing: 2) {
                    Text(countryName)
                        .font(.body.weight(.semibold))
                        .foregroundStyle(.primary)
                    if let country {
                        Text(country.dialingPrefix)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }

                Spacer(minLength: 0)

                if let badgeText {
                    Text(badgeText)
                        .font(.caption.weight(.medium))
                        .foregroundStyle(.secondary)
                }

                Image(systemName: "chevron.right")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(HKSpace.md)
            .background(Color(.secondarySystemGroupedBackground))
            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
        }
        .buttonStyle(.plain)
        .accessibilityHint(String(localized: "Checks whether Kevin is available for this mobile-number country."))
    }

    private var marketAvailabilityStep: some View {
        VStack(spacing: HKSpace.lg) {
            Spacer()

            KevinMark(size: 64)

            marketAvailabilityContent

            Spacer()
        }
    }

    @ViewBuilder
    private var marketAvailabilityContent: some View {
        if let marketAdmission {
            switch marketAdmission {
            case .allowed:
                ProgressView()
                    .accessibilityLabel(String(localized: "Continuing"))
            case .unavailable(let message):
                Image(systemName: "globe.badge.chevron.backward")
                    .font(.system(size: 34, weight: .medium))
                    .foregroundStyle(.hkBlue)

                VStack(spacing: HKSpace.sm) {
                    Text(marketCountryName)
                        .font(.title.bold())
                        .multilineTextAlignment(.center)
                    Text(message)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }

                Text(String(localized: "Availability follows the country that issued your mobile number. If you are traveling, choose that number’s country to check it."))
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .lineSpacing(2)
                    .padding(.horizontal, HKSpace.sm)

                availabilityActions(retryCountryCode: nil)
            case .checkFailed(let message):
                Image(systemName: "exclamationmark.triangle")
                    .font(.system(size: 34, weight: .medium))
                    .foregroundStyle(.orange)

                VStack(spacing: HKSpace.sm) {
                    Text(String(localized: "Couldn’t check availability"))
                        .font(.title.bold())
                        .multilineTextAlignment(.center)
                    Text(message)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }

                Text(String(localized: "No account has been created. Try again or choose a different mobile-number country."))
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .lineSpacing(2)
                    .padding(.horizontal, HKSpace.sm)

                availabilityActions(retryCountryCode: marketCountryCode)
            }
        } else {
            ProgressView()
                .controlSize(.large)
            Text(String(localized: "Checking availability…"))
                .font(.title3.weight(.semibold))
            Text(marketCountryName)
                .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private func availabilityActions(retryCountryCode: String?) -> some View {
        VStack(spacing: HKSpace.sm) {
            if let retryCountryCode {
                Button {
                    checkMarketAvailability(for: retryCountryCode)
                } label: {
                    Text(String(localized: "Try again"))
                }
                .buttonStyle(HKDarkPrimaryButtonStyle())
            }

            Button {
                showCountrySelection()
            } label: {
                Text(String(localized: "Choose a different country"))
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 10)
            }
            .buttonStyle(.bordered)

            if marketAdmissionDestination == .signIn {
                Button {
                    enterSignIn()
                } label: {
                    Text(String(localized: "Already a member? Sign in"))
                        .font(.system(size: 14))
                        .foregroundStyle(.secondary)
                        .padding(.vertical, 8)
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.top, HKSpace.sm)
    }

    // MARK: - Sign In with Apple

    private var signInStep: some View {
        VStack(spacing: 24) {
            Spacer()

            if isRecoveryMode {
                KevinMark(size: 64)
                    .padding(.bottom, 8)

                Text(String(localized: "Reconnect Your Account"))
                    .font(.title.bold())
                    .multilineTextAlignment(.center)

                Text(String(localized: "Sign in with Apple to reconnect your Kevin number and restore your account."))
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal)
            } else {
                Text(String(localized: "Sign In"))
                    .font(.title.bold())

                Text(String(localized: "Create your account to get started."))
                    .foregroundStyle(.secondary)
            }

            SignInWithAppleButton(isRecoveryMode ? .signIn : .signUp) { request in
                request.requestedScopes = [.fullName, .email]
            } onCompletion: { result in
                handleSignIn(result)
            }
            .signInWithAppleButtonStyle(.black)
            .frame(height: 50)
            .clipShape(RoundedRectangle(cornerRadius: 12))
            .disabled(isLoading || (!isRecoveryMode && !acceptedTerms))
            .opacity((isLoading || (!isRecoveryMode && !acceptedTerms)) ? 0.5 : 1.0)

            if !isRecoveryMode {
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
            }

            if isLoading {
                ProgressView()
                    .padding(.top, 8)
            }

            if !errorMessage.isEmpty {
                Text(errorMessage)
                    .foregroundStyle(.red)
                    .font(.caption)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal)
            }

            Spacer()
        }
    }

    // MARK: - Phone Entry

    private var phoneEntryStep: some View {
        VStack(spacing: 20) {
            Spacer()

            Text(String(localized: "Your Phone Number"))
                .font(.title.bold())

            Text(String(localized: "Kevin needs your number to identify your account across devices."))
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)

            HStack(spacing: 8) {
                Menu {
                    ForEach(SignupCountry.supported) { country in
                        Button {
                            guard selectedCountryCode != country.code else { return }
                            errorMessage = ""
                            marketAdmissionDestination = .phoneEntry
                            countrySelectionReturnDestination = .phoneEntry
                            checkMarketAvailability(for: country.code)
                        } label: {
                            Text("\(country.flagEmoji) \(country.localizedName()) (\(country.dialingPrefix))")
                        }
                    }
                } label: {
                    HStack(spacing: 4) {
                        let currentCountry = SignupCountry.forCode(selectedCountryCode) ?? SignupCountry.supported[0]
                        Text(currentCountry.flagEmoji)
                            .font(.title3)
                        Text(currentCountry.dialingPrefix)
                            .font(.body.weight(.medium))
                            .foregroundStyle(.primary)
                        Image(systemName: "chevron.down")
                            .font(.caption2.weight(.bold))
                            .foregroundStyle(.secondary)
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 8)
                    .background(Color(.secondarySystemGroupedBackground))
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                    .overlay(
                        RoundedRectangle(cornerRadius: 8)
                            .stroke(Color(.systemGray4), lineWidth: 1)
                    )
                }

                TextField(
                    SignupCountry.forCode(selectedCountryCode)?.placeholder ?? "(650) 555-1234",
                    text: $phoneNumber
                )
                .textFieldStyle(.roundedBorder)
                .textContentType(.telephoneNumber)
                .keyboardType(.phonePad)
                .font(.title3)
                .padding(.vertical, 4)
            }
            .padding(.horizontal)

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
            .disabled(!SignupPhone.validate(phoneNumber, countryCode: selectedCountryCode).isValid || isLoading)
            .clipShape(RoundedRectangle(cornerRadius: 14))

            if !errorMessage.isEmpty {
                Text(errorMessage)
                    .foregroundStyle(.red)
                    .font(.caption)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal)
            }
        }
    }

    // MARK: - Mode Selection

    private var personalProduct: Product? {
        subscriptionManager.products.first(where: { $0.id == "com.kevin.callscreen.personal.monthly" })
    }

    private var businessProduct: Product? {
        subscriptionManager.products.first(where: { $0.id == "com.kevin.callscreen.business.monthly" })
    }

    private var personalPriceDisplay: String {
        personalProduct?.displayPrice ?? (subscriptionManager.isLoading ? "…" : String(localized: "Unavailable"))
    }

    private var businessPriceDisplay: String {
        businessProduct?.displayPrice ?? (subscriptionManager.isLoading ? "…" : String(localized: "Unavailable"))
    }

    private var modeSelectStep: some View {
        ZStack {
            himsAmbientCanvas(for: 1)
                .padding(-24)

            VStack(alignment: .leading, spacing: 18) {
                ScrollView(showsIndicators: false) {
                    VStack(alignment: .leading, spacing: 14) {
                        Text(String(localized: "STEP 01 / PROFILE SETUP"))
                            .font(.system(size: 11.5, weight: .semibold))
                            .tracking(0.7)
                            .foregroundStyle(Color(red: 0.54, green: 0.52, blue: 0.49))

                        Text(String(localized: "Do you intend to use Kevin as your personal assistant or as your business receptionist?"))
                            .font(.system(size: 26, weight: .bold))
                            .tracking(-0.6)
                            .foregroundStyle(Color(red: 0.098, green: 0.090, blue: 0.086))
                            .fixedSize(horizontal: false, vertical: true)

                        Text(String(localized: "Choose how Kevin should answer your line. Your setup and home dashboard will launch directly in the profile you pick."))
                            .font(.system(size: 14.5))
                            .foregroundStyle(Color(red: 0.34, green: 0.32, blue: 0.31))
                            .fixedSize(horizontal: false, vertical: true)

                        VStack(spacing: 12) {
                            modeCard(
                                mode: "business",
                                iconSystemName: "briefcase",
                                title: String(localized: "Business Receptionist"),
                                desc: String(localized: "Answers with your company name, asks trade-specific intake questions, shares ballpark pricing, and texts you structured job leads."),
                                tags: [
                                    String(localized: "Company greeting"),
                                    String(localized: "Services & ballpark quotes"),
                                    String(localized: "Lead capture SMS"),
                                ]
                            )

                            modeCard(
                                mode: "personal",
                                iconSystemName: "shield",
                                title: String(localized: "Personal Assistant"),
                                desc: String(localized: "Screens unknown callers in real time, blocks robocalls silently, and lets your saved contacts ring straight through to you."),
                                tags: [
                                    String(localized: "Contacts ring through"),
                                    String(localized: "Spam filtering"),
                                    String(localized: "Live call transcript"),
                                ]
                            )
                        }
                        .padding(.top, 4)
                    }
                    .padding(.bottom, 12)
                }

                VStack(spacing: 8) {
                    Button {
                        withAnimation(.spring(response: 0.32, dampingFraction: 0.86)) {
                            if selectedMode == "personal" {
                                step = .personalInfo
                            } else {
                                selectedMode = "business"
                                businessSubStep = 0
                                step = .businessInfo
                            }
                        }
                    } label: {
                        HStack(spacing: 8) {
                            Text(selectedMode == "personal"
                                 ? String(localized: "Continue as Personal Assistant")
                                 : String(localized: "Continue as Business Receptionist"))
                            Image(systemName: "arrow.right")
                                .font(.system(size: 13.5, weight: .semibold))
                        }
                        .font(.system(size: 15.5, weight: .semibold))
                        .foregroundStyle(.white)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 16)
                        .background(
                            Capsule()
                                .fill(Color(red: 0.098, green: 0.090, blue: 0.086))
                        )
                    }
                    .buttonStyle(.plain)

                    Text(String(localized: "You can switch profiles anytime in the Kevin tab."))
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(Color(red: 0.54, green: 0.52, blue: 0.49))
                        .frame(maxWidth: .infinity, alignment: .center)
                }
            }
        }
        .task {
            if subscriptionManager.products.isEmpty {
                await subscriptionManager.fetchProducts()
            }
        }
    }

    @ViewBuilder
    private func modeCard(
        mode: String,
        iconSystemName: String,
        title: String,
        desc: String,
        tags: [String]
    ) -> some View {
        let selected = selectedMode == mode
        let ink = Color(red: 0.098, green: 0.090, blue: 0.086)
        let sand = Color(red: 0.949, green: 0.925, blue: 0.894)
        let sandDeep = Color(red: 0.902, green: 0.875, blue: 0.835)
        Button {
            withAnimation(.spring(response: 0.28, dampingFraction: 0.82)) {
                selectedMode = mode
            }
        } label: {
            VStack(alignment: .leading, spacing: 10) {
                HStack(alignment: .center) {
                    Image(systemName: iconSystemName)
                        .font(.system(size: 17, weight: .semibold))
                        .foregroundStyle(selected ? .white : ink)
                        .frame(width: 40, height: 40)
                        .background(
                            RoundedRectangle(cornerRadius: 12, style: .continuous)
                                .fill(selected ? ink : sand)
                        )

                    Spacer()

                    ZStack {
                        Circle()
                            .strokeBorder(selected ? ink : Color.black.opacity(0.18), lineWidth: 1.5)
                            .background(Circle().fill(selected ? ink : Color.clear))
                            .frame(width: 22, height: 22)
                        if selected {
                            Image(systemName: "checkmark")
                                .font(.system(size: 10.5, weight: .bold))
                                .foregroundStyle(.white)
                        }
                    }
                }

                Text(title)
                    .font(.system(size: 17, weight: .semibold))
                    .tracking(-0.2)
                    .foregroundStyle(ink)

                Text(desc)
                    .font(.system(size: 13.5))
                    .foregroundStyle(Color(red: 0.34, green: 0.32, blue: 0.31))
                    .multilineTextAlignment(.leading)
                    .lineSpacing(2)
                    .fixedSize(horizontal: false, vertical: true)

                FlowLayout(spacing: 6, lineSpacing: 6) {
                    ForEach(tags, id: \.self) { tag in
                        Text(tag)
                            .font(.system(size: 11.5, weight: .medium))
                            .foregroundStyle(selected ? ink : Color(red: 0.34, green: 0.32, blue: 0.31))
                            .padding(.horizontal, 10)
                            .padding(.vertical, 4)
                            .background(
                                Capsule()
                                    .fill(selected ? sandDeep : sand)
                            )
                    }
                }
            }
            .padding(18)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: 18, style: .continuous)
                    .fill(Color.white)
                    .shadow(
                        color: Color.black.opacity(selected ? 0.06 : 0.02),
                        radius: selected ? 12 : 5,
                        x: 0,
                        y: selected ? 4 : 2
                    )
            )
            .overlay(
                RoundedRectangle(cornerRadius: 18, style: .continuous)
                    .stroke(selected ? ink : Color.black.opacity(0.09), lineWidth: selected ? 1.5 : 1)
            )
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
                            signupContinuation?.explicitContactConsent = true
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
                    signupContinuation?.explicitContactConsent = false
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
        ZStack {
            himsAmbientCanvas(for: 2)
                .padding(-24)

            VStack(alignment: .leading, spacing: 18) {
                HStack {
                    Button {
                        withAnimation(.spring(response: 0.32, dampingFraction: 0.86)) {
                            step = .modeSelect
                        }
                    } label: {
                        Image(systemName: "arrow.left")
                            .font(.system(size: 13.5, weight: .semibold))
                            .foregroundStyle(Color(red: 0.098, green: 0.090, blue: 0.086))
                            .frame(width: 34, height: 34)
                            .background(Circle().fill(Color.white))
                            .overlay(Circle().stroke(Color.black.opacity(0.09), lineWidth: 1))
                    }
                    .buttonStyle(.plain)
                    Spacer()
                }

                VStack(alignment: .leading, spacing: 14) {
                    Text(String(localized: "STEP 02 / PERSONAL ASSISTANT"))
                        .font(.system(size: 11.5, weight: .semibold))
                        .tracking(0.7)
                        .foregroundStyle(Color(red: 0.54, green: 0.52, blue: 0.49))

                    Text(String(localized: "What name should Kevin use when greeting callers?"))
                        .font(.system(size: 26, weight: .bold))
                        .tracking(-0.6)
                        .foregroundStyle(Color(red: 0.098, green: 0.090, blue: 0.086))

                    Text(String(localized: "Kevin will introduce himself as your personal assistant and ask who's calling and why."))
                        .font(.system(size: 14.5))
                        .foregroundStyle(Color(red: 0.34, green: 0.32, blue: 0.31))

                    VStack(alignment: .leading, spacing: 5) {
                        Text(String(localized: "YOUR FIRST NAME"))
                            .font(.system(size: 11, weight: .semibold))
                            .tracking(0.5)
                            .foregroundStyle(Color(red: 0.54, green: 0.52, blue: 0.49))
                        TextField(String(localized: "e.g. Deli"), text: $ownerName)
                            .font(.system(size: 17, weight: .semibold))
                            .foregroundStyle(Color(red: 0.098, green: 0.090, blue: 0.086))
                            .textContentType(.name)
                            .focused($focusedField, equals: .personalOwnerName)
                    }
                    .padding(16)
                    .contentShape(Rectangle())
                    .onTapGesture {
                        focusedField = .personalOwnerName
                    }
                    .background(
                        RoundedRectangle(cornerRadius: 16, style: .continuous)
                            .fill(Color.white)
                            .overlay(
                                RoundedRectangle(cornerRadius: 16, style: .continuous)
                                    .stroke(
                                        focusedField == .personalOwnerName
                                            ? Color(red: 0.098, green: 0.090, blue: 0.086)
                                            : Color.black.opacity(0.09),
                                        lineWidth: 1.5
                                    )
                            )
                    )
                }

                Spacer()

                Button {
                    focusedField = nil
                    selectedMode = "personal"
                    appState.mode = "personal"
                    appState.userName = ownerName
                    if let onDebugPreviewComplete, !appState.contractorId.isEmpty {
                        Task {
                            _ = try? await APIClient.shared.patchContractor(
                                appState.contractorId,
                                body: [
                                    "owner_name": ownerName,
                                    "mode": "personal",
                                ]
                            )
                            appState.mode = "personal"
                            onDebugPreviewComplete()
                        }
                    } else {
                        step = .contactsPermission
                    }
                } label: {
                    HStack(spacing: 8) {
                        Text(String(localized: "Continue as Personal Assistant"))
                        Image(systemName: "arrow.right")
                            .font(.system(size: 13.5, weight: .semibold))
                    }
                    .font(.system(size: 15.5, weight: .semibold))
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 16)
                    .background(
                        Capsule()
                            .fill(Color(red: 0.098, green: 0.090, blue: 0.086))
                    )
                }
                .buttonStyle(.plain)
                .disabled(ownerName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .opacity(ownerName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? 0.45 : 1.0)
            }
        }
    }

    // MARK: - Hims-Style Guided Business Intake (5 Dynamic Micro-Steps)

    private var currentHimsStepIndex: Int {
        if step == .businessInfo {
            return businessSubStep == 0 ? 1 : 2
        } else {
            return min(5, 3 + servicesSubStep)
        }
    }

    @ViewBuilder
    private func himsAmbientCanvas(for _: Int) -> some View {
        Color(red: 0.980, green: 0.969, blue: 0.949)
            .ignoresSafeArea()
    }

    @ViewBuilder
    private func himsProgressHeader(
        stepIndex: Int,
        showSkip: Bool,
        onBack: @escaping () -> Void,
        onSkip: (() -> Void)? = nil
    ) -> some View {
        let ink = Color(red: 0.098, green: 0.090, blue: 0.086)
        HStack(spacing: 12) {
            Button(action: onBack) {
                Image(systemName: "arrow.left")
                    .font(.system(size: 13.5, weight: .semibold))
                    .foregroundStyle(ink)
                    .frame(width: 34, height: 34)
                    .background(Circle().fill(Color.white))
                    .overlay(Circle().stroke(Color.black.opacity(0.09), lineWidth: 1))
            }
            .buttonStyle(.plain)

            HStack(spacing: 5) {
                ForEach(1...5, id: \.self) { idx in
                    Capsule()
                        .fill(idx <= stepIndex
                              ? ink
                              : Color(red: 0.902, green: 0.875, blue: 0.835))
                        .frame(height: 3)
                }
            }

            if showSkip, let onSkip {
                Button(action: onSkip) {
                    Text(String(localized: "Skip"))
                        .font(.system(size: 13, weight: .medium))
                        .foregroundStyle(Color(red: 0.34, green: 0.32, blue: 0.31))
                        .padding(.horizontal, 6)
                        .padding(.vertical, 6)
                }
                .buttonStyle(.plain)
                .disabled(isLoading || isExtractingPriceList)
            } else {
                Color.clear.frame(width: 34, height: 34)
            }
        }
        .padding(.bottom, 4)
    }

    private var businessInfoStep: some View {
        let ink = Color(red: 0.098, green: 0.090, blue: 0.086)
        return ZStack {
            himsAmbientCanvas(for: currentHimsStepIndex)
                .padding(-24)

            VStack(alignment: .leading, spacing: 18) {
                himsProgressHeader(
                    stepIndex: currentHimsStepIndex,
                    showSkip: false,
                    onBack: {
                        focusedField = nil
                        withAnimation(.spring(response: 0.32, dampingFraction: 0.86)) {
                            if businessSubStep > 0 {
                                businessSubStep = 0
                            } else {
                                step = .modeSelect
                            }
                        }
                    }
                )

                if businessSubStep == 0 {
                    ScrollView(showsIndicators: false) {
                        VStack(alignment: .leading, spacing: 14) {
                            Text(String(localized: "STEP 02 / INDUSTRY"))
                                .font(.system(size: 11.5, weight: .semibold))
                                .tracking(0.7)
                                .foregroundStyle(Color(red: 0.54, green: 0.52, blue: 0.49))

                            Text(String(localized: "What kind of work do you do?"))
                                .font(.system(size: 26, weight: .bold))
                                .tracking(-0.6)
                                .foregroundStyle(ink)

                            Text(String(localized: "Kevin tailors his intake questions, urgency triage, and starter service list to your trade."))
                                .font(.system(size: 14.5))
                                .foregroundStyle(Color(red: 0.341, green: 0.325, blue: 0.306))
                                .lineSpacing(2)

                            LazyVGrid(
                                columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)],
                                spacing: 10
                            ) {
                                ForEach(OnboardingTradeCategory.allCases) { trade in
                                    himsTradeCard(trade)
                                }
                            }
                            .padding(.top, 4)
                        }
                    }

                    Button {
                        if !hasSeededTradeServices {
                            serviceType = selectedTrade.serviceTypeKey
                            servicesDraft.applyTradeTemplate(selectedTrade)
                            hasSeededTradeServices = true
                        }
                        withAnimation(.spring(response: 0.32, dampingFraction: 0.86)) {
                            businessSubStep = 1
                        }
                    } label: {
                        HStack(spacing: 8) {
                            Text(String(localized: "Continue"))
                            Image(systemName: "arrow.right")
                                .font(.system(size: 13.5, weight: .semibold))
                        }
                        .font(.system(size: 15.5, weight: .semibold))
                        .foregroundStyle(.white)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 16)
                        .background(Capsule().fill(ink))
                    }
                    .buttonStyle(.plain)
                } else {
                    ScrollView(showsIndicators: false) {
                        VStack(alignment: .leading, spacing: 14) {
                            Text(String(localized: "STEP 03 / VOICE SCRIPT"))
                                .font(.system(size: 11.5, weight: .semibold))
                                .tracking(0.7)
                                .foregroundStyle(Color(red: 0.54, green: 0.52, blue: 0.49))

                            Text(String(localized: "How should Kevin answer your business line?"))
                                .font(.system(size: 26, weight: .bold))
                                .tracking(-0.6)
                                .foregroundStyle(ink)

                            Text(String(localized: "Callers hear this greeting the moment Kevin picks up."))
                                .font(.system(size: 14.5))
                                .foregroundStyle(Color(red: 0.341, green: 0.325, blue: 0.306))

                            // Live Voice Greeting Card
                            VStack(alignment: .leading, spacing: 12) {
                                HStack {
                                    Text(String(localized: "LIVE GREETING PREVIEW"))
                                        .font(.system(size: 11, weight: .semibold))
                                        .tracking(0.6)
                                        .foregroundStyle(Color(red: 0.655, green: 0.953, blue: 0.816))
                                    Spacer()
                                    HStack(spacing: 3) {
                                        ForEach([8.0, 14.0, 10.0, 16.0, 9.0], id: \.self) { h in
                                            Capsule()
                                                .fill(Color(red: 0.655, green: 0.953, blue: 0.816))
                                                .frame(width: 2.5, height: h)
                                        }
                                    }
                                }
                                let previewOwner = ownerName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "Deli" : ownerName.trimmingCharacters(in: .whitespacesAndNewlines)
                                let previewBiz = businessName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "Matsuo Electric" : businessName.trimmingCharacters(in: .whitespacesAndNewlines)
                                Text("“Hi, this is Kevin, \(previewOwner)’s assistant at \(previewBiz). How can I help you today?”")
                                    .font(.system(size: 15.5, weight: .medium))
                                    .foregroundStyle(Color(red: 0.980, green: 0.969, blue: 0.949))
                                    .lineSpacing(3)
                            }
                            .padding(18)
                            .background(
                                RoundedRectangle(cornerRadius: 20, style: .continuous)
                                    .fill(ink)
                            )

                            // Conversational First Name Card
                            VStack(alignment: .leading, spacing: 5) {
                                Text(String(localized: "YOUR FIRST NAME"))
                                    .font(.system(size: 11, weight: .semibold))
                                    .tracking(0.5)
                                    .foregroundStyle(Color(red: 0.54, green: 0.52, blue: 0.49))
                                TextField(String(localized: "e.g. Deli"), text: $ownerName)
                                    .font(.system(size: 17, weight: .semibold))
                                    .foregroundStyle(ink)
                                    .textContentType(.name)
                                    .focused($focusedField, equals: .businessOwnerName)
                                    .submitLabel(.next)
                                    .onSubmit {
                                        focusedField = .businessName
                                    }
                            }
                            .padding(15)
                            .contentShape(Rectangle())
                            .onTapGesture {
                                focusedField = .businessOwnerName
                            }
                            .background(
                                RoundedRectangle(cornerRadius: 16, style: .continuous)
                                    .fill(Color.white)
                            )
                            .overlay(
                                RoundedRectangle(cornerRadius: 16, style: .continuous)
                                    .stroke(
                                        focusedField == .businessOwnerName ? ink : Color.black.opacity(0.09),
                                        lineWidth: 1.5
                                    )
                            )

                            // Conversational Business Name Card
                            VStack(alignment: .leading, spacing: 5) {
                                Text(String(localized: "YOUR BUSINESS NAME"))
                                    .font(.system(size: 11, weight: .semibold))
                                    .tracking(0.5)
                                    .foregroundStyle(Color(red: 0.54, green: 0.52, blue: 0.49))
                                TextField(String(localized: "e.g. Matsuo Electric"), text: $businessName)
                                    .font(.system(size: 17, weight: .semibold))
                                    .foregroundStyle(ink)
                                    .textContentType(.organizationName)
                                    .focused($focusedField, equals: .businessName)
                                    .submitLabel(.done)
                                    .onSubmit {
                                        focusedField = nil
                                    }
                            }
                            .padding(15)
                            .contentShape(Rectangle())
                            .onTapGesture {
                                focusedField = .businessName
                            }
                            .background(
                                RoundedRectangle(cornerRadius: 16, style: .continuous)
                                    .fill(Color.white)
                            )
                            .overlay(
                                RoundedRectangle(cornerRadius: 16, style: .continuous)
                                    .stroke(
                                        focusedField == .businessName ? ink : Color.black.opacity(0.09),
                                        lineWidth: 1.5
                                    )
                            )
                        }
                    }

                    let canProceed = !ownerName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                        && !businessName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                    Button {
                        focusedField = nil
                        selectedMode = "business"
                        errorMessage = ""
                        if !hasSeededTradeServices {
                            serviceType = selectedTrade.serviceTypeKey
                            servicesDraft.applyTradeTemplate(selectedTrade)
                            hasSeededTradeServices = true
                        }
                        servicesSubStep = 0
                        withAnimation(.spring(response: 0.32, dampingFraction: 0.86)) {
                            step = .businessServices
                        }
                    } label: {
                        HStack(spacing: 8) {
                            Text(String(localized: "Continue"))
                            Image(systemName: "arrow.right")
                                .font(.system(size: 13.5, weight: .semibold))
                        }
                        .font(.system(size: 15.5, weight: .semibold))
                        .foregroundStyle(.white)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 16)
                        .background(
                            Capsule().fill(
                                canProceed ? ink : Color.black.opacity(0.22)
                            )
                        )
                    }
                    .buttonStyle(.plain)
                    .disabled(!canProceed)
                }
            }
        }
    }

    @ViewBuilder
    private func himsTradeCard(_ trade: OnboardingTradeCategory) -> some View {
        let isSelected = selectedTrade == trade
        let ink = Color(red: 0.098, green: 0.090, blue: 0.086)
        let sand = Color(red: 0.949, green: 0.925, blue: 0.894)
        Button {
            withAnimation(.spring(response: 0.28, dampingFraction: 0.8)) {
                selectedTrade = trade
                serviceType = trade.serviceTypeKey
                servicesDraft.applyTradeTemplate(trade)
                hasSeededTradeServices = true
            }
        } label: {
            VStack(alignment: .leading, spacing: 14) {
                HStack {
                    Image(systemName: trade.iconSystemName)
                        .font(.system(size: 16, weight: .semibold))
                        .foregroundStyle(isSelected ? .white : ink)
                        .frame(width: 36, height: 36)
                        .background(
                            RoundedRectangle(cornerRadius: 10, style: .continuous)
                                .fill(isSelected ? ink : sand)
                        )
                    Spacer()
                    ZStack {
                        Circle()
                            .strokeBorder(isSelected ? ink : Color.black.opacity(0.18), lineWidth: 1.5)
                            .background(Circle().fill(isSelected ? ink : Color.clear))
                            .frame(width: 22, height: 22)
                        if isSelected {
                            Image(systemName: "checkmark")
                                .font(.system(size: 10.5, weight: .bold))
                                .foregroundStyle(.white)
                        }
                    }
                }
                Spacer(minLength: 2)
                Text(trade.label)
                    .font(.system(size: 14.5, weight: .semibold))
                    .foregroundStyle(ink)
                    .multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(15)
            .frame(maxWidth: .infinity, minHeight: 108, alignment: .topLeading)
            .background(
                RoundedRectangle(cornerRadius: 18, style: .continuous)
                    .fill(Color.white)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 18, style: .continuous)
                    .stroke(isSelected ? ink : Color.black.opacity(0.09), lineWidth: isSelected ? 1.5 : 1)
            )
            .shadow(color: Color.black.opacity(isSelected ? 0.05 : 0.02), radius: isSelected ? 10 : 4, y: 3)
        }
        .buttonStyle(.plain)
    }

    // MARK: - Business Services & Price List (Hims-Style Guided Steps 3, 4, 5)

    private var businessServicesStep: some View {
        ZStack {
            himsAmbientCanvas(for: currentHimsStepIndex)
                .padding(-24)

            VStack(alignment: .leading, spacing: 14) {
                himsProgressHeader(
                    stepIndex: currentHimsStepIndex,
                    showSkip: servicesSubStep < 2,
                    onBack: {
                        focusedField = nil
                        errorMessage = ""
                        withAnimation(.spring(response: 0.32, dampingFraction: 0.86)) {
                            if servicesSubStep > 0 {
                                servicesSubStep -= 1
                            } else {
                                businessSubStep = 1
                                step = .businessInfo
                            }
                        }
                    },
                    onSkip: {
                        focusedField = nil
                        withAnimation(.spring(response: 0.32, dampingFraction: 0.86)) {
                            if servicesSubStep == 0 {
                                skipHimsServicesSelectionSubStep()
                            } else if servicesSubStep == 1 {
                                servicesDraft.selectedRuleIDs.removeAll()
                                servicesDraft.additionalNotes = ""
                                servicesSubStep = 2
                            } else {
                                proceedFromBusinessServices(skipServices: true)
                            }
                        }
                    }
                )

                if servicesSubStep == 0 {
                    himsServicesSelectionSubStep
                } else if servicesSubStep == 1 {
                    himsQuotingRulesSubStep
                } else {
                    himsReceptionistRevealSubStep
                }
            }
        }
        .onAppear {
            if !hasSeededTradeServices && servicesDraft.items.isEmpty {
                if serviceType.isEmpty {
                    serviceType = selectedTrade.serviceTypeKey
                }
                servicesDraft.applyTradeTemplate(selectedTrade)
                hasSeededTradeServices = true
            }
        }
        .fileImporter(
            isPresented: $showServicesFileImporter,
            allowedContentTypes: [.pdf, .commaSeparatedText, .plainText, .text, .image],
            allowsMultipleSelection: false
        ) { result in
            handleServicesFileImport(result)
        }
        .onChange(of: selectedPriceListPhoto) { _, newItem in
            guard let newItem else { return }
            Task { await handlePriceListPhotoSelection(newItem) }
        }
    }

    private func skipHimsServicesSelectionSubStep() {
        for idx in servicesDraft.items.indices {
            servicesDraft.items[idx].isSelected = false
        }
        servicesDraft.selectedRuleIDs.removeAll()
        draftServiceName = ""
        draftServiceMinPrice = ""
        draftServiceMaxPrice = ""
        servicesSubStep = 1
    }

    private var himsScannerBanner: some View {
        let ink = Color(red: 0.098, green: 0.090, blue: 0.086)
        let sand = Color(red: 0.949, green: 0.925, blue: 0.894)
        return VStack(alignment: .leading, spacing: 11) {
            VStack(alignment: .leading, spacing: 2) {
                Text(servicesDraft.uploadedFileName != nil
                     ? String(localized: "Imported \(servicesDraft.uploadedFileName!)")
                     : String(localized: "Have an existing price sheet?"))
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(ink)
                Text(isExtractingPriceList
                     ? String(localized: "Reading price sheet…")
                     : (!servicesUploadMessage.isEmpty
                        ? servicesUploadMessage
                        : String(localized: "Upload a PDF/CSV or photograph your menu to import automatically.")))
                    .font(.system(size: 12.5))
                    .foregroundStyle(Color(red: 0.341, green: 0.325, blue: 0.306))
            }

            HStack(spacing: 8) {
                Button {
                    showServicesFileImporter = true
                } label: {
                    Label(String(localized: "Upload File"), systemImage: "doc.text")
                        .font(.system(size: 12.5, weight: .semibold))
                        .foregroundStyle(ink)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 9)
                        .background(Capsule().fill(Color.white))
                        .overlay(Capsule().stroke(Color.black.opacity(0.14), lineWidth: 1))
                }
                .buttonStyle(.plain)
                .disabled(isExtractingPriceList || isLoading)

                PhotosPicker(selection: $selectedPriceListPhoto, matching: .images) {
                    Label(String(localized: "Scan Photo"), systemImage: "camera")
                        .font(.system(size: 12.5, weight: .semibold))
                        .foregroundStyle(ink)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 9)
                        .background(Capsule().fill(Color.white))
                        .overlay(Capsule().stroke(Color.black.opacity(0.14), lineWidth: 1))
                }
                .buttonStyle(.plain)
                .disabled(isExtractingPriceList || isLoading)
            }
        }
        .padding(15)
        .background(
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .fill(sand)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .stroke(Color.black.opacity(0.08), lineWidth: 1)
        )
    }

    @ViewBuilder
    private func himsServiceCard(_ entry: OnboardingServiceEntry) -> some View {
        let active = entry.isSelected
        let ink = Color(red: 0.098, green: 0.090, blue: 0.086)
        let sage = Color(red: 0.176, green: 0.353, blue: 0.263)
        VStack(alignment: .leading, spacing: 10) {
            Button {
                withAnimation(.spring(response: 0.25, dampingFraction: 0.82)) {
                    servicesDraft.toggleService(id: entry.id)
                }
            } label: {
                HStack(spacing: 10) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(entry.name)
                            .font(.system(size: 15, weight: .semibold))
                            .foregroundStyle(ink)
                        Text(active
                             ? (entry.formattedPrice.map { String(localized: "Included · Quotes \($0)") }
                                ?? String(localized: "Included · No price quoted on calls"))
                             : String(localized: "Tap to include"))
                            .font(.system(size: 12.5, weight: .medium))
                            .foregroundStyle(active ? sage : Color(red: 0.54, green: 0.52, blue: 0.49))
                    }
                    Spacer()
                    ZStack {
                        Circle()
                            .strokeBorder(active ? ink : Color.black.opacity(0.18), lineWidth: 1.5)
                            .background(Circle().fill(active ? ink : Color.clear))
                            .frame(width: 22, height: 22)
                        Image(systemName: active ? "checkmark" : "plus")
                            .font(.system(size: 10.5, weight: .bold))
                            .foregroundStyle(active ? .white : Color(red: 0.54, green: 0.52, blue: 0.49))
                    }
                }
            }
            .buttonStyle(.plain)

            if active {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 6) {
                        ForEach(entry.presets, id: \.self) { preset in
                            himsPricePresetChip(preset, for: entry)
                        }
                    }
                }
            }
        }
        .padding(15)
        .background(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .fill(Color.white)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .stroke(active ? ink : Color.black.opacity(0.09), lineWidth: active ? 1.5 : 1)
        )
    }

    @ViewBuilder
    private func himsPricePresetChip(_ preset: OnboardingPricePreset, for entry: OnboardingServiceEntry) -> some View {
        let isPresetSelected = entry.priceMin == preset.priceMin && entry.priceMax == preset.priceMax
        let ink = Color(red: 0.098, green: 0.090, blue: 0.086)
        let sand = Color(red: 0.949, green: 0.925, blue: 0.894)
        Button {
            servicesDraft.applyPricePreset(preset, to: entry.id)
        } label: {
            Text(preset.label)
                .font(.system(size: 12, weight: isPresetSelected ? .semibold : .medium))
                .foregroundStyle(isPresetSelected ? .white : Color(red: 0.341, green: 0.325, blue: 0.306))
                .padding(.horizontal, 11)
                .padding(.vertical, 6)
                .background(
                    Capsule().fill(isPresetSelected ? ink : sand)
                )
        }
        .buttonStyle(.plain)
    }

    private var himsServicesSelectionSubStep: some View {
        let ink = Color(red: 0.098, green: 0.090, blue: 0.086)
        return VStack(alignment: .leading, spacing: 12) {
            ScrollView(showsIndicators: false) {
                VStack(alignment: .leading, spacing: 12) {
                    Text(String(localized: "STEP 04 / OPTIONAL · \(selectedTrade.label.uppercased())"))
                        .font(.system(size: 11.5, weight: .semibold))
                        .tracking(0.7)
                        .foregroundStyle(Color(red: 0.54, green: 0.52, blue: 0.49))

                    Text(String(localized: "What services do you want Kevin to know?"))
                        .font(.system(size: 26, weight: .bold))
                        .tracking(-0.6)
                        .foregroundStyle(ink)

                    Text(String(localized: "Select the services you offer. Choose a ballpark range if you want Kevin to quote estimates, or leave it unpriced."))
                        .font(.system(size: 14.5))
                        .foregroundStyle(Color(red: 0.341, green: 0.325, blue: 0.306))
                        .lineSpacing(2)

                    himsScannerBanner

                    ForEach(servicesDraft.items) { entry in
                        himsServiceCard(entry)
                    }

                    // Quick Add Custom Service Pill Bar
                    HStack(spacing: 8) {
                        TextField(String(localized: "Add another service..."), text: $draftServiceName)
                            .font(.system(size: 13.5, weight: .medium))
                            .foregroundStyle(ink)
                            .focused($focusedField, equals: .customService)
                            .submitLabel(.done)
                            .onSubmit {
                                commitTypedDraftService()
                            }
                            .padding(.horizontal, 16)
                            .padding(.vertical, 11)
                            .background(
                                Capsule().fill(Color.white)
                            )
                            .overlay(
                                Capsule().stroke(
                                    focusedField == .customService ? ink : Color.black.opacity(0.09),
                                    lineWidth: 1.5
                                )
                            )

                        Button {
                            commitTypedDraftService()
                        } label: {
                            Text(String(localized: "Add"))
                                .font(.system(size: 13, weight: .semibold))
                                .foregroundStyle(.white)
                                .padding(.horizontal, 18)
                                .padding(.vertical, 11)
                                .background(Capsule().fill(ink))
                        }
                        .buttonStyle(.plain)
                        .disabled(draftServiceName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    }
                }
            }

            let activeCount = servicesDraft.activeItems.count
            VStack(spacing: 8) {
                Button {
                    focusedField = nil
                    if !draftServiceName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        commitTypedDraftService()
                    }
                    withAnimation(.spring(response: 0.32, dampingFraction: 0.86)) {
                        servicesSubStep = 1
                    }
                } label: {
                    HStack(spacing: 8) {
                        Text(activeCount > 0
                             ? String(localized: "Continue with \(activeCount) service\(activeCount == 1 ? "" : "s")")
                             : String(localized: "Continue"))
                        Image(systemName: "arrow.right")
                            .font(.system(size: 13.5, weight: .semibold))
                    }
                    .font(.system(size: 15.5, weight: .semibold))
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 16)
                    .background(Capsule().fill(ink))
                }
                .buttonStyle(.plain)

                Button {
                    focusedField = nil
                    withAnimation(.spring(response: 0.32, dampingFraction: 0.86)) {
                        skipHimsServicesSelectionSubStep()
                    }
                } label: {
                    Text(String(localized: "Skip for now — I'll add services later"))
                        .font(.system(size: 13, weight: .medium))
                        .foregroundStyle(Color(red: 0.54, green: 0.52, blue: 0.49))
                }
                .buttonStyle(.plain)
            }
        }
    }

    @ViewBuilder
    private func himsQuotingRuleCard(_ rule: OnboardingQuotingRule) -> some View {
        let isSelected = servicesDraft.selectedRuleIDs.contains(rule.id)
        let ink = Color(red: 0.098, green: 0.090, blue: 0.086)
        Button {
            withAnimation(.spring(response: 0.25, dampingFraction: 0.82)) {
                servicesDraft.toggleQuotingRule(id: rule.id)
            }
        } label: {
            HStack(alignment: .top, spacing: 12) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(rule.title)
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(ink)
                        .multilineTextAlignment(.leading)
                    Text(rule.subtitle)
                        .font(.system(size: 13))
                        .foregroundStyle(Color(red: 0.341, green: 0.325, blue: 0.306))
                        .multilineTextAlignment(.leading)
                        .lineSpacing(2)
                }
                Spacer()
                ZStack {
                    Circle()
                        .strokeBorder(isSelected ? ink : Color.black.opacity(0.18), lineWidth: 1.5)
                        .background(Circle().fill(isSelected ? ink : Color.clear))
                        .frame(width: 22, height: 22)
                    if isSelected {
                        Image(systemName: "checkmark")
                            .font(.system(size: 10.5, weight: .bold))
                            .foregroundStyle(.white)
                    }
                }
                .padding(.top, 2)
            }
            .padding(16)
            .background(
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .fill(Color.white)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .stroke(isSelected ? ink : Color.black.opacity(0.09), lineWidth: isSelected ? 1.5 : 1)
            )
        }
        .buttonStyle(.plain)
    }

    private var himsQuotingRulesSubStep: some View {
        let ink = Color(red: 0.098, green: 0.090, blue: 0.086)
        return VStack(alignment: .leading, spacing: 12) {
            ScrollView(showsIndicators: false) {
                VStack(alignment: .leading, spacing: 12) {
                    Text(String(localized: "STEP 05 / OPTIONAL · QUOTING POLICY"))
                        .font(.system(size: 11.5, weight: .semibold))
                        .tracking(0.7)
                        .foregroundStyle(Color(red: 0.54, green: 0.52, blue: 0.49))

                    Text(String(localized: "How should Kevin handle pricing questions?"))
                        .font(.system(size: 26, weight: .bold))
                        .tracking(-0.6)
                        .foregroundStyle(ink)

                    Text(String(localized: "Select the guidelines Kevin should follow when callers ask about cost."))
                        .font(.system(size: 14.5))
                        .foregroundStyle(Color(red: 0.341, green: 0.325, blue: 0.306))

                    ForEach(OnboardingQuotingRule.catalog) { rule in
                        himsQuotingRuleCard(rule)
                    }

                    VStack(alignment: .leading, spacing: 5) {
                        Text(String(localized: "ADDITIONAL NOTES FOR CALLERS (OPTIONAL)"))
                            .font(.system(size: 11, weight: .semibold))
                            .tracking(0.5)
                            .foregroundStyle(Color(red: 0.54, green: 0.52, blue: 0.49))
                        TextField(
                            String(localized: "e.g. Licensed & insured, senior discount…"),
                            text: $servicesDraft.additionalNotes
                        )
                        .font(.system(size: 14.5, weight: .medium))
                        .foregroundStyle(ink)
                        .focused($focusedField, equals: .additionalNotes)
                    }
                    .padding(15)
                    .contentShape(Rectangle())
                    .onTapGesture {
                        focusedField = .additionalNotes
                    }
                    .background(
                        RoundedRectangle(cornerRadius: 16, style: .continuous)
                            .fill(Color.white)
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: 16, style: .continuous)
                            .stroke(
                                focusedField == .additionalNotes ? ink : Color.black.opacity(0.09),
                                lineWidth: 1.5
                            )
                    )
                }
            }

            VStack(spacing: 8) {
                Button {
                    focusedField = nil
                    withAnimation(.spring(response: 0.32, dampingFraction: 0.86)) {
                        servicesSubStep = 2
                    }
                } label: {
                    HStack(spacing: 8) {
                        Text(String(localized: "Review Receptionist Setup"))
                        Image(systemName: "arrow.right")
                            .font(.system(size: 13.5, weight: .semibold))
                    }
                    .font(.system(size: 15.5, weight: .semibold))
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 16)
                    .background(Capsule().fill(ink))
                }
                .buttonStyle(.plain)

                Button {
                    focusedField = nil
                    servicesDraft.selectedRuleIDs.removeAll()
                    servicesDraft.additionalNotes = ""
                    withAnimation(.spring(response: 0.32, dampingFraction: 0.86)) {
                        servicesSubStep = 2
                    }
                } label: {
                    Text(String(localized: "Skip for now"))
                        .font(.system(size: 13, weight: .medium))
                        .foregroundStyle(Color(red: 0.54, green: 0.52, blue: 0.49))
                }
                .buttonStyle(.plain)
            }
        }
    }

    private var himsReceptionistRevealSubStep: some View {
        let ink = Color(red: 0.098, green: 0.090, blue: 0.086)
        let sage = Color(red: 0.176, green: 0.353, blue: 0.263)
        let sageTint = Color(red: 0.914, green: 0.941, blue: 0.925)
        let sand = Color(red: 0.949, green: 0.925, blue: 0.894)
        return VStack(alignment: .leading, spacing: 14) {
            ScrollView(showsIndicators: false) {
                VStack(alignment: .leading, spacing: 14) {
                    Text(String(localized: "READY TO LAUNCH"))
                        .font(.system(size: 11.5, weight: .semibold))
                        .tracking(0.7)
                        .foregroundStyle(Color(red: 0.54, green: 0.52, blue: 0.49))

                    Text(String(localized: "Your business receptionist is ready."))
                        .font(.system(size: 26, weight: .bold))
                        .tracking(-0.6)
                        .foregroundStyle(ink)

                    let displayBiz = businessName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "your business" : businessName
                    Text(String(localized: "Kevin will launch directly in Business Receptionist mode for \(displayBiz)."))
                        .font(.system(size: 14.5))
                        .foregroundStyle(Color(red: 0.341, green: 0.325, blue: 0.306))

                    VStack(alignment: .leading, spacing: 14) {
                        HStack(spacing: 12) {
                            Image(systemName: selectedTrade.iconSystemName)
                                .font(.system(size: 17, weight: .semibold))
                                .foregroundStyle(.white)
                                .frame(width: 40, height: 40)
                                .background(
                                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                                        .fill(ink)
                                )
                            VStack(alignment: .leading, spacing: 2) {
                                Text(displayBiz)
                                    .font(.system(size: 16.5, weight: .bold))
                                    .foregroundStyle(ink)
                                Text(String(localized: "Business Receptionist Profile · \(selectedTrade.label)"))
                                    .font(.system(size: 12.5, weight: .medium))
                                    .foregroundStyle(sage)
                            }
                            Spacer()
                        }

                        Divider()

                        let activeServices = servicesDraft.activeItems
                        Text(String(localized: "SERVICES & BALLPARK PRICING (\(activeServices.count))"))
                            .font(.system(size: 11, weight: .semibold))
                            .tracking(0.5)
                            .foregroundStyle(Color(red: 0.54, green: 0.52, blue: 0.49))

                        if activeServices.isEmpty {
                            Text(String(localized: "No services listed yet — Kevin will capture caller details and text you right away."))
                                .font(.system(size: 13))
                                .foregroundStyle(Color(red: 0.54, green: 0.52, blue: 0.49))
                        } else {
                            ForEach(activeServices) { svc in
                                HStack {
                                    Text(svc.name)
                                        .font(.system(size: 13.5, weight: .medium))
                                        .foregroundStyle(ink)
                                    Spacer()
                                    Text(svc.formattedPrice ?? String(localized: "No price"))
                                        .font(.system(size: 11.5, weight: .semibold))
                                        .foregroundStyle(svc.formattedPrice == nil
                                                         ? Color(red: 0.341, green: 0.325, blue: 0.306)
                                                         : sage)
                                        .padding(.horizontal, 9)
                                        .padding(.vertical, 4)
                                        .background(
                                            Capsule().fill(svc.formattedPrice == nil ? sand : sageTint)
                                        )
                                }
                            }
                        }

                        let activeRules = OnboardingQuotingRule.catalog.filter {
                            servicesDraft.selectedRuleIDs.contains($0.id)
                        }
                        if !activeRules.isEmpty {
                            Divider()
                            Text(String(localized: "QUOTING POLICY"))
                                .font(.system(size: 11, weight: .semibold))
                                .tracking(0.5)
                                .foregroundStyle(Color(red: 0.54, green: 0.52, blue: 0.49))
                            VStack(alignment: .leading, spacing: 6) {
                                ForEach(activeRules) { rule in
                                    Text(rule.title)
                                        .font(.system(size: 12, weight: .medium))
                                        .foregroundStyle(ink)
                                        .padding(.horizontal, 10)
                                        .padding(.vertical, 5)
                                        .background(
                                            Capsule().fill(sand)
                                        )
                                }
                            }
                        }
                    }
                    .padding(18)
                    .background(
                        RoundedRectangle(cornerRadius: 18, style: .continuous)
                            .fill(Color.white)
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: 18, style: .continuous)
                            .stroke(ink, lineWidth: 1.5)
                    )

                    if !errorMessage.isEmpty {
                        Text(errorMessage)
                            .foregroundStyle(.red)
                            .font(.caption)
                            .multilineTextAlignment(.center)
                    }
                }
            }

            Button {
                proceedFromBusinessServices(skipServices: !servicesDraft.hasContent)
            } label: {
                if isLoading {
                    ProgressView()
                        .tint(.white)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 16)
                        .background(Capsule().fill(ink))
                } else {
                    HStack(spacing: 8) {
                        Text(appState.hasBusinessEntitlement
                             ? String(localized: "Launch Business Receptionist")
                             : String(localized: "Start 14-Day Free Trial"))
                        Image(systemName: "arrow.right")
                            .font(.system(size: 13.5, weight: .semibold))
                    }
                    .font(.system(size: 15.5, weight: .semibold))
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 16)
                    .background(Capsule().fill(ink))
                }
            }
            .buttonStyle(.plain)
            .disabled(isLoading || isExtractingPriceList)
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

    private var forwardingCodes: ForwardingCodes? {
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
            // codes from it. The existing carrier picker is available only in the US.
            Group {
                if forwardingCountry != "US" {
                    Text(forwardingCountry == "CA" ? CanadaForwardingGuidance.title : String(localized: "Carrier setup in preparation"))
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
                if let fetched = await APIClient.shared.getForwardingInstructions(countryCode: forwardingCountry) {
                    forwardingInstructions = fetched
                }
            }

            if let codes = forwardingCodes {
                VStack(spacing: 12) {
                    // Step 1: Clear existing forwarding
                    Button {
                        if let url = ForwardingDialCodes.telURL(codes.clearExisting) {
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
                        if let url = ForwardingDialCodes.telURL(codes.activate) {
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
                }
            } else {
                VStack(alignment: .leading, spacing: 8) {
                    Label(forwardingCountry == "CA" ? CanadaForwardingGuidance.title : String(localized: "Call forwarding in preparation"), systemImage: "info.circle.fill")
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(.blue)
                    Text(forwardingCountry == "CA" ? CanadaForwardingGuidance.setup : String(localized: "Call forwarding for your region is currently being prepared. No carrier dialing codes are required at this time."))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    if forwardingCountry == "CA" {
                        Text(CanadaForwardingGuidance.baseline)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding()
                .background(Color.blue.opacity(0.10))
                .clipShape(RoundedRectangle(cornerRadius: 12))
            }

            // iOS Live Voicemail answers calls on-device before the carrier's
            // no-answer timer fires, so the forward never triggers. Apple's own
            // guidance is to turn it off when carrier forwarding misbehaves.
            // There is no URL scheme that deep-links here — Settings can only be
            // opened to our own app's pane — so these are plain instructions.
            if forwardingCountry != "CA" {
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
            }

            Text(String(localized: "Your Kevin number: \(kevinNumber)"))
                .font(.subheadline.monospacedDigit())
                .foregroundStyle(.secondary)

            Spacer()

            Button {
                recordForwardingOutcome(didSetUp: true)
                completeOnboarding()
            } label: {
                Text(forwardingCountry == "CA" ? String(localized: "Continue — set up with my carrier later") : String(localized: "I'm All Set"))
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
        if selectedMode == "business" {
            PaywallView(
                purpose: .businessActivation,
                canDismiss: false,
                isOnboarding: true,
                preferredProductID: PaywallPolicy.businessProductID,
                onSubscribed: { authContext in
                    await activateBusinessAfterPurchase(authContext: authContext) {
                        appState.isOnboarded = true
                    }
                }
            )
            .environmentObject(appState)
        } else {
            PaywallView(
                purpose: .allPlans,
                canDismiss: false,
                isOnboarding: true,
                preferredProductID: PaywallPolicy.personalProductID
            )
            .environmentObject(appState)
        }
    }

    // MARK: - Logic

    /// Starts new-account onboarding with an explicit number-country choice.
    /// The device region is displayed as a hint in that choice, never applied
    /// as a silent substitute for the user's mobile-number country.
    @MainActor
    private func beginNewSignup() {
        marketAdmissionDestination = .signIn
        countrySelectionReturnDestination = .welcome
        newSignupAdmissionCountryCode = nil
        showCountrySelection()
    }

    @MainActor
    private func showCountrySelection() {
        marketCheckID = UUID()
        marketAdmission = nil
        errorMessage = ""
        step = .countrySelection
    }

    @MainActor
    private func leaveCountrySelection() {
        marketCheckID = UUID()
        marketAdmission = nil
        errorMessage = ""
        if countrySelectionReturnDestination == .welcome {
            newSignupAdmissionCountryCode = nil
        }
        step = Self.countrySelectionBackStep(for: countrySelectionReturnDestination)
    }

    @MainActor
    private func enterSignIn() {
        marketCheckID = UUID()
        marketAdmission = nil
        errorMessage = ""
        newSignupAdmissionCountryCode = nil
        step = .signIn
    }

    /// Checks the selected number country before the user can enter a new
    /// account flow. The final admission checks before account creation remain
    /// in place as a defense against stale UI state or a later country change.
    @MainActor
    private func checkMarketAvailability(for untrustedCountryCode: String) {
        guard let countryCode = SignupCountry.normalizedCountryCode(untrustedCountryCode) else {
            showCountrySelection()
            return
        }

        let checkID = UUID()
        marketCheckID = checkID
        marketCountryCode = countryCode
        marketAdmission = nil
        errorMessage = ""
        if marketAdmissionDestination == .signIn {
            newSignupAdmissionCountryCode = nil
        }
        step = .marketAvailability

        Task { @MainActor in
            let admission = await SignupAdmission.checkAvailability(countryCode: countryCode)
            guard marketCheckID == checkID, marketCountryCode == countryCode else { return }

            switch admission {
            case .allowed:
                if marketAdmissionDestination == .phoneEntry,
                   selectedCountryCode != countryCode {
                    phoneNumber = ""
                }
                selectedCountryCode = countryCode
                if marketAdmissionDestination == .signIn {
                    newSignupAdmissionCountryCode = countryCode
                }
                step = marketAdmissionDestination == .phoneEntry ? .phoneEntry : .signIn
            case .unavailable, .checkFailed:
                marketAdmission = admission
            }
        }
    }

    private func handleSignIn(_ result: Result<ASAuthorization, Error>) {
        guard !isLoading else { return }
        switch result {
        case .failure(let error): errorMessage = error.localizedDescription
        case .success(let authorization):
            guard let credential = authorization.credential as? ASAuthorizationAppleIDCredential,
                  let tokenData = credential.identityToken,
                  let token = String(data: tokenData, encoding: .utf8), !token.isEmpty else {
                errorMessage = String(localized: "Sign in expired. Please tap Sign in with Apple again to continue.")
                return
            }
            let coordinator = AccountRestoreCoordinator.shared
            guard let revision = coordinator.beginAttempt() else { return }
            signInAttemptRevision = revision
            isLoading = true
            errorMessage = ""
            let retainedId = appState.contractorId
            let retainedApple = appState.appleUserId
            let retainedNumber = appState.kevinNumber
            let name = [credential.fullName?.givenName, credential.fullName?.familyName].compactMap { $0 }.joined(separator: " ")
            Task { @MainActor in
                let outcome = await coordinator.restoreAccount(
                    appleUserId: credential.user, appleIdentityToken: token,
                    isRecoveryMode: isRecoveryMode, retainedContractorId: retainedId,
                    retainedAppleUserId: retainedApple, retainedKevinNumber: retainedNumber,
                    attemptRevision: revision)
                guard signInAttemptRevision == revision else { return }
                isLoading = false
                signInAttemptRevision = nil
                switch outcome {
                case .success(_, let needsProvisioning):
                    if needsProvisioning && !isRecoveryMode {
                        step = .provisioning
                        await provision(mode: appState.isPersonalMode ? "personal" : "business")
                    }
                case .newAccountNeeded:
                    guard !isRecoveryMode else { return }
                    signupContinuation = nil
                    appState.appleUserId = credential.user
                    appState.appleIdentityToken = token
                    if !name.isEmpty { ownerName = name }
                    if newSignupAdmissionCountryCode == selectedCountryCode {
                        // A new account reached Apple sign-in from the explicit
                        // country check. Consume that authorization before phone
                        // entry so a later direct sign-in cannot reuse it.
                        newSignupAdmissionCountryCode = nil
                        step = .phoneEntry
                    } else {
                        // "Already a member" is still allowed to restore an
                        // existing account, but a person whom the server does
                        // not recognize as existing must choose and pass a
                        // number-country check before seeing phone entry.
                        marketAdmissionDestination = .phoneEntry
                        countrySelectionReturnDestination = .signIn
                        showCountrySelection()
                    }
                case .authFailed(let message), .notFound(let message), .failed(let message): errorMessage = message
                case .superseded: break
                }
            }
        }
    }

    /// The phone lookup can also discover a returning user. Re-verify that
    /// identity and use the same staged restore as Sign in with Apple.
    private func restoreBootstrapResponse(_ response: [String: Any]?, provisioningMode: String? = nil) async {
        guard !isRecoveryMode else { return }
        let coordinator = AccountRestoreCoordinator.shared
        guard let revision = coordinator.beginAttempt() else { return }
        signInAttemptRevision = revision
        let outcome = await coordinator.restoreBootstrapResponse(response,
            appleUserId: appState.appleUserId, appleIdentityToken: appState.appleIdentityToken,
            retainedContractorId: appState.contractorId,
            retainedAppleUserId: appState.appleUserId, retainedKevinNumber: appState.kevinNumber,
            signupContinuation: signupContinuation,
            attemptRevision: revision)
        guard signInAttemptRevision == revision else { return }
        signInAttemptRevision = nil
        switch outcome {
        case .success(_, let needsProvisioning):
            if needsProvisioning {
                step = .provisioning
                await provision(mode: provisioningMode ?? (appState.isPersonalMode ? "personal" : "business"))
            }
        case .authFailed(let message), .notFound(let message), .failed(let message): errorMessage = message
        case .newAccountNeeded:
            if let id = response?["contractor_id"] as? String {
                signupContinuation = SignupContinuation(contractorId: id, appleUserId: appState.appleUserId)
            }
            step = .modeSelect
        case .superseded: break
        }
    }

    private func restoreOrContinue() async {
        guard !isRecoveryMode else { return }
        let currentCountry = selectedCountryCode
        let validation = SignupPhone.validate(phoneNumber, countryCode: currentCountry)
        guard case .valid(let validPhone, _) = validation else {
            errorMessage = validation.failureReason ?? String(localized: "Please enter a valid mobile phone number.")
            return
        }

        let originSnapshot = SignupAdmissionSnapshot(
            countryCode: currentCountry,
            phone: validPhone,
            authContext: appState.currentAuthContext(),
            appleUserId: appState.appleUserId
        )

        // Admission Gate: check GET /api/markets before creating contractor
        let admission = await SignupAdmission.checkAvailability(countryCode: currentCountry)
        guard originSnapshot.isCurrent(
            countryCode: selectedCountryCode,
            phone: (SignupPhone.validate(phoneNumber, countryCode: selectedCountryCode).e164 ?? ""),
            authContext: appState.currentAuthContext(),
            appleUserId: appState.appleUserId
        ) else { return }

        guard case .allowed = admission else {
            errorMessage = admission.errorMessage ?? String(localized: "Kevin is not yet available in this country. We are preparing the service.")
            return
        }

        let outcome = await callWithFreshAppleTokenOnAuthFailure { [appState, validPhone, ownerName, currentCountry] in
            try await SignupAdmission.runIfAllowed(
                status: admission,
                snapshot: originSnapshot,
                current: {
                    SignupAdmissionSnapshot(
                        countryCode: self.selectedCountryCode,
                        phone: SignupPhone.validate(self.phoneNumber, countryCode: self.selectedCountryCode).e164 ?? "",
                        authContext: self.appState.currentAuthContext(),
                        appleUserId: self.appState.appleUserId
                    )
                }
            ) {
                try await APIClient.shared.createContractor(
                    ownerName: ownerName,
                    businessName: "",
                    serviceType: "general",
                    ownerPhone: validPhone,
                    countryCode: currentCountry,
                    appleUserId: appState.appleUserId,
                    appleIdentityToken: appState.appleIdentityToken
                )
            }
        }

        guard originSnapshot.isCurrent(
            countryCode: selectedCountryCode,
            phone: (SignupPhone.validate(phoneNumber, countryCode: selectedCountryCode).e164 ?? ""),
            authContext: appState.currentAuthContext(),
            appleUserId: appState.appleUserId
        ) else { return }

        switch outcome {
        case .authFailed:
            await handleBootstrapAuthFailure()
            return
        case .success(let result):
            if result?["status"] as? String == "error" {
                errorMessage = result?["error"] as? String ?? String(localized: "Failed to create profile. Please try again.")
                return
            }
            await restoreBootstrapResponse(result)
        }
    }

    private func provision(mode: String) async {
        guard !isRecoveryMode else { return }
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
        // Same resolution as the names above: prefer whatever the user just
        // typed on the provisioning-failure screen (regulatoryAddress/City,
        // captured by retryProvisioningWithAddress) over the last confirmed
        // AppState value. Reading local form state here — not
        // appState.businessAddress directly — is what keeps this call from
        // needing to write an unconfirmed address into AppState before the
        // create request that is the actual confirmation.
        let resolvedBusinessAddress: String = {
            let local = regulatoryAddress.trimmingCharacters(in: .whitespacesAndNewlines)
            return local.isEmpty ? appState.businessAddress : local
        }()
        let resolvedBusinessCity: String = {
            let local = regulatoryCity.trimmingCharacters(in: .whitespacesAndNewlines)
            return local.isEmpty ? appState.businessCity : local
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

        let initialAuth = appState.currentAuthContext()
        let initialAppleUserId = appState.appleUserId

        // Reuse existing contractor if we have one, otherwise create new
        let existingContractorId = appState.contractorId
        var existingProfileKnowledge: String?

        // Fast-path: contractor already exists and the profile already has a Kevin
        // number. Persist the selected mode before finishing onboarding, but avoid
        // overwriting restored profile fields with potentially-stale form values.
        if !existingContractorId.isEmpty {
            guard let profile = await APIClient.shared.getContractorProfile(
                contractorId: initialAuth.contractorId,
                bearerToken: initialAuth.bearerToken
            ) else {
                if appState.currentAuthContext() == initialAuth && appState.appleUserId == initialAppleUserId {
                    errorMessage = String(localized: "Could not reconnect your account. Please try again.")
                }
                isLoading = false
                return
            }
            existingProfileKnowledge = profile["knowledge"] as? String
            guard appState.currentAuthContext() == initialAuth,
                  appState.appleUserId == initialAppleUserId else {
                isLoading = false
                return
            }
            if let existing = profile["twilio_number"] as? String,
               !existing.isEmpty {
                do {
                    let updated = try await APIClient.shared.patchContractor(
                        initialAuth.contractorId,
                        body: ["mode": mode],
                        bearerToken: initialAuth.bearerToken
                    )
                    guard appState.currentAuthContext() == initialAuth,
                          appState.appleUserId == initialAppleUserId else {
                        isLoading = false
                        return
                    }
                    if !updated {
                        errorMessage = mode == "business"
                            ? String(localized: "Business mode requires an active Business subscription. Restore purchases or choose Personal.")
                            : String(localized: "Failed to update profile. Please try again.")
                        isLoading = false
                        return
                    }
                } catch {
                    guard appState.currentAuthContext() == initialAuth,
                          appState.appleUserId == initialAppleUserId else {
                        isLoading = false
                        return
                    }
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
                appState.serviceBinding = ServiceBindingParser.parse(from: profile)
                step = .forwarding
                isLoading = false
                return
            }

            // Restored profile without assigned number: gate before any mode write or provisioning!
            let profileCountry = SettingsCountry.accountCountry(from: profile) ?? selectedCountryCode
            let profilePhone = (profile["owner_phone"] as? String) ?? phoneNumber
            let snapshot = SignupAdmissionSnapshot(
                countryCode: profileCountry,
                phone: profilePhone,
                authContext: initialAuth,
                appleUserId: initialAppleUserId
            )
            let admission = await SignupAdmission.checkAvailability(countryCode: profileCountry)
            guard snapshot.isCurrent(
                countryCode: profileCountry,
                phone: profilePhone,
                authContext: appState.currentAuthContext(),
                appleUserId: appState.appleUserId
            ) else {
                isLoading = false
                return
            }
            guard case .allowed = admission else {
                if appState.currentAuthContext() == initialAuth && appState.appleUserId == initialAppleUserId {
                    errorMessage = admission.errorMessage ?? String(localized: "Kevin is not yet available in this country. We are preparing the service.")
                }
                isLoading = false
                return
            }
            guard appState.currentAuthContext() == initialAuth,
                  appState.appleUserId == initialAppleUserId else {
                isLoading = false
                return
            }
        }

        let opAuthContext: CallAuthContext
        let opAppleUserId: String
        let patchProfileClosure: (() async throws -> Bool)?
        let patchFailureMessage: String
        let servicesPayloadForBusiness = (mode == "business") ? effectiveServicesPayload : nil
        let knowledgeForBusiness = (mode == "business") ? effectiveKnowledgeMarkdown : nil

        if !existingContractorId.isEmpty {
            // Pre-existing unassigned account: construct optional patch closure using captured token
            var updateBody: [String: Any] = ["mode": mode]
            if !resolvedOwnerName.isEmpty { updateBody["owner_name"] = resolvedOwnerName }
            if !bizName.isEmpty { updateBody["business_name"] = bizName }
            if let servicesPayloadForBusiness { updateBody["services"] = servicesPayloadForBusiness }
            if let mergedKnowledge = OnboardingServicesDraft.mergeKnowledge(
                existing: existingProfileKnowledge,
                additionalSection: knowledgeForBusiness
            ) {
                updateBody["knowledge"] = mergedKnowledge
            }

            opAuthContext = initialAuth
            opAppleUserId = initialAppleUserId
            patchProfileClosure = {
                try await APIClient.shared.patchContractor(
                    initialAuth.contractorId,
                    body: updateBody,
                    bearerToken: initialAuth.bearerToken
                )
            }
            patchFailureMessage = mode == "business"
                ? String(localized: "Business mode requires an active Business subscription. Restore purchases or choose Personal.")
                : String(localized: "Failed to update profile. Please try again.")
        } else {
            // No existing contractor — validate phone and market gate before creation
            let currentCountry = selectedCountryCode
            let validation = SignupPhone.validate(phoneNumber, countryCode: currentCountry)
            guard case .valid(let validPhone, _) = validation else {
                errorMessage = validation.failureReason ?? String(localized: "Please enter a valid mobile phone number.")
                isLoading = false
                return
            }

            let originSnapshot = SignupAdmissionSnapshot(
                countryCode: currentCountry,
                phone: validPhone,
                authContext: appState.currentAuthContext(),
                appleUserId: appState.appleUserId
            )
            let admission = await SignupAdmission.checkAvailability(countryCode: currentCountry)
            guard originSnapshot.isCurrent(
                countryCode: selectedCountryCode,
                phone: (SignupPhone.validate(phoneNumber, countryCode: selectedCountryCode).e164 ?? ""),
                authContext: appState.currentAuthContext(),
                appleUserId: appState.appleUserId
            ) else {
                isLoading = false
                return
            }
            guard case .allowed = admission else {
                errorMessage = admission.errorMessage ?? String(localized: "Kevin is not yet available in this country. We are preparing the service.")
                isLoading = false
                return
            }

            let createOutcome = await callWithFreshAppleTokenOnAuthFailure { [appState, validPhone, currentCountry, resolvedOwnerName, bizName, svcType, resolvedBusinessAddress, resolvedBusinessCity, mode, servicesPayloadForBusiness, knowledgeForBusiness] in
                try await SignupAdmission.runIfAllowed(
                    status: admission,
                    snapshot: originSnapshot,
                    current: {
                        SignupAdmissionSnapshot(
                            countryCode: self.selectedCountryCode,
                            phone: SignupPhone.validate(self.phoneNumber, countryCode: self.selectedCountryCode).e164 ?? "",
                            authContext: self.appState.currentAuthContext(),
                            appleUserId: self.appState.appleUserId
                        )
                    }
                ) {
                    try await APIClient.shared.createContractor(
                        ownerName: resolvedOwnerName,
                        businessName: bizName,
                        serviceType: svcType,
                        mode: mode,
                        ownerPhone: validPhone,
                        countryCode: currentCountry,
                        appleUserId: appState.appleUserId,
                        appleIdentityToken: appState.appleIdentityToken,
                        businessAddress: resolvedBusinessAddress,
                        businessCity: resolvedBusinessCity,
                        declaredOnboardingIntent: mode,
                        services: servicesPayloadForBusiness,
                        knowledge: knowledgeForBusiness
                    )
                }
            }
            guard originSnapshot.isCurrent(
                countryCode: selectedCountryCode,
                phone: (SignupPhone.validate(phoneNumber, countryCode: selectedCountryCode).e164 ?? ""),
                authContext: appState.currentAuthContext(),
                appleUserId: appState.appleUserId
            ) else {
                isLoading = false
                return
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
            let newContractorId = result?["contractor_id"] as? String ?? ""
            if newContractorId.isEmpty {
                errorMessage = result?["error"] as? String ?? String(localized: "Failed to create profile. Please try again.")
                isLoading = false
                return
            }
            if result?["existing"] as? Bool == true {
                await restoreBootstrapResponse(result, provisioningMode: mode)
                isLoading = false
                return
            }
            appState.contractorId = newContractorId
            if let apiToken = result?["api_token"] as? String, !apiToken.isEmpty {
                APIClient.shared.contractorToken = apiToken
            }
            opAuthContext = appState.currentAuthContext()
            opAppleUserId = appState.appleUserId
            patchProfileClosure = nil
            patchFailureMessage = ""
        }

        let isCurrentIdentity: () -> Bool = { [appState, opAuthContext, opAppleUserId] in
            appState.currentAuthContext() == opAuthContext && appState.appleUserId == opAppleUserId
        }

        let opResult = await ProvisioningOperation.run(
            capturedAuth: opAuthContext,
            isCurrent: isCurrentIdentity,
            cachedNumber: appState.kevinNumber,
            cachedCountry: appState.countryCode,
            cachedBinding: appState.serviceBinding,
            patchProfile: patchProfileClosure,
            patchFailureMessage: patchFailureMessage,
            fetchProfile: {
                await APIClient.shared.getContractorProfile(
                    contractorId: opAuthContext.contractorId,
                    bearerToken: opAuthContext.bearerToken
                )
            },
            provisionNumber: {
                await APIClient.shared.provisionNumber(
                    contractorId: opAuthContext.contractorId,
                    bearerToken: opAuthContext.bearerToken
                )
            },
            syncContacts: appState.contactsUploadConsent ? {
                let syncResult = await ContactSyncManager.shared.syncContacts(
                    contractorId: opAuthContext.contractorId,
                    force: true
                )
                if case .success(let synced, _) = syncResult {
                    return synced
                }
                return nil
            } : nil,
            commit: { output in
                if !resolvedOwnerName.isEmpty { appState.userName = resolvedOwnerName }
                if !resolvedBusinessName.isEmpty { appState.businessName = resolvedBusinessName }
                appState.mode = mode
                kevinNumber = output.number
                appState.kevinNumber = output.number
                if let country = output.country {
                    appState.countryCode = country
                }
                appState.serviceBinding = output.binding
                if let subUUID = output.subscriptionUUID {
                    appState.subscriptionUUID = subUUID
                }
                if let synced = output.syncedContacts {
                    contactsSynced = synced
                }
                appState.appleIdentityToken = ""
                AppDelegate.requestPushAuthorization()
                step = .forwarding
            }
        )

        isLoading = false

        switch opResult {
        case .completed:
            break
        case .superseded:
            break
        case .failed(let message):
            if isCurrentIdentity() {
                errorMessage = message
            }
        }
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
        let address = regulatoryAddress.trimmingCharacters(in: .whitespacesAndNewlines)
        let city = regulatoryCity.trimmingCharacters(in: .whitespacesAndNewlines)

        let contractorId = appState.contractorId
        if !contractorId.isEmpty {
            // An existing contractor: patch first, and adopt the value into
            // AppState only once the server has confirmed it — the same
            // rule SettingsView.saveRegulatoryAddress follows. On failure,
            // AppState is left untouched and the typed values stay on
            // screen next to the error.
            let saved = await APIClient.shared.updateBusinessAddress(
                contractorId: contractorId,
                address: address,
                city: city
            )
            guard saved else {
                regulatoryAddressError = String(localized: "Failed to save address. Please try again.")
                return
            }
            appState.businessAddress = address
            appState.businessCity = city
        }
        // else: no contractor exists yet — there is nothing to patch.
        // `provision(mode:)` below creates the contractor and resolves its
        // own businessAddress/businessCity from this same
        // regulatoryAddress/regulatoryCity form state (mirroring how it
        // already resolves owner/business name), so the create request
        // itself carries these values without AppState ever holding an
        // unconfirmed draft. AppState later adopts the confirmed value the
        // ordinary way, via SettingsView.loadKnowledge reading it back from
        // the saved profile — the same path business_name already takes.
        errorMessage = ""
        await provision(mode: selectedMode)
    }

    private var effectiveServicesPayload: [[String: Any]]? {
        guard !didSkipServicesStep else { return nil }
        let payload = servicesDraft.servicesPayload
        return payload.isEmpty ? nil : payload
    }

    private var effectiveKnowledgeMarkdown: String? {
        guard !didSkipServicesStep else { return nil }
        let markdown = servicesDraft.knowledgeMarkdown
        return markdown.isEmpty ? nil : markdown
    }

    private func commitTypedDraftService() {
        let added = servicesDraft.addTypedEntry(
            name: draftServiceName,
            minPriceText: draftServiceMinPrice,
            maxPriceText: draftServiceMaxPrice
        )
        if added {
            draftServiceName = ""
            draftServiceMinPrice = ""
            draftServiceMaxPrice = ""
        }
    }

    private func handleServicesFileImport(_ result: Result<[URL], Error>) {
        errorMessage = ""
        servicesUploadMessage = ""
        switch result {
        case .failure:
            servicesUploadMessage = String(localized: "Could not open the selected file.")
        case .success(let urls):
            guard let url = urls.first else { return }
            isExtractingPriceList = true
            Task {
                let gainedAccess = url.startAccessingSecurityScopedResource()
                defer {
                    if gainedAccess { url.stopAccessingSecurityScopedResource() }
                }
                let fileName = url.lastPathComponent
                let ext = url.pathExtension.lowercased()
                let fileData = try? Data(contentsOf: url)
                let extractedText: String = if let fileData {
                    await OnboardingServicesDraft.extractText(from: fileData, pathExtension: ext)
                } else {
                    ""
                }

                await MainActor.run {
                    isExtractingPriceList = false
                    let trimmed = extractedText.trimmingCharacters(in: .whitespacesAndNewlines)
                    guard !trimmed.isEmpty else {
                        servicesUploadMessage = String(localized: "Could not read text from \(fileName). Try typing your services or another file.")
                        return
                    }
                    let count = servicesDraft.importUploadedText(trimmed, fileName: fileName)
                    if count > 0 {
                        servicesUploadMessage = String(localized: "Added \(count) service(s) from \(fileName).")
                    } else {
                        servicesUploadMessage = String(localized: "No new services found in \(fileName).")
                    }
                }
            }
        }
    }

    @MainActor
    private func handlePriceListPhotoSelection(_ item: PhotosPickerItem) async {
        errorMessage = ""
        servicesUploadMessage = ""
        isExtractingPriceList = true
        defer {
            isExtractingPriceList = false
            selectedPriceListPhoto = nil
        }
        guard let data = try? await item.loadTransferable(type: Data.self), !data.isEmpty else {
            servicesUploadMessage = String(localized: "Could not load the selected photo.")
            return
        }
        let text = await OnboardingServicesDraft.extractText(from: data, pathExtension: "jpg")
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            servicesUploadMessage = String(localized: "No readable text found in photo. You can type services above.")
            return
        }
        let count = servicesDraft.importUploadedText(trimmed, fileName: String(localized: "Price sheet photo"))
        if count > 0 {
            servicesUploadMessage = String(localized: "Added \(count) service(s) from photo.")
        } else {
            servicesUploadMessage = String(localized: "No new services found in photo.")
        }
    }

    @MainActor
    private func proceedFromBusinessServices(skipServices: Bool) {
        selectedMode = "business"
        errorMessage = ""
        didSkipServicesStep = skipServices
        if !skipServices, !draftServiceName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            commitTypedDraftService()
        }
        if let onDebugPreviewComplete, !appState.contractorId.isEmpty {
            Task {
                let ready = await prepareBusinessDraftProfile()
                guard ready else { return }
                _ = try? await APIClient.shared.patchContractor(
                    appState.contractorId,
                    body: ["mode": "business"]
                )
                appState.mode = "business"
                appState.userName = ownerName
                appState.businessName = businessName
                appState.serviceType = serviceType
                onDebugPreviewComplete()
            }
            return
        }
        if appState.hasBusinessEntitlement {
            Task {
                let ready = await prepareBusinessDraftProfile()
                guard ready else { return }
                let result = await activateBusinessAfterPurchase(
                    authContext: appState.currentAuthContext()
                ) {
                    step = .contactsPermission
                }
                if case .failure(let message) = result {
                    errorMessage = message
                }
            }
        } else {
            Task {
                let ready = await prepareBusinessDraftProfile()
                if ready {
                    showPaywall = true
                }
            }
        }
    }

    @MainActor
    private func prepareBusinessDraftProfile() async -> Bool {
        isLoading = true
        errorMessage = ""
        defer { isLoading = false }

        let servicesToSave = effectiveServicesPayload
        let knowledgeToSave = effectiveKnowledgeMarkdown

        var profilePatchBody: [String: Any] = [
            "owner_name": ownerName,
            "business_name": businessName,
            "service_type": serviceType,
        ]
        if let servicesToSave { profilePatchBody["services"] = servicesToSave }

        if appState.contractorId.isEmpty {
            let currentCountry = selectedCountryCode
            let validation = SignupPhone.validate(phoneNumber, countryCode: currentCountry)
            guard case .valid(let validPhone, _) = validation else {
                errorMessage = validation.failureReason ?? String(localized: "Please enter a valid mobile phone number.")
                return false
            }

            let originSnapshot = SignupAdmissionSnapshot(
                countryCode: currentCountry,
                phone: validPhone,
                authContext: appState.currentAuthContext(),
                appleUserId: appState.appleUserId
            )
            let admission = await SignupAdmission.checkAvailability(countryCode: currentCountry)
            guard originSnapshot.isCurrent(
                countryCode: selectedCountryCode,
                phone: (SignupPhone.validate(phoneNumber, countryCode: selectedCountryCode).e164 ?? ""),
                authContext: appState.currentAuthContext(),
                appleUserId: appState.appleUserId
            ) else {
                return false
            }
            guard case .allowed = admission else {
                errorMessage = admission.errorMessage ?? String(localized: "Kevin is not yet available in this country. We are preparing the service.")
                return false
            }

            let outcome = await callWithFreshAppleTokenOnAuthFailure { [appState, validPhone, ownerName, businessName, serviceType, currentCountry, servicesToSave, knowledgeToSave] in
                try await SignupAdmission.runIfAllowed(
                    status: admission,
                    snapshot: originSnapshot,
                    current: {
                        SignupAdmissionSnapshot(
                            countryCode: self.selectedCountryCode,
                            phone: SignupPhone.validate(self.phoneNumber, countryCode: self.selectedCountryCode).e164 ?? "",
                            authContext: self.appState.currentAuthContext(),
                            appleUserId: self.appState.appleUserId
                        )
                    }
                ) {
                    try await APIClient.shared.createContractor(
                        ownerName: ownerName,
                        businessName: businessName,
                        serviceType: serviceType,
                        mode: "personal",
                        ownerPhone: validPhone,
                        countryCode: currentCountry,
                        appleUserId: appState.appleUserId,
                        appleIdentityToken: appState.appleIdentityToken,
                        declaredOnboardingIntent: "business",
                        services: servicesToSave,
                        knowledge: knowledgeToSave
                    )
                }
            }
            guard originSnapshot.isCurrent(
                countryCode: selectedCountryCode,
                phone: (SignupPhone.validate(phoneNumber, countryCode: selectedCountryCode).e164 ?? ""),
                authContext: appState.currentAuthContext(),
                appleUserId: appState.appleUserId
            ) else {
                return false
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
                errorMessage = result?["error"] as? String ?? String(localized: "Failed to prepare your business profile. Please try again.")
                return false
            }
            appState.contractorId = contractorId
            if let apiToken = result?["api_token"] as? String, !apiToken.isEmpty {
                APIClient.shared.contractorToken = apiToken
            }
            if let knowledgeToSave {
                let existingKnowledge: String?
                if result?["existing"] as? Bool == true,
                   let existingProfile = await APIClient.shared.getContractorProfile(contractorId: contractorId) {
                    existingKnowledge = existingProfile["knowledge"] as? String
                } else {
                    existingKnowledge = nil
                }
                if let merged = OnboardingServicesDraft.mergeKnowledge(
                    existing: existingKnowledge,
                    additionalSection: knowledgeToSave
                ) {
                    profilePatchBody["knowledge"] = merged
                }
            }
            do {
                let updated = try await APIClient.shared.patchContractor(contractorId, body: profilePatchBody)
                if !updated {
                    errorMessage = String(localized: "Failed to save your business profile. Please try again.")
                    return false
                }
            } catch {
                errorMessage = String(localized: "Failed to save your business profile. Please try again.")
                return false
            }
        } else {
            if let knowledgeToSave {
                let existingProfile = await APIClient.shared.getContractorProfile(contractorId: appState.contractorId)
                let existingKnowledge = existingProfile?["knowledge"] as? String
                if let merged = OnboardingServicesDraft.mergeKnowledge(
                    existing: existingKnowledge,
                    additionalSection: knowledgeToSave
                ) {
                    profilePatchBody["knowledge"] = merged
                }
            }
            do {
                let updated = try await APIClient.shared.patchContractor(appState.contractorId, body: profilePatchBody)
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
            appState.serviceBinding = ServiceBindingParser.parse(from: profile)
        }

        return true
    }

    @MainActor
    private func activateBusinessAfterPurchase(
        authContext: CallAuthContext,
        onCommit: (@MainActor () -> Void)? = nil
    ) async -> PaywallCompletionResult {
        let servicesToSave = effectiveServicesPayload
        let knowledgeToSave = effectiveKnowledgeMarkdown
        return await BusinessActivationOperation.activate(
            capturedAuth: authContext,
            currentAuth: { appState.currentAuthContext() },
            hasBusinessEntitlement: { appState.hasBusinessEntitlement },
            patch: { contractorId, _, bearerToken in
                var patchBody: [String: Any] = [
                    "owner_name": ownerName,
                    "business_name": businessName,
                    "service_type": serviceType,
                    "mode": "business",
                ]
                if let servicesToSave { patchBody["services"] = servicesToSave }
                if let knowledgeToSave {
                    let existingProfile = await APIClient.shared.getContractorProfile(
                        contractorId: contractorId,
                        bearerToken: bearerToken
                    )
                    let existingKnowledge = existingProfile?["knowledge"] as? String
                    if let merged = OnboardingServicesDraft.mergeKnowledge(
                        existing: existingKnowledge,
                        additionalSection: knowledgeToSave
                    ) {
                        patchBody["knowledge"] = merged
                    }
                }
                return try await APIClient.shared.patchContractor(
                    contractorId,
                    body: patchBody,
                    bearerToken: bearerToken
                )
            },
            commit: {
                selectedMode = "business"
                appState.mode = "business"
                appState.userName = ownerName
                appState.businessName = businessName
                appState.serviceType = serviceType
                if let onCommit {
                    onCommit()
                } else {
                    showPaywall = false
                    step = .contactsPermission
                }
            }
        )
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
        guard forwardingCodes != nil else { return }
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
