import Foundation

/// Per-country carrier dial codes as served by `GET /api/forwarding-instructions`.
///
/// Only the backend's *new* response shape is accepted — the one that carries
/// `disable_unanswered`. The older shape's generic `disable` is `##21#`, which
/// erases unconditional forwarding (GSM service code 21) and leaves a no-reply
/// forward (service code 61) in place, so a client that trusted it could not
/// turn Kevin off. Anything else is treated as "no instructions" and the app
/// falls back to the codes it has always dialed.
struct ForwardingInstructions: Equatable {
    let countryCode: String
    /// Contains the literal `{number}` placeholder.
    let forwardUnansweredTemplate: String
    let disableUnanswered: String
    let disableAll: String
    let disableEverything: String
}

enum ForwardingInstructionsParser {
    static func parse(response: URLResponse?, data: Data?) -> ForwardingInstructions? {
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
    /// Informational: screens gate the US carrier picker on
    /// `ForwardingCountry.isNANP`, not on this flag.
    let isServerDriven: Bool
}

enum ForwardingCountry {
    /// Countries where the app keeps its existing Verizon/GSM behaviour and
    /// never consults the server. The two in-repo sources for US codes
    /// disagree; resolving that is a product decision, not a client one.
    static let nanp: Set<String> = ["US", "CA"]

    static func isNANP(_ countryCode: String) -> Bool {
        nanp.contains(countryCode.uppercased())
    }

    /// The forwarding codes belong to the carrier of the phone being
    /// forwarded — the user's own SIM. The account country is what the user
    /// told us and wins when it is a well-formed two-letter code; otherwise
    /// the device region is the best signal available. Defaults to US, which
    /// preserves today's behaviour for a locale with no region.
    static func resolve(accountCountry: String? = nil, locale: Locale = .current) -> String {
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
    static func codes(
        countryCode: String,
        instructions: ForwardingInstructions?,
        number: String,
        isVerizon: Bool
    ) -> ForwardingCodes {
        let digits = number.filter { $0.isNumber }
        let country = countryCode.uppercased()

        if !ForwardingCountry.isNANP(country),
           let instructions,
           instructions.countryCode == country {
            return ForwardingCodes(
                activate: instructions.forwardUnansweredTemplate
                    .replacingOccurrences(of: "{number}", with: digits),
                deactivate: instructions.disableUnanswered,
                clearExisting: instructions.disableAll,
                clearAll: instructions.disableEverything,
                isServerDriven: true
            )
        }

        // Built-in codes: exactly what the app dialed before server-driven
        // instructions existed, so an offline device or a backend without the
        // new shape behaves as it always has.
        if ForwardingCountry.isNANP(country), isVerizon {
            // Verizon: *71<number> forwards on no-answer; *73 cancels it.
            return ForwardingCodes(
                activate: "*71\(digits)",
                deactivate: "*73",
                clearExisting: "*73",
                clearAll: nil,
                isServerDriven: false
            )
        }
        // GSM: *61*<number># forwards on no-answer; ##61# cancels it; ##21#
        // clears an unconditional forward; ##002# erases every type.
        return ForwardingCodes(
            activate: "*61*\(digits)#",
            deactivate: "##61#",
            clearExisting: "##21#",
            clearAll: "##002#",
            isServerDriven: false
        )
    }

    /// `#` would start a URL fragment, so it must be percent-encoded; `*` is
    /// legal in a `tel:` URL and the dialer needs it verbatim.
    static func telURL(_ code: String) -> URL? {
        URL(string: "tel:\(code.replacingOccurrences(of: "#", with: "%23"))")
    }
}
