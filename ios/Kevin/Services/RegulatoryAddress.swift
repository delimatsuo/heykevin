import Foundation

/// Business-address rules for the six countries that require a business
/// street address and city before Twilio number provisioning can succeed.
///
/// `provision_twilio_number` (app/db/contractors.py) refuses without a
/// business address and city in these countries, and `api_provision_number`
/// (app/api/contractors.py) pre-checks the same fields. This type is pure —
/// no UI, no networking — so the decisions it makes are unit-tested directly,
/// matching the pattern in `SettingsCountry.swift`.
enum RegulatoryAddress {
    /// Mirrors REGULATORY_COUNTRIES in app/db/contractors.py.
    static let countries: Set<String> = ["DE", "FR", "IT", "ES", "PT", "BR"]
    static let maxAddressLength = 500   // ContractorCreate.business_address
    static let maxCityLength = 100      // ContractorCreate.business_city

    /// Whether the account country requires a business address and city
    /// before a Twilio number can be provisioned. Trims and uppercases;
    /// `""` and unknown codes are false.
    static func requiresAddress(countryCode: String) -> Bool {
        countries.contains(countryCode.trimmingCharacters(in: .whitespacesAndNewlines).uppercased())
    }

    /// Whether a provisioning error message indicates the caller needs to
    /// supply (or correct) the business address before retrying. True when
    /// the message, lowercased, contains "address and city" or "address
    /// verification failed".
    ///
    /// Through `api_provision_number` today, only two messages actually
    /// match: its own pre-check ("Business address and city are required
    /// for number provisioning in your country.") and the rejected-bundle
    /// remap ("Address verification failed. Please check your business
    /// address.", the fix-not-retry case). `provision_twilio_number`'s
    /// service-layer exception ("Business address and city required for
    /// number provisioning in this country") is not currently reachable
    /// through the API as distinct text: the pre-check above it in
    /// `api_provision_number` already returns before that exception can be
    /// raised, and `except Exception` would remap it to the "Address
    /// verification failed" message (its text also contains "address")
    /// before any response left the server anyway. The "address and city"
    /// branch of the match still covers that message directly — kept for
    /// defense in depth should a future code path surface it unmapped.
    /// False for the other server messages ("No phone numbers available in
    /// your area…", "Your country is not yet supported…", the generic
    /// "Failed to provision phone number…") and for "".
    static func needsAddressCapture(errorMessage: String) -> Bool {
        let lowercased = errorMessage.lowercased()
        return lowercased.contains("address and city") || lowercased.contains("address verification failed")
    }

    enum ValidationResult: Equatable {
        case valid, missingAddress, missingCity, addressTooLong, cityTooLong
    }

    /// Validates a candidate business address and city against the same
    /// limits the backend enforces (`ContractorCreate.business_address` and
    /// `.business_city`). Trims both; an empty address fails first, then an
    /// empty city; length is checked against the trimmed string.
    static func validate(address: String, city: String) -> ValidationResult {
        let trimmedAddress = address.trimmingCharacters(in: .whitespacesAndNewlines)
        let trimmedCity = city.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmedAddress.isEmpty { return .missingAddress }
        if trimmedCity.isEmpty { return .missingCity }
        if trimmedAddress.count > maxAddressLength { return .addressTooLong }
        if trimmedCity.count > maxCityLength { return .cityTooLong }
        return .valid
    }
}
