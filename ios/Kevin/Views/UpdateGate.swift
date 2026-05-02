import SwiftUI

/// Attaches the app-version gate to any view: a forced-update full-screen cover when
/// the running build is below `min_version`, and an optional, dismissible bottom sheet
/// when only a newer `latest_version` exists. Add to the root view in `KevinApp`.
struct AppVersionGate: ViewModifier {
    @ObservedObject private var service = AppVersionService.shared

    func body(content: Content) -> some View {
        content
            .fullScreenCover(isPresented: forcedBinding) {
                ForcedUpdateView(state: service.state)
            }
            .sheet(isPresented: optionalBinding) {
                OptionalUpdateSheet(state: service.state)
                    .presentationDetents([.medium])
                    .presentationDragIndicator(.visible)
            }
    }

    private var forcedBinding: Binding<Bool> {
        Binding(
            get: {
                if case .forced = service.state { return true }
                return false
            },
            set: { _ in /* not user-dismissable */ }
        )
    }

    private var optionalBinding: Binding<Bool> {
        Binding(
            get: {
                if case .optional = service.state { return true }
                return false
            },
            set: { newValue in
                if !newValue { service.dismissOptional() }
            }
        )
    }
}

extension View {
    /// Attach the version gate. Idempotent — apply once at the WindowGroup root.
    func appVersionGate() -> some View { modifier(AppVersionGate()) }
}

// MARK: - Forced update

private struct ForcedUpdateView: View {
    let state: VersionGateState
    @Environment(\.openURL) private var openURL

    var body: some View {
        ZStack {
            Color(.systemGroupedBackground).ignoresSafeArea()
            VStack(spacing: HKSpace.xl) {
                Spacer()

                KevinMark(size: 84)

                VStack(spacing: HKSpace.md) {
                    Text(String(localized: "Update required"))
                        .font(.system(size: 30, weight: .bold))
                        .tracking(-0.6)
                        .multilineTextAlignment(.center)

                    Text(messageText)
                        .font(.system(size: 15))
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                        .lineSpacing(3)
                        .padding(.horizontal, HKSpace.lg)
                        .fixedSize(horizontal: false, vertical: true)
                }

                Spacer()

                VStack(spacing: HKSpace.xs) {
                    Button {
                        openStore()
                    } label: {
                        Text(String(localized: "Update on the App Store"))
                    }
                    .buttonStyle(HKDarkPrimaryButtonStyle())

                    Text(String(localized: "Hey Kevin needs the latest version to keep working."))
                        .font(.system(size: 12))
                        .foregroundStyle(.tertiary)
                        .padding(.top, 4)
                }
                .padding(.horizontal, HKSpace.lg)
            }
            .padding(.bottom, HKSpace.xl)
        }
        .interactiveDismissDisabled()
    }

    private var messageText: String {
        if case .forced(_, _, let message) = state, let m = message, !m.isEmpty {
            return m
        }
        return String(localized: "A newer version of Hey Kevin is required to continue. Update from the App Store to keep your call screening running.")
    }

    private func openStore() {
        guard case .forced(_, let url, _) = state, let store = URL(string: url) else { return }
        openURL(store)
    }
}

// MARK: - Optional update

private struct OptionalUpdateSheet: View {
    let state: VersionGateState
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL

    var body: some View {
        VStack(spacing: 0) {
            Spacer(minLength: HKSpace.lg)

            KevinMark(size: 64)
                .padding(.bottom, HKSpace.lg)

            VStack(spacing: HKSpace.sm) {
                Text(String(localized: "Update available"))
                    .font(.system(size: 22, weight: .bold))
                    .tracking(-0.3)

                Text(String(localized: "A newer version of Hey Kevin is on the App Store. The latest fixes and improvements are waiting."))
                    .font(.system(size: 14))
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .lineSpacing(2)
                    .padding(.horizontal, HKSpace.xl)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Spacer()

            VStack(spacing: HKSpace.xs) {
                Button {
                    openStore()
                    dismiss()
                } label: {
                    Text(String(localized: "Update now"))
                }
                .buttonStyle(HKDarkPrimaryButtonStyle())

                Button {
                    dismiss()
                } label: {
                    Text(String(localized: "Maybe later"))
                        .font(.system(size: 14))
                        .foregroundStyle(.secondary)
                        .padding(.vertical, HKSpace.sm)
                }
                .buttonStyle(.plain)
            }
            .padding(.horizontal, HKSpace.lg)
            .padding(.bottom, HKSpace.lg)
        }
    }

    private func openStore() {
        guard case .optional(_, let url) = state, let store = URL(string: url) else { return }
        openURL(store)
    }
}
