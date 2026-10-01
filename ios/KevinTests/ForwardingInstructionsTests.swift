import Foundation
import XCTest
@testable import Kevin

/// Forwarding-code contract for the iOS client.
///
/// Carrier dial codes are keyed by the country of the phone being forwarded
/// (the user's own SIM), not by the Kevin number. NANP (US/CA) keeps the
/// existing Verizon/GSM behaviour unchanged; every other country takes its
/// codes from GET /api/forwarding-instructions. A response that is not the
/// new shape — in particular one without `disable_unanswered` — is rejected
/// outright: the older backend's generic `disable` is `##21#`, which erases
/// unconditional forwarding and leaves a no-reply forward in place.
final class ForwardingInstructionsTests: XCTestCase {
    private func httpResponse(status: Int) throws -> HTTPURLResponse {
        try XCTUnwrap(HTTPURLResponse(
            url: XCTUnwrap(URL(string: "https://example.com/api/forwarding-instructions?country_code=BR")),
            statusCode: status,
            httpVersion: "HTTP/1.1",
            headerFields: nil
        ))
    }

    private func json(_ object: [String: Any]) throws -> Data {
        try JSONSerialization.data(withJSONObject: object)
    }

    private let brazil: [String: Any] = [
        "supported": true,
        "country_code": "BR",
        "forward_all": "**21*{number}#",
        "forward_unanswered": "**61*{number}#",
        "disable": "##61#",
        "disable_all": "##21#",
        "disable_unanswered": "##61#",
        "disable_everything": "##002#",
        "recommended": "forward_unanswered",
        "notes": "Standard GSM codes.",
        "fallback_message": "If these codes don't work, contact your carrier.",
    ]

    /// GSM-shaped server instructions for any country. The `**61*` template
    /// deliberately differs from the built-in `*61*`, so a NANP test that
    /// passes these can only succeed if the NANP guard ignored them.
    private func gsmInstructions(for country: String) -> ForwardingInstructions {
        ForwardingInstructions(
            countryCode: country,
            forwardUnansweredTemplate: "**61*{number}#",
            disableUnanswered: "##61#",
            disableAll: "##21#",
            disableEverything: "##002#"
        )
    }

    private var brazilInstructions: ForwardingInstructions { gsmInstructions(for: "BR") }

    // MARK: - Parsing

    func testParsesNewShape() throws {
        let parsed = ForwardingInstructionsParser.parse(
            response: try httpResponse(status: 200),
            data: try json(brazil)
        )
        XCTAssertEqual(parsed, brazilInstructions)
    }

    func testOldShapeWithoutDisableUnansweredIsUnusable() throws {
        // The pre-fix backend has no disable_unanswered and its `disable` is
        // ##21# — trusting any of it would strand users unable to turn Kevin off.
        var old = brazil
        old.removeValue(forKey: "disable_unanswered")
        old.removeValue(forKey: "disable_all")
        old.removeValue(forKey: "disable_everything")
        old["disable"] = "##21#"
        XCTAssertNil(ForwardingInstructionsParser.parse(
            response: try httpResponse(status: 200),
            data: try json(old)
        ))
    }

    func testUnsupportedCountryIsUnusable() throws {
        let body: [String: Any] = [
            "supported": false,
            "country_code": "ZZ",
            "message": "Call forwarding instructions not available for ZZ.",
        ]
        XCTAssertNil(ForwardingInstructionsParser.parse(
            response: try httpResponse(status: 200),
            data: try json(body)
        ))
    }

    func testTemplateWithoutNumberPlaceholderIsUnusable() throws {
        var bad = brazil
        bad["forward_unanswered"] = "**61*#"
        XCTAssertNil(ForwardingInstructionsParser.parse(
            response: try httpResponse(status: 200),
            data: try json(bad)
        ))
    }

    func testNon200IsUnusable() throws {
        XCTAssertNil(ForwardingInstructionsParser.parse(
            response: try httpResponse(status: 500),
            data: try json(brazil)
        ))
    }

    func testNonJSONIsUnusable() throws {
        XCTAssertNil(ForwardingInstructionsParser.parse(
            response: try httpResponse(status: 200),
            data: Data("<html>maintenance</html>".utf8)
        ))
    }

    func testMissingResponseIsUnusable() throws {
        XCTAssertNil(ForwardingInstructionsParser.parse(response: nil, data: try json(brazil)))
    }

    // MARK: - Dial codes
    // All non-US/CA countries return nil (unavailable) until a carrier contract exists.

    func testNonNANPCountriesReturnNilDialCodes() {
        let nonNANP = ["BR", "GB", "DE", "FR", "IT", "ES", "PT", "ZZ"]
        let numbers = ["+55 11 98765-4321", "+14155551234"]
        for country in nonNANP {
            for number in numbers {
                for isVerizon in [false, true] {
                    let codes = ForwardingDialCodes.codes(
                        countryCode: country,
                        instructions: brazilInstructions,
                        number: number,
                        isVerizon: isVerizon
                    )
                    XCTAssertNil(codes, "ForwardingDialCodes.codes must be nil for non-NANP country \(country) number \(number) isVerizon \(isVerizon)")
                }
            }
        }
    }

    func testBrazilReturnsNilDialCodes() {
        let codes = ForwardingDialCodes.codes(
            countryCode: "BR",
            instructions: brazilInstructions,
            number: "+55 11 98765-4321",
            isVerizon: false
        )
        XCTAssertNil(codes)
    }

    func testUKReturnsNilDialCodes() {
        let codes = ForwardingDialCodes.codes(
            countryCode: "GB",
            instructions: nil,
            number: "447700900123",
            isVerizon: false
        )
        XCTAssertNil(codes)
    }

    func testUSVerizonKeepsExistingCodesAndIgnoresServer() {
        let codes = ForwardingDialCodes.codes(
            countryCode: "US",
            instructions: gsmInstructions(for: "US"),
            number: "14155551234",
            isVerizon: true
        )
        XCTAssertNotNil(codes)
        XCTAssertEqual(codes?.activate, "*7114155551234")
        XCTAssertEqual(codes?.deactivate, "*73")
        XCTAssertEqual(codes?.clearExisting, "*73")
        XCTAssertNil(codes?.clearAll)
        XCTAssertFalse(codes?.isServerDriven ?? true)

        let e164Codes = ForwardingDialCodes.codes(
            countryCode: "US",
            instructions: gsmInstructions(for: "US"),
            number: "+14155551234",
            isVerizon: true
        )
        XCTAssertEqual(e164Codes?.activate, "*7114155551234")

        let tenDigitCodes = ForwardingDialCodes.codes(
            countryCode: "US",
            instructions: gsmInstructions(for: "US"),
            number: "4155551234",
            isVerizon: true
        )
        XCTAssertEqual(tenDigitCodes?.activate, "*714155551234")
    }

    func testUSNonVerizonKeepsExistingGSMCodes() {
        let codes = ForwardingDialCodes.codes(
            countryCode: "US",
            instructions: gsmInstructions(for: "US"),
            number: "14155551234",
            isVerizon: false
        )
        XCTAssertNotNil(codes)
        XCTAssertEqual(codes?.activate, "*61*14155551234#")
        XCTAssertEqual(codes?.deactivate, "##61#")
        XCTAssertEqual(codes?.clearExisting, "##21#")
        XCTAssertEqual(codes?.clearAll, "##002#")
        XCTAssertFalse(codes?.isServerDriven ?? true)

        let e164Codes = ForwardingDialCodes.codes(
            countryCode: "US",
            instructions: gsmInstructions(for: "US"),
            number: "+14155551234",
            isVerizon: false
        )
        XCTAssertEqual(e164Codes?.activate, "*61*14155551234#")

        let tenDigitCodes = ForwardingDialCodes.codes(
            countryCode: "US",
            instructions: gsmInstructions(for: "US"),
            number: "4155551234",
            isVerizon: false
        )
        XCTAssertEqual(tenDigitCodes?.activate, "*61*4155551234#")
    }

    func testCanadaIsTreatedAsNANP() {
        let codes = ForwardingDialCodes.codes(
            countryCode: "CA",
            instructions: gsmInstructions(for: "CA"),
            number: "14165551234",
            isVerizon: false
        )
        XCTAssertNotNil(codes)
        XCTAssertEqual(codes?.activate, "*61*14165551234#")
        XCTAssertFalse(codes?.isServerDriven ?? true)
    }

    func testNANPCountriesRejectForeignAndMalformedDestinations() {
        let nanpCountries = ["US", "CA"]
        let badNumbers = ["+5511987654321", "+447700900123", "garbage", "", "1111111111"]
        for country in nanpCountries {
            for number in badNumbers {
                for isVerizon in [false, true] {
                    let codes = ForwardingDialCodes.codes(
                        countryCode: country,
                        instructions: gsmInstructions(for: country),
                        number: number,
                        isVerizon: isVerizon
                    )
                    XCTAssertNil(codes, "ForwardingDialCodes.codes must be nil for NANP country \(country) number \(number) isVerizon \(isVerizon)")
                }
            }
        }
    }

    func testExtractNANPDigitsValidAndInvalid() {
        XCTAssertEqual(ForwardingDialCodes.extractNANPDigits("+1 (650) 555-1234"), "6505551234")
        XCTAssertEqual(ForwardingDialCodes.extractNANPDigits("6505551234"), "6505551234")
        XCTAssertEqual(ForwardingDialCodes.extractNANPDigits("16505551234"), "6505551234")
        XCTAssertEqual(ForwardingDialCodes.extractNANPDigits("+16505551234"), "6505551234")

        // Reject invalid prefixes, characters, and structures
        XCTAssertNil(ForwardingDialCodes.extractNANPDigits("+5511987654321"), "Must not strip +55 into a US dial code")
        XCTAssertNil(ForwardingDialCodes.extractNANPDigits("1-800-CALL-KEV"), "Vanity numbers with letters must be rejected")
        XCTAssertNil(ForwardingDialCodes.extractNANPDigits("650-555-123A"))
        XCTAssertNil(ForwardingDialCodes.extractNANPDigits("0505551234"), "Area code starting with 0 is invalid")
        XCTAssertNil(ForwardingDialCodes.extractNANPDigits("1505551234"), "Area code starting with 1 is invalid")
        XCTAssertNil(ForwardingDialCodes.extractNANPDigits("6500551234"), "Exchange code starting with 0 is invalid")
        XCTAssertNil(ForwardingDialCodes.extractNANPDigits("6501551234"), "Exchange code starting with 1 is invalid")
        XCTAssertNil(ForwardingDialCodes.extractNANPDigits(""))
        XCTAssertNil(ForwardingDialCodes.extractNANPDigits(nil))
    }

    // MARK: - tel: URL encoding

    func testTelURLEncodesHashAndKeepsStar() {
        XCTAssertEqual(ForwardingDialCodes.telURL("**61*123#")?.absoluteString, "tel:**61*123%23")
        XCTAssertEqual(ForwardingDialCodes.telURL("*71123")?.absoluteString, "tel:*71123")
        XCTAssertEqual(ForwardingDialCodes.telURL("##002#")?.absoluteString, "tel:%23%23002%23")
        XCTAssertNil(ForwardingDialCodes.telURL(nil))
        XCTAssertNil(ForwardingDialCodes.telURL(""))
    }

    // MARK: - Country resolution

    func testCountryComesFromLocaleRegionWhenUnassigned() {
        XCTAssertEqual(ForwardingCountry.resolve(hasAssignedNumber: false, locale: Locale(identifier: "pt_BR")), "BR")
        XCTAssertEqual(ForwardingCountry.resolve(hasAssignedNumber: false, locale: Locale(identifier: "en_GB")), "GB")
    }

    func testCountryDefaultsToUSWithoutRegion() {
        XCTAssertEqual(ForwardingCountry.resolve(hasAssignedNumber: false, locale: Locale(identifier: "en")), "US")
    }

    func testNANPMembership() {
        XCTAssertTrue(ForwardingCountry.isNANP("US"))
        XCTAssertTrue(ForwardingCountry.isNANP("ca"))
        XCTAssertFalse(ForwardingCountry.isNANP("BR"))
        XCTAssertFalse(ForwardingCountry.isNANP("GB"))
    }
}

// MARK: - Account country and ServiceBinding precedence

extension ForwardingInstructionsTests {
    func testAccountCountryOverridesDeviceRegionWhenUnassigned() {
        XCTAssertEqual(
            ForwardingCountry.resolve(accountCountry: "BR", hasAssignedNumber: false, locale: Locale(identifier: "en_US")),
            "BR"
        )
    }

    func testEmptyAccountCountryFallsBackToDeviceRegionWhenUnassigned() {
        XCTAssertEqual(
            ForwardingCountry.resolve(accountCountry: "", hasAssignedNumber: false, locale: Locale(identifier: "pt_BR")),
            "BR"
        )
        XCTAssertEqual(
            ForwardingCountry.resolve(accountCountry: nil, hasAssignedNumber: false, locale: Locale(identifier: "en_GB")),
            "GB"
        )
    }

    func testMalformedAccountCountryIsIgnoredWhenUnassigned() {
        XCTAssertEqual(
            ForwardingCountry.resolve(accountCountry: "zz9", hasAssignedNumber: false, locale: Locale(identifier: "en_GB")),
            "GB"
        )
        XCTAssertEqual(
            ForwardingCountry.resolve(accountCountry: "B", hasAssignedNumber: false, locale: Locale(identifier: "en_GB")),
            "GB"
        )
    }

    func testAccountCountryIsTrimmedAndUppercased() {
        XCTAssertEqual(
            ForwardingCountry.resolve(accountCountry: " gb ", hasAssignedNumber: false, locale: Locale(identifier: "en_US")),
            "GB"
        )
    }

    func testServiceBindingOverridesAccountCountryWhenAssigned() {
        let binding = ServiceBinding(countryCode: "BR", provider: "twilio", numberType: "mobile")
        XCTAssertEqual(
            ForwardingCountry.resolve(serviceBinding: binding, accountCountry: "US", assignedNumber: "+5511987654321", hasAssignedNumber: true, locale: Locale(identifier: "en_US")),
            "BR"
        )
    }

    func testAssignedAccountWithoutBindingUsesAccountCountryIfValidNANP() {
        XCTAssertEqual(
            ForwardingCountry.resolve(serviceBinding: nil, accountCountry: "CA", assignedNumber: "+14165551234", hasAssignedNumber: true, locale: Locale(identifier: "pt_BR")),
            "CA"
        )
    }

    func testAssignedAccountWithMalformedBindingFailsClosed() {
        let badBinding = ServiceBinding(countryCode: "invalid_code", provider: "twilio")
        XCTAssertEqual(
            ForwardingCountry.resolve(serviceBinding: badBinding, accountCountry: "", assignedNumber: "+16505551234", hasAssignedNumber: true, locale: Locale(identifier: "pt_BR")),
            "",
            "Malformed binding when assigned must fail closed and return empty string"
        )
    }
}
