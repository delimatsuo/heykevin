import Foundation
import XCTest
@testable import Kevin

final class SignupPhoneTests: XCTestCase {

    // MARK: - Brazilian Phone Validation

    func testValidBrazilPhoneFormats() {
        let inputs = [
            "+5511987654321",
            "11987654321",
            "(11) 98765-4321",
            "+55 (11) 98765-4321",
            "5511987654321",
            "+55.11.98765.4321",
            "(11) 98765 4321",
            "11-98765-4321"
        ]

        for input in inputs {
            let result = SignupPhone.validate(input, countryCode: "BR")
            XCTAssertTrue(result.isValid, "Expected '\(input)' to be valid for BR")
            XCTAssertEqual(result.e164, "+5511987654321")
            XCTAssertEqual(result.nationalDigits, "11987654321")
        }
    }

    func testAll67ValidBrazilDDDs() {
        let validDDDs = [
            "11", "12", "13", "14", "15", "16", "17", "18", "19",
            "21", "22", "24", "27", "28",
            "31", "32", "33", "34", "35", "37", "38",
            "41", "42", "43", "44", "45", "46", "47", "48", "49",
            "51", "53", "54", "55",
            "61", "62", "63", "64", "65", "66", "67", "68", "69",
            "71", "73", "74", "75", "77", "79",
            "81", "82", "83", "84", "85", "86", "87", "88", "89",
            "91", "92", "93", "94", "95", "96", "97", "98", "99"
        ]
        XCTAssertEqual(validDDDs.count, 67, "ANATEL specifies exactly 67 valid DDDs in Brazil")

        for ddd in validDDDs {
            let phone = "\(ddd)987654321"
            let result = SignupPhone.validate(phone, countryCode: "BR")
            XCTAssertTrue(result.isValid, "DDD \(ddd) must be accepted")
            XCTAssertEqual(result.e164, "+55\(phone)")
        }
    }

    func testInvalidBrazilDDDs() {
        let invalidDDDs = ["00", "10", "20", "23", "25", "26", "29", "30", "36", "39", "40", "50", "52", "56", "70", "72", "76", "78", "90"]
        for ddd in invalidDDDs {
            let phone = "\(ddd)987654321"
            let result = SignupPhone.validate(phone, countryCode: "BR")
            XCTAssertFalse(result.isValid, "DDD \(ddd) must be rejected")
            if case .invalid(let reason, let code) = result {
                XCTAssertEqual(code, "invalid_ddd")
                XCTAssertEqual(reason, "Invalid area code (DDD).")
            } else {
                XCTFail("Expected invalid_ddd for \(ddd)")
            }
        }
    }

    func testRejectBrazilMissingNinthDigit() {
        // 10-digit number (legacy landline or missing 9)
        let phone = "1187654321"
        let result = SignupPhone.validate(phone, countryCode: "BR")
        XCTAssertFalse(result.isValid)
        if case .invalid(let reason, let code) = result {
            XCTAssertEqual(code, "missing_ninth_digit")
            XCTAssertEqual(reason, "Brazilian mobile phone must have 11 digits with DDD and 9th digit 9.")
        }
    }

    func testRejectBrazilNinthDigitNotNine() {
        // 11 digits but 3rd digit is 8 instead of 9
        let phone = "11887654321"
        let result = SignupPhone.validate(phone, countryCode: "BR")
        XCTAssertFalse(result.isValid)
        if case .invalid(let reason, let code) = result {
            XCTAssertEqual(code, "invalid_ninth_digit")
            XCTAssertEqual(reason, "The ninth digit of the mobile phone must be 9.")
        }
    }

    func testRejectBrazilPrefixMismatch() {
        let result = SignupPhone.validate("+111987654321", countryCode: "BR")
        XCTAssertFalse(result.isValid)
        if case .invalid(let reason, let code) = result {
            XCTAssertEqual(code, "country_phone_mismatch")
            XCTAssertEqual(reason, "The phone number does not match the selected country.")
        }
    }

    // MARK: - NANP (US / CA) Phone Validation

    func testValidUSPhoneFormats() {
        let inputs = [
            "6505551234",
            "+16505551234",
            "16505551234",
            "(650) 555-1234",
            "+1 (650) 555-1234",
            "650-555-1234",
            "650.555.1234"
        ]

        for input in inputs {
            let result = SignupPhone.validate(input, countryCode: "US")
            XCTAssertTrue(result.isValid, "Expected '\(input)' to be valid for US")
            XCTAssertEqual(result.e164, "+16505551234")
            XCTAssertEqual(result.nationalDigits, "6505551234")
        }
    }

    func testValidCanadaPhoneFormats() {
        let result = SignupPhone.validate("(416) 555-1234", countryCode: "CA")
        XCTAssertTrue(result.isValid)
        XCTAssertEqual(result.e164, "+14165551234")
        XCTAssertEqual(result.nationalDigits, "4165551234")
    }

    func testRejectNANPInvalidAreaCodeOrExchange() {
        // Area code starting with 0
        XCTAssertFalse(SignupPhone.validate("0505551234", countryCode: "US").isValid)
        // Area code starting with 1
        XCTAssertFalse(SignupPhone.validate("1505551234", countryCode: "US").isValid)
        // Exchange code starting with 0
        XCTAssertFalse(SignupPhone.validate("6500551234", countryCode: "US").isValid)
        // Exchange code starting with 1
        XCTAssertFalse(SignupPhone.validate("6501551234", countryCode: "US").isValid)
    }

    func testRejectNANPPrefixMismatch() {
        let result = SignupPhone.validate("+556505551234", countryCode: "US")
        XCTAssertFalse(result.isValid)
        if case .invalid(let reason, let code) = result {
            XCTAssertEqual(code, "country_phone_mismatch")
            XCTAssertEqual(reason, "The phone number does not match the selected country.")
        }
    }

    // MARK: - UK (GB) Phone Validation

    func testValidUKPhoneFormats() {
        let inputs = [
            "7700900123",
            "07700900123",
            "+447700900123",
            "447700900123",
            "07700 900123",
            "+44 7700 900123"
        ]

        for input in inputs {
            let result = SignupPhone.validate(input, countryCode: "GB")
            XCTAssertTrue(result.isValid, "Expected '\(input)' to be valid for GB")
            XCTAssertEqual(result.e164, "+447700900123")
            XCTAssertEqual(result.nationalDigits, "7700900123")
        }
    }

    func testRejectUKPrefixMismatch() {
        let result = SignupPhone.validate("+17700900123", countryCode: "GB")
        XCTAssertFalse(result.isValid)
        if case .invalid(let reason, let code) = result {
            XCTAssertEqual(code, "country_phone_mismatch")
            XCTAssertEqual(reason, "The phone number does not match the selected country.")
        }
    }

    // MARK: - Invalid Characters & Security Guarding

    func testRejectLettersAndSymbols() {
        XCTAssertFalse(SignupPhone.validate("650-555-123A", countryCode: "US").isValid)
        XCTAssertFalse(SignupPhone.validate("11987654321 ext 123", countryCode: "BR").isValid)
        XCTAssertFalse(SignupPhone.validate("++5511987654321", countryCode: "BR").isValid)
        XCTAssertFalse(SignupPhone.validate("55+11987654321", countryCode: "BR").isValid)
        XCTAssertFalse(SignupPhone.validate("650 555 1234#", countryCode: "US").isValid)
        XCTAssertFalse(SignupPhone.validate("650 555 1234*", countryCode: "US").isValid)
        XCTAssertFalse(SignupPhone.validate("١١٩٨٧٦٥٤٣٢١", countryCode: "BR").isValid) // Arabic digits
    }

    func testRejectEmptyOrWhitespace() {
        XCTAssertFalse(SignupPhone.validate("", countryCode: "BR").isValid)
        XCTAssertFalse(SignupPhone.validate("   ", countryCode: "US").isValid)
    }

    // MARK: - Display Formatting

    func testFormatDisplay() {
        XCTAssertEqual(SignupPhone.formatDisplay("6505551234", countryCode: "US"), "(650) 555-1234")
        XCTAssertEqual(SignupPhone.formatDisplay("+16505551234", countryCode: "US"), "(650) 555-1234")
        XCTAssertEqual(SignupPhone.formatDisplay("11987654321", countryCode: "BR"), "(11) 98765-4321")
        XCTAssertEqual(SignupPhone.formatDisplay("+5511987654321", countryCode: "BR"), "(11) 98765-4321")
        XCTAssertEqual(SignupPhone.formatDisplay("7700900123", countryCode: "GB"), "07700 900123")
    }

    // MARK: - SignupCountry

    func testSignupCountryMetadata() {
        XCTAssertEqual(SignupCountry.supported.map(\.code), ["US", "CA", "BR", "GB"])
        XCTAssertEqual(SignupCountry.forCode("br")?.code, "BR")
        XCTAssertEqual(SignupCountry.forCode("US")?.dialingPrefix, "+1")
        XCTAssertEqual(SignupCountry.forCode("BR")?.dialingPrefix, "+55")
        XCTAssertEqual(SignupCountry.forCode("GB")?.dialingPrefix, "+44")
        XCTAssertNil(SignupCountry.forCode("DE"))

        XCTAssertEqual(SignupCountry.suggestedCountry(locale: Locale(identifier: "pt_BR")), "BR")
        XCTAssertEqual(SignupCountry.suggestedCountry(locale: Locale(identifier: "en_CA")), "CA")
        XCTAssertEqual(SignupCountry.suggestedCountry(locale: Locale(identifier: "ja_JP")), "US")
    }
}
