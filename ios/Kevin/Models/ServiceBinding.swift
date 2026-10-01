import Foundation
import CoreFoundation

struct ServiceBinding: Codable, Equatable, Sendable {
    let countryCode: String?
    let provider: String?
    let numberType: String?
    let capabilities: [String: Bool]?

    init(
        countryCode: String? = nil,
        provider: String? = nil,
        numberType: String? = nil,
        capabilities: [String: Bool]? = nil
    ) {
        self.countryCode = ServiceBinding.normalizeCountryCode(countryCode)
        self.provider = provider
        self.numberType = numberType
        self.capabilities = capabilities
    }

    enum CodingKeys: String, CodingKey {
        case countryCode = "country_code"
        case provider
        case numberType = "number_type"
        case capabilities
    }

    var isVoiceCapable: Bool {
        capabilities?["voice"] ?? false
    }

    var isSMSCapable: Bool {
        capabilities?["sms"] ?? false
    }

    static func normalizeCountryCode(_ code: String?) -> String? {
        guard let code else { return nil }
        let clean = code.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
        guard clean.count == 2, clean.allSatisfy({ $0.isASCII && $0.isLetter }) else {
            return nil
        }
        return clean
    }
}

enum ServiceBindingParser {
    /// Reads ONLY profile["service_binding"] from the outer profile dictionary.
    static func parse(from profile: [String: Any]) -> ServiceBinding? {
        parseBinding(profile["service_binding"])
    }

    /// Requires dictionary and parses its optional fields.
    /// Missing/null binding returns nil.
    /// Explicit empty or all-null dictionary returns a non-nil unknown binding.
    static func parseBinding(_ value: Any?) -> ServiceBinding? {
        guard let bindingDict = value as? [String: Any] else {
            return nil
        }

        let country = ServiceBinding.normalizeCountryCode(bindingDict["country_code"] as? String)
        let provider = bindingDict["provider"] as? String
        let numberType = bindingDict["number_type"] as? String

        var strictCapabilities: [String: Bool]? = nil
        if let rawCaps = bindingDict["capabilities"] as? [String: Any] {
            var caps: [String: Bool] = [:]
            for (k, v) in rawCaps {
                let cfVal = v as CFTypeRef
                if CFGetTypeID(cfVal) == CFBooleanGetTypeID() {
                    caps[k] = (v as? NSNumber)?.boolValue ?? false
                }
            }
            strictCapabilities = caps
        }

        return ServiceBinding(
            countryCode: country,
            provider: provider,
            numberType: numberType,
            capabilities: strictCapabilities
        )
    }

    /// Delegates outer-profile parsing.
    static func parse(data: Data?) -> ServiceBinding? {
        guard let data,
              let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            return nil
        }
        return parse(from: json)
    }
}
