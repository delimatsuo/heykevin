import SwiftUI

/// Main native screened call journal view.
/// Integrates bounded pagination, date grouping, live active call cards,
/// search and filtering before pagination, and single-owner account sheet navigation.
struct CallHistoryView: View {
    @EnvironmentObject var appState: AppState
    @StateObject private var historyModel: CallHistoryModel
    var onOpenSettings: (() -> Void)? = nil

    init(historyModel: CallHistoryModel? = nil, onOpenSettings: (() -> Void)? = nil) {
        if let model = historyModel {
            _historyModel = StateObject(wrappedValue: model)
        } else {
            #if DEBUG
            if AppStoreScreenshotFixtures.isEnabled {
                let fixtureCalls = AppStoreScreenshotFixtures.seededCalls
                let model = CallHistoryModel(
                    authProvider: { AppState.shared.currentAuthContext() },
                    fetchCalls: { _ in fixtureCalls },
                    markServerRead: { _, _ in },
                    loadReadIds: { _ in [] },
                    commitReadState: { _, _, _ in },
                    resetEffect: { _ in },
                    reauthEffect: { _ in },
                    clock: { AppStoreScreenshotFixtures.now }
                )
                _historyModel = StateObject(wrappedValue: model)
            } else {
                _historyModel = StateObject(wrappedValue: CallHistoryModel())
            }
            #else
            _historyModel = StateObject(wrappedValue: CallHistoryModel())
            #endif
        }
        self.onOpenSettings = onOpenSettings
    }

    var body: some View {
        NavigationStack {
            List {
                // Top prominent active call card if a call is currently live
                if appState.hasActiveCall {
                    Section {
                        ActiveCallCard()
                            .listRowInsets(EdgeInsets(top: 8, leading: 16, bottom: 8, trailing: 16))
                            .listRowBackground(Color.clear)
                            .listRowSeparator(.hidden)
                    }
                }

                // Filter tabs (All, Unread, Spam)
                Section {
                    filterPicker
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
                                NavigationLink(destination: CallDetailView(call: call, historyModel: historyModel)) {
                                    CallRow(call: call, isUnread: historyModel.isCallUnread(call))
                                }
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
            .background(Color.hkWarmCanvas)
            .navigationTitle(String(localized: "Calls"))
            .searchable(
                text: $historyModel.searchQuery,
                placement: .navigationBarDrawer(displayMode: .automatic),
                prompt: Text(String(localized: "Search by name, number or excerpt"))
            )
            .accessibilityIdentifier("calls.search")
            .onChange(of: historyModel.searchQuery) { _, newQuery in
                historyModel.setSearchQuery(newQuery)
            }
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    if historyModel.unreadCount > 0 {
                        Button(String(localized: "Mark All Read")) {
                            historyModel.markAllAsRead()
                        }
                        .font(.subheadline.weight(.medium))
                        .accessibilityIdentifier("calls.markAllRead")
                    }
                }

                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        onOpenSettings?()
                    } label: {
                        Image(systemName: "person.crop.circle")
                            .font(.system(size: 18))
                    }
                    .accessibilityLabel(String(localized: "Account Settings"))
                    .accessibilityIdentifier("nav.settings")
                }
            }
            .sheet(isPresented: Binding(
                get: { !appState.notificationCallSid.isEmpty },
                set: { if !$0 { appState.notificationCallSid = ""; appState.notificationCallMessage = "" } }
            )) {
                NavigationStack {
                    if let matchedCall = historyModel.call(for: appState.notificationCallSid) {
                        CallDetailView(call: matchedCall, historyModel: historyModel)
                    } else {
                        ContentUnavailableView(
                            String(localized: "Call details unavailable"),
                            systemImage: "phone.badge.questionmark",
                            description: Text(appState.notificationCallMessage.isEmpty
                                ? String(localized: "This call is no longer in your history.")
                                : appState.notificationCallMessage)
                        )
                    }
                }
            }
            .refreshable {
                guard !AppStoreScreenshotFixtures.isEnabled else { return }
                await historyModel.loadCalls()
            }
            .task {
                #if DEBUG
                if AppStoreScreenshotFixtures.isEnabled {
                    let fixtureCalls = AppStoreScreenshotFixtures.seededCalls
                    let normalized = CallHistoryModel.normalize(rawCalls: fixtureCalls, now: AppStoreScreenshotFixtures.now)
                    if historyModel.allCalls.isEmpty {
                        historyModel.invalidate()
                    }
                    return
                }
                #endif
                await historyModel.loadCalls()
            }
            .onAppear {
                #if DEBUG
                if AppStoreScreenshotFixtures.isEnabled { return }
                #endif
                StoreReviewManager.shared.requestReviewIfEligible()
            }
            .onReceive(NotificationCenter.default.publisher(for: CallSessionEpoch.didChangeNotification)) { _ in
                historyModel.invalidate()
                Task { await historyModel.loadCalls() }
            }
        }
    }

    // MARK: - Subviews

    private var filterPicker: some View {
        Picker("Filter", selection: Binding(
            get: { historyModel.selectedFilter },
            set: { historyModel.setFilter($0) }
        )) {
            ForEach(CallHistoryFilter.allCases) { filter in
                Text(filter.localizedTitle).tag(filter)
            }
        }
        .pickerStyle(.segmented)
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
                .padding(.top, 4)
            } else if state == .noUnread || state == .noSpam {
                Button(String(localized: "Show all")) {
                    historyModel.setFilter(.all)
                }
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(Color.hkCobalt)
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
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 10)
                        .background(Color.hkCobalt.opacity(0.08), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("calls.showMore")
            }

            if !historyModel.showingCountLabel.isEmpty {
                Text(historyModel.showingCountLabel)
                    .font(.footnote)
                    .foregroundStyle(Color.hkInkSecondary)
                    .accessibilityIdentifier("calls.count")
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
    let call: CallRecord
    var isUnread: Bool = false

    var body: some View {
        HStack(alignment: .center, spacing: HKSpace.md) {
            HKAvatar(name: call.callerName, phone: call.callerPhone, size: 40)

            VStack(alignment: .leading, spacing: 2) {
                Text(call.displayName)
                    .font(.system(size: 15, weight: isUnread ? .bold : .medium))
                    .foregroundStyle(call.isSpamOrBlocked ? Color.hkRed : Color.hkInk)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .monospacedDigit()

                Text(call.callerExcerpt)
                    .font(.system(size: 13))
                    .foregroundStyle(isUnread ? Color.hkInk : Color.hkInkSecondary)
                    .lineLimit(1)
                    .truncationMode(.tail)
            }

            Spacer(minLength: 6)

            VStack(alignment: .trailing, spacing: 5) {
                Text(Self.timeLabel(for: call.timestamp))
                    .font(.system(size: 12, weight: isUnread ? .semibold : .regular))
                    .foregroundStyle(isUnread ? Color.hkCobalt : Color.hkInkSecondary)
                    .monospacedDigit()

                if isUnread {
                    Circle()
                        .fill(Color.hkCobalt)
                        .frame(width: 8, height: 8)
                }
            }
            .frame(minWidth: 44, alignment: .trailing)
        }
        .padding(.vertical, 4)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(call.displayName), \(call.callerExcerpt), \(Self.timeLabel(for: call.timestamp))\(isUnread ? ", unread" : "")")
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

// MARK: - Call Detail View

struct CallDetailView: View {
    @EnvironmentObject var appState: AppState
    @Environment(\.openURL) private var openURL
    let call: CallRecord
    var historyModel: CallHistoryModel? = nil
    var authContext: CallAuthContext? = nil

    @State private var appointmentConfirmed = false
    @State private var callerWasNotified = false
    @State private var isConfirming = false
    @State private var confirmError = ""
    @State private var capturedAuth: CallAuthContext? = nil

    private var callerWasTexted: Bool {
        callerWasNotified || call.appointmentCallerNotified
    }

    private var appointmentStatus: String {
        appointmentConfirmed ? "confirmed" : (call.appointmentStatus ?? "")
    }

    var body: some View {
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
        .background(Color(.systemGroupedBackground))
        .navigationTitle(String(localized: "Details"))
        .navigationBarTitleDisplayMode(.inline)
        .onAppear {
            let auth = authContext ?? appState.currentAuthContext()
            capturedAuth = auth
            if auth.isValid {
                if let model = historyModel {
                    model.markAsRead(callId: call.id, expectedAuth: auth)
                } else {
                    appState.markCallAsRead(call.id)
                    if !AppStoreScreenshotFixtures.isEnabled {
                        Task { await APIClient.shared.markCallsRead([call.id], authContext: auth) }
                    }
                }
            }
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

                Link(call.formattedPhone, destination: phoneURL(for: call.callerPhone))
                    .font(.body)
                    .foregroundStyle(.tint)
            }

            ViewThatFits(in: .horizontal) {
                HStack(spacing: 8) {
                    StatusPill(title: outcomeText, systemImage: outcomeIcon, color: outcomeColor)
                    if call.trustScore > 0 {
                        StatusPill(title: "\(call.trustScore)/100", systemImage: "checkmark.shield.fill", color: trustColor)
                    }
                }

                VStack(spacing: 8) {
                    StatusPill(title: outcomeText, systemImage: outcomeIcon, color: outcomeColor)
                    if call.trustScore > 0 {
                        StatusPill(title: "\(call.trustScore)/100", systemImage: "checkmark.shield.fill", color: trustColor)
                    }
                }
            }

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
        .disabled(isConfirming)
        .accessibilityLabel(String(localized: "Confirm"))
        .accessibilityHint(formattedAppointmentStart)
    }

    private func confirmAppointment() async {
        guard !isConfirming else { return }
        let currentAuth = appState.currentAuthContext()
        guard let auth = capturedAuth, auth == currentAuth, auth.isValid else {
            confirmError = String(localized: "Authentication required.")
            return
        }
        isConfirming = true
        confirmError = ""
        do {
            callerWasNotified = try await APIClient.shared.confirmAppointment(callSid: call.id, authContext: auth)
            guard appState.currentAuthContext() == auth else { return }
            appointmentConfirmed = true
            StoreReviewManager.shared.recordAppointmentConfirmed()
            StoreReviewManager.shared.requestReviewIfEligible()
        } catch let failure as AppointmentConfirmFailure {
            confirmError = failure.localizedDescription
        } catch {
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
            if call.trustScore > 0 {
                Divider()
                DetailRow(
                    title: String(localized: "Trust"),
                    value: trustLabel,
                    systemImage: "checkmark.shield.fill",
                    tint: trustColor
                )
            }
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
            guard let auth = capturedAuth, auth == currentAuth, auth.isValid else { return }
            openURL(destination)
        } label: {
            Label(title, systemImage: systemImage)
                .font(.headline)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 12)
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

    private var trustLabel: String {
        switch call.trustScore {
        case 85...100: return String(localized: "Trusted \(call.trustScore)/100")
        case 45..<85: return String(localized: "Review \(call.trustScore)/100")
        default: return String(localized: "Unknown \(call.trustScore)/100")
        }
    }

    private var trustColor: Color {
        switch call.trustScore {
        case 85...100: return .green
        case 45..<85: return .orange
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
                    .foregroundStyle(isKevin ? .white : .primary)
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
