import Foundation

/// Represents a screened call record.
struct CallRecord: Identifiable {
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
        appointmentTitle: String? = nil
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
    }

    /// Whether the caller left a message (has transcript with caller speech beyond the initial exchange).
    var hasMessage: Bool {
        if outcome == "spam" || outcome == "blocked" { return false }
        let callerLines = transcript.components(separatedBy: "\n")
            .filter { $0.hasPrefix("Caller:") }
        return callerLines.count >= 2
    }
}
