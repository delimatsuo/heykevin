import SwiftUI

struct CallHistoryView: View {
    @EnvironmentObject var appState: AppState
    @State private var calls: [CallRecord] = []
    @State private var isLoading = false
    @State private var errorMessage = ""

    var body: some View {
        NavigationStack {
            List {
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
                            calls.forEach { appState.markCallAsRead($0.id) }
                            appState.updateUnreadCount(calls: calls)
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
    let call: CallRecord

    var body: some View {
        List {
            // Caller info
            Section {
                HStack {
                    ZStack {
                        Circle()
                            .fill(Color(.systemGray4))
                            .frame(width: 56, height: 56)
                        if !callerInitials.isEmpty {
                            Text(callerInitials)
                                .font(.title3.weight(.medium))
                                .foregroundStyle(.white)
                        } else {
                            Image(systemName: "phone.fill")
                                .font(.title3)
                                .foregroundStyle(.white)
                        }
                    }

                    VStack(alignment: .leading, spacing: 4) {
                        Text(call.callerName.isEmpty ? formattedPhone : call.callerName)
                            .font(.title3.weight(.semibold))
                        if !call.callerName.isEmpty {
                            Text(formattedPhone)
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                        }
                    }
                    .padding(.leading, 8)
                }
                .listRowBackground(Color.clear)
            }

            // Call details
            Section(String(localized: "Details")) {
                HStack {
                    Text(String(localized: "Date"))
                    Spacer()
                    Text(call.timestamp, format: .dateTime.month(.abbreviated).day().hour().minute())
                        .foregroundStyle(.secondary)
                }

                HStack {
                    Text(String(localized: "Action"))
                    Spacer()
                    Label(outcomeText, systemImage: outcomeIcon)
                        .foregroundStyle(outcomeColor)
                }

                if call.trustScore > 0 {
                    HStack {
                        Text(String(localized: "Trust Score"))
                        Spacer()
                        Text("\(call.trustScore)/100")
                            .foregroundStyle(.secondary)
                    }
                }
            }

            // Transcript
            if !call.transcript.isEmpty {
                Section(String(localized: "Transcript")) {
                    let lines = call.transcript.components(separatedBy: "\n").filter { !$0.isEmpty }
                    ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                        TranscriptRow(line: line)
                    }
                }
            }

            // Actions — prefer callback number if the caller gave one
            Section {
                let callbackPhone = call.callbackNumber ?? call.callerPhone
                Button {
                    let digits = callbackPhone.filter { $0.isNumber }
                    if let url = URL(string: "tel://\(digits)") {
                        UIApplication.shared.open(url)
                    }
                } label: {
                    Label(String(localized: "Call Back \(PhoneFormatter.format(callbackPhone))"), systemImage: "phone.fill")
                }

                Button {
                    let digits = callbackPhone.filter { $0.isNumber }
                    if let url = URL(string: "sms:\(digits)") {
                        UIApplication.shared.open(url)
                    }
                } label: {
                    Label(String(localized: "Send Text"), systemImage: "message.fill")
                }
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle(String(localized: "Call Details"))
        .navigationBarTitleDisplayMode(.inline)
        .onAppear { appState.markCallAsRead(call.id) }
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
        VStack(alignment: .leading, spacing: 2) {
            if !speaker.isEmpty {
                Text(speaker)
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(speaker == "Kevin" ? .blue : .secondary)
            }
            Text(linkedText)
                .font(.subheadline)
        }
    }
}
