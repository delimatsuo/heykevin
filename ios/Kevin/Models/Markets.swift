import Foundation

enum MarketStatus: String, Codable, Sendable, Equatable {
    case available
    case qualificationRequired = "qualification_required"
    case unsupported
}

struct MarketInfo: Codable, Equatable, Sendable {
    let countryCode: String
    let status: MarketStatus

    init(countryCode: String, status: MarketStatus) {
        self.countryCode = countryCode.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
        self.status = status
    }

    enum CodingKeys: String, CodingKey {
        case countryCode = "country_code"
        case status
    }
}

struct MarketsResponse: Codable, Equatable, Sendable {
    let markets: [MarketInfo]
    let qualificationOrder: [String]?

    init(markets: [MarketInfo], qualificationOrder: [String]? = nil) {
        self.markets = markets
        self.qualificationOrder = qualificationOrder
    }

    enum CodingKeys: String, CodingKey {
        case markets
        case qualificationOrder = "qualification_order"
    }

    func status(for countryCode: String) -> MarketStatus? {
        let code = countryCode.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
        return markets.first(where: { $0.countryCode == code })?.status
    }

    func isAvailable(countryCode: String) -> Bool {
        status(for: countryCode) == .available
    }
}

enum MarketsParser {
    static func parse(data: Data?, response: URLResponse?) -> MarketsResponse? {
        guard let http = response as? HTTPURLResponse,
              http.statusCode == 200,
              let data else {
            return nil
        }
        guard let decoded = try? JSONDecoder().decode(MarketsResponse.self, from: data) else {
            return nil
        }
        guard !decoded.markets.isEmpty else {
            return nil
        }
        var seenCodes = Set<String>()
        for market in decoded.markets {
            let code = market.countryCode
            guard code.count == 2, code.allSatisfy({ $0.isASCII && $0.isUppercase }) else {
                return nil
            }
            if seenCodes.contains(code) {
                return nil
            }
            seenCodes.insert(code)
        }
        return decoded
    }
}

struct APIErrorDetail: Codable, Equatable, Sendable {
    let code: String
    let countryCode: String?
    let message: String?

    init(code: String, countryCode: String? = nil, message: String? = nil) {
        self.code = code
        self.countryCode = countryCode
        self.message = message
    }

    enum CodingKeys: String, CodingKey {
        case code
        case countryCode = "country_code"
        case message
    }

    var localizedDescription: String {
        switch code {
        case "country_not_available":
            let upper = countryCode?.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
            if upper == "BR" {
                return String(localized: "Kevin is not yet available in Brazil. We are preparing the service.")
            } else if upper == "GB" {
                return String(localized: "Kevin is not yet available in the United Kingdom. We are preparing the service.")
            } else {
                return String(localized: "Kevin is not yet available in this country. We are preparing the service.")
            }
        case "country_locked_to_number":
            return String(localized: "Your account country is locked to your assigned Kevin number.")
        case "invalid_owner_phone":
            return String(localized: "Please enter a valid mobile phone number.")
        case "country_phone_mismatch":
            return String(localized: "The phone number does not match the selected country.")
        default:
            if let msg = message, !msg.isEmpty {
                return msg
            }
            return String(localized: "An error occurred. Please try again.")
        }
    }
}

enum APIErrorParser {
    static func parse(data: Data?, response: URLResponse?) -> APIErrorDetail? {
        guard let data, !data.isEmpty else { return nil }

        if let direct = try? JSONDecoder().decode(APIErrorDetail.self, from: data), !direct.code.isEmpty {
            return direct
        }

        if let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] {
            if let detailDict = json["detail"] as? [String: Any],
               let code = detailDict["code"] as? String {
                let country = detailDict["country_code"] as? String
                let message = detailDict["message"] as? String
                return APIErrorDetail(code: code, countryCode: country, message: message)
            }
            if let code = json["code"] as? String {
                let country = json["country_code"] as? String
                let message = json["message"] as? String
                return APIErrorDetail(code: code, countryCode: country, message: message)
            }
            if let errorMsg = json["error"] as? String {
                return APIErrorDetail(code: "server_error", countryCode: nil, message: errorMsg)
            }
            if let detailMsg = json["detail"] as? String {
                return APIErrorDetail(code: "server_error", countryCode: nil, message: detailMsg)
            }
        }
        return nil
    }
}
