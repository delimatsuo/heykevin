import Foundation
import CoreFoundation

extension APIClient {
    // MARK: - Acquisition Measurement

    func getAcquisitionStatus(bearerToken: String) async -> Bool {
        guard !bearerToken.isEmpty, let url = URL(string: "\(baseURL)/api/acquisition/status") else {
            return false
        }

        var request = URLRequest(url: url)
        request.timeoutInterval = 10
        request.setValue("Bearer \(bearerToken)", forHTTPHeaderField: "Authorization")

        do {
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200,
                  let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let eligible = CallActionResponseParser.boolean(json["eligible"]) else {
                return false
            }
            return eligible
        } catch {
            return false
        }
    }

    func postAppleAdsAttribution(token: String, bearerToken: String) async throws -> AttributionPostResult {
        guard !bearerToken.isEmpty, !token.isEmpty,
              let url = URL(string: "\(baseURL)/api/acquisition/apple-ads") else {
            return AttributionPostResult(status: .ineligible, retryAfterSeconds: nil)
        }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 10
        request.setValue("Bearer \(bearerToken)", forHTTPHeaderField: "Authorization")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["token": token])

        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            return AttributionPostResult(status: .retryable, retryAfterSeconds: 5)
        }

        return AttributionResponseParser.parse(data: data, response: http)
    }
}

enum AttributionResponseParser {
    static func parse(data: Data, response: HTTPURLResponse) -> AttributionPostResult {
        let statusCode = response.statusCode
        if statusCode == 401 || statusCode == 403 {
            return AttributionPostResult(status: .ineligible, retryAfterSeconds: nil)
        }
        guard statusCode == 200 else {
            return AttributionPostResult(status: .retryable, retryAfterSeconds: 5)
        }

        guard let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            return AttributionPostResult(status: .retryable, retryAfterSeconds: 5)
        }

        // Exact keys only: "status" and optional "retry_after_seconds"
        for key in json.keys {
            if key != "status" && key != "retry_after_seconds" {
                return AttributionPostResult(status: .retryable, retryAfterSeconds: 5)
            }
        }

        guard let rawStatus = json["status"] as? String else {
            return AttributionPostResult(status: .retryable, retryAfterSeconds: 5)
        }

        let status: AttributionStatus
        switch rawStatus {
        case "recorded":
            status = .recorded
        case "already_recorded":
            status = .alreadyRecorded
        case "unattributed":
            status = .unattributed
        case "ineligible":
            status = .ineligible
        case "disabled":
            status = .disabled
        case "retryable":
            status = .retryable
        case "exhausted":
            status = .exhausted
        default:
            return AttributionPostResult(status: .retryable, retryAfterSeconds: 5)
        }

        if let rawRetry = json["retry_after_seconds"] {
            guard let num = rawRetry as? NSNumber,
                  CFGetTypeID(num) != CFBooleanGetTypeID() else {
                return AttributionPostResult(status: .retryable, retryAfterSeconds: 5)
            }
            let d = num.doubleValue
            guard d.isFinite, floor(d) == d, d >= 5.0, d <= 15.0 else {
                return AttributionPostResult(status: .retryable, retryAfterSeconds: 5)
            }
            let delay = Int(d)

            if status != .retryable {
                return AttributionPostResult(status: .retryable, retryAfterSeconds: 5)
            }
            return AttributionPostResult(status: .retryable, retryAfterSeconds: delay)
        } else {
            if status == .retryable {
                return AttributionPostResult(status: .retryable, retryAfterSeconds: 5)
            }
            return AttributionPostResult(status: status, retryAfterSeconds: nil)
        }
    }
}

enum AttributionStatus: String, Sendable, Equatable {
    case recorded
    case alreadyRecorded = "already_recorded"
    case unattributed
    case ineligible
    case disabled
    case retryable
    case exhausted
    case unknown
}

struct AttributionPostResult: Sendable, Equatable {
    let status: AttributionStatus
    let retryAfterSeconds: Int?
}
