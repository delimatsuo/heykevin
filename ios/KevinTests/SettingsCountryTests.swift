import Foundation
import XCTest
@testable import Kevin

/// The account country is root-authoritative on the server and is what the
/// forwarding codes key on. The client writes it through the validated
/// `PUT /api/settings` path and adopts only a positively confirmed value:
/// the endpoint answers HTTP 200 with `{"error": …}` on a failed save, so a
/// status code alone proves nothing.
final class SettingsCountryTests: XCTestCase {
    private func httpResponse(status: Int) throws -> HTTPURLResponse {
        try XCTUnwrap(HTTPURLResponse(
            url: XCTUnwrap(URL(string: "https://example.com/api/settings?contractor_id=c1")),
            statusCode: status,
            httpVersion: "HTTP/1.1",
            headerFields: nil
        ))
    }

    private func json(_ object: [String: Any]) throws -> Data {
        try JSONSerialization.data(withJSONObject: object)
    }

    // MARK: - Supported set

    func testSupportedCountriesMatchTheBackend() {
        XCTAssertEqual(SettingsCountry.supported, ["US", "CA", "BR", "GB", "DE", "FR", "IT", "ES", "PT"])
    }

    func testIsSupportedIsCaseInsensitive() {
        XCTAssertTrue(SettingsCountry.isSupported("br"))
        XCTAssertTrue(SettingsCountry.isSupported("GB"))
        XCTAssertFalse(SettingsCountry.isSupported("ZZ"))
        XCTAssertFalse(SettingsCountry.isSupported(""))
    }

    func testDisplayNameUsesTheLocale() {
        XCTAssertEqual(SettingsCountry.displayName("BR", locale: Locale(identifier: "pt_BR")), "Brasil")
        XCTAssertEqual(SettingsCountry.displayName("US", locale: Locale(identifier: "en_US")), "United States")
        XCTAssertEqual(SettingsCountry.displayName("", locale: Locale(identifier: "en_US")), "Unknown Country")
        XCTAssertEqual(SettingsCountry.displayName("ZZ", locale: Locale(identifier: "en_US")), "Unknown Country")
    }

    // MARK: - Parsing the PUT response

    func testConfirmedCountryIsAdopted() throws {
        let body: [String: Any] = ["country_code": "BR", "greeting_name": "", "quiet_hours_enabled": false]
        XCTAssertEqual(
            SettingsCountryParser.parse(response: try httpResponse(status: 200), data: try json(body)),
            "BR"
        )
    }

    func testConfirmedCountryIsUppercased() throws {
        XCTAssertEqual(
            SettingsCountryParser.parse(response: try httpResponse(status: 200), data: try json(["country_code": "gb"])),
            "GB"
        )
    }

    func testErrorBodyOn200IsRejected() throws {
        // The backend reports a failed country save as 200 {"error": ...}.
        let body: [String: Any] = ["error": "Failed to save country_code"]
        XCTAssertNil(SettingsCountryParser.parse(response: try httpResponse(status: 200), data: try json(body)))
    }

    func testUnsupportedCountryInBodyIsRejected() throws {
        XCTAssertNil(SettingsCountryParser.parse(response: try httpResponse(status: 200), data: try json(["country_code": "ZZ"])))
    }

    func testValidationFailureStatusIsRejected() throws {
        XCTAssertNil(SettingsCountryParser.parse(response: try httpResponse(status: 422), data: try json(["country_code": "BR"])))
    }

    func testNonJSONIsRejected() throws {
        XCTAssertNil(SettingsCountryParser.parse(response: try httpResponse(status: 200), data: Data("<html>maintenance</html>".utf8)))
    }

    func testMissingResponseIsRejected() throws {
        XCTAssertNil(SettingsCountryParser.parse(response: nil, data: try json(["country_code": "BR"])))
    }
}

// MARK: - Picker flow decisions

extension SettingsCountryTests {
    func testWritesOnlyWhenThePickDiffersFromTheAccountAndUnassigned() {
        XCTAssertTrue(SettingsCountryFlow.shouldWrite(picked: "GB", accountCountry: "BR", hasAssignedNumber: false))
        XCTAssertFalse(SettingsCountryFlow.shouldWrite(picked: "BR", accountCountry: "BR", hasAssignedNumber: false))
        // Unknown account: an explicit pick is a real request.
        XCTAssertTrue(SettingsCountryFlow.shouldWrite(picked: "US", accountCountry: "", hasAssignedNumber: false))
        // When assigned, shouldWrite is always false
        XCTAssertFalse(SettingsCountryFlow.shouldWrite(picked: "GB", accountCountry: "BR", hasAssignedNumber: true))
        XCTAssertFalse(SettingsCountryFlow.shouldWrite(picked: "US", accountCountry: "", hasAssignedNumber: true))
    }

    func testDisplayedSelectionPrefersTheAccountCountryWhenUnassigned() {
        XCTAssertEqual(SettingsCountryFlow.displayedSelection(accountCountry: "BR", hasAssignedNumber: false, locale: Locale(identifier: "en_US")), "BR")
    }

    func testDisplayedSelectionFallsBackToASupportedDeviceRegionWhenUnassigned() {
        // Matches what the forwarding codes key on, so the two never disagree.
        XCTAssertEqual(SettingsCountryFlow.displayedSelection(accountCountry: "", hasAssignedNumber: false, locale: Locale(identifier: "pt_BR")), "BR")
    }

    func testDisplayedSelectionFallsBackToUSForAnUnsupportedRegionWhenUnassigned() {
        XCTAssertEqual(SettingsCountryFlow.displayedSelection(accountCountry: "", hasAssignedNumber: false, locale: Locale(identifier: "ja_JP")), "US")
        XCTAssertEqual(SettingsCountryFlow.displayedSelection(accountCountry: "", hasAssignedNumber: false, locale: Locale(identifier: "en")), "US")
    }

    func testDisplayedSelectionUsesServiceBindingWhenAssigned() {
        let binding = ServiceBinding(countryCode: "BR", provider: "twilio", numberType: "mobile")
        XCTAssertEqual(
            SettingsCountryFlow.displayedSelection(serviceBinding: binding, accountCountry: "US", hasAssignedNumber: true, locale: Locale(identifier: "en_US")),
            "BR"
        )
    }

    func testDisplayedSelectionUsesAccountCountryWhenAssignedWithoutBinding() {
        XCTAssertEqual(
            SettingsCountryFlow.displayedSelection(serviceBinding: nil, accountCountry: "CA", hasAssignedNumber: true, locale: Locale(identifier: "pt_BR")),
            "CA"
        )
    }

    func testDisplayedSelectionNeverUsesLocaleWhenAssignedWithMalformedBinding() {
        let badBinding = ServiceBinding(countryCode: "ZZ", provider: "twilio")
        XCTAssertEqual(
            SettingsCountryFlow.displayedSelection(serviceBinding: badBinding, accountCountry: "", hasAssignedNumber: true, locale: Locale(identifier: "pt_BR")),
            "",
            "When assigned with malformed binding, displayedSelection must return empty string"
        )
    }

    func testDisplayedSelectionReturnsEmptyWhenAssignedWithUnsupportedAccountCountry() {
        XCTAssertEqual(
            SettingsCountryFlow.displayedSelection(serviceBinding: nil, accountCountry: "ZZ", hasAssignedNumber: true),
            ""
        )
    }

    func testAdoptsOnlyTheRequestedCountry() {
        // A successful write whose read-back fell back to the server default
        // must not snap the picker to a country the user did not choose.
        XCTAssertTrue(SettingsCountryFlow.isConfirmed(requested: "GB", returned: "GB"))
        XCTAssertFalse(SettingsCountryFlow.isConfirmed(requested: "GB", returned: "US"))
        XCTAssertFalse(SettingsCountryFlow.isConfirmed(requested: "GB", returned: nil))
    }
}

// MARK: - Reading the account country from a server dictionary

extension SettingsCountryTests {
    func testAccountCountryIsReadAndUppercased() {
        XCTAssertEqual(SettingsCountry.accountCountry(from: ["country_code": "br", "status": "ok"]), "BR")
        XCTAssertEqual(SettingsCountry.accountCountry(from: ["country_code": "GB"]), "GB")
    }

    func testAccountCountryIsNilWhenMissingUnsupportedOrNotAString() {
        XCTAssertNil(SettingsCountry.accountCountry(from: ["status": "ok"]))
        XCTAssertNil(SettingsCountry.accountCountry(from: ["country_code": "ZZ"]))
        XCTAssertNil(SettingsCountry.accountCountry(from: ["country_code": 55]))
        XCTAssertNil(SettingsCountry.accountCountry(from: ["country_code": ""]))
    }
}
