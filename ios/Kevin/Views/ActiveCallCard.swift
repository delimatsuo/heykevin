import SwiftUI

/// Prominent dark active call card displayed at the top of the Calls tab.
/// Backed by the actual live transcript, coordinator states, and captured presentation lease.
struct ActiveCallCard: View {
    @EnvironmentObject var appState: AppState
    @ObservedObject var coordinator = CallActionCoordinator.shared

    var body: some View {
        let sid = appState.activeCallSid
        let auth = appState.currentAuthContext()

        VStack(alignment: .leading, spacing: HKSpace.md) {
            // Status and Elapsed Header
            HStack(spacing: 8) {
                if appState.callIgnored || coordinator.isTakingMessage(for: sid) {
                    HKStatusDot(color: .hkOrange, size: 8)
                    Text(String(localized: "Taking a message"))
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(Color.hkOrange)
                } else if coordinator.isDeclinePending(for: sid) {
                    ProgressView()
                        .tint(.white)
                        .scaleEffect(0.7)
                    Text(String(localized: "Requesting a message"))
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(Color.hkOrange)
                } else if coordinator.isAcceptPending(for: sid) {
                    ProgressView()
                        .tint(.white)
                        .scaleEffect(0.7)
                    Text(String(localized: "Connecting"))
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(Color.hkGreen)
                } else {
                    HKPulseDot(color: .hkGreen, size: 7)
                    Text(String(localized: "Live"))
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(Color.hkGreen)
                }

                if coordinator.isUrgent(for: sid) {
                    Text(String(localized: "Urgent"))
                        .font(.system(size: 10, weight: .bold))
                        .tracking(0.6)
                        .foregroundStyle(.white)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(Color.hkRed, in: RoundedRectangle(cornerRadius: 4))
                }

                Spacer()

                TimelineView(.periodic(from: .now, by: 1.0)) { _ in
                    Text(formattedElapsed)
                        .font(.system(size: 13, weight: .medium))
                        .monospacedDigit()
                        .foregroundStyle(Color.white.opacity(0.7))
                }
            }
            .accessibilityElement(children: .combine)

            // Caller Identity
            HStack(spacing: HKSpace.md) {
                HKAvatar(
                    name: appState.activeCallerName,
                    phone: appState.activeCallerPhone,
                    size: 42
                )

                VStack(alignment: .leading, spacing: 2) {
                    Text(callerDisplayName)
                        .font(.system(size: 17, weight: .semibold))
                        .foregroundStyle(.white)
                        .lineLimit(1)
                        .truncationMode(.tail)

                    Text(callerSubtitle)
                        .font(.system(size: 13))
                        .foregroundStyle(Color.white.opacity(0.7))
                        .lineLimit(1)
                }
                Spacer(minLength: 0)
            }

            // Live Transcript Excerpt
            if let latestLine = latestTranscriptLine {
                Text(latestLine)
                    .font(.system(size: 14))
                    .foregroundStyle(Color.white.opacity(0.9))
                    .lineLimit(2)
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.white.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
            }

            // Coordinator Error Message
            if let errorMsg = coordinator.errorMessage(for: sid) {
                HStack(spacing: 6) {
                    Image(systemName: "exclamationmark.circle.fill")
                        .foregroundStyle(.red)
                    Text(errorMsg)
                        .font(.footnote)
                        .foregroundStyle(.red)
                }
            }

            // Actions
            if appState.callIgnored || coordinator.isTakingMessage(for: sid) {
                Button {
                    appState.clearActiveCall()
                    StoreReviewManager.shared.incrementScreenedCallCount()
                    StoreReviewManager.shared.requestReviewIfEligible()
                } label: {
                    HStack(spacing: 6) {
                        Image(systemName: "xmark")
                            .font(.system(size: 14, weight: .semibold))
                        Text(String(localized: "Dismiss"))
                    }
                    .frame(maxWidth: .infinity, minHeight: 44)
                }
                .buttonStyle(.bordered)
                .tint(.white)
                .accessibilityIdentifier("call.dismiss")
            } else {
                VStack(spacing: 8) {
                    if coordinator.canCheckStatus(for: sid) {
                        Button(String(localized: "Check status")) {
                            Task { _ = await coordinator.checkStatus(callSid: sid) }
                        }
                        .buttonStyle(HKSecondaryButtonStyle(tint: .white))
                        .accessibilityIdentifier("call.checkStatus")
                    }

                    HStack(spacing: 10) {
                        Button {
                            Task { _ = await coordinator.pickUp(callSid: sid, authContext: auth) }
                        } label: {
                            HStack(spacing: 6) {
                                if coordinator.isAcceptPending(for: sid) {
                                    ProgressView()
                                        .tint(.white)
                                } else {
                                    Image(systemName: "phone.fill")
                                        .font(.system(size: 15, weight: .bold))
                                }
                                Text(String(localized: "Pick up"))
                            }
                            .frame(maxWidth: .infinity, minHeight: 46)
                        }
                        .buttonStyle(HKPrimaryButtonStyle(tint: .hkGreen))
                        .disabled(coordinator.isActionPending(for: sid))
                        .accessibilityIdentifier("call.pickup")

                        Button(role: .destructive) {
                            Task { _ = await coordinator.takeMessage(callSid: sid, authContext: auth) }
                        } label: {
                            HStack(spacing: 6) {
                                if coordinator.isDeclinePending(for: sid) {
                                    ProgressView()
                                        .tint(.white)
                                        .scaleEffect(0.8)
                                } else {
                                    Image(systemName: "xmark")
                                        .font(.system(size: 14, weight: .semibold))
                                }
                                Text(String(localized: "Take a message"))
                            }
                            .frame(maxWidth: .infinity, minHeight: 46)
                        }
                        .buttonStyle(HKDestructiveButtonStyle())
                        .disabled(coordinator.isActionPending(for: sid))
                        .accessibilityIdentifier("call.message")
                    }
                }
            }
        }
        .padding(HKSpace.lg)
        .background(
            RoundedRectangle(cornerRadius: HKRadius.card, style: .continuous)
                .fill(Color.hkActiveDark)
        )
        .overlay(
            RoundedRectangle(cornerRadius: HKRadius.card, style: .continuous)
                .stroke(Color.white.opacity(0.12), lineWidth: 1)
        )
        .shadow(color: Color.black.opacity(0.18), radius: 10, y: 4)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("call.activeCard")
    }

    private var callerDisplayName: String {
        let name = appState.activeCallerName.trimmingCharacters(in: .whitespacesAndNewlines)
        if !name.isEmpty { return name }
        let phone = appState.activeCallerPhone.trimmingCharacters(in: .whitespacesAndNewlines)
        return phone.isEmpty ? String(localized: "Unknown Caller") : PhoneFormatter.format(phone)
    }

    private var callerSubtitle: String {
        if !appState.activeCallerName.isEmpty {
            return PhoneFormatter.format(appState.activeCallerPhone)
        }
        return String(localized: "Kevin is screening")
    }

    private var latestTranscriptLine: String? {
        guard let line = appState.transcriptLines.last?.text, !line.isEmpty else { return nil }
        return line
    }

    private var formattedElapsed: String {
        #if DEBUG
        if AppStoreScreenshotFixtures.isEnabled {
            return "2:17"
        }
        #endif
        guard let start = appState.callStartTime else { return "0:00" }
        let elapsed = max(0, Date().timeIntervalSince(start))
        let minutes = Int(elapsed) / 60
        let seconds = Int(elapsed) % 60
        return String(format: "%d:%02d", minutes, seconds)
    }
}

/// Compact return to call banner for Kevin tab and Account sheet.
struct CompactReturnToCallCard: View {
    let onTap: () -> Void

    var body: some View {
        Button(action: onTap) {
            HStack(spacing: 10) {
                HKPulseDot(color: .hkGreen, size: 6)

                Text(String(localized: "Active Call in Progress"))
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(.white)

                Spacer()

                HStack(spacing: 4) {
                    Text(String(localized: "Return to call"))
                        .font(.system(size: 13, weight: .medium))
                    Image(systemName: "chevron.right")
                        .font(.system(size: 11, weight: .semibold))
                }
                .foregroundStyle(Color.hkGreen)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .background(Color.hkActiveDark, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .stroke(Color.white.opacity(0.12), lineWidth: 1)
            )
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("call.return")
    }
}
