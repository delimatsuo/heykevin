import Foundation

enum AccountDeletionOutcome: Equatable {
    case deleted
    case failed
}

/// Decides whether the server confirmed an account deletion. Local state
/// (contractor ID, token, onboarding) may only be cleared on `.deleted`;
/// clearing it on failure strands the user — logged out locally while the
/// account stays active and billing, with no credentials left to retry.
enum AccountDeletionResponseParser {
    static func parse(response: URLResponse?, data: Data? = nil) -> AccountDeletionOutcome {
        guard let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) else {
            return .failed
        }
        // Backends deployed before the 5xx fix report failure as
        // 200 {"status": "error", ...}. Treat any parseable non-"ok" status
        // as failure; a missing or unparseable body on a 2xx stays success.
        if let data,
           let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
           let status = json["status"] as? String,
           status != "ok" {
            return .failed
        }
        return .deleted
    }
}
