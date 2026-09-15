import SwiftUI

/// Main native screened call journal view.
/// Integrates bounded pagination, date grouping, live active call cards,
/// search and filtering before pagination, and single-owner navigation.
struct CallHistoryView: View {
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @EnvironmentObject var appState: AppState
    @ObservedObject var historyModel: CallHistoryModel
    @ObservedObject var navigation: FrontendNavigation
    var onOpenSettings: (() -> Void)? = nil

    init(
        historyModel: CallHistoryModel,
        navigation: FrontendNavigation,
        onOpenSettings: (() -> Void)? = nil
    ) {
        self.historyModel = historyModel
        self.navigation = navigation
        self.onOpenSettings = onOpenSettings
    }

    var body: some View {
        NavigationStack {
            List {
                // Top prominent active call card if a call is currently live
                if let lease = appState.ownedActiveCallLease {
                    Section {
                        ActiveCallCard(lease: lease, onOpenLive: { validLease in
                            navigation.openLive(lease: validLease)
                        })
                        .listRowInsets(EdgeInsets(top: 8, leading: 16, bottom: 8, trailing: 16))
                        .listRowBackground(Color.clear)
                        .listRowSeparator(.hidden)
                    }
                }

                // Search, Filter, and Count Header Section
                Section {
                    VStack(alignment: .leading, spacing: 12) {
                        searchBar
                        filterPicker
                        if !historyModel.showingCountLabel.isEmpty {
                            Text(historyModel.showingCountLabel)
                                .font(.footnote)
                                .foregroundStyle(Color.hkInkSecondary)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .accessibilityIdentifier("calls.count")
                        }
                    }
                    .listRowInsets(EdgeInsets(top: 4, leading: 16, bottom: 4, trailing: 16))
                    .listRowBackground(Color.clear)
                    .listRowSeparator(.hidden)
                }

                // Retained error banner on reload failure while existing calls stay visible
                if let retainedError = historyModel.retainedErrorMessage {
                    Section {
                        retainedErrorBanner(message: retainedError)
                            .listRowInsets(EdgeInsets(top: 4, leading: 16, bottom: 4, trailing: 16))
                            .listRowBackground(Color.clear)
                            .listRowSeparator(.hidden)
                    }
                }

                // Initial load error state when no calls are cached
                if let errorMessage = historyModel.errorMessage, !historyModel.hasCalls {
                    Section {
                        initialErrorView(message: errorMessage)
                            .listRowInsets(EdgeInsets(top: 24, leading: 16, bottom: 24, trailing: 16))
                            .listRowBackground(Color.clear)
                            .listRowSeparator(.hidden)
                    }
                }

                // Empty state
                if let emptyState = historyModel.emptyState {
                    Section {
                        emptyStateView(emptyState)
                            .listRowInsets(EdgeInsets(top: 32, leading: 16, bottom: 32, trailing: 16))
                            .listRowBackground(Color.clear)
                            .listRowSeparator(.hidden)
                    }
                }

                // Grouped call rows
                if !historyModel.groupedVisibleCalls.isEmpty {
                    ForEach(historyModel.groupedVisibleCalls) { group in
                        Section(header: Text(group.title).font(.subheadline.weight(.semibold)).foregroundStyle(Color.hkInkSecondary)) {
                            ForEach(group.calls) { call in
                                Button(action: navigation.makeHistoricalSelectionAction(call: call, expectedAuth: historyModel.activeAuthContext)) {
                                    CallRow(call: call, isUnread: historyModel.isCallUnread(call))
                                }
                                .buttonStyle(.plain)
                                .accessibilityIdentifier("calls.row.\(call.id)")
                            }
                        }
                    }

                    // Bounded pagination footer
                    Section {
                        paginationFooter
                            .listRowInsets(EdgeInsets(top: 12, leading: 16, bottom: 24, trailing: 16))
                            .listRowBackground(Color.clear)
                            .listRowSeparator(.hidden)
                    }
                }
            }
            .listStyle(.insetGrouped)
            .scrollContentBackground(.hidden)
            .background(Color.hkWarmCanvas.ignoresSafeArea())
            .navigationTitle(String(localized: "Calls"))
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    if historyModel.unreadCount > 0 {
                        Button(
                            String(localized: "Mark All Read"),
                            action: historyModel.makeMarkAllReadAction(expectedAuth: historyModel.activeAuthContext)
                        )
                        .font(.subheadline.weight(.medium))
                        .accessibilityIdentifier("calls.markAllRead")
                        .accessibilityHint(String(localized: "Marks all calls in this history as read, including hidden pages."))
                    }
                }

                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        onOpenSettings?()
                    } label: {
                        HStack(spacing: 4) {
                            Image(systemName: "person.crop.circle")
                            Text(String(localized: "Settings"))
                        }
                        .frame(minWidth: 44, minHeight: 44)
                        .contentShape(Rectangle())
                    }
                    .accessibilityIdentifier("nav.settings")
                    .accessibilityLabel(String(localized: "Settings"))
                }
            }
            .refreshable {
                await historyModel.loadCalls()
            }
            .onAppear {
                #if DEBUG
                if AppStoreScreenshotFixtures.isEnabled { return }
                #endif
                StoreReviewManager.shared.requestReviewIfEligible()
            }
        }
    }

    // MARK: - Subviews

    private var searchBar: some View {
        HStack(spacing: 8) {
            Image(systemName: "magnifyingglass")
                .foregroundStyle(Color.hkInkSecondary)
                .font(.subheadline)

            TextField(
                String(localized: "Search calls"),
                text: Binding(
                    get: { historyModel.searchQuery },
                    set: { historyModel.setSearchQuery($0) }
                )
            )
            .font(.body)
            .textFieldStyle(.plain)
            .accessibilityIdentifier("calls.search")

            if !historyModel.searchQuery.isEmpty {
                Button {
                    historyModel.clearSearch()
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(Color.hkInkSecondary)
                        .frame(minWidth: 44, minHeight: 44)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(String(localized: "Clear search"))
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(Color(.secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
    }

    private var filterPicker: some View {
        let layout = dynamicTypeSize.isAccessibilitySize
            ? AnyLayout(VStackLayout(spacing: 8))
            : AnyLayout(HStackLayout(spacing: 8))

        return layout {
            ForEach(CallHistoryFilter.allCases) { filter in
                let isSelected = historyModel.selectedFilter == filter
                Button {
                    historyModel.setFilter(filter)
                } label: {
                    Text(filter.localizedTitle)
                        .font(.subheadline.weight(isSelected ? .semibold : .regular))
                        .fixedSize(horizontal: false, vertical: true)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 4)
                        .frame(maxWidth: .infinity, minHeight: 44)
                        .background(
                            isSelected ? Color.hkCobalt.opacity(0.15) : Color(.secondarySystemGroupedBackground),
                            in: RoundedRectangle(cornerRadius: 8, style: .continuous)
                        )
                        .foregroundStyle(isSelected ? Color.hkCobalt : Color.hkInk)
                        .overlay {
                            if isSelected {
                                RoundedRectangle(cornerRadius: 8, style: .continuous)
                                    .stroke(Color.hkCobalt.opacity(0.3), lineWidth: 1)
                            }
                        }
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(isSelected ? .isSelected : [])
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("calls.filter")
    }

    private func retainedErrorBanner(message: String) -> some View {
        HStack(spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.hkOrange)
                .font(.subheadline)
            Text(message)
                .font(.footnote)
                .foregroundStyle(Color.hkInk)
                .lineLimit(2)
            Spacer()
            Button(String(localized: "Retry")) {
                Task { await historyModel.loadCalls() }
            }
            .font(.footnote.weight(.semibold))
            .foregroundStyle(Color.hkCobalt)
            .frame(minWidth: 44, minHeight: 44)
            .contentShape(Rectangle())
        }
        .padding(10)
        .background(Color.hkOrange.opacity(0.12), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private func initialErrorView(message: String) -> some View {
        VStack(spacing: 12) {
            Image(systemName: "wifi.exclamationmark")
                .font(.system(size: 36))
                .foregroundStyle(.secondary)
            Text(message)
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            Button(String(localized: "Retry")) {
                Task { await historyModel.loadCalls() }
            }
            .buttonStyle(.borderedProminent)
            .tint(Color.hkCobalt)
            .frame(minHeight: 44)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 20)
    }

    private func emptyStateView(_ state: CallHistoryEmptyState) -> some View {
        VStack(spacing: 12) {
            Image(systemName: stateIcon(for: state))
                .font(.system(size: 44))
                .foregroundStyle(Color.hkInkSecondary.opacity(0.6))

            Text(state.title)
                .font(.headline)
                .foregroundStyle(Color.hkInk)

            Text(state.description)
                .font(.subheadline)
                .foregroundStyle(Color.hkInkSecondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 24)

            if state == .noMatches {
                Button(String(localized: "Clear search")) {
                    historyModel.clearSearch()
                }
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(Color.hkCobalt)
                .frame(minHeight: 44)
                .contentShape(Rectangle())
                .padding(.top, 4)
            } else if state == .noUnread || state == .noSpam {
                Button(String(localized: "Show all")) {
                    historyModel.setFilter(.all)
                }
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(Color.hkCobalt)
                .frame(minHeight: 44)
                .contentShape(Rectangle())
                .padding(.top, 4)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 24)
        .accessibilityElement(children: .combine)
    }

    private func stateIcon(for state: CallHistoryEmptyState) -> String {
        switch state {
        case .noCalls:   return "phone.badge.waveform"
        case .noUnread:  return "tray"
        case .noSpam:    return "hand.raised.slash"
        case .noMatches: return "magnifyingglass"
        }
    }

    private var paginationFooter: some View {
        VStack(spacing: 10) {
            if historyModel.canShowMore {
                Button {
                    historyModel.showMore()
                } label: {
                    Text(String(localized: "Show 20 more"))
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(Color.hkCobalt)
                        .frame(maxWidth: .infinity, minHeight: 44)
                        .background(Color.hkCobalt.opacity(0.08), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("calls.showMore")
            }

            Text(historyModel.historyFootnoteLabel)
                .font(.caption2)
                .foregroundStyle(Color.hkInkSecondary.opacity(0.8))
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 8)
    }
}

// MARK: - Call Row

struct CallRow: View {
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    let call: CallRecord
    var isUnread: Bool = false

    private var isAccessibilitySize: Bool {
        dynamicTypeSize.isAccessibilitySize
    }

    var body: some View {
        let layout = isAccessibilitySize
            ? AnyLayout(VStackLayout(alignment: .leading, spacing: 8))
            : AnyLayout(HStackLayout(alignment: .center, spacing: HKSpace.md))

        layout {
            HStack(spacing: HKSpace.md) {
                HKAvatar(name: call.callerName, phone: call.callerPhone, size: 40)
                if isAccessibilitySize {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(call.displayName)
                            .font(.body.weight(isUnread ? .bold : .medium))
                            .foregroundStyle(call.isSpamOrBlocked ? Color.hkRed : Color.hkInk)
                            .fixedSize(horizontal: false, vertical: true)
                            .monospacedDigit()
                    }
                }
            }

            VStack(alignment: .leading, spacing: 2) {
                if !isAccessibilitySize {
                    Text(call.displayName)
                        .font(.body.weight(isUnread ? .bold : .medium))
                        .foregroundStyle(call.isSpamOrBlocked ? Color.hkRed : Color.hkInk)
                        .fixedSize(horizontal: false, vertical: true)
                        .monospacedDigit()
                }

                Text(call.callerExcerpt)
                    .font(.subheadline)
                    .foregroundStyle(isUnread ? Color.hkInk : Color.hkInkSecondary)
                    .lineLimit(isAccessibilitySize ? nil : 2)
                    .fixedSize(horizontal: false, vertical: isAccessibilitySize)
                    .truncationMode(.tail)
            }

            if !isAccessibilitySize {
                Spacer(minLength: 6)
            }

            HStack(spacing: 6) {
                Text(Self.timeLabel(for: call.timestamp))
                    .font(.caption.weight(isUnread ? .semibold : .regular))
                    .foregroundStyle(isUnread ? Color.hkCobalt : Color.hkInkSecondary)
                    .monospacedDigit()

                if isUnread {
                    Circle()
                        .fill(Color.hkCobalt)
                        .frame(width: 8, height: 8)
                }
            }
            .frame(minWidth: isAccessibilitySize ? nil : 44, alignment: isAccessibilitySize ? .leading : .trailing)
        }
        .padding(.vertical, 4)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(call.displayName), \(call.callerExcerpt), \(Self.timeLabel(for: call.timestamp)), \(isUnread ? String(localized: "Unread") : String(localized: "Read"))")
    }

    static func timeLabel(for date: Date) -> String {
        let calendar = Calendar.current
        #if DEBUG
        if AppStoreScreenshotFixtures.isEnabled {
            let now = AppStoreScreenshotFixtures.now
            if calendar.isDate(date, inSameDayAs: now) {
                let minutes = Int(now.timeIntervalSince(date) / 60)
                if minutes < 1 { return String(localized: "now") }
                if minutes < 60 { return String(localized: "\(minutes)m ago") }
                return date.formatted(date: .omitted, time: .shortened)
            } else if let yesterday = calendar.date(byAdding: .day, value: -1, to: now),
                      calendar.isDate(date, inSameDayAs: yesterday) {
                return String(localized: "Yest.")
            } else {
                let weekday = date.formatted(.dateTime.weekday(.abbreviated))
                if let days = calendar.dateComponents([.day], from: date, to: now).day, days < 7 {
                    return weekday
                }
                return date.formatted(.dateTime.month(.abbreviated).day())
            }
        }
        #endif
        if calendar.isDateInToday(date) {
            let minutes = Int(Date().timeIntervalSince(date) / 60)
            if minutes < 1 { return String(localized: "now") }
            if minutes < 60 { return String(localized: "\(minutes)m ago") }
            return date.formatted(date: .omitted, time: .shortened)
        } else if calendar.isDateInYesterday(date) {
            return String(localized: "Yest.")
        } else {
            let weekday = date.formatted(.dateTime.weekday(.abbreviated))
            if let days = calendar.dateComponents([.day], from: date, to: Date()).day, days < 7 {
                return weekday
            }
            return date.formatted(.dateTime.month(.abbreviated).day())
        }
    }
}

/// Pure routing and validation policy for links tapped within historical call details
/// (including AttributedString telephone links in transcripts).
enum HistoricalCallLinkRouter {
    /// Validates whether a URL action is permitted given the presentation lease,
    /// current authorization context, and screenshot fixture status.
    /// If permitted, forwards the URL to `openEffect` and returns true.
    /// Otherwise, rejects the action and returns false.
    @discardableResult
    static func perform(
        url: URL,
        lease: HistoricalCallPresentationLease,
        currentAuth: CallAuthContext,
        isFixture: Bool,
        openEffect: (URL) -> Void
    ) -> Bool {
        guard !isFixture else { return false }
        guard lease.isValid(currentAuth: currentAuth) else { return false }
        openEffect(url)
        return true
    }
}

// MARK: - Call Detail View

struct CallDetailView: View {
    @EnvironmentObject var appState: AppState
    @Environment(\.openURL) private var openURL
    let lease: HistoricalCallPresentationLease
    let historyModel: CallHistoryModel

    @State private var appointmentConfirmed = false
    @State private var callerWasNotified = false
    @State private var isConfirming = false
    @State private var confirmError = ""

    private var isFixtureMode: Bool {
        #if DEBUG
        return AppStoreScreenshotFixtures.isEnabled
        #else
        return false
        #endif
    }

    private var call: CallRecord {
        lease.call
    }

    private var callerWasTexted: Bool {
        callerWasNotified || call.appointmentCallerNotified
    }

    private var appointmentStatus: String {
        appointmentConfirmed ? "confirmed" : (call.appointmentStatus ?? "")
    }

    var body: some View {
        let currentAuth = appState.currentAuthContext()

        if lease.isValid(currentAuth: currentAuth) {
            ScrollView {
                VStack(spacing: 18) {
                    callerHeader
                    primaryActions
                    callSummary
                    transcriptSection
                }
                .padding(.horizontal, 20)
                .padding(.top, 12)
                .padding(.bottom, 32)
            }
            .environment(\.openURL, OpenURLAction { url in
                let auth = appState.currentAuthContext()
                let permitted = HistoricalCallLinkRouter.perform(
                    url: url,
                    lease: lease,
                    currentAuth: auth,
                    isFixture: isFixtureMode
                ) { targetURL in
                    openURL(targetURL)
                }
                return permitted ? .handled : .discarded
            })
            .background(Color(.systemGroupedBackground))
            .navigationTitle(String(localized: "Details"))
            .navigationBarTitleDisplayMode(.inline)
            .onAppear {
                let auth = appState.currentAuthContext()
                if lease.isValid(currentAuth: auth) {
                    historyModel.markAsRead(callId: lease.callId, expectedAuth: lease.auth)
                }
            }
        } else {
            ContentUnavailableView(
                String(localized: "Call details unavailable"),
                systemImage: "phone.badge.questionmark",
                description: Text(String(localized: "This call is no longer in your history."))
            )
        }
    }

    private var callerHeader: some View {
        VStack(spacing: 14) {
            ZStack {
                Circle()
                    .fill(
                        LinearGradient(
                            colors: [outcomeColor.opacity(0.22), Color(.tertiarySystemFill)],
                            startPoint: .topLeading,
                            endPoint: .bottomTrailing
                        )
                    )
                    .frame(width: 84, height: 84)
                if !callerInitials.isEmpty {
                    Text(callerInitials)
                        .font(.title.weight(.semibold))
                        .foregroundStyle(.primary)
                } else {
                    Image(systemName: "phone.fill")
                        .font(.title2.weight(.semibold))
                        .foregroundStyle(.primary)
                }
            }

            VStack(spacing: 5) {
                Text(call.displayName)
                    .font(.title2.weight(.semibold))
                    .multilineTextAlignment(.center)
                    .lineLimit(2)
                    .minimumScaleFactor(0.82)

                Button {
                    let currentAuth = appState.currentAuthContext()
                    HistoricalCallLinkRouter.perform(
                        url: phoneURL(for: call.callerPhone),
                        lease: lease,
                        currentAuth: currentAuth,
                        isFixture: isFixtureMode
                    ) { targetURL in
                        openURL(targetURL)
                    }
                } label: {
                    Text(call.formattedPhone)
                        .font(.body)
                        .foregroundStyle(Color.hkCobalt)
                }
                .buttonStyle(.plain)
            }

            StatusPill(title: outcomeText, systemImage: outcomeIcon, color: outcomeColor)

            Text(call.timestamp.formatted(date: .abbreviated, time: .shortened))
                .font(.subheadline)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 6)
        .accessibilityElement(children: .combine)
    }

    private var primaryActions: some View {
        VStack(spacing: 10) {
            if appointmentStatus == "pending_owner_confirmation" {
                appointmentRequestCard
                confirmAppointmentButton
                if !confirmError.isEmpty {
                    Text(confirmError)
                        .font(.footnote)
                        .foregroundStyle(.red)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            } else if appointmentStatus == "confirmed" || appointmentStatus == "booked" {
                appointmentConfirmedCard
            }

            ViewThatFits(in: .horizontal) {
                HStack(spacing: 12) {
                    actionButton(title: String(localized: "Call Back"), systemImage: "phone.fill", destination: phoneURL(for: callbackPhone), isPrimary: appointmentStatus != "pending_owner_confirmation")
                    actionButton(title: String(localized: "Message"), systemImage: "message.fill", destination: messageURL(for: callbackPhone), isPrimary: false)
                }

                VStack(spacing: 10) {
                    actionButton(title: String(localized: "Call Back"), systemImage: "phone.fill", destination: phoneURL(for: callbackPhone), isPrimary: appointmentStatus != "pending_owner_confirmation")
                    actionButton(title: String(localized: "Message"), systemImage: "message.fill", destination: messageURL(for: callbackPhone), isPrimary: false)
                }
            }
        }
    }

    private var appointmentRequestCard: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let title = call.appointmentTitle, !title.isEmpty {
                Text(title)
                    .font(.subheadline.weight(.semibold))
            }
            Text(formattedAppointmentStart)
                .font(.headline)
            Text(String(localized: "Adds this time to Google Calendar. Nothing is booked until you confirm."))
                .font(.footnote)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background {
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(Color(.secondarySystemGroupedBackground))
        }
        .accessibilityElement(children: .combine)
    }

    private var appointmentConfirmedCard: some View {
        VStack(alignment: .leading, spacing: 4) {
            Label(String(localized: "Added to Google Calendar"), systemImage: "checkmark.circle.fill")
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.green)
            if let title = call.appointmentTitle, !title.isEmpty {
                Text(title)
                    .font(.subheadline)
            }
            Text(formattedAppointmentStart)
                .font(.headline)
            if callerWasTexted {
                Text(String(localized: "The caller was texted this time."))
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background {
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(Color(.secondarySystemGroupedBackground))
        }
        .accessibilityElement(children: .combine)
    }

    private var formattedAppointmentStart: String {
        guard let raw = call.appointmentStartTime, !raw.isEmpty else {
            return String(localized: "Requested time")
        }
        let withFractional = ISO8601DateFormatter()
        withFractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let basic = ISO8601DateFormatter()
        basic.formatOptions = [.withInternetDateTime]
        if let date = withFractional.date(from: raw) ?? basic.date(from: raw) {
            return date.formatted(date: .abbreviated, time: .shortened)
        }
        return raw
    }

    private var confirmAppointmentButton: some View {
        Button {
            Task { await confirmAppointment() }
        } label: {
            Label(
                isConfirming ? String(localized: "Confirming…") : String(localized: "Confirm"),
                systemImage: "calendar.badge.checkmark"
            )
            .font(.headline)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 12)
        }
        .buttonStyle(.plain)
        .foregroundStyle(.white)
        .background {
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(Color.accentColor)
        }
        .disabled(isConfirming || AppStoreScreenshotFixtures.isEnabled)
        .accessibilityLabel(String(localized: "Confirm"))
        .accessibilityHint(formattedAppointmentStart)
    }

    private func confirmAppointment() async {
        guard !isConfirming else { return }
        #if DEBUG
        if AppStoreScreenshotFixtures.isEnabled { return }
        #endif
        let currentAuth = appState.currentAuthContext()
        guard lease.isValid(currentAuth: currentAuth) else {
            confirmError = String(localized: "Authentication required.")
            return
        }
        isConfirming = true
        confirmError = ""
        do {
            let notified = try await APIClient.shared.confirmAppointment(callSid: lease.callId, authContext: lease.auth)
            let postAuth = appState.currentAuthContext()
            guard lease.isValid(currentAuth: postAuth) else { return }
            _ = historyModel.applyAppointmentConfirmation(
                callId: lease.callId,
                callerNotified: notified,
                expectedAuth: lease.auth
            )
            callerWasNotified = notified
            appointmentConfirmed = true
            StoreReviewManager.shared.recordAppointmentConfirmed()
            StoreReviewManager.shared.requestReviewIfEligible()
        } catch let failure as AppointmentConfirmFailure {
            let postAuth = appState.currentAuthContext()
            guard lease.isValid(currentAuth: postAuth) else { return }
            confirmError = failure.localizedDescription
        } catch {
            let postAuth = appState.currentAuthContext()
            guard lease.isValid(currentAuth: postAuth) else { return }
            confirmError = String(localized: "Couldn't add this to Google Calendar. Try again.")
        }
        isConfirming = false
    }

    private var callSummary: some View {
        CallDetailSection(title: String(localized: "Call"), systemImage: "phone.badge.waveform") {
            DetailRow(
                title: String(localized: "Outcome"),
                value: outcomeText,
                systemImage: outcomeIcon,
                tint: outcomeColor
            )
            Divider()
            DetailRow(
                title: String(localized: "Time"),
                value: call.timestamp.formatted(date: .abbreviated, time: .shortened),
                systemImage: "calendar",
                tint: .secondary
            )
            if let callbackNumber = call.callbackNumber, callbackNumber != call.callerPhone {
                Divider()
                DetailRow(
                    title: String(localized: "Callback"),
                    value: PhoneFormatter.format(callbackNumber),
                    systemImage: "phone.arrow.up.right.fill",
                    tint: .blue
                )
            }
        }
    }

    @ViewBuilder
    private var transcriptSection: some View {
        if !transcriptLines.isEmpty {
            CallDetailSection(title: String(localized: "Conversation"), systemImage: "text.bubble") {
                VStack(spacing: 12) {
                    ForEach(Array(transcriptLines.enumerated()), id: \.offset) { _, line in
                        TranscriptRow(line: line)
                    }
                }
            }
        }
    }

    private func actionButton(title: String, systemImage: String, destination: URL, isPrimary: Bool) -> some View {
        Button {
            let currentAuth = appState.currentAuthContext()
            HistoricalCallLinkRouter.perform(
                url: destination,
                lease: lease,
                currentAuth: currentAuth,
                isFixture: isFixtureMode
            ) { targetURL in
                openURL(targetURL)
            }
        } label: {
            Label(title, systemImage: systemImage)
                .font(.headline)
                .frame(maxWidth: .infinity, minHeight: 44)
        }
        .buttonStyle(.plain)
        .foregroundStyle(isPrimary ? .white : .primary)
        .background {
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(isPrimary ? Color.accentColor : Color(.secondarySystemGroupedBackground))
        }
        .overlay {
            if !isPrimary {
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .stroke(Color(.separator), lineWidth: 0.5)
            }
        }
        .disabled(isFixtureMode)
        .accessibilityLabel(title)
    }

    private var callerInitials: String {
        let name = call.callerName
        guard !name.isEmpty else { return "" }
        let parts = name.split(separator: " ")
        if parts.count >= 2 {
            return "\(parts[0].prefix(1))\(parts[1].prefix(1))".uppercased()
        }
        return String(name.prefix(2)).uppercased()
    }

    private var callbackPhone: String {
        call.callbackNumber ?? call.callerPhone
    }

    private var transcriptLines: [String] {
        call.transcript
            .components(separatedBy: "\n")
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
    }

    private var outcomeText: String {
        switch call.outcome {
        case "picked_up": return String(localized: "Answered")
        case "voicemail": return String(localized: "Voicemail")
        case "ignored", "declined": return String(localized: "Ignored")
        case "spam", "blocked": return String(localized: "Blocked")
        default: return String(localized: "Screened")
        }
    }

    private var outcomeIcon: String {
        switch call.outcome {
        case "picked_up": return "phone.fill"
        case "voicemail": return "recordingtape"
        case "ignored", "declined": return "phone.arrow.down.left"
        case "spam", "blocked": return "hand.raised.fill"
        default: return "phone.badge.checkmark"
        }
    }

    private var outcomeColor: Color {
        switch call.outcome {
        case "picked_up": return .green
        case "voicemail": return .blue
        case "ignored", "declined": return .orange
        case "spam", "blocked": return .red
        default: return .secondary
        }
    }

    private func phoneURL(for phone: String) -> URL {
        let digits = phone.filter { $0.isNumber || $0 == "+" }
        return URL(string: "tel://\(digits)")!
    }

    private func messageURL(for phone: String) -> URL {
        let digits = phone.filter { $0.isNumber || $0 == "+" }
        return URL(string: "sms:\(digits)")!
    }
}

private struct CallDetailSection<Content: View>: View {
    let title: String
    let systemImage: String
    private let content: Content

    init(title: String, systemImage: String, @ViewBuilder content: () -> Content) {
        self.title = title
        self.systemImage = systemImage
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label(title, systemImage: systemImage)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.secondary)

            VStack(alignment: .leading, spacing: 0) {
                content
            }
            .padding(14)
            .background(Color(.secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .stroke(Color(.separator), lineWidth: 0.5)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct StatusPill: View {
    let title: String
    let systemImage: String
    let color: Color

    var body: some View {
        Label(title, systemImage: systemImage)
            .font(.footnote.weight(.semibold))
            .foregroundStyle(color)
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(color.opacity(0.12), in: Capsule())
            .accessibilityElement(children: .combine)
    }
}

private struct DetailRow: View {
    let title: String
    let value: String
    let systemImage: String
    let tint: Color

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Image(systemName: systemImage)
                .font(.body)
                .foregroundStyle(tint)
                .frame(width: 24)

            Text(title)
                .font(.body)

            Spacer(minLength: 16)

            Text(value)
                .font(.body)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.trailing)
        }
        .padding(.vertical, 9)
        .accessibilityElement(children: .combine)
    }
}

struct TranscriptRow: View {
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

    private var linkedText: AttributedString {
        var result = AttributedString(text)
        let pattern = #"[\+]?[\d][\d\s\-\.\(\)]{5,}[\d]"#
        guard let regex = try? NSRegularExpression(pattern: pattern) else { return result }
        let nsText = text as NSString
        let matches = regex.matches(in: text, range: NSRange(location: 0, length: nsText.length))
        for match in matches {
            guard let swiftRange = Range(match.range, in: text) else { continue }
            let phone = String(text[swiftRange])
            let digits = phone.filter { $0.isNumber || $0 == "+" }
            if let attrRange = result.range(of: phone),
               let url = URL(string: "tel://\(digits)") {
                result[attrRange].link = url
            }
        }
        return result
    }

    var body: some View {
        HStack(alignment: .bottom, spacing: 8) {
            if isKevin {
                Spacer(minLength: 36)
            }

            if isCaller {
                speakerAvatar
            }

            VStack(alignment: isKevin ? .trailing : .leading, spacing: 4) {
                if !speaker.isEmpty {
                    Text(speaker)
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(.secondary)
                }

                Text(linkedText)
                    .font(.body)
                    .foregroundStyle(isKevin ? .white : Color.hkInk)
                    .textSelection(.enabled)
                    .padding(.horizontal, 13)
                    .padding(.vertical, 10)
                    .background(
                        isKevin ? Color.accentColor : Color(.tertiarySystemGroupedBackground),
                        in: RoundedRectangle(cornerRadius: 18, style: .continuous)
                    )
            }
            .frame(maxWidth: 290, alignment: isKevin ? .trailing : .leading)

            if isKevin {
                speakerAvatar
            }

            if !isKevin {
                Spacer(minLength: 36)
            }
        }
        .frame(maxWidth: .infinity, alignment: isKevin ? .trailing : .leading)
        .accessibilityElement(children: .combine)
    }

    private var isKevin: Bool {
        speaker == "Kevin"
    }

    private var isCaller: Bool {
        speaker == "Caller"
    }

    private var speakerAvatar: some View {
        ZStack {
            Circle()
                .fill(isKevin ? Color.accentColor.opacity(0.16) : Color(.tertiarySystemFill))
                .frame(width: 30, height: 30)

            if isKevin {
                Image(systemName: "phone.bubble.left.fill")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Color.accentColor)
            } else {
                Image(systemName: "person.fill")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
            }
        }
    }
}
