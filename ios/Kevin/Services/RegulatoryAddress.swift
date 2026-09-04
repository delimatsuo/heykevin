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
    /// the message, lowercased, contains "address and city" — matching both
    /// `api_provision_number`'s pre-check ("Business address and city are
    /// required for number provisioning in your country.") and
    /// `provision_twilio_number`'s service-layer check ("Business address
    /// and city required for number provisioning in this country") — or
    /// "address verification failed" (the rejected-bundle message, which the
    /// user fixes by correcting the address, not by retrying blindly).
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
