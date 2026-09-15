import Foundation

/// Represents a screened call record.
struct CallRecord: Identifiable, Equatable, Hashable, Sendable {
    let id: String
    let callerPhone: String
    let callerName: String
    let timestamp: Date
    let trustScore: Int
    let outcome: String
    let transcript: String
    let voicemailURL: String?
    let callbackNumber: String?
    let readOnServer: Bool  // persisted read state from Firestore
    let appointmentStatus: String?
    let appointmentStartTime: String?
    let appointmentTitle: String?
    /// Whether the backend recorded that it texted the caller, from the
    /// persisted `caller_notified_at`. Without this the confirmed card
    /// suppressed the line for every historical confirmation, because the
    /// in-view state only learns the answer from a confirm this session.
    let appointmentCallerNotified: Bool

    init(
        id: String,
        callerPhone: String,
        callerName: String,
        timestamp: Date,
        trustScore: Int,
        outcome: String,
        transcript: String,
        voicemailURL: String?,
        callbackNumber: String?,
        readOnServer: Bool,
        appointmentStatus: String? = nil,
        appointmentStartTime: String? = nil,
        appointmentTitle: String? = nil,
        appointmentCallerNotified: Bool = false
    ) {
        self.id = id
        self.callerPhone = callerPhone
        self.callerName = callerName
        self.timestamp = timestamp
        self.trustScore = trustScore
        self.outcome = outcome
        self.transcript = transcript
        self.voicemailURL = voicemailURL
        self.callbackNumber = callbackNumber
        self.readOnServer = readOnServer
        self.appointmentStatus = appointmentStatus
        self.appointmentStartTime = appointmentStartTime
        self.appointmentTitle = appointmentTitle
        self.appointmentCallerNotified = appointmentCallerNotified
    }

    /// Whether the caller left a message (has transcript with caller speech beyond the initial exchange).
    var hasMessage: Bool {
        if outcome == "spam" || outcome == "blocked" { return false }
        let callerLines = transcript.components(separatedBy: "\n")
            .filter { $0.hasPrefix("Caller:") }
        return callerLines.count >= 2
    }

    var isSpamOrBlocked: Bool {
        outcome == "spam" || outcome == "blocked"
    }

    var formattedPhone: String {
        PhoneFormatter.format(callerPhone)
    }

    var displayName: String {
        let trimmedName = callerName.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmedName.isEmpty {
            return trimmedName
        }
        let phone = formattedPhone
        return phone.isEmpty ? String(localized: "Unknown Caller") : phone
    }

    /// Digits-only normalized representation for search matching
    var normalizedPhoneDigits: String {
        callerPhone.filter { $0.isNumber }
    }

    /// Kevin's truthful one-line caller excerpt or outcome fallback.
    /// Never claims to be an AI summary.
    var callerExcerpt: String {
        if isSpamOrBlocked {
            return outcomeFallback
        }
        let callerLines = transcript
            .components(separatedBy: "\n")
            .compactMap { line -> String? in
                guard line.hasPrefix("Caller:") else { return nil }
                let body = line.dropFirst("Caller:".count).trimmingCharacters(in: .whitespaces)
                return body.isEmpty ? nil : body
            }
        if let longest = callerLines.max(by: { $0.count < $1.count }), longest.count > 12 {
            return longest
        }
        if let first = callerLines.first {
            return first
        }
        return outcomeFallback
    }

    var outcomeFallback: String {
        switch outcome {
        case "picked_up":           return String(localized: "Answered.")
        case "voicemail":           return String(localized: "Left a voicemail.")
        case "ignored", "declined": return String(localized: "Kevin handled the call.")
        case "spam":                return String(localized: "Marked as spam.")
        case "blocked":             return String(localized: "Blocked call.")
        default:                    return String(localized: "Kevin screened the call.")
        }
    }
}
