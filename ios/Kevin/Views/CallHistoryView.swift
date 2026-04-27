import SwiftUI

struct CallHistoryView: View {
    @EnvironmentObject var appState: AppState
    @State private var calls: [CallRecord] = []
    @State private var isLoading = false
    @State private var errorMessage = ""

    var body: some View {
        NavigationStack {
            List {
                // Screen all calls toggle — inverted from ringThroughContacts backend field
                Section {
                    Toggle(isOn: Binding(
                        get: { !appState.ringThroughContacts },
                        set: { appState.ringThroughContacts = !$0 }
                    )) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(String(localized: "Screen all calls"))
                                .font(.subheadline.weight(.medium))
                            Text(!appState.ringThroughContacts
                                 ? String(localized: "Kevin screens everyone, including contacts")
                                 : String(localized: "Contacts bypass Kevin and ring directly"))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                    .onChange(of: appState.ringThroughContacts) { _, newValue in
                        Task {
                            guard !appState.contractorId.isEmpty else { return }
                            _ = try? await APIClient.shared.patchContractor(
                                appState.contractorId,
                                body: ["ring_through_contacts": newValue]
                            )
                        }
                    }
                }

                if !errorMessage.isEmpty {
                    Section {
                        HStack {
                            Image(systemName: "exclamationmark.triangle")
                                .foregroundStyle(.red)
                            Text(errorMessage)
                                .foregroundStyle(.red)
                                .font(.subheadline)
                        }
                        .listRowBackground(Color.clear)
                    }
                } else if calls.isEmpty && !isLoading {
                    Section {
                        Text(String(localized: "No screened calls yet. When someone calls, Kevin will screen and it will show here."))
                            .foregroundStyle(.secondary)
                            .font(.subheadline)
                            .listRowBackground(Color.clear)
                    }
                }

                if !calls.isEmpty {
                    Section {
                        ForEach(calls) { call in
                            NavigationLink(destination: CallDetailView(call: call)) {
                                CallRow(call: call, isUnread: appState.isCallUnread(call))
                            }
                        }
                    }
                }
            }
            .listStyle(.insetGrouped)
            .navigationTitle(String(localized: "Recents"))
            .toolbar {
                if calls.contains(where: { appState.isCallUnread($0) }) {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button(String(localized: "Mark All Read")) {
                            let unread = calls.filter { appState.isCallUnread($0) }.map { $0.id }
                            unread.forEach { appState.markCallAsRead($0) }
                            appState.updateUnreadCount(calls: calls)
                            Task { await APIClient.shared.markCallsRead(unread) }
                        }
                        .font(.subheadline)
                    }
                }
            }
            .refreshable { await loadCalls() }
            .task { await loadCalls() }
        }
    }

    private func loadCalls() async {
        isLoading = true
        errorMessage = ""
        do {
            calls = try await APIClient.shared.getCallHistory()
            await MainActor.run { appState.updateUnreadCount(calls: calls) }
        } catch {
            errorMessage = String(localized: "Failed to load calls: \(error.localizedDescription)")
        }
        isLoading = false
    }
}

// MARK: - Call Row

struct CallRow: View {
    let call: CallRecord
    var isUnread: Bool = false

    var body: some View {
        HStack(spacing: 8) {
            if isUnread {
                Circle()
                    .fill(.blue)
                    .frame(width: 10, height: 10)
            }

            VStack(alignment: .leading, spacing: 2) {
                Text(displayName)
                    .font(.body)
                    .fontWeight(isUnread ? .semibold : .regular)
                    .foregroundStyle(call.outcome == "spam" ? .red : .primary)

                Text(formattedPhone)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Spacer()

            VStack(alignment: .trailing, spacing: 2) {
                Text(Self.timeLabel(for: call.timestamp))
                    .font(.caption)
                    .foregroundStyle(.secondary)

                Label(outcomeText, systemImage: outcomeIcon)
                    .font(.caption2)
                    .foregroundStyle(outcomeColor)
            }
        }
    }

    static func timeLabel(for date: Date) -> String {
        let calendar = Calendar.current
        if calendar.isDateInToday(date) {
            return date.formatted(date: .omitted, time: .shortened) // "10:51 AM"
        } else if calendar.isDateInYesterday(date) {
            return String(localized: "Yesterday")
        } else {
            return date.formatted(.dateTime.month(.abbreviated).day()) // "Apr 6"
        }
    }

    private var displayName: String {
        call.callerName.isEmpty ? formattedPhone : call.callerName
    }

    private var formattedPhone: String {
        PhoneFormatter.format(call.callerPhone)
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
}

// MARK: - Call Detail View

struct CallDetailView: View {
    @EnvironmentObject var appState: AppState
    @Environment(\.openURL) private var openURL
    let call: CallRecord

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
            appState.markCallAsRead(call.id)
            Task { await APIClient.shared.markCallsRead([call.id]) }
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
                Text(displayName)
                    .font(.title2.weight(.semibold))
                    .multilineTextAlignment(.center)
                    .lineLimit(2)
                    .minimumScaleFactor(0.82)

                Link(formattedPhone, destination: phoneURL(for: call.callerPhone))
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
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 12) {
                actionButton(title: String(localized: "Call Back"), systemImage: "phone.fill", destination: phoneURL(for: callbackPhone), isPrimary: true)
                actionButton(title: String(localized: "Message"), systemImage: "message.fill", destination: messageURL(for: callbackPhone), isPrimary: false)
            }

            VStack(spacing: 10) {
                actionButton(title: String(localized: "Call Back"), systemImage: "phone.fill", destination: phoneURL(for: callbackPhone), isPrimary: true)
                actionButton(title: String(localized: "Message"), systemImage: "message.fill", destination: messageURL(for: callbackPhone), isPrimary: false)
            }
        }
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

    private var displayName: String {
        call.callerName.isEmpty ? formattedPhone : call.callerName
    }

    private var formattedPhone: String {
        PhoneFormatter.format(call.callerPhone)
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

    /// Build an AttributedString with phone numbers as tappable tel: links.
    private var linkedText: AttributedString {
        var result = AttributedString(text)
        // Match sequences of 7+ digits (possibly with spaces, dashes, parens, dots, or plus)
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
