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

    /// Whether the caller left a message (has transcript with caller speech beyond the initial exchange).
    var hasMessage: Bool {
        if outcome == "spam" || outcome == "blocked" { return false }
        let callerLines = transcript.components(separatedBy: "\n")
            .filter { $0.hasPrefix("Caller:") }
        return callerLines.count >= 2
    }
}
