import SwiftUI

/// Feature flag: hide "Text reply" until A2P 10DLC carrier registration is approved.
/// Outbound SMS to US numbers from an unregistered Twilio long-code is silently
/// dropped at the carrier (Twilio error 30034), so the button promises something
/// the backend can't yet deliver. Flip this to `true` after A2P registration
/// clears and ship a new TestFlight/App Store build to re-enable.
private let kTextReplyEnabled = false

struct TranscriptLine: Identifiable {
    let id = UUID()
    let text: String
}

struct ContentView: View {
    @EnvironmentObject var appState: AppState
    @ObservedObject var callManager = CallManager.shared
    @Environment(\.scenePhase) var scenePhase
    @State private var showForcedPaywall = false

    var body: some View {
        TabView(selection: $appState.selectedTab) {
            LiveCallTab()
                .tabItem {
                    Label(String(localized: "Live"), systemImage: "waveform")
                }
                .tag(AppTab.live)
                .badge(appState.hasActiveCall ? "1" : nil)

            CallHistoryView()
                .tabItem {
                    Label(String(localized: "Recents"), systemImage: "clock")
                }
                .tag(AppTab.recents)
                .badge(appState.unreadCallCount > 0 ? "\(appState.unreadCallCount)" : nil)

            SettingsView()
                .tabItem {
                    Label(String(localized: "Settings"), systemImage: "gear")
                }
                .tag(AppTab.settings)
        }
        .onChange(of: scenePhase) {
            if scenePhase == .active {
                appState.checkForActiveCall()
            }
        }
        .onChange(of: appState.showActiveCall) {
            // Auto-switch to Live tab when a call comes in
            if appState.showActiveCall {
                appState.selectedTab = .live
            }
        }
        .fullScreenCover(isPresented: $callManager.isOnCall, onDismiss: {
            // When the in-call screen closes, clear state and go to Recents
            appState.clearActiveCall()
            appState.selectedTab = .recents
        }) {
            InCallView()
        }
        // Force paywall when trial expires — cannot be dismissed without subscribing
        .fullScreenCover(isPresented: $showForcedPaywall) {
            PaywallView(canDismiss: false)
                .environmentObject(appState)
        }
        .onAppear {
            showForcedPaywall = appState.subscriptionStatus == "expired"
        }
        .onChange(of: appState.subscriptionStatus) {
            if appState.subscriptionStatus == "expired" {
                showForcedPaywall = true
            } else {
                showForcedPaywall = false
            }
        }
        // Re-auth alert when token is invalid (e.g. app reinstalled, Keychain cleared)
        .alert("Session Expired", isPresented: $appState.needsReauth) {
            Button("Sign In Again") {
                appState.needsReauth = false
                appState.isOnboarded = false
            }
            Button("Later", role: .cancel) {
                appState.needsReauth = false
            }
        } message: {
            Text("Your session has expired. Sign in again to continue.")
        }
    }
}

// MARK: - Live Call Tab

struct LiveCallTab: View {
    @EnvironmentObject var appState: AppState
    @State private var timer: Timer?
    @State private var elapsed: TimeInterval = 0
    @State private var elapsedTimer: Timer?
    @State private var pickingUp = false
    @State private var showTextReplySheet = false
    @State private var customMessage = ""
    @State private var sendingReply = false

    var body: some View {
        NavigationStack {
            if appState.hasActiveCall {
                activeCallContent
            } else {
                emptyState
            }
        }
        .onAppear {
            if appState.hasActiveCall {
                startPolling()
                startElapsedTimer()
            }
        }
        .onDisappear {
            stopPolling()
            elapsedTimer?.invalidate()
            elapsedTimer = nil
        }
        .onChange(of: appState.hasActiveCall) {
            if appState.hasActiveCall {
                startPolling()
                startElapsedTimer()
            } else {
                stopPolling()
                elapsedTimer?.invalidate()
            }
        }
    }

    // MARK: - Empty State

    private var emptyState: some View {
        VStack(spacing: 16) {
            Spacer()

            Image(systemName: "phone.badge.checkmark")
                .font(.system(size: 48))
                .foregroundStyle(.tertiary)

            Text(String(localized: "No Active Call"))
                .font(.title3.weight(.medium))
                .foregroundStyle(.secondary)

            Text(String(localized: "When someone calls, Kevin will screen it\nand the live transcript will appear here."))
                .font(.subheadline)
                .foregroundStyle(.tertiary)
                .multilineTextAlignment(.center)

            Spacer()
        }
        .navigationTitle(String(localized: "Live"))
    }

    // MARK: - Active Call Content

    private var activeCallContent: some View {
        VStack(spacing: 0) {
            callerHeader

            transcript
                .frame(maxHeight: .infinity)

            actionButtons
                .padding(.horizontal, HKSpace.lg)
                .padding(.top, HKSpace.md)
                .padding(.bottom, HKSpace.lg)
                .background(.ultraThinMaterial)
                .overlay(alignment: .top) {
                    Rectangle()
                        .fill(Color(.systemGray5).opacity(0.7))
                        .frame(height: 0.5)
                }
        }
        .background(Color(.systemGroupedBackground))
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .principal) {
                HStack(spacing: 6) {
                    if appState.callIgnored {
                        HKStatusDot(color: .hkOrange)
                        Text(String(localized: "Taking a message"))
                            .font(.system(size: 14, weight: .semibold))
                            .foregroundStyle(.hkOrange)
                    } else {
                        HKPulseDot(color: .hkGreen, size: 7)
                        Text(String(localized: "Live"))
                            .font(.system(size: 14, weight: .semibold))
                            .foregroundStyle(.hkGreen)
                    }
                }
            }

            ToolbarItem(placement: .topBarTrailing) {
                Text(formattedElapsed)
                    .font(.system(size: 14, weight: .medium))
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }
        }
    }

    // MARK: - Caller Strip
    // Compact horizontal strip. Phone number is the primary identifier when the
    // caller is not in contacts, matching the iOS Phone app convention.

    private var callerHeader: some View {
        HStack(spacing: HKSpace.md) {
            HKAvatar(
                name: appState.activeCallerName,
                phone: appState.activeCallerPhone,
                size: 40
            )
            VStack(alignment: .leading, spacing: 2) {
                Text(callerPrimaryIdentifier)
                    .font(.system(size: 17, weight: .semibold))
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .monospacedDigit()
                Text(callerSecondaryLine)
                    .font(.system(size: 13))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, HKSpace.xl)
        .padding(.top, 10)
        .padding(.bottom, 14)
        .overlay(alignment: .bottom) {
            Rectangle()
                .fill(Color(.systemGray5).opacity(0.7))
                .frame(height: 0.5)
        }
    }

    private var callerPrimaryIdentifier: String {
        let name = appState.activeCallerName.trimmingCharacters(in: .whitespaces)
        if !name.isEmpty { return name }
        let phone = appState.activeCallerPhone.trimmingCharacters(in: .whitespaces)
        return phone.isEmpty ? String(localized: "Unknown") : PhoneFormatter.format(phone)
    }

    private var callerSecondaryLine: String {
        let hasName = !appState.activeCallerName.isEmpty
        if hasName {
            return formattedPhone
        }
        if appState.callIgnored {
            return String(localized: "Kevin is taking a message")
        }
        return String(localized: "Unknown caller")
    }

    // MARK: - Transcript (Chat Bubbles)

    private var transcript: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(spacing: 6) {
                    if appState.transcriptLines.isEmpty {
                        VStack(spacing: 8) {
                            ProgressView()
                            Text(String(localized: "Waiting for conversation..."))
                                .font(.subheadline)
                                .foregroundStyle(.tertiary)
                        }
                        .padding(.top, 40)
                    }

                    ForEach(appState.transcriptLines) { line in
                        ChatBubble(line: line.text)
                            .id(line.id)
                    }
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
            }
            .onChange(of: appState.transcriptLines.count) {
                if let last = appState.transcriptLines.last {
                    withAnimation(.easeOut(duration: 0.2)) {
                        proxy.scrollTo(last.id, anchor: .bottom)
                    }
                }
            }
        }
    }

    // MARK: - Action Buttons

    @ViewBuilder
    private var actionButtons: some View {
        if appState.callIgnored {
            // Call ignored — Kevin is taking a message. Single Dismiss action.
            Button {
                appState.clearActiveCall()
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: "xmark")
                        .font(.system(size: 15, weight: .semibold))
                    Text(String(localized: "Dismiss"))
                }
            }
            .buttonStyle(HKSecondaryButtonStyle(tint: .secondary))
        } else {
            VStack(spacing: 10) {
                Button {
                    pickUp()
                } label: {
                    HStack(spacing: 8) {
                        if pickingUp {
                            ProgressView()
                                .tint(.white)
                        } else {
                            Image(systemName: "phone.fill")
                                .font(.system(size: 17, weight: .bold))
                        }
                        Text(String(localized: "Pick up"))
                    }
                }
                .buttonStyle(HKPrimaryButtonStyle(tint: .hkGreen))
                .disabled(pickingUp)

                if kTextReplyEnabled {
                    HStack(spacing: 10) {
                        Button {
                            showTextReplySheet = true
                        } label: {
                            HStack(spacing: 6) {
                                Image(systemName: "message.fill")
                                    .font(.system(size: 14))
                                Text(String(localized: "Text reply"))
                            }
                        }
                        .buttonStyle(HKSecondaryButtonStyle(tint: .hkBlue))
                        .disabled(pickingUp)

                        Button(role: .destructive) {
                            ignore()
                        } label: {
                            HStack(spacing: 6) {
                                Image(systemName: "xmark")
                                    .font(.system(size: 14, weight: .semibold))
                                Text(String(localized: "Ignore"))
                            }
                        }
                        .buttonStyle(HKDestructiveButtonStyle())
                        .disabled(pickingUp)
                    }
                    .sheet(isPresented: $showTextReplySheet) {
                        TextReplySheet(
                            callSid: appState.activeCallSid,
                            callerPhone: formattedPhone,
                            sendingReply: $sendingReply,
                            onSend: { message in
                                sendTextReply(message)
                            }
                        )
                        .presentationDetents([.medium])
                    }
                } else {
                    Button(role: .destructive) {
                        ignore()
                    } label: {
                        HStack(spacing: 6) {
                            Image(systemName: "xmark")
                                .font(.system(size: 14, weight: .semibold))
                            Text(String(localized: "Ignore"))
                        }
                    }
                    .buttonStyle(HKDestructiveButtonStyle())
                    .disabled(pickingUp)
                }
            }
        }
    }

    // MARK: - Helpers

    private var callerInitials: String {
        let name = appState.activeCallerName
        guard !name.isEmpty else { return "" }
        let parts = name.split(separator: " ")
        if parts.count >= 2 {
            return "\(parts[0].prefix(1))\(parts[1].prefix(1))".uppercased()
        }
        return String(name.prefix(2)).uppercased()
    }

    private var formattedPhone: String {
        let phone = appState.activeCallerPhone
        guard !phone.isEmpty else { return String(localized: "Unknown") }
        return PhoneFormatter.format(phone)
    }

    private var formattedElapsed: String {
        let minutes = Int(elapsed) / 60
        let seconds = Int(elapsed) % 60
        return String(format: "%d:%02d", minutes, seconds)
    }

    // MARK: - Actions

    private func pickUp() {
        guard !pickingUp else { return }
        pickingUp = true

        // Tell backend to move caller to conference, get token to join directly
        Task {
            if let response = await APIClient.shared.sendCallAction(
                callSid: appState.activeCallSid, action: "accept"
            ) {
                let accessToken = response["access_token"] as? String ?? ""
                let conferenceName = response["conference_name"] as? String ?? ""

                if !accessToken.isEmpty && !conferenceName.isEmpty {
                    // Connect via Twilio Voice SDK
                    await MainActor.run {
                        CallManager.shared.callerName = appState.activeCallerName
                        CallManager.shared.callerPhone = appState.activeCallerPhone
                        CallManager.shared.connectDirectly(
                            accessToken: accessToken,
                            conferenceName: conferenceName
                        )
                    }
                }
            }
            await MainActor.run { pickingUp = false }
        }
    }

    private func sendTextReply(_ message: String) {
        sendingReply = true
        Task {
            _ = await APIClient.shared.sendCallAction(
                callSid: appState.activeCallSid, action: "text_reply", message: message
            )
            await MainActor.run {
                sendingReply = false
                showTextReplySheet = false
            }
        }
    }

    private func ignore() {
        Task {
            _ = await APIClient.shared.sendCallAction(
                callSid: appState.activeCallSid, action: "decline"
            )
        }
        // Don't clear the call — Kevin continues taking a message.
        // Just mark as ignored so the UI updates (hide pick up, change status).
        appState.callIgnored = true
    }

    // MARK: - Polling

    @State private var isPolling = false

    private func startPolling() {
        stopPolling()
        timer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { _ in
            // Skip if previous poll is still in-flight — prevents task accumulation
            guard !isPolling else { return }
            Task {
                isPolling = true
                await poll()
                isPolling = false
            }
        }
    }

    private func stopPolling() {
        timer?.invalidate()
        timer = nil
        isPolling = false
    }

    private func startElapsedTimer() {
        elapsedTimer?.invalidate()
        if let start = appState.callStartTime {
            elapsed = Date().timeIntervalSince(start)
        }
        elapsedTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { _ in
            if let start = appState.callStartTime {
                elapsed = Date().timeIntervalSince(start)
            }
        }
    }

    private func poll() async {
        let sid = await MainActor.run { appState.activeCallSid }
        guard !sid.isEmpty else { return }

        // Check if the call is still active — if not, clear the live screen
        let activeCall = await APIClient.shared.getActiveCall()
        if activeCall == nil {
            await MainActor.run {
                appState.clearActiveCall()
                appState.selectedTab = .recents
            }
            return
        }

        if let t = await APIClient.shared.getTranscript(callSid: sid) {
            let lines = t.components(separatedBy: "\n")
                .filter { !$0.isEmpty }
                .map { TranscriptLine(text: $0) }
            await MainActor.run { appState.transcriptLines = lines }
        }
    }
}

// MARK: - Text Reply Sheet

struct TextReplySheet: View {
    let callSid: String
    let callerPhone: String
    @Binding var sendingReply: Bool
    let onSend: (String) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var customMessage = ""
    @FocusState private var isCustomFocused: Bool

    private let quickReplies = [
        String(localized: "Can't talk right now. What's up?"),
        String(localized: "I'll call you back in a few minutes."),
        String(localized: "Sorry, I'm busy. I'll get back to you soon."),
    ]

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                Text(String(localized: "to \(callerPhone)"))
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .padding(.top, 4)
                    .padding(.bottom, 12)

                VStack(spacing: 8) {
                    ForEach(quickReplies, id: \.self) { reply in
                        Button {
                            onSend(reply)
                        } label: {
                            Text(reply)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(.horizontal, 14)
                                .padding(.vertical, 12)
                        }
                        .buttonStyle(.bordered)
                        .clipShape(RoundedRectangle(cornerRadius: 12))
                        .disabled(sendingReply)
                    }
                }
                .padding(.horizontal, 20)

                Divider()
                    .padding(.vertical, 12)

                HStack(spacing: 10) {
                    TextField(String(localized: "Type a message..."), text: $customMessage)
                        .textFieldStyle(.roundedBorder)
                        .focused($isCustomFocused)

                    Button {
                        guard !customMessage.trimmingCharacters(in: .whitespaces).isEmpty else { return }
                        onSend(customMessage)
                    } label: {
                        if sendingReply {
                            ProgressView()
                                .frame(width: 32, height: 32)
                        } else {
                            Image(systemName: "arrow.up.circle.fill")
                                .font(.title2)
                        }
                    }
                    .disabled(customMessage.trimmingCharacters(in: .whitespaces).isEmpty || sendingReply)
                }
                .padding(.horizontal, 20)

                Spacer()
            }
            .navigationTitle(String(localized: "Text Reply"))
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button(String(localized: "Cancel")) { dismiss() }
                }
            }
        }
    }
}

// MARK: - Chat Bubble

struct ChatBubble: View {
    let line: String

    private var speaker: String {
        if line.hasPrefix("Caller:") { return "Caller" }
        if line.hasPrefix("Kevin:") { return "Kevin" }
        return ""
    }

    private var speakerLabel: String {
        speaker.isEmpty ? String(localized: "System") : speaker
    }

    private var text: String {
        if let range = line.range(of: ": ") {
            return String(line[range.upperBound...])
        }
        return line
    }

    private var isKevin: Bool { speaker == "Kevin" }

    var body: some View {
        HStack(alignment: .bottom, spacing: 8) {
            if isKevin { Spacer(minLength: 48) }

            VStack(alignment: isKevin ? .trailing : .leading, spacing: 4) {
                Text(speakerLabel.uppercased())
                    .font(.system(size: 11, weight: .semibold))
                    .tracking(0.6)
                    .foregroundStyle(isKevin ? Color.hkBlue : Color.secondary)
                    .padding(.horizontal, 4)

                Text(text)
                    .font(.system(size: 15))
                    .lineSpacing(2)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 9)
                    .background(
                        isKevin
                            ? Color.hkBlue.opacity(0.10)
                            : Color.hkSurface
                    )
                    .foregroundStyle(.primary)
                    .clipShape(
                        UnevenRoundedRectangle(
                            cornerRadii: .init(
                                topLeading: HKRadius.bubble,
                                bottomLeading: isKevin ? HKRadius.bubble : 6,
                                bottomTrailing: isKevin ? 6 : HKRadius.bubble,
                                topTrailing: HKRadius.bubble
                            ),
                            style: .continuous
                        )
                    )
            }

            if !isKevin { Spacer(minLength: 48) }
        }
    }
}
