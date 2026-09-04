import Foundation
import XCTest
@testable import Kevin

/// `provision_twilio_number` (app/db/contractors.py) and `api_provision_number`
/// (app/api/contractors.py) both refuse to provision a Twilio number in six
/// regulatory countries unless the contractor has a business street address
/// and city on file. These tests hold `RegulatoryAddress` to that contract so
/// a divergence from the backend's `REGULATORY_COUNTRIES` set, its server
/// messages, or its field length limits fails loudly here rather than in
/// production.
final class RegulatoryAddressTests: XCTestCase {

    // MARK: - The regulatory country set

    func testCountriesMatchesTheBackendSet() {
        XCTAssertEqual(RegulatoryAddress.countries, ["DE", "FR", "IT", "ES", "PT", "BR"])
    }

    // MARK: - requiresAddress

    func testRequiresAddressIsTrueForEachRegulatoryCountry() {
        for code in ["DE", "FR", "IT", "ES", "PT", "BR"] {
            XCTAssertTrue(RegulatoryAddress.requiresAddress(countryCode: code), "expected \(code) to require an address")
        }
    }

    func testRequiresAddressIsFalseForNonRegulatoryCountries() {
        XCTAssertFalse(RegulatoryAddress.requiresAddress(countryCode: "US"))
        XCTAssertFalse(RegulatoryAddress.requiresAddress(countryCode: "CA"))
        XCTAssertFalse(RegulatoryAddress.requiresAddress(countryCode: "GB"))
    }

    func testRequiresAddressIsFalseForEmptyOrUnknownCodes() {
        XCTAssertFalse(RegulatoryAddress.requiresAddress(countryCode: ""))
        XCTAssertFalse(RegulatoryAddress.requiresAddress(countryCode: "xx"))
    }

    func testRequiresAddressIsCaseInsensitiveAndTrimmed() {
        XCTAssertTrue(RegulatoryAddress.requiresAddress(countryCode: "de"))
        XCTAssertTrue(RegulatoryAddress.requiresAddress(countryCode: " DE "))
    }

    // MARK: - needsAddressCapture

    func testNeedsAddressCaptureTrueForTheApiPreCheckMessage() {
        XCTAssertTrue(RegulatoryAddress.needsAddressCapture(
            errorMessage: "Business address and city are required for number provisioning in your country."
        ))
    }

    // provision_twilio_number's internal exception text is not reachable
    // through api_provision_number today: the endpoint's own pre-check
    // already returns before that exception can be raised, and if it were
    // ever raised, `except Exception` would remap it to the "Address
    // verification failed" message before any response left the server (its
    // text also contains "address"). This test only proves the substring
    // match still covers that text directly — defense in depth for a
    // hypothetical future code path, not a message the client can see today.
    func testNeedsAddressCaptureTrueForTheUnreachableServiceLayerMessage() {
        XCTAssertTrue(RegulatoryAddress.needsAddressCapture(
            errorMessage: "Business address and city required for number provisioning in this country"
        ))
    }

    func testNeedsAddressCaptureTrueForTheRejectedBundleMessage() {
        XCTAssertTrue(RegulatoryAddress.needsAddressCapture(
            errorMessage: "Address verification failed. Please check your business address."
        ))
    }

    func testNeedsAddressCaptureIsCaseInsensitive() {
        XCTAssertTrue(RegulatoryAddress.needsAddressCapture(
            errorMessage: "BUSINESS ADDRESS AND CITY ARE REQUIRED FOR NUMBER PROVISIONING IN YOUR COUNTRY."
        ))
        XCTAssertTrue(RegulatoryAddress.needsAddressCapture(
            errorMessage: "ADDRESS VERIFICATION FAILED. PLEASE CHECK YOUR BUSINESS ADDRESS."
        ))
    }

    func testNeedsAddressCaptureFalseForOtherServerMessages() {
        XCTAssertFalse(RegulatoryAddress.needsAddressCapture(
            errorMessage: "No phone numbers available in your area. Please try a different city."
        ))
        XCTAssertFalse(RegulatoryAddress.needsAddressCapture(
            errorMessage: "Your country is not yet supported for number provisioning."
        ))
        XCTAssertFalse(RegulatoryAddress.needsAddressCapture(
            errorMessage: "Failed to provision phone number. Please try again or contact support."
        ))
        XCTAssertFalse(RegulatoryAddress.needsAddressCapture(errorMessage: ""))
    }

    // MARK: - validate

    func testValidateSucceedsForATrimmedAddressAndCity() {
        XCTAssertEqual(RegulatoryAddress.validate(address: "Musterstrasse 1", city: "Berlin"), .valid)
    }

    func testValidateFailsForAWhitespaceOnlyAddress() {
        XCTAssertEqual(RegulatoryAddress.validate(address: "   ", city: "Berlin"), .missingAddress)
    }

    func testValidateFailsForAWhitespaceOnlyCity() {
        XCTAssertEqual(RegulatoryAddress.validate(address: "Musterstrasse 1", city: "  "), .missingCity)
    }

    func testValidateChecksAddressBeforeCity() {
        XCTAssertEqual(RegulatoryAddress.validate(address: "", city: ""), .missingAddress)
    }

    func testValidateFailsForAnOverLongAddress() {
        let tooLong = String(repeating: "a", count: 501)
        XCTAssertEqual(RegulatoryAddress.validate(address: tooLong, city: "Berlin"), .addressTooLong)
    }

    func testValidateFailsForAnOverLongCity() {
        let tooLong = String(repeating: "a", count: 101)
        XCTAssertEqual(RegulatoryAddress.validate(address: "Musterstrasse 1", city: tooLong), .cityTooLong)
    }

    func testValidateAllowsTheExactBoundaryLengths() {
        let maxAddress = String(repeating: "a", count: 500)
        let maxCity = String(repeating: "b", count: 100)
        XCTAssertEqual(RegulatoryAddress.validate(address: maxAddress, city: maxCity), .valid)
    }

    func testValidateCountsTheTrimmedStringAgainstTheLimit() {
        // Padding beyond the boundary must not push a boundary-length value
        // into the too-long case once whitespace is trimmed.
        let maxAddress = String(repeating: "a", count: 500)
        let padded = "  \(maxAddress)  "
        XCTAssertEqual(RegulatoryAddress.validate(address: padded, city: "Berlin"), .valid)
    }
}
