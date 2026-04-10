import SwiftUI

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
            // Caller header
            callerHeader
                .padding(.top, 8)
                .padding(.bottom, 16)

            Divider()

            // Transcript — Kevin screening conversation
            transcript
                .frame(maxHeight: .infinity)

            Divider()

            // Action buttons
            actionButtons
                .padding(.horizontal, 20)
                .padding(.vertical, 16)
                .background(.ultraThinMaterial)
        }
        .background(Color(.systemGroupedBackground))
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .principal) {
                HStack(spacing: 6) {
                    Circle()
                        .fill(.green)
                        .frame(width: 7, height: 7)
                    Text(String(localized: "Live"))
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(.green)
                }
            }

            ToolbarItem(placement: .topBarTrailing) {
                Text(formattedElapsed)
                    .font(.subheadline.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
        }
    }

    // MARK: - Caller Header

    private var callerHeader: some View {
        VStack(spacing: 8) {
            ZStack {
                Circle()
                    .fill(Color(.systemGray4))
                    .frame(width: 64, height: 64)

                if !callerInitials.isEmpty {
                    Text(callerInitials)
                        .font(.title2.weight(.medium))
                        .foregroundStyle(.white)
                } else {
                    Image(systemName: "phone.fill")
                        .font(.title3)
                        .foregroundStyle(.white)
                }
            }

            if !appState.activeCallerName.isEmpty {
                Text(appState.activeCallerName)
                    .font(.title3.weight(.semibold))

                Text(formattedPhone)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            } else {
                Text(formattedPhone)
                    .font(.title3.weight(.semibold))
            }

            HStack(spacing: 6) {
                Circle()
                    .fill(appState.isOnCall ? .green : (appState.callIgnored ? .orange : .green))
                    .frame(width: 6, height: 6)
                Text(appState.isOnCall ? String(localized: "Connected") : (appState.callIgnored ? String(localized: "Kevin is taking a message") : String(localized: "Kevin is screening")))
                    .font(.caption)
                    .foregroundStyle(appState.isOnCall ? .green : (appState.callIgnored ? .orange : .green))
            }
        }
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
            // Call ignored — Kevin is taking a message
            VStack(spacing: 10) {
                Button {
                    appState.clearActiveCall()
                } label: {
                    HStack(spacing: 8) {
                        Image(systemName: "xmark")
                        Text(String(localized: "Dismiss"))
                            .fontWeight(.semibold)
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 14)
                }
                .buttonStyle(.bordered)
                .clipShape(RoundedRectangle(cornerRadius: 14))
            }
        } else {
            // Screening state — Pick Up / Text Reply / Ignore
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
                        }
                        Text(String(localized: "Pick Up"))
                            .fontWeight(.semibold)
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 14)
                }
                .buttonStyle(.borderedProminent)
                .tint(.green)
                .clipShape(RoundedRectangle(cornerRadius: 14))
                .disabled(pickingUp)

                HStack(spacing: 10) {
                    Button {
                        showTextReplySheet = true
                    } label: {
                        HStack(spacing: 6) {
                            Image(systemName: "message.fill")
                            Text(String(localized: "Text Reply"))
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 12)
                    }
                    .buttonStyle(.bordered)
                    .tint(.blue)
                    .clipShape(RoundedRectangle(cornerRadius: 12))
                    .disabled(pickingUp)

                    Button(role: .destructive) {
                        ignore()
                    } label: {
                        HStack(spacing: 6) {
                            Image(systemName: "hand.raised.fill")
                            Text(String(localized: "Ignore"))
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 12)
                    }
                    .buttonStyle(.bordered)
                    .clipShape(RoundedRectangle(cornerRadius: 12))
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

            VStack(alignment: isKevin ? .trailing : .leading, spacing: 3) {
                Text(speaker.isEmpty ? String(localized: "System") : speaker)
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(isKevin ? .blue : .secondary)
                    .padding(.horizontal, 4)

                Text(text)
                    .font(.subheadline)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 9)
                    .background(
                        isKevin
                            ? Color.blue.opacity(0.12)
                            : Color(.systemGray5)
                    )
                    .foregroundStyle(.primary)
                    .clipShape(
                        RoundedRectangle(cornerRadius: 18, style: .continuous)
                    )
            }

            if !isKevin { Spacer(minLength: 48) }
        }
    }
}
