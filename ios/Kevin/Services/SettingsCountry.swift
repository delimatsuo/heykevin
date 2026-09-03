import Foundation

/// Countries the backend accepts for `country_code` (`SUPPORTED_COUNTRIES`
/// in app/db/contractors.py), in the backend's order.
enum SettingsCountry {
    static let supported: [String] = ["US", "CA", "BR", "GB", "DE", "FR", "IT", "ES", "PT"]

    static func isSupported(_ code: String) -> Bool {
        supported.contains(code.trimmingCharacters(in: .whitespacesAndNewlines).uppercased())
    }

    static func displayName(_ code: String, locale: Locale = .current) -> String {
        locale.localizedString(forRegionCode: code) ?? code
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
    static func shouldWrite(picked: String, accountCountry: String) -> Bool {
        picked != accountCountry
    }

    /// What the picker shows: the account country when known, else the
    /// device-region country the forwarding codes fall back to (so the two
    /// never disagree), else US.
    static func displayedSelection(accountCountry: String, locale: Locale = .current) -> String {
        if SettingsCountry.isSupported(accountCountry) {
            return accountCountry.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
        }
        let region = ForwardingCountry.resolve(accountCountry: nil, locale: locale)
        return SettingsCountry.isSupported(region) ? region : "US"
    }

    /// A write is confirmed only when the server returns exactly what was
    /// requested. `_get_settings` falls back to "US" when its read-back
    /// fails, so a returned default is not confirmation of the write.
    static func isConfirmed(requested: String, returned: String?) -> Bool {
        returned == requested
    }
}
