import Foundation

/// Represents a screened call record.
struct CallRecord: Identifiable, Codable {
    let id: String          // call_sid
    let callerPhone: String
    let callerName: String
    let timestamp: Date
    let trustScore: Int
    let outcome: String     // picked_up, voicemail, ignored, spam
    let transcript: String
    let voicemailURL: String?

    enum CodingKeys: String, CodingKey {
        case id = "call_sid"
        case callerPhone = "caller_phone"
        case callerName = "caller_name"
        case timestamp
        case trustScore = "trust_score"
        case outcome
        case transcript
        case voicemailURL = "voicemail_url"
    }
}
