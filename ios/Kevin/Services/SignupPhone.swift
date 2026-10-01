import Foundation

struct SignupCountry: Identifiable, Equatable, Hashable, Sendable {
    let code: String
    let dialingPrefix: String
    let flagEmoji: String
    let placeholder: String

    var id: String { code }

    init(code: String, dialingPrefix: String, flagEmoji: String, placeholder: String) {
        self.code = code.uppercased()
        self.dialingPrefix = dialingPrefix
        self.flagEmoji = flagEmoji
        self.placeholder = placeholder
    }

    static let supported: [SignupCountry] = [
        SignupCountry(code: "US", dialingPrefix: "+1", flagEmoji: "🇺🇸", placeholder: "(650) 555-1234"),
        SignupCountry(code: "CA", dialingPrefix: "+1", flagEmoji: "🇨🇦", placeholder: "(416) 555-1234"),
        SignupCountry(code: "BR", dialingPrefix: "+55", flagEmoji: "🇧🇷", placeholder: "(11) 98765-4321"),
        SignupCountry(code: "GB", dialingPrefix: "+44", flagEmoji: "🇬🇧", placeholder: "07700 900123"),
    ]

    static func forCode(_ code: String) -> SignupCountry? {
        let clean = code.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
        return supported.first(where: { $0.code == clean })
    }

    func localizedName(locale: Locale = .current) -> String {
        locale.localizedString(forRegionCode: code) ?? code
    }

    static func suggestedCountry(locale: Locale = .current) -> String {
        guard let region = locale.region?.identifier.uppercased() else {
            return "US"
        }
        if supported.contains(where: { $0.code == region }) {
            return region
        }
        return "US"
    }
}

enum PhoneValidationResult: Equatable, Sendable {
    case valid(e164: String, nationalDigits: String)
    case invalid(reason: String, code: String)

    var isValid: Bool {
        if case .valid = self { return true }
        return false
    }

    var e164: String? {
        if case .valid(let e164, _) = self { return e164 }
        return nil
    }

    var nationalDigits: String? {
        if case .valid(_, let digits) = self { return digits }
        return nil
    }

    var failureReason: String? {
        if case .invalid(let reason, _) = self { return reason }
        return nil
    }
}

enum SignupPhone {
    private static let validBrazilDDDs: Set<String> = [
        // SP
        "11", "12", "13", "14", "15", "16", "17", "18", "19",
        // RJ / ES
        "21", "22", "24", "27", "28",
        // MG
        "31", "32", "33", "34", "35", "37", "38",
        // PR / SC
        "41", "42", "43", "44", "45", "46", "47", "48", "49",
        // RS
        "51", "53", "54", "55",
        // DF / GO / TO / MT / MS / AC / RO
        "61", "62", "63", "64", "65", "66", "67", "68", "69",
        // BA / SE
        "71", "73", "74", "75", "77", "79",
        // PE / AL / PB / RN / CE / PI
        "81", "82", "83", "84", "85", "86", "87", "88", "89",
        // PA / AM / RR / AP / MA
        "91", "92", "93", "94", "95", "96", "97", "98", "99"
    ]

    private static let allowedSeparators: Set<Character> = [" ", "-", "(", ")", ".", "/", "\t"]

    static func validate(_ rawInput: String, countryCode: String) -> PhoneValidationResult {
        let trimmed = rawInput.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return .invalid(
                reason: String(localized: "Please enter your phone number."),
                code: "empty_phone"
            )
        }

        // Check for disallowed characters: must only contain ASCII digits, allowed separators, and at most one leading '+'
        var hasPlus = false
        var digitCount = 0
        for (index, char) in trimmed.enumerated() {
            if char == "+" {
                if index != 0 || hasPlus {
                    return .invalid(
                        reason: String(localized: "Phone number contains invalid characters."),
                        code: "invalid_characters"
                    )
                }
                hasPlus = true
            } else if char.isASCII && char.isNumber {
                digitCount += 1
            } else if allowedSeparators.contains(char) {
                continue
            } else {
                return .invalid(
                    reason: String(localized: "Phone number contains invalid characters."),
                    code: "invalid_characters"
                )
            }
        }

        guard digitCount > 0 else {
            return .invalid(
                reason: String(localized: "Please enter your phone number."),
                code: "empty_phone"
            )
        }

        let digits = trimmed.filter { $0.isASCII && $0.isNumber }
        let targetCountry = countryCode.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()

        switch targetCountry {
        case "US", "CA":
            return validateNANP(digits: digits, hasPlus: hasPlus, rawInput: trimmed, countryCode: targetCountry)
        case "BR":
            return validateBrazil(digits: digits, hasPlus: hasPlus, rawInput: trimmed)
        case "GB":
            return validateUK(digits: digits, hasPlus: hasPlus, rawInput: trimmed)
        default:
            return .invalid(
                reason: String(localized: "Country is not supported."),
                code: "country_not_supported"
            )
        }
    }

    private static func validateNANP(digits: String, hasPlus: Bool, rawInput: String, countryCode: String) -> PhoneValidationResult {
        // Country prefix mismatch check
        if hasPlus {
            if digits.hasPrefix("55") || digits.hasPrefix("44") {
                return .invalid(
                    reason: String(localized: "The phone number does not match the selected country."),
                    code: "country_phone_mismatch"
                )
            }
            if !digits.hasPrefix("1") {
                return .invalid(
                    reason: String(localized: "The phone number does not match the selected country."),
                    code: "country_phone_mismatch"
                )
            }
        }

        var nationalDigits = digits
        if nationalDigits.count == 11 && nationalDigits.hasPrefix("1") {
            nationalDigits = String(nationalDigits.dropFirst())
        }

        guard nationalDigits.count == 10 else {
            return .invalid(
                reason: String(localized: "US and Canada phone numbers must be 10 digits."),
                code: "invalid_length"
            )
        }

        // NANP Area code cannot start with 0 or 1
        let firstChar = nationalDigits.first!
        if firstChar == "0" || firstChar == "1" {
            return .invalid(
                reason: String(localized: "Invalid area code."),
                code: "invalid_area_code"
            )
        }

        // Subscriber / exchange code (4th digit) cannot start with 0 or 1
        let exchangeFirst = nationalDigits[nationalDigits.index(nationalDigits.startIndex, offsetBy: 3)]
        if exchangeFirst == "0" || exchangeFirst == "1" {
            return .invalid(
                reason: String(localized: "Invalid phone number format."),
                code: "invalid_exchange_code"
            )
        }

        let e164 = "+1\(nationalDigits)"
        return .valid(e164: e164, nationalDigits: nationalDigits)
    }

    private static func validateBrazil(digits: String, hasPlus: Bool, rawInput: String) -> PhoneValidationResult {
        // Country prefix mismatch check
        if hasPlus {
            if digits.hasPrefix("1") || digits.hasPrefix("44") {
                return .invalid(
                    reason: String(localized: "The phone number does not match the selected country."),
                    code: "country_phone_mismatch"
                )
            }
            if !digits.hasPrefix("55") {
                return .invalid(
                    reason: String(localized: "The phone number does not match the selected country."),
                    code: "country_phone_mismatch"
                )
            }
        }

        var nationalDigits = digits
        if nationalDigits.count == 13 && nationalDigits.hasPrefix("55") {
            nationalDigits = String(nationalDigits.dropFirst(2))
        }

        // Reject missing or invalid DDD / length
        // Brazilian mobile numbers MUST have 11 digits (2 DDD + 9 mobile digits)
        guard nationalDigits.count == 11 else {
            if nationalDigits.count == 10 {
                return .invalid(
                    reason: String(localized: "Brazilian mobile phone must have 11 digits with DDD and 9th digit 9."),
                    code: "missing_ninth_digit"
                )
            }
            return .invalid(
                reason: String(localized: "Brazilian mobile phone must have 11 digits with DDD and 9th digit 9."),
                code: "invalid_length"
            )
        }

        // Check DDD
        let ddd = String(nationalDigits.prefix(2))
        guard validBrazilDDDs.contains(ddd) else {
            return .invalid(
                reason: String(localized: "Invalid area code (DDD)."),
                code: "invalid_ddd"
            )
        }

        // Check ninth digit: mobile numbers must start with 9 after DDD
        let ninthDigitIndex = nationalDigits.index(nationalDigits.startIndex, offsetBy: 2)
        guard nationalDigits[ninthDigitIndex] == "9" else {
            return .invalid(
                reason: String(localized: "The ninth digit of the mobile phone must be 9."),
                code: "invalid_ninth_digit"
            )
        }

        let e164 = "+55\(nationalDigits)"
        return .valid(e164: e164, nationalDigits: nationalDigits)
    }

    private static func validateUK(digits: String, hasPlus: Bool, rawInput: String) -> PhoneValidationResult {
        // Country prefix mismatch check
        if hasPlus {
            if digits.hasPrefix("1") || digits.hasPrefix("55") {
                return .invalid(
                    reason: String(localized: "The phone number does not match the selected country."),
                    code: "country_phone_mismatch"
                )
            }
            if !digits.hasPrefix("44") {
                return .invalid(
                    reason: String(localized: "The phone number does not match the selected country."),
                    code: "country_phone_mismatch"
                )
            }
        }

        var nationalDigits = digits
        if nationalDigits.count == 12 && nationalDigits.hasPrefix("44") {
            nationalDigits = String(nationalDigits.dropFirst(2))
        }

        // Strip national leading zero if present
        if nationalDigits.count == 11 && nationalDigits.hasPrefix("0") {
            nationalDigits = String(nationalDigits.dropFirst(1))
        }

        guard nationalDigits.count == 10 else {
            return .invalid(
                reason: String(localized: "UK phone numbers must be 10 digits (excluding leading 0)."),
                code: "invalid_length"
            )
        }

        let e164 = "+44\(nationalDigits)"
        return .valid(e164: e164, nationalDigits: nationalDigits)
    }

    static func formatDisplay(_ phone: String, countryCode: String) -> String {
        let digits = phone.filter { $0.isASCII && $0.isNumber }
        let targetCountry = countryCode.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()

        switch targetCountry {
        case "US", "CA":
            var national = digits
            if national.count == 11 && national.hasPrefix("1") {
                national = String(national.dropFirst())
            }
            guard national.count == 10 else { return phone }
            let area = national.prefix(3)
            let prefix = national.dropFirst(3).prefix(3)
            let line = national.suffix(4)
            return "(\(area)) \(prefix)-\(line)"

        case "BR":
            var national = digits
            if national.count == 13 && national.hasPrefix("55") {
                national = String(national.dropFirst(2))
            }
            guard national.count == 11 else { return phone }
            let ddd = national.prefix(2)
            let part1 = national.dropFirst(2).prefix(5)
            let part2 = national.suffix(4)
            return "(\(ddd)) \(part1)-\(part2)"

        case "GB":
            var national = digits
            if national.count == 12 && national.hasPrefix("44") {
                national = String(national.dropFirst(2))
            }
            if national.count == 10 {
                let part1 = national.prefix(4)
                let part2 = national.suffix(6)
                return "0\(part1) \(part2)"
            }
            return phone

        default:
            return phone
        }
    }
}
