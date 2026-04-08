import SwiftUI

struct InCallView: View {
    @ObservedObject var callManager = CallManager.shared
    @State private var elapsed: TimeInterval = 0
    @State private var timer: Timer?

    var body: some View {
        ZStack {
            // Dark background
            Color(red: 0.07, green: 0.07, blue: 0.07)
                .ignoresSafeArea()

            VStack(spacing: 0) {
                Spacer()
                    .frame(height: 60)

                // Caller avatar
                ZStack {
                    Circle()
                        .fill(Color(.systemGray3))
                        .frame(width: 96, height: 96)

                    if !callerInitials.isEmpty {
                        Text(callerInitials)
                            .font(.system(size: 36, weight: .medium))
                            .foregroundStyle(.white)
                    } else {
                        Image(systemName: "person.fill")
                            .font(.system(size: 36))
                            .foregroundStyle(.white)
                    }
                }

                // Caller name
                Text(displayName)
                    .font(.title2.weight(.semibold))
                    .foregroundStyle(.white)
                    .padding(.top, 16)

                // Phone number (if name is available, show number below)
                if !callManager.callerName.isEmpty && !callManager.callerPhone.isEmpty {
                    Text(PhoneFormatter.format(callManager.callerPhone))
                        .font(.subheadline)
                        .foregroundStyle(.white.opacity(0.6))
                        .padding(.top, 2)
                }

                // Call duration
                Text(formattedElapsed)
                    .font(.subheadline.monospacedDigit())
                    .foregroundStyle(.white.opacity(0.6))
                    .padding(.top, 8)

                Spacer()

                // Control buttons
                HStack(spacing: 48) {
                    // Mute
                    CallControlButton(
                        icon: callManager.isMuted ? "mic.slash.fill" : "mic.fill",
                        label: String(localized: "Mute"),
                        isActive: callManager.isMuted
                    ) {
                        callManager.toggleMute()
                    }

                    // Speaker
                    CallControlButton(
                        icon: callManager.isSpeaker ? "speaker.wave.3.fill" : "speaker.fill",
                        label: String(localized: "Speaker"),
                        isActive: callManager.isSpeaker
                    ) {
                        callManager.toggleSpeaker()
                    }
                }
                .padding(.bottom, 48)

                // End call button
                Button {
                    callManager.endCall()
                } label: {
                    ZStack {
                        Circle()
                            .fill(.red)
                            .frame(width: 72, height: 72)

                        Image(systemName: "phone.down.fill")
                            .font(.system(size: 28))
                            .foregroundStyle(.white)
                    }
                }
                .padding(.bottom, 12)

                Text(String(localized: "End"))
                    .font(.caption)
                    .foregroundStyle(.white.opacity(0.6))

                Spacer()
                    .frame(height: 48)
            }
        }
        .onAppear { startTimer() }
        .onDisappear { stopTimer() }
        .onChange(of: callManager.callStartTime) {
            startTimer()
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
