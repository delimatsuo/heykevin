import Foundation

/// Communicates with the Kevin backend API.
actor APIClient {
    static let shared = APIClient()

    private let baseURL: String

    init() {
        self.baseURL = AppState.shared.backendURL
    }

    // MARK: - Device Registration

    /// Register VoIP device token with the backend.
    func registerDevice(voipToken: String) async {
        do {
            let url = URL(string: "\(baseURL)/api/register-device")!
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONEncoder().encode([
                "voip_token": voipToken,
                "platform": "ios",
            ])

            let (_, response) = try await URLSession.shared.data(for: request)
            if let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 {
                print("Device registered successfully")
                await MainActor.run {
                    AppState.shared.isRegistered = true
                    AppState.shared.voipToken = voipToken
                }
            }
        } catch {
            print("Device registration failed: \(error)")
        }
    }

    // MARK: - VoIP Token

    /// Request a Twilio access token to join a conference.
    func getVoIPToken(callSid: String, conferenceName: String) async -> String? {
        do {
            let url = URL(string: "\(baseURL)/api/voip-token")!
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONEncoder().encode([
                "call_sid": callSid,
                "conference_name": conferenceName,
            ])

            let (data, response) = try await URLSession.shared.data(for: request)
            if let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 {
                let result = try JSONDecoder().decode([String: String].self, from: data)
                return result["token"]
            }
        } catch {
            print("VoIP token request failed: \(error)")
        }
        return nil
    }

    // MARK: - Call Actions

    /// Send a call action (accept, decline, voicemail, text_reply).
    func sendCallAction(callSid: String, action: String) async {
        do {
            let url = URL(string: "\(baseURL)/api/call-action")!
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONEncoder().encode([
                "call_sid": callSid,
                "action": action,
            ])

            let (_, _) = try await URLSession.shared.data(for: request)
        } catch {
            print("Call action failed: \(error)")
        }
    }

    // MARK: - Call History

    /// Fetch call history.
    func getCallHistory() async -> [CallRecord] {
        do {
            let url = URL(string: "\(baseURL)/api/calls")!
            let (data, _) = try await URLSession.shared.data(from: url)
            let result = try JSONDecoder().decode([String: [CallRecord]].self, from: data)
            return result["calls"] ?? []
        } catch {
            print("Call history fetch failed: \(error)")
            return []
        }
    }
}
