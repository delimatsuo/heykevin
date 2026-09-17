import SwiftUI

struct InCallView: View {
    let lease: CallPresentationLease
    @EnvironmentObject var appState: AppState
    @ObservedObject var callManager = CallManager.shared
    @State private var elapsed: TimeInterval = 0
    @State private var timer: Timer?

    var body: some View {
        let auth = appState.currentAuthContext()
        let scope = appState.callLifecycleSnapshot

        ZStack {
            // Dark background
            Color(red: 0.07, green: 0.07, blue: 0.07)
                .ignoresSafeArea()

            if lease.isValid(auth: auth, scope: scope) {
                let snapshot = appState.screeningTranscript(for: lease)
                let lines = snapshot?.lines ?? []

                VStack(spacing: 0) {
                    // Compact Caller Header
                    compactHeader
                        .padding(.top, 16)
                        .padding(.horizontal, 20)
                        .padding(.bottom, 12)

                    Divider()
                        .background(Color.white.opacity(0.12))

                    // Frozen Screening Transcript Area (occupies available space)
                    VStack(alignment: .leading, spacing: 10) {
                        HStack {
                            Text(String(localized: "Before you joined"))
                                .font(.caption.weight(.semibold))
                                .textCase(.uppercase)
                                .tracking(0.6)
                                .foregroundStyle(Color.white.opacity(0.6))
                                .accessibilityIdentifier("incall.section.beforeJoined")
                            Spacer()
                        }
                        .padding(.horizontal, 20)
                        .padding(.top, 12)

                        if lines.isEmpty {
                            VStack(spacing: 8) {
                                Spacer()
                                Text(String(localized: "No screening transcript was captured before you joined."))
                                    .font(.subheadline)
                                    .foregroundStyle(Color.white.opacity(0.5))
                                    .multilineTextAlignment(.center)
                                    .padding(.horizontal, 24)
                                    .accessibilityIdentifier("incall.emptyTranscript")
                                Spacer()
                            }
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                        } else {
                            ScrollView {
                                VStack(spacing: 10) {
                                    ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                                        InCallFrozenTranscriptBubble(text: line)
                                    }
                                }
                                .padding(.horizontal, 20)
                                .padding(.vertical, 8)
                            }
                        }
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)

                    Divider()
                        .background(Color.white.opacity(0.12))

                    // Persistent Call Controls (Outside scroll)
                    callControls
                        .padding(.top, 16)
                        .padding(.bottom, 32)
                }
            } else {
                VStack {
                    Spacer()
                    Text(String(localized: "Call is no longer active."))
                        .font(.headline)
                        .foregroundStyle(.white.opacity(0.7))
                    Spacer()
                }
            }
        }
        .accessibilityIdentifier("incall.view")
        .onAppear { startTimer() }
        .onDisappear { stopTimer() }
        .onChange(of: callManager.callStartTime) {
            startTimer()
        }
    }

    // MARK: - Compact Header

    private var compactHeader: some View {
        HStack(spacing: 14) {
            ZStack {
                Circle()
                    .fill(Color(.systemGray3))
                    .frame(width: 48, height: 48)

                if !callerInitials.isEmpty {
                    Text(callerInitials)
                        .font(.system(size: 18, weight: .semibold))
                        .foregroundStyle(.white)
                } else {
                    Image(systemName: "person.fill")
                        .font(.system(size: 20))
                        .foregroundStyle(.white)
                }
            }

            VStack(alignment: .leading, spacing: 2) {
                Text(displayName)
                    .font(.headline.weight(.semibold))
                    .foregroundStyle(.white)
                    .lineLimit(1)

                if !callManager.callerName.isEmpty && !callManager.callerPhone.isEmpty {
                    Text(PhoneFormatter.format(callManager.callerPhone))
                        .font(.caption)
                        .foregroundStyle(.white.opacity(0.6))
                }
                if let reason = appState.screeningTranscript(for: lease)?.reason, !reason.isEmpty {
                    Text(reason)
                        .font(.caption)
                        .foregroundStyle(.white.opacity(0.7))
                        .lineLimit(2)
                }
            }

            Spacer()

            Text(formattedElapsed)
                .font(.subheadline.monospacedDigit().weight(.medium))
                .foregroundStyle(.white.opacity(0.7))
                .accessibilityIdentifier("incall.duration")
        }
    }

    // MARK: - Call Controls

    private var callControls: some View {
        VStack(spacing: 20) {
            HStack(spacing: 48) {
                // Mute
                CallControlButton(
                    icon: callManager.isMuted ? "mic.slash.fill" : "mic.fill",
                    label: String(localized: "Mute"),
                    isActive: callManager.isMuted
                ) {
                    callManager.toggleMute()
                }
                .accessibilityIdentifier("incall.mute")

                // Speaker
                CallControlButton(
                    icon: callManager.isSpeaker ? "speaker.wave.3.fill" : "speaker.fill",
                    label: String(localized: "Speaker"),
                    isActive: callManager.isSpeaker
                ) {
                    callManager.toggleSpeaker()
                }
                .accessibilityIdentifier("incall.speaker")
            }

            // End call button
            VStack(spacing: 6) {
                Button {
                    callManager.endCall()
                } label: {
                    ZStack {
                        Circle()
                            .fill(.red)
                            .frame(width: 68, height: 68)

                        Image(systemName: "phone.down.fill")
                            .font(.system(size: 26))
                            .foregroundStyle(.white)
                    }
                }
                .accessibilityIdentifier("incall.end")

                Text(String(localized: "End"))
                    .font(.caption)
                    .foregroundStyle(.white.opacity(0.6))
            }
        }
    }

    // MARK: - Helpers

    private var displayName: String {
        if !callManager.callerName.isEmpty {
            return callManager.callerName
        }
        if !callManager.callerPhone.isEmpty {
            return PhoneFormatter.format(callManager.callerPhone)
        }
        return String(localized: "Unknown")
    }

    private var callerInitials: String {
        let name = callManager.callerName
        guard !name.isEmpty else { return "" }
        let parts = name.split(separator: " ")
        if parts.count >= 2 {
            return "\(parts[0].prefix(1))\(parts[1].prefix(1))".uppercased()
        }
        return String(name.prefix(2)).uppercased()
    }

    private var formattedElapsed: String {
        let total = Int(elapsed)
        let hours = total / 3600
        let minutes = (total % 3600) / 60
        let seconds = total % 60
        if hours > 0 {
            return String(format: "%d:%02d:%02d", hours, minutes, seconds)
        }
        return String(format: "%d:%02d", minutes, seconds)
    }

    private func startTimer() {
        stopTimer()
        #if DEBUG
        if AppStoreScreenshotFixtures.isNativeUIReview {
            elapsed = 137
            return
        }
        #endif
        if let start = callManager.callStartTime {
            elapsed = Date().timeIntervalSince(start)
        }
        timer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { _ in
            if let start = callManager.callStartTime {
                elapsed = Date().timeIntervalSince(start)
            }
        }
    }

    private func stopTimer() {
        timer?.invalidate()
        timer = nil
    }
}

// MARK: - Call Control Button

struct CallControlButton: View {
    let icon: String
    let label: String
    var isActive: Bool = false
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            VStack(spacing: 8) {
                ZStack {
                    Circle()
                        .fill(isActive ? .white : .white.opacity(0.12))
                        .frame(width: 60, height: 60)

                    Image(systemName: icon)
                        .font(.system(size: 24))
                        .foregroundStyle(isActive ? .black : .white)
                }

                Text(label)
                    .font(.caption)
                    .foregroundStyle(.white.opacity(0.6))
            }
        }
    }
}

private struct InCallFrozenTranscriptBubble: View {
    let text: String

    private var isKevin: Bool {
        text.hasPrefix("Kevin:")
    }

    private var speakerName: String {
        if text.hasPrefix("Kevin:") { return "Kevin" }
        if text.hasPrefix("Caller:") { return "Caller" }
        return ""
    }

    private var bodyText: String {
        if let range = text.range(of: ": ") {
            return String(text[range.upperBound...])
        }
        return text
    }

    var body: some View {
        HStack(alignment: .bottom, spacing: 8) {
            if isKevin {
                Spacer(minLength: 28)
            }

            VStack(alignment: isKevin ? .trailing : .leading, spacing: 3) {
                if !speakerName.isEmpty {
                    Text(speakerName)
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(Color.white.opacity(0.6))
                }

                Text(bodyText)
                    .font(.subheadline)
                    .foregroundStyle(.white)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 8)
                    .background(
                        isKevin ? Color.accentColor : Color.white.opacity(0.12),
                        in: RoundedRectangle(cornerRadius: 14, style: .continuous)
                    )
            }
            .frame(maxWidth: 280, alignment: isKevin ? .trailing : .leading)

            if !isKevin {
                Spacer(minLength: 28)
            }
        }
        .frame(maxWidth: .infinity, alignment: isKevin ? .trailing : .leading)
    }
}
