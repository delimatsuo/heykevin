import SwiftUI

/// Prominent dark active call card displayed at the top of the Calls tab.
/// Backed by the actual live transcript, coordinator states, and an immutable presentation lease.
struct ActiveCallCard: View {
    @EnvironmentObject var appState: AppState
    @ObservedObject var coordinator = CallActionCoordinator.shared
    let lease: CallPresentationLease
    var onOpenLive: ((CallPresentationLease) -> Void)? = nil

    var body: some View {
        let currentAuth = appState.currentAuthContext()
        let currentScope = appState.callLifecycleSnapshot

        if lease.isValid(auth: currentAuth, scope: currentScope) {
            let sid = lease.scope.callSid

            VStack(alignment: .leading, spacing: HKSpace.md) {
                // Status, Elapsed Header, and View Live Control
                HStack(spacing: 8) {
                    if appState.callIgnored || coordinator.isTakingMessage(for: sid) {
                        HKStatusDot(color: .hkOrange, size: 8)
                        Text(String(localized: "Taking a message"))
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(Color.hkOrange)
                    } else if coordinator.isDeclinePending(for: sid) {
                        ProgressView()
                            .tint(.white)
                            .scaleEffect(0.7)
                        Text(String(localized: "Requesting a message"))
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(Color.hkOrange)
                    } else if coordinator.isAcceptPending(for: sid) {
                        ProgressView()
                            .tint(.white)
                            .scaleEffect(0.7)
                        Text(String(localized: "Connecting"))
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(Color.hkGreen)
                    } else {
                        HKPulseDot(color: .hkGreen, size: 7)
                        Text(String(localized: "Live"))
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(Color.hkGreen)
                    }

                    if coordinator.isUrgent(for: sid) {
                        Text(String(localized: "Urgent"))
                            .font(.caption2.weight(.bold))
                            .tracking(0.6)
                            .foregroundStyle(.white)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(Color.hkRed, in: RoundedRectangle(cornerRadius: 4))
                    }

                    Spacer()

                    TimelineView(.periodic(from: .now, by: 1.0)) { _ in
                        Text(formattedElapsed)
                            .font(.subheadline.weight(.medium))
                            .monospacedDigit()
                            .foregroundStyle(Color.white.opacity(0.7))
                    }
                }
                .accessibilityElement(children: .combine)

                // Caller Identity
                VStack(alignment: .leading, spacing: HKSpace.sm) {
                    HStack(spacing: HKSpace.md) {
                        HKAvatar(
                            name: appState.activeCallerName,
                            phone: appState.activeCallerPhone,
                            size: 42
                        )

                        VStack(alignment: .leading, spacing: 2) {
                            Text(callerDisplayName)
                                .font(.headline)
                                .foregroundStyle(.white)
                                .fixedSize(horizontal: false, vertical: true)

                            Text(callerSubtitle)
                                .font(.subheadline)
                                .foregroundStyle(Color.white.opacity(0.7))
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }

                    // Visible 'View live call' control
                    Button {
                        let auth = appState.currentAuthContext()
                        let scope = appState.callLifecycleSnapshot
                        guard lease.isValid(auth: auth, scope: scope) else { return }
                        onOpenLive?(lease)
                    } label: {
                        HStack(spacing: 4) {
                            Text(String(localized: "View live call"))
                                .font(.subheadline.weight(.semibold))
                            Image(systemName: "chevron.right")
                                .font(.caption.weight(.bold))
                        }
                        .foregroundStyle(Color.hkGreen)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 6)
                        .frame(minHeight: 44)
                        .background(Color.white.opacity(0.08), in: Capsule())
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("call.viewLive")
                }

                // Live Transcript Excerpt
                if let latestLine = latestTranscriptLine {
                    Text(latestLine)
                        .font(.subheadline)
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
                        let auth = appState.currentAuthContext()
                        let scope = appState.callLifecycleSnapshot
                        if lease.isValid(auth: auth, scope: scope) {
                            appState.clearActiveCall()
                            StoreReviewManager.shared.incrementScreenedCallCount()
                            StoreReviewManager.shared.requestReviewIfEligible()
                        }
                    } label: {
                        HStack(spacing: 6) {
                            Image(systemName: "xmark")
                                .font(.subheadline.weight(.semibold))
                            Text(String(localized: "Dismiss"))
                                .font(.headline)
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
                                Task {
                                    #if DEBUG
                                    if AppStoreScreenshotFixtures.isEnabled { return }
                                    #endif
                                    let auth = appState.currentAuthContext()
                                    let scope = appState.callLifecycleSnapshot
                                    guard lease.isValid(auth: auth, scope: scope) else { return }
                                    _ = await coordinator.checkStatus(callSid: sid)
                                }
                            }
                            .buttonStyle(HKSecondaryButtonStyle(tint: .white))
                            .disabled(AppStoreScreenshotFixtures.isEnabled || coordinator.isActionPending(for: sid))
                            .accessibilityIdentifier("call.checkStatus")
                        }

                        ViewThatFits(in: .horizontal) {
                            HStack(spacing: 10) {
                                pickupButton(sid: sid)
                                messageButton(sid: sid)
                            }

                            VStack(spacing: 10) {
                                pickupButton(sid: sid)
                                messageButton(sid: sid)
                            }
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
        } else {
            EmptyView()
        }
    }

    private func pickupButton(sid: String) -> some View {
        Button {
            Task {
                #if DEBUG
                if AppStoreScreenshotFixtures.isEnabled { return }
                #endif
                let auth = appState.currentAuthContext()
                let scope = appState.callLifecycleSnapshot
                guard lease.isValid(auth: auth, scope: scope) else { return }
                _ = await coordinator.pickUp(callSid: sid, authContext: lease.auth)
            }
        } label: {
            HStack(spacing: 6) {
                if coordinator.isAcceptPending(for: sid) {
                    ProgressView()
                        .tint(.white)
                } else {
                    Image(systemName: "phone.fill")
                        .font(.headline.weight(.bold))
                }
                Text(String(localized: "Pick up"))
                    .font(.headline)
            }
            .frame(maxWidth: .infinity, minHeight: 46)
        }
        .buttonStyle(HKPrimaryButtonStyle(tint: .hkForest))
        .disabled(AppStoreScreenshotFixtures.isEnabled || coordinator.isActionPending(for: sid))
        .accessibilityIdentifier("call.pickup")
    }

    private func messageButton(sid: String) -> some View {
        Button(role: .destructive) {
            Task {
                #if DEBUG
                if AppStoreScreenshotFixtures.isEnabled { return }
                #endif
                let auth = appState.currentAuthContext()
                let scope = appState.callLifecycleSnapshot
                guard lease.isValid(auth: auth, scope: scope) else { return }
                _ = await coordinator.takeMessage(callSid: sid, authContext: lease.auth)
            }
        } label: {
            HStack(spacing: 6) {
                if coordinator.isDeclinePending(for: sid) {
                    ProgressView()
                        .tint(.white)
                        .scaleEffect(0.8)
                } else {
                    Image(systemName: "xmark")
                        .font(.subheadline.weight(.semibold))
                }
                Text(String(localized: "Take a message"))
                    .font(.headline)
            }
            .frame(maxWidth: .infinity, minHeight: 46)
        }
        .buttonStyle(HKDestructiveButtonStyle())
        .disabled(AppStoreScreenshotFixtures.isEnabled || coordinator.isActionPending(for: sid))
        .accessibilityIdentifier("call.message")
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
    @EnvironmentObject var appState: AppState
    let lease: CallPresentationLease
    let onTap: (CallPresentationLease) -> Void

    var body: some View {
        let auth = appState.currentAuthContext()
        let scope = appState.callLifecycleSnapshot

        if lease.isValid(auth: auth, scope: scope) {
            Button {
                let currentAuth = appState.currentAuthContext()
                let currentScope = appState.callLifecycleSnapshot
                guard lease.isValid(auth: currentAuth, scope: currentScope) else { return }
                onTap(lease)
            } label: {
                HStack(spacing: 10) {
                    HKPulseDot(color: .hkGreen, size: 6)

                    Text(String(localized: "Active Call in Progress"))
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.white)

                    Spacer()

                    HStack(spacing: 4) {
                        Text(String(localized: "Return to call"))
                            .font(.subheadline.weight(.medium))
                        Image(systemName: "chevron.right")
                            .font(.caption.weight(.semibold))
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
        } else {
            EmptyView()
        }
    }
}

/// Full live call transcript detail view presenting the real-time transcript, caller identity,
/// coordinator states, and live action buttons.
struct LiveCallDetailView: View {
    @EnvironmentObject var appState: AppState
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @ObservedObject var coordinator = CallActionCoordinator.shared
    let lease: CallPresentationLease
    var onDone: (() -> Void)? = nil

    var body: some View {
        let currentAuth = appState.currentAuthContext()
        let currentScope = appState.callLifecycleSnapshot

        if lease.isValid(auth: currentAuth, scope: currentScope) {
            let sid = lease.scope.callSid

            VStack(spacing: 0) {
                // Header Strip
                liveHeaderStrip(sid: sid)

                // Scrollable Live Transcript
                ScrollViewReader { proxy in
                    ScrollView {
                        VStack(spacing: 12) {
                            if appState.transcriptLines.isEmpty {
                                VStack(spacing: 8) {
                                    ProgressView()
                                        .padding(.top, 32)
                                    Text(String(localized: "Listening to conversation…"))
                                        .font(.subheadline)
                                        .foregroundStyle(.secondary)
                                }
                                .frame(maxWidth: .infinity)
                            } else {
                                ForEach(Array(appState.transcriptLines.enumerated()), id: \.offset) { idx, line in
                                    TranscriptBubble(text: line.text)
                                        .id(idx)
                                }
                            }
                        }
                        .padding(.horizontal, 16)
                        .padding(.vertical, 14)
                    }
                    .onChange(of: appState.transcriptLines.count) { _, newCount in
                        if newCount > 0 {
                            withAnimation {
                                proxy.scrollTo(newCount - 1, anchor: .bottom)
                            }
                        }
                    }
                }

                // Coordinator Error
                if let errorMsg = coordinator.errorMessage(for: sid) {
                    HStack(spacing: 6) {
                        Image(systemName: "exclamationmark.circle.fill")
                            .foregroundStyle(.red)
                        Text(errorMsg)
                            .font(.footnote)
                            .foregroundStyle(.red)
                    }
                    .padding(.horizontal, 16)
                    .padding(.vertical, 6)
                }

                // Bottom Action Bar
                liveActionBar(sid: sid)
            }
            .background(Color(.systemGroupedBackground))
            .navigationTitle(String(localized: "Live Call"))
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button(String(localized: "Done")) {
                        onDone?()
                    }
                    .font(.headline)
                    .accessibilityIdentifier("call.liveDone")
                }
            }
        } else {
            ContentUnavailableView(
                String(localized: "Call details unavailable"),
                systemImage: "phone.badge.questionmark",
                description: Text(String(localized: "This call is no longer active."))
            )
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button(String(localized: "Done")) {
                        onDone?()
                    }
                    .font(.headline)
                    .accessibilityIdentifier("call.liveDone")
                }
            }
        }
    }

    private func liveHeaderStrip(sid: String) -> some View {
        let isAX = dynamicTypeSize.isAccessibilitySize
        let headerLayout = isAX ? AnyLayout(VStackLayout(alignment: .leading, spacing: 12)) : AnyLayout(HStackLayout(spacing: 12))

        return headerLayout {
            HStack(spacing: 12) {
                HKAvatar(name: appState.activeCallerName, phone: appState.activeCallerPhone, size: 44)

                VStack(alignment: .leading, spacing: 2) {
                    Text(callerDisplayName)
                        .font(.headline)
                        .fixedSize(horizontal: false, vertical: true)

                    Text(callerSubtitle)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            if !isAX {
                Spacer()
            }

            VStack(alignment: isAX ? .leading : .trailing, spacing: 3) {
                HStack(spacing: 6) {
                    if appState.callIgnored || coordinator.isTakingMessage(for: sid) {
                        HKStatusDot(color: .hkOrange, size: 7)
                        Text(String(localized: "Taking message"))
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(Color.hkOrange)
                    } else if coordinator.isDeclinePending(for: sid) {
                        ProgressView()
                            .scaleEffect(0.6)
                        Text(String(localized: "Requesting…"))
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(Color.hkOrange)
                    } else if coordinator.isAcceptPending(for: sid) {
                        ProgressView()
                            .scaleEffect(0.6)
                        Text(String(localized: "Connecting…"))
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(Color.hkGreen)
                    } else {
                        HKPulseDot(color: .hkGreen, size: 6)
                        Text(String(localized: "Screening"))
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(Color.hkGreen)
                    }
                }

                TimelineView(.periodic(from: .now, by: 1.0)) { _ in
                    Text(formattedElapsed)
                        .font(.caption.weight(.medium))
                        .monospacedDigit()
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .background(Color(.secondarySystemGroupedBackground))
        .overlay(alignment: .bottom) {
            Divider()
        }
    }

    private func liveActionBar(sid: String) -> some View {
        VStack(spacing: 10) {
            if appState.callIgnored || coordinator.isTakingMessage(for: sid) {
                Button {
                    let auth = appState.currentAuthContext()
                    let scope = appState.callLifecycleSnapshot
                    if lease.isValid(auth: auth, scope: scope) {
                        appState.clearActiveCall()
                        StoreReviewManager.shared.incrementScreenedCallCount()
                        StoreReviewManager.shared.requestReviewIfEligible()
                        onDone?()
                    }
                } label: {
                    Text(String(localized: "Dismiss Call"))
                        .font(.headline)
                        .frame(maxWidth: .infinity, minHeight: 46)
                }
                .buttonStyle(.bordered)
                .tint(.primary)
                .accessibilityIdentifier("call.liveDismiss")
            } else {
                if coordinator.canCheckStatus(for: sid) {
                    Button(String(localized: "Check status")) {
                        Task {
                            #if DEBUG
                            if AppStoreScreenshotFixtures.isEnabled { return }
                            #endif
                            let auth = appState.currentAuthContext()
                            let scope = appState.callLifecycleSnapshot
                            guard lease.isValid(auth: auth, scope: scope) else { return }
                            _ = await coordinator.checkStatus(callSid: sid)
                        }
                    }
                    .buttonStyle(HKSecondaryButtonStyle())
                    .disabled(AppStoreScreenshotFixtures.isEnabled || coordinator.isActionPending(for: sid))
                    .accessibilityIdentifier("call.liveCheckStatus")
                }

                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 12) {
                        pickupButton(sid: sid)
                        takeMessageButton(sid: sid)
                    }

                    VStack(spacing: 10) {
                        pickupButton(sid: sid)
                        takeMessageButton(sid: sid)
                    }
                }
            }
        }
        .padding(.horizontal, 16)
        .padding(.top, 10)
        .padding(.bottom, 16)
        .background(Color(.secondarySystemGroupedBackground))
        .overlay(alignment: .top) {
            Divider()
        }
    }

    private func pickupButton(sid: String) -> some View {
        Button {
            Task {
                #if DEBUG
                if AppStoreScreenshotFixtures.isEnabled { return }
                #endif
                let auth = appState.currentAuthContext()
                let scope = appState.callLifecycleSnapshot
                guard lease.isValid(auth: auth, scope: scope) else { return }
                _ = await coordinator.pickUp(callSid: sid, authContext: lease.auth)
            }
        } label: {
            HStack(spacing: 6) {
                if coordinator.isAcceptPending(for: sid) {
                    ProgressView()
                        .tint(.white)
                } else {
                    Image(systemName: "phone.fill")
                        .font(.headline.weight(.bold))
                }
                Text(String(localized: "Pick up"))
                    .font(.headline)
            }
            .frame(maxWidth: .infinity, minHeight: 46)
        }
        .buttonStyle(HKPrimaryButtonStyle(tint: .hkForest))
        .disabled(AppStoreScreenshotFixtures.isEnabled || coordinator.isActionPending(for: sid))
        .accessibilityIdentifier("call.livePickup")
    }

    private func takeMessageButton(sid: String) -> some View {
        Button(role: .destructive) {
            Task {
                #if DEBUG
                if AppStoreScreenshotFixtures.isEnabled { return }
                #endif
                let auth = appState.currentAuthContext()
                let scope = appState.callLifecycleSnapshot
                guard lease.isValid(auth: auth, scope: scope) else { return }
                _ = await coordinator.takeMessage(callSid: sid, authContext: lease.auth)
            }
        } label: {
            HStack(spacing: 6) {
                if coordinator.isDeclinePending(for: sid) {
                    ProgressView()
                        .tint(.white)
                        .scaleEffect(0.8)
                } else {
                    Image(systemName: "xmark")
                        .font(.subheadline.weight(.semibold))
                }
                Text(String(localized: "Take a message"))
                    .font(.headline)
            }
            .frame(maxWidth: .infinity, minHeight: 46)
        }
        .buttonStyle(HKDestructiveButtonStyle())
        .disabled(AppStoreScreenshotFixtures.isEnabled || coordinator.isActionPending(for: sid))
        .accessibilityIdentifier("call.liveMessage")
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
        return String(localized: "Screened by Kevin")
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

/// A single transcript chat bubble.
private struct TranscriptBubble: View {
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
                Spacer(minLength: 32)
            }

            VStack(alignment: isKevin ? .trailing : .leading, spacing: 4) {
                if !speakerName.isEmpty {
                    Text(speakerName)
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(.secondary)
                }

                Text(bodyText)
                    .font(.body)
                    .foregroundStyle(isKevin ? .white : Color.hkInk)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                    .background(
                        isKevin ? Color.accentColor : Color(.secondarySystemGroupedBackground),
                        in: RoundedRectangle(cornerRadius: 16, style: .continuous)
                    )
            }
            .frame(maxWidth: 290, alignment: isKevin ? .trailing : .leading)

            if !isKevin {
                Spacer(minLength: 32)
            }
        }
        .frame(maxWidth: .infinity, alignment: isKevin ? .trailing : .leading)
        .accessibilityElement(children: .combine)
    }
}
