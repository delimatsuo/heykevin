import Foundation

/// Countries the backend accepts for `country_code` (`SUPPORTED_COUNTRIES`
/// in app/db/contractors.py), in the backend's order.
enum SettingsCountry {
    static let supported: [String] = ["US", "CA", "BR", "GB", "DE", "FR", "IT", "ES", "PT"]

    static func isSupported(_ code: String) -> Bool {
        supported.contains(code.trimmingCharacters(in: .whitespacesAndNewlines).uppercased())
    }

    static func displayName(_ code: String, locale: Locale = .current) -> String {
        let clean = code.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
        guard !clean.isEmpty else {
            return String(localized: "Unknown Country")
        }
        if let name = locale.localizedString(forRegionCode: clean), !name.isEmpty {
            return name
        }
        return isSupported(clean) ? clean : String(localized: "Unknown Country")
    }

    /// The account country carried by a contractor profile or provisioning
    /// response, or nil when absent, unsupported, or not a string.
    static func accountCountry(from dictionary: [String: Any]) -> String? {
        guard let raw = dictionary["country_code"] as? String, isSupported(raw) else { return nil }
        return raw.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
    }
}

/// Extracts the country the server actually returned from `PUT /api/settings`.
/// The endpoint reports a failed save as HTTP 200 `{"error": …}`, so a status
/// code alone proves nothing; whether the returned value matches the request
/// is `SettingsCountryFlow.isConfirmed`'s job.
enum SettingsCountryParser {
    static func parse(response: URLResponse?, data: Data?) -> String? {
        guard let http = response as? HTTPURLResponse,
              (200...299).contains(http.statusCode),
              let data,
              let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              json["error"] == nil,
              let code = json["country_code"] as? String,
              SettingsCountry.isSupported(code) else {
            return nil
        }
        return code.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
    }
}

/// Decisions behind the Settings country picker, kept pure so the state
/// machine — not just the parser — is under test.
enum SettingsCountryFlow {
    /// A pick writes only when it differs from what the account already
    /// holds; with the account unknown (""), any explicit pick is a request.
    /// When a number is assigned, the country is locked and should never write.
    static func shouldWrite(picked: String, accountCountry: String, hasAssignedNumber: Bool = false) -> Bool {
        if hasAssignedNumber { return false }
        return picked != accountCountry
    }

    /// Overload for backwards compatibility.
    static func shouldWrite(picked: String, accountCountry: String) -> Bool {
        shouldWrite(picked: picked, accountCountry: accountCountry, hasAssignedNumber: false)
    }

    /// What the picker shows:
    /// - When assigned (hasAssignedNumber is true):
    ///   - serviceBinding.countryCode if present and supported.
    ///   - accountCountry if binding is absent and accountCountry is supported.
    ///   - else "" (empty string, displayed as localized "Unknown Country").
    /// - When unassigned (hasAssignedNumber is false):
    ///   - accountCountry if supported.
    ///   - device-region country from locale if supported.
    ///   - else "US".
    static func displayedSelection(
        serviceBinding: ServiceBinding? = nil,
        accountCountry: String,
        hasAssignedNumber: Bool = false,
        locale: Locale = .current
    ) -> String {
        if hasAssignedNumber {
            if let binding = serviceBinding {
                if let bindingCountry = binding.countryCode?.trimmingCharacters(in: .whitespacesAndNewlines).uppercased(),
                   SettingsCountry.isSupported(bindingCountry) {
                    return bindingCountry
                }
                return ""
            }
            let trimmedAccount = accountCountry.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
            if SettingsCountry.isSupported(trimmedAccount) {
                return trimmedAccount
            }
            return ""
        }
        let trimmedAccount = accountCountry.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
        if SettingsCountry.isSupported(trimmedAccount) {
            return trimmedAccount
        }
        let region = ForwardingCountry.resolve(serviceBinding: nil, accountCountry: nil, hasAssignedNumber: false, locale: locale)
        return SettingsCountry.isSupported(region) ? region : "US"
    }

    /// Overload for backwards compatibility with existing callers.
    static func displayedSelection(accountCountry: String, locale: Locale = .current) -> String {
        displayedSelection(serviceBinding: nil, accountCountry: accountCountry, hasAssignedNumber: false, locale: locale)
    }

    /// A write is confirmed only when the server returns exactly what was
    /// requested. `_get_settings` falls back to "US" when its read-back
    /// fails, so a returned default is not confirmation of the write.
    static func isConfirmed(requested: String, returned: String?) -> Bool {
        returned == requested
    }
}
