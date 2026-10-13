import Foundation

/// Validated server shape for future carrier instructions. Canadian templates
/// are always rejected; code generation currently retains only US local profiles.
struct ForwardingInstructions: Equatable {
    let countryCode: String
    /// Contains the literal `{number}` placeholder.
    let forwardUnansweredTemplate: String
    let disableUnanswered: String
    let disableAll: String
    let disableEverything: String
}

enum ForwardingInstructionsParser {
    static func parse(response: URLResponse?, data: Data?, expectedCountry: String? = nil) -> ForwardingInstructions? {
        guard let http = response as? HTTPURLResponse,
              (200...299).contains(http.statusCode) else {
            return nil
        }
        guard let data,
              let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              json["supported"] as? Bool == true,
              let country = nonEmpty(json["country_code"]),
              let template = nonEmpty(json["forward_unanswered"]),
              template.contains("{number}"),
              let disableUnanswered = nonEmpty(json["disable_unanswered"]),
              let disableAll = nonEmpty(json["disable_all"]),
              let disableEverything = nonEmpty(json["disable_everything"]) else {
            return nil
        }
        let normalizedCountry = country.uppercased()
        guard normalizedCountry != "CA",
              normalizedCountry.count == 2,
              normalizedCountry.allSatisfy({ $0.isASCII && $0.isLetter }),
              expectedCountry.map({ $0.trimmingCharacters(in: .whitespacesAndNewlines).uppercased() == normalizedCountry }) ?? true else {
            return nil
        }
        return ForwardingInstructions(
            countryCode: country.uppercased(),
            forwardUnansweredTemplate: template,
            disableUnanswered: disableUnanswered,
            disableAll: disableAll,
            disableEverything: disableEverything
        )
    }

    private static func nonEmpty(_ value: Any?) -> String? {
        guard let string = value as? String, !string.isEmpty else { return nil }
        return string
    }
}

/// The codes a screen actually dials, without the `tel:` prefix.
struct ForwardingCodes: Equatable {
    let activate: String
    let deactivate: String
    /// Clears a forward left over from another source before activating.
    let clearExisting: String
    /// Erases every forwarding type at once; nil where no such code exists.
    let clearAll: String?
    /// True when the codes came from the server for the resolved country.
    /// Informational; the carrier picker is available only for US accounts.
    let isServerDriven: Bool
}

enum ForwardingCountry {
    /// Geographic membership only; this does not qualify a carrier or plan.
    static let nanp: Set<String> = ["US", "CA"]

    static func isNANP(_ countryCode: String) -> Bool {
        nanp.contains(countryCode.trimmingCharacters(in: .whitespacesAndNewlines).uppercased())
    }

    /// Resolves the country for forwarding logic.
    /// When assigned (hasAssignedNumber is true):
    /// - Uses serviceBinding.countryCode if valid and non-empty.
    /// - If serviceBinding is present but unknown/invalid, fails closed and returns empty string.
    /// - If serviceBinding is absent, uses confirmed accountCountry ONLY IF confirmed US/CA
    ///   AND assignedNumber has valid NANP structure (+1 + 10 ASCII digits).
    /// When unassigned (hasAssignedNumber is false):
    /// - Uses explicit accountCountry if valid.
    /// - Otherwise falls back to device region from locale.
    static func resolve(
        serviceBinding: ServiceBinding? = nil,
        accountCountry: String? = nil,
        assignedNumber: String? = nil,
        hasAssignedNumber: Bool = false,
        locale: Locale = .current
    ) -> String {
        if hasAssignedNumber {
            if let binding = serviceBinding {
                if let bindingCountry = binding.countryCode?.trimmingCharacters(in: .whitespacesAndNewlines).uppercased(),
                   bindingCountry.count == 2,
                   bindingCountry.allSatisfy({ $0.isASCII && $0.isLetter }) {
                    return bindingCountry
                }
                return ""
            }

            if let account = accountCountry?.trimmingCharacters(in: .whitespacesAndNewlines).uppercased(),
               isNANP(account),
               ForwardingDialCodes.extractNANPDigits(assignedNumber) != nil {
                return account
            }
            return ""
        }

        if let account = accountCountry?.trimmingCharacters(in: .whitespacesAndNewlines).uppercased(),
           account.count == 2,
           account.allSatisfy({ $0.isASCII && $0.isLetter }) {
            return account
        }
        guard let region = locale.region?.identifier, !region.isEmpty else { return "US" }
        return region.uppercased()
    }
}

enum ForwardingDialCodes {
    static func extractNANPDigits(_ number: String?) -> String? {
        guard let number else { return nil }
        let trimmed = number.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }

        var hasPlus = false
        for (index, char) in trimmed.enumerated() {
            if char == "+" {
                if index != 0 || hasPlus { return nil }
                hasPlus = true
            } else if char.isASCII && char.isNumber {
                continue
            } else if [" ", "-", "(", ")", ".", "/"].contains(char) {
                continue
            } else {
                return nil
            }
        }

        let digits = trimmed.filter { $0.isASCII && $0.isNumber }
        if hasPlus {
            guard digits.hasPrefix("1") else { return nil }
        }

        var national = digits
        if national.count == 11 && national.hasPrefix("1") {
            national = String(national.dropFirst())
        }

        guard national.count == 10 else { return nil }

        let first = national.first!
        if first == "0" || first == "1" { return nil }
        let exchangeFirst = national[national.index(national.startIndex, offsetBy: 3)]
        if exchangeFirst == "0" || exchangeFirst == "1" { return nil }

        return national
    }

    static func codes(
        countryCode: String,
        instructions: ForwardingInstructions? = nil,
        number: String,
        isVerizon: Bool
    ) -> ForwardingCodes? {
        let country = countryCode.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()

        // Only US has legacy local profiles. Canadian +1 numbers and server
        // templates never qualify Canadian forwarding or baseline restoration.
        guard country == "US" else {
            return nil
        }

        guard extractNANPDigits(number) != nil else {
            return nil
        }

        let destinationDigits = number.filter { $0.isASCII && $0.isNumber }

        // Existing US carrier behavior.
        if isVerizon {
            // Verizon: *71<number> forwards on no-answer; *73 cancels it.
            return ForwardingCodes(
                activate: "*71\(destinationDigits)",
                deactivate: "*73",
                clearExisting: "*73",
                clearAll: nil,
                isServerDriven: false
            )
        }
        // GSM: *61*<number># forwards on no-answer; ##61# cancels it; ##21#
        // clears an unconditional forward; ##002# erases every type.
        return ForwardingCodes(
            activate: "*61*\(destinationDigits)#",
            deactivate: "##61#",
            clearExisting: "##21#",
            clearAll: "##002#",
            isServerDriven: false
        )
    }

    /// `#` would start a URL fragment, so it must be percent-encoded; `*` is
    /// legal in a `tel:` URL and the dialer needs it verbatim.
    static func telURL(_ code: String?) -> URL? {
        guard let code, !code.isEmpty else { return nil }
        return URL(string: "tel:\(code.replacingOccurrences(of: "#", with: "%23"))")
    }
}


/// A recorded baseline is necessary for future Canadian carrier qualification,
/// but cannot itself authorize a dial code. No Canadian carrier profile is qualified.
struct CanadaForwardingBaseline: Equatable {
    struct Route: Equatable {
        let enabled: Bool
        let destination: String?
        var isRecorded: Bool { !enabled || destination?.isEmpty == false }
    }
    var carrier: String? = nil
    var specificPlan: String? = nil
    var planEligibilityConfirmed = false
    var unconditional: Route? = nil
    var noAnswer: Route? = nil
    var busy: Route? = nil
    var unreachable: Route? = nil
    var noAnswerTimerSeconds: Int? = nil
    var voicemail: Bool? = nil
    var callWaiting: Bool? = nil
    var liveVoicemail: Bool? = nil

    var isComplete: Bool {
        carrier?.isEmpty == false && specificPlan?.isEmpty == false && planEligibilityConfirmed
            && [unconditional, noAnswer, busy, unreachable].allSatisfy { $0?.isRecorded == true }
            && noAnswerTimerSeconds.map { $0 > 0 } == true
            && voicemail != nil && callWaiting != nil && liveVoicemail != nil
    }
    // Capturing a baseline or tapping a button is not carrier/device evidence.
    var permitsActivationOrRestoration: Bool { false }
}

enum CanadaForwardingGuidance {
    static let title = String(localized: "Carrier-specific setup required")
    static let setup = String(localized: "Ask your existing carrier to confirm that your specific plan supports conditional forwarding to your Kevin number. A Canadian +1 number alone does not confirm eligibility.")
    static let baseline = String(localized: "Before changing anything, record the current state and destination for unconditional, no-answer, busy and unreachable forwarding, the no-answer timer, voicemail, call waiting and Live Voicemail. Ask your carrier how to restore those exact settings. Kevin cannot verify forwarding from a setup request.")
    static let restoration = String(localized: "To stop forwarding or restore voicemail, contact your existing carrier with your recorded settings. Kevin has no qualified Canadian activation or restoration codes yet.")
}
