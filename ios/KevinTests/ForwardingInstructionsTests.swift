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

    func testServerCodesForBrazil() {
        let codes = ForwardingDialCodes.codes(
            countryCode: "BR",
            instructions: brazilInstructions,
            number: "+55 11 98765-4321",
            isVerizon: false
        )
        XCTAssertEqual(codes.activate, "**61*5511987654321#")
        XCTAssertEqual(codes.deactivate, "##61#")
        XCTAssertEqual(codes.clearExisting, "##21#")
        XCTAssertEqual(codes.clearAll, "##002#")
        XCTAssertTrue(codes.isServerDriven)
    }

    func testVerizonToggleIsIgnoredOutsideNANP() {
        let codes = ForwardingDialCodes.codes(
            countryCode: "BR",
            instructions: brazilInstructions,
            number: "5511987654321",
            isVerizon: true
        )
        XCTAssertEqual(codes.activate, "**61*5511987654321#")
        XCTAssertEqual(codes.deactivate, "##61#")
    }

    func testUSVerizonKeepsExistingCodesAndIgnoresServer() {
        // Server instructions for US must not change live US behaviour — even
        // ones that would produce different codes if consulted.
        let codes = ForwardingDialCodes.codes(
            countryCode: "US",
            instructions: gsmInstructions(for: "US"),
            number: "14155551234",
            isVerizon: true
        )
        XCTAssertEqual(codes.activate, "*7114155551234")
        XCTAssertEqual(codes.deactivate, "*73")
        XCTAssertEqual(codes.clearExisting, "*73")
        XCTAssertNil(codes.clearAll)
        XCTAssertFalse(codes.isServerDriven)
    }

    func testUSNonVerizonKeepsExistingGSMCodes() {
        // Matching-country server instructions with a different template:
        // only the NANP guard can keep the built-in `*61*` here.
        let codes = ForwardingDialCodes.codes(
            countryCode: "US",
            instructions: gsmInstructions(for: "US"),
            number: "14155551234",
            isVerizon: false
        )
        XCTAssertEqual(codes.activate, "*61*14155551234#")
        XCTAssertEqual(codes.deactivate, "##61#")
        XCTAssertEqual(codes.clearExisting, "##21#")
        XCTAssertEqual(codes.clearAll, "##002#")
        XCTAssertFalse(codes.isServerDriven)
    }

    func testCanadaIsTreatedAsNANP() {
        let codes = ForwardingDialCodes.codes(
            countryCode: "CA",
            instructions: gsmInstructions(for: "CA"),
            number: "14165551234",
            isVerizon: false
        )
        XCTAssertEqual(codes.activate, "*61*14165551234#")
        XCTAssertFalse(codes.isServerDriven)
    }

    func testVerizonToggleWithoutInstructionsOutsideNANPFallsBackToGSM() {
        // A non-NANP user who once flipped the (US-only) Verizon toggle gets
        // the standard GSM codes, not Verizon's — the toggle is hidden for
        // them and must not steer the fallback.
        let codes = ForwardingDialCodes.codes(
            countryCode: "GB",
            instructions: nil,
            number: "447700900123",
            isVerizon: true
        )
        XCTAssertEqual(codes.activate, "*61*447700900123#")
        XCTAssertEqual(codes.deactivate, "##61#")
        XCTAssertEqual(codes.clearExisting, "##21#")
        XCTAssertEqual(codes.clearAll, "##002#")
        XCTAssertFalse(codes.isServerDriven)
    }

    func testMissingInstructionsFallBackToBuiltInGSM() {
        // Offline, or the backend has not been deployed with the new shape:
        // behave exactly as the app does today.
        let codes = ForwardingDialCodes.codes(
            countryCode: "GB",
            instructions: nil,
            number: "447700900123",
            isVerizon: false
        )
        XCTAssertEqual(codes.activate, "*61*447700900123#")
        XCTAssertEqual(codes.deactivate, "##61#")
        XCTAssertEqual(codes.clearExisting, "##21#")
        XCTAssertEqual(codes.clearAll, "##002#")
        XCTAssertFalse(codes.isServerDriven)
    }

    func testInstructionsForADifferentCountryAreNotApplied() {
        // A stale fetch for another region must not leak across.
        let codes = ForwardingDialCodes.codes(
            countryCode: "GB",
            instructions: brazilInstructions,
            number: "447700900123",
            isVerizon: false
        )
        XCTAssertFalse(codes.isServerDriven)
        XCTAssertEqual(codes.activate, "*61*447700900123#")
    }

    // MARK: - tel: URL encoding

    func testTelURLEncodesHashAndKeepsStar() {
        XCTAssertEqual(ForwardingDialCodes.telURL("**61*123#")?.absoluteString, "tel:**61*123%23")
        XCTAssertEqual(ForwardingDialCodes.telURL("*71123")?.absoluteString, "tel:*71123")
        XCTAssertEqual(ForwardingDialCodes.telURL("##002#")?.absoluteString, "tel:%23%23002%23")
    }

    // MARK: - Country resolution

    func testCountryComesFromLocaleRegion() {
        XCTAssertEqual(ForwardingCountry.resolve(locale: Locale(identifier: "pt_BR")), "BR")
        XCTAssertEqual(ForwardingCountry.resolve(locale: Locale(identifier: "en_GB")), "GB")
    }

    func testCountryDefaultsToUSWithoutRegion() {
        XCTAssertEqual(ForwardingCountry.resolve(locale: Locale(identifier: "en")), "US")
    }

    func testNANPMembership() {
        XCTAssertTrue(ForwardingCountry.isNANP("US"))
        XCTAssertTrue(ForwardingCountry.isNANP("ca"))
        XCTAssertFalse(ForwardingCountry.isNANP("BR"))
    }
}
