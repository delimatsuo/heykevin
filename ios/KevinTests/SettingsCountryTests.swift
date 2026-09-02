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
    func testWritesOnlyWhenThePickDiffersFromTheAccount() {
        XCTAssertTrue(SettingsCountryFlow.shouldWrite(picked: "GB", accountCountry: "BR"))
        XCTAssertFalse(SettingsCountryFlow.shouldWrite(picked: "BR", accountCountry: "BR"))
        // Unknown account: an explicit pick is a real request.
        XCTAssertTrue(SettingsCountryFlow.shouldWrite(picked: "US", accountCountry: ""))
    }

    func testDisplayedSelectionPrefersTheAccountCountry() {
        XCTAssertEqual(SettingsCountryFlow.displayedSelection(accountCountry: "BR", locale: Locale(identifier: "en_US")), "BR")
    }

    func testDisplayedSelectionFallsBackToASupportedDeviceRegion() {
        // Matches what the forwarding codes key on, so the two never disagree.
        XCTAssertEqual(SettingsCountryFlow.displayedSelection(accountCountry: "", locale: Locale(identifier: "pt_BR")), "BR")
    }

    func testDisplayedSelectionFallsBackToUSForAnUnsupportedRegion() {
        XCTAssertEqual(SettingsCountryFlow.displayedSelection(accountCountry: "", locale: Locale(identifier: "ja_JP")), "US")
        XCTAssertEqual(SettingsCountryFlow.displayedSelection(accountCountry: "", locale: Locale(identifier: "en")), "US")
    }

    func testAdoptsOnlyTheRequestedCountry() {
        // A successful write whose read-back fell back to the server default
        // must not snap the picker to a country the user did not choose.
        XCTAssertTrue(SettingsCountryFlow.isConfirmed(requested: "GB", returned: "GB"))
        XCTAssertFalse(SettingsCountryFlow.isConfirmed(requested: "GB", returned: "US"))
        XCTAssertFalse(SettingsCountryFlow.isConfirmed(requested: "GB", returned: nil))
    }
}
