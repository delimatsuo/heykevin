import SwiftUI

/// Deterministic, network-free App Store screenshot scenarios.
///
/// These fixtures are activated only by the Debug-only environment variable
/// `APP_STORE_SCREENSHOT_SCENARIO`. Production and Staging launches always use
/// the normal app root and live backend data.
enum AppStoreScreenshotFixtures {
    enum Scenario: String {
        case businessLive = "business-live"
        case businessRecents = "business-recents"
        case businessDetail = "business-detail"
        case personalLive = "personal-live"
        case personalRecents = "personal-recents"
        case personalDetail = "personal-detail"

        var isBusiness: Bool {
            switch self {
            case .businessLive, .businessRecents, .businessDetail:
                return true
            case .personalLive, .personalRecents, .personalDetail:
                return false
            }
        }

        var title: String {
            switch self {
            case .businessLive:
                return "Every missed call can become a job"
            case .businessRecents:
                return "See which calls need you first"
            case .businessDetail:
                return "Call back with the whole story"
            case .personalLive:
                return "Unknown caller? Let Kevin ask why"
            case .personalRecents:
                return "Keep the people. Lose the robocalls."
            case .personalDetail:
                return "Know what they need before you call back"
            }
        }

        var subtitle: String {
            switch self {
            case .businessLive:
                return "Kevin answers, qualifies the caller, and lets you take over live."
            case .businessRecents:
                return "Urgent jobs, caller details, and summaries in one place."
            case .businessDetail:
                return "Review what the customer needs before you return the call."
            case .personalLive:
                return "Watch the conversation live and pick up only when you want."
            case .personalRecents:
                return "Contacts ring through; unknown callers are screened and summarized."
            case .personalDetail:
                return "Read the conversation and decide whether the call deserves your time."
            }
        }
    }

    static var scenario: Scenario? {
        #if DEBUG
        guard let value = ProcessInfo.processInfo.environment["APP_STORE_SCREENSHOT_SCENARIO"] else {
            return nil
        }
        return Scenario(rawValue: value)
        #else
        return nil
        #endif
    }

    static var isEnabled: Bool { scenario != nil }

    /// Fixed anchor time for deterministic screenshots.
    /// Using a constant "now" eliminates wall-clock flakiness in labels and details.
    static var now: Date {
        var comps = DateComponents()
        comps.calendar = Calendar(identifier: .gregorian)
        comps.timeZone = TimeZone(secondsFromGMT: 0)
        comps.year = 2025
        comps.month = 1
        comps.day = 15
        comps.hour = 17
        comps.minute = 0
        comps.second = 0
        return comps.date!
    }

    static var seededCalls: [CallRecord] {
        guard let scenario else { return [] }
        return scenario.isBusiness ? businessCalls : personalCalls
    }

    static var featuredCall: CallRecord {
        seededCalls.first ?? businessCalls[0]
    }

    @MainActor
    static func configure(_ appState: AppState) {
        guard let scenario else { return }

        UIView.setAnimationsEnabled(false)

        appState.isOnboarded = true
        appState.contractorId = "app-store-screenshot-fixture"
        appState.subscriptionStatus = "trial"
        appState.subscriptionTier = scenario.isBusiness ? "business" : "personal"
        appState.kevinNumber = "+16505550142"
        appState.forwardingActivated = true
        appState.userName = scenario.isBusiness ? "Alex Rivera" : "Alex"
        appState.businessName = scenario.isBusiness ? "Northside Plumbing" : ""
        appState.serviceType = scenario.isBusiness ? "Plumbing" : ""
        appState.mode = scenario.isBusiness ? "business" : "personal"
        appState.ringThroughContacts = true
        appState.readCallIds = Set(seededCalls.filter(\.readOnServer).map(\.id))
        appState.unreadCallCount = seededCalls.filter { appState.isCallUnread($0) }.count

        switch scenario {
        case .businessLive, .personalLive:
            appState.selectedTab = .live
            appState.setActiveCall(
                callSid: "CA_APP_STORE_SCREENSHOT",
                callerPhone: scenario.isBusiness ? "+16505550187" : "+14155550126",
                callerName: scenario.isBusiness ? "Maria Santos" : ""
            )
            appState.callStartTime = now.addingTimeInterval(-137)
            appState.transcriptLines = liveTranscript(forBusiness: scenario.isBusiness)
        case .businessRecents, .personalRecents, .businessDetail, .personalDetail:
            appState.clearActiveCall()
            appState.selectedTab = .recents
        }
    }

    private static func liveTranscript(forBusiness: Bool) -> [TranscriptLine] {
        let lines: [String]
        if forBusiness {
            lines = [
                "Kevin: Thanks for calling Northside Plumbing. How can I help?",
                "Caller: My water heater is leaking and I need someone today.",
                "Kevin: I can help with that. What's the service address?",
                "Caller: 418 Oak Street. The leak is getting worse.",
                "Kevin: Got it. I'm alerting the team now.",
            ]
        } else {
            lines = [
                "Kevin: Hi, this is Kevin. Who's calling and what is this regarding?",
                "Caller: This is Sam from City Auto. I'm calling about the service estimate.",
                "Kevin: Thanks, Sam. Is this urgent, or can Alex call you back later today?",
                "Caller: Later today is fine. My number is 415-555-0126.",
            ]
        }
        return lines.map(TranscriptLine.init(text:))
    }

    private static var businessCalls: [CallRecord] {
        [
            CallRecord(
                id: "business-urgent",
                callerPhone: "+16505550187",
                callerName: "Maria Santos",
                timestamp: now.addingTimeInterval(-6 * 60),
                trustScore: 91,
                outcome: "voicemail",
                transcript: """
                Kevin: Thanks for calling Northside Plumbing. How can I help?
                Caller: My water heater is leaking and I need someone today.
                Kevin: What's the service address?
                Caller: 418 Oak Street. The leak is getting worse.
                """,
                voicemailURL: nil,
                callbackNumber: "+16505550187",
                readOnServer: false
            ),
            CallRecord(
                id: "business-install",
                callerPhone: "+16505550163",
                callerName: "Jake Thompson",
                timestamp: now.addingTimeInterval(-31 * 60),
                trustScore: 82,
                outcome: "voicemail",
                transcript: """
                Kevin: What can we help with?
                Caller: I'd like an estimate for a tankless water heater.
                Kevin: What is the best time to call you back?
                Caller: Any time after 2 PM works.
                """,
                voicemailURL: nil,
                callbackNumber: "+16505550163",
                readOnServer: false
            ),
            CallRecord(
                id: "business-drain",
                callerPhone: "+16505550109",
                callerName: "Priya Shah",
                timestamp: now.addingTimeInterval(-96 * 60),
                trustScore: 88,
                outcome: "picked_up",
                transcript: """
                Kevin: How can we help today?
                Caller: Our kitchen sink keeps backing up.
                Kevin: Is there any active flooding?
                Caller: No flooding, but we'd like a visit this week.
                """,
                voicemailURL: nil,
                callbackNumber: "+16505550109",
                readOnServer: true
            ),
            CallRecord(
                id: "business-follow-up",
                callerPhone: "+16505550144",
                callerName: "Daniel Kim",
                timestamp: now.addingTimeInterval(-4 * 60 * 60),
                trustScore: 96,
                outcome: "voicemail",
                transcript: """
                Kevin: What are you calling about?
                Caller: I'm following up on estimate 1042.
                Kevin: Is there a message for the team?
                Caller: Please confirm whether Friday still works.
                """,
                voicemailURL: nil,
                callbackNumber: "+16505550144",
                readOnServer: true
            ),
            CallRecord(
                id: "business-spam",
                callerPhone: "+18885550199",
                callerName: "",
                timestamp: now.addingTimeInterval(-7 * 60 * 60),
                trustScore: 3,
                outcome: "spam",
                transcript: "Caller: Congratulations, your business has been selected.",
                voicemailURL: nil,
                callbackNumber: nil,
                readOnServer: true
            ),
        ]
    }

    private static var personalCalls: [CallRecord] {
        [
            CallRecord(
                id: "personal-estimate",
                callerPhone: "+14155550126",
                callerName: "City Auto",
                timestamp: now.addingTimeInterval(-9 * 60),
                trustScore: 83,
                outcome: "voicemail",
                transcript: """
                Kevin: Who's calling and what is this regarding?
                Caller: This is Sam from City Auto about Alex's service estimate.
                Kevin: Can Alex call you back later today?
                Caller: Yes, later today is fine.
                """,
                voicemailURL: nil,
                callbackNumber: "+14155550126",
                readOnServer: false
            ),
            CallRecord(
                id: "personal-delivery",
                callerPhone: "+14155550158",
                callerName: "Delivery Desk",
                timestamp: now.addingTimeInterval(-42 * 60),
                trustScore: 74,
                outcome: "voicemail",
                transcript: """
                Kevin: What can I help with?
                Caller: I need a gate code for today's delivery.
                Kevin: Can the delivery be left at the front desk?
                Caller: Yes, that works. I'll note the account.
                """,
                voicemailURL: nil,
                callbackNumber: "+14155550158",
                readOnServer: false
            ),
            CallRecord(
                id: "personal-school",
                callerPhone: "+14155550171",
                callerName: "Riverside School",
                timestamp: now.addingTimeInterval(-2 * 60 * 60),
                trustScore: 93,
                outcome: "picked_up",
                transcript: """
                Kevin: Who's calling?
                Caller: This is Riverside School with a routine attendance update.
                Kevin: Would you like Alex to call back?
                Caller: No callback is needed. Thank you.
                """,
                voicemailURL: nil,
                callbackNumber: "+14155550171",
                readOnServer: true
            ),
            CallRecord(
                id: "personal-pharmacy",
                callerPhone: "+14155550134",
                callerName: "Local Pharmacy",
                timestamp: now.addingTimeInterval(-5 * 60 * 60),
                trustScore: 89,
                outcome: "voicemail",
                transcript: """
                Kevin: What is this regarding?
                Caller: A prescription is ready for pickup.
                Kevin: Is a callback required?
                Caller: No, this is just a notification.
                """,
                voicemailURL: nil,
                callbackNumber: "+14155550134",
                readOnServer: true
            ),
            CallRecord(
                id: "personal-spam",
                callerPhone: "+18005550198",
                callerName: "",
                timestamp: now.addingTimeInterval(-8 * 60 * 60),
                trustScore: 2,
                outcome: "spam",
                transcript: "Caller: Your vehicle warranty is about to expire.",
                voicemailURL: nil,
                callbackNumber: nil,
                readOnServer: true
            ),
        ]
    }
}

struct AppStoreScreenshotRoot: View {
    @EnvironmentObject private var appState: AppState

    private var scenario: AppStoreScreenshotFixtures.Scenario {
        AppStoreScreenshotFixtures.scenario ?? .businessLive
    }

    var body: some View {
        AppStoreMarketingFrame(
            title: scenario.title,
            subtitle: scenario.subtitle,
            isBusiness: scenario.isBusiness
        ) {
            screenshotContent
        }
        .preferredColorScheme(.light)
        .background {
            LinearGradient(
                colors: scenario.isBusiness
                    ? [Color(red: 0.02, green: 0.12, blue: 0.28), Color(red: 0.02, green: 0.34, blue: 0.66)]
                    : [Color(red: 0.13, green: 0.08, blue: 0.30), Color(red: 0.40, green: 0.16, blue: 0.62)],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
            .ignoresSafeArea()
        }
    }

    @ViewBuilder
    private var screenshotContent: some View {
        switch scenario {
        case .businessLive, .personalLive, .businessRecents, .personalRecents:
            ContentView()
                .environmentObject(appState)
        case .businessDetail, .personalDetail:
            NavigationStack {
                CallDetailView(call: AppStoreScreenshotFixtures.featuredCall)
            }
            .environmentObject(appState)
        }
    }
}

private struct AppStoreMarketingFrame<Content: View>: View {
    let title: String
    let subtitle: String
    let isBusiness: Bool
    let content: Content

    init(
        title: String,
        subtitle: String,
        isBusiness: Bool,
        @ViewBuilder content: () -> Content
    ) {
        self.title = title
        self.subtitle = subtitle
        self.isBusiness = isBusiness
        self.content = content()
    }

    var body: some View {
        GeometryReader { geometry in
            ZStack {
                backgroundGradient
                    .ignoresSafeArea()

                Circle()
                    .fill(Color.white.opacity(0.10))
                    .frame(width: geometry.size.width * 0.95)
                    .blur(radius: 2)
                    .offset(x: geometry.size.width * 0.38, y: -geometry.size.height * 0.34)

                VStack(spacing: 18) {
                    VStack(spacing: 10) {
                        Text(title)
                            .font(.system(size: 36, weight: .bold, design: .rounded))
                            .foregroundStyle(.white)
                            .multilineTextAlignment(.center)
                            .minimumScaleFactor(0.78)
                            .lineLimit(2)

                        Text(subtitle)
                            .font(.system(size: 18, weight: .medium, design: .rounded))
                            .foregroundStyle(Color.white.opacity(0.84))
                            .multilineTextAlignment(.center)
                            .lineSpacing(3)
                            .lineLimit(3)
                    }
                    .padding(.horizontal, 12)

                    content
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .background(Color(.systemBackground))
                        .clipShape(RoundedRectangle(cornerRadius: 30, style: .continuous))
                        .overlay {
                            RoundedRectangle(cornerRadius: 30, style: .continuous)
                                .stroke(Color.white.opacity(0.22), lineWidth: 1)
                        }
                        .shadow(color: .black.opacity(0.28), radius: 24, y: 10)
                }
                .padding(.horizontal, 18)
                .padding(.top, 12)
                .padding(.bottom, 18)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background {
                backgroundGradient
                    .ignoresSafeArea()
            }
        }
    }

    private var backgroundGradient: LinearGradient {
        LinearGradient(
            colors: isBusiness
                ? [Color(red: 0.02, green: 0.12, blue: 0.28), Color(red: 0.02, green: 0.34, blue: 0.66)]
                : [Color(red: 0.13, green: 0.08, blue: 0.30), Color(red: 0.40, green: 0.16, blue: 0.62)],
            startPoint: .topLeading,
            endPoint: .bottomTrailing
        )
    }
}
