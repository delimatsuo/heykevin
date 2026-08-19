import SwiftUI

/// One-time "what's new" sheet shown after updating to a release that adds a
/// capability users would otherwise never find.
///
/// Appointment Confirm lives three taps deep (Recents → a call → Details), so
/// without this the feature ships invisible. The bigger constraint is
/// activation rather than awareness: Confirm writes to Google Calendar, and
/// almost no one has connected one, so the primary action is the connection —
/// not a "Got it" that teaches nothing.
///
/// The CTA routes to Settings rather than starting OAuth here. The connect flow
/// validates the authorize URL (https + google.com host) before opening it, and
/// a second copy of that check is a second thing to drift.
struct WhatsNewSheet: View {
    /// Bump the suffix when a future release has something worth announcing.
    /// Keying on the release rather than a bool lets 1.2.8's sheet show to
    /// someone who already dismissed an earlier one.
    static let seenKey = "whatsNewSeen_1_2_8"

    @EnvironmentObject var appState: AppState
    @Environment(\.dismiss) private var dismiss

    /// Whether to nudge the Google Calendar connection. Callers pass the live
    /// value so a contractor who already connected gets an acknowledgement
    /// instead of a nudge to do what they have done.
    let needsCalendar: Bool

    var body: some View {
        VStack(spacing: 24) {
            Spacer(minLength: 8)

            Image(systemName: "calendar.badge.checkmark")
                .font(.system(size: 52))
                .foregroundStyle(.tint)
                .accessibilityHidden(true)

            VStack(spacing: 12) {
                Text(String(localized: "Kevin can now book appointments"))
                    .font(.title2.bold())
                    .multilineTextAlignment(.center)

                Text(String(localized: "When a caller asks for a time, Kevin takes down the request. You review it in Recents and tap Confirm — Kevin adds it to your Google Calendar and texts the caller to confirm."))
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)

                Text(String(localized: "Kevin never books anything without your tap."))
                    .font(.footnote.weight(.medium))
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }
            .padding(.horizontal, 8)

            Spacer(minLength: 8)

            VStack(spacing: 12) {
                if needsCalendar {
                    Button {
                        markSeen()
                        appState.selectedTab = .settings
                        dismiss()
                    } label: {
                        Text(String(localized: "Connect Google Calendar"))
                            .font(.headline)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 14)
                    }
                    .buttonStyle(.borderedProminent)

                    Button {
                        markSeen()
                        dismiss()
                    } label: {
                        Text(String(localized: "Not now"))
                            .font(.subheadline)
                    }
                } else {
                    Button {
                        markSeen()
                        dismiss()
                    } label: {
                        Text(String(localized: "Got it"))
                            .font(.headline)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 14)
                    }
                    .buttonStyle(.borderedProminent)
                }
            }
        }
        .padding(24)
        .presentationDetents([.medium, .large])
        // Dismissing by swipe still counts as seen. Re-showing an announcement
        // someone swiped away reads as a bug, not a reminder.
        .onDisappear(perform: markSeen)
    }

    private func markSeen() {
        UserDefaults.standard.set(true, forKey: Self.seenKey)
    }
}

extension WhatsNewSheet {
    /// Whether this build should announce itself to this user.
    ///
    /// Deliberately not shown to someone still onboarding — the onboarding flow
    /// introduces the product already, and interrupting it to announce a
    /// feature is noise. Screenshot fixtures suppress it so App Store captures
    /// are not covered by a sheet.
    static func shouldPresent(isOnboarded: Bool) -> Bool {
        guard !AppStoreScreenshotFixtures.isEnabled else { return false }
        guard isOnboarded else { return false }
        return !UserDefaults.standard.bool(forKey: seenKey)
    }
}
