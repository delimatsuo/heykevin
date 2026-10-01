import Foundation
import XCTest
@testable import Kevin

final class ServiceBindingTests: XCTestCase {

    // MARK: - Parsing Service Binding from Profile

    func testParseServiceBindingFromProfile() {
        let profile: [String: Any] = [
            "contractor_id": "c_123",
            "country_code": "US",
            "service_binding": [
                "country_code": "BR",
                "provider": "twilio",
                "number_type": "mobile",
                "capabilities": [
                    "sms": true,
                    "voice": true
                ]
            ]
        ]

        let binding = ServiceBindingParser.parse(from: profile)
        XCTAssertNotNil(binding)
        XCTAssertEqual(binding?.countryCode, "BR")
        XCTAssertEqual(binding?.provider, "twilio")
        XCTAssertEqual(binding?.numberType, "mobile")
        XCTAssertEqual(binding?.capabilities?["sms"], true)
        XCTAssertEqual(binding?.capabilities?["voice"], true)
        XCTAssertTrue(binding?.isVoiceCapable ?? false)
        XCTAssertTrue(binding?.isSMSCapable ?? false)
    }

    func testConflictingOuterAndInnerCountryCodesInnerWins() {
        let profile: [String: Any] = [
            "contractor_id": "c_123",
            "country_code": "US",
            "service_binding": [
                "country_code": "BR",
                "provider": "twilio"
            ]
        ]

        let binding = ServiceBindingParser.parse(from: profile)
        XCTAssertNotNil(binding)
        XCTAssertEqual(binding?.countryCode, "BR", "Inner service_binding country code must take precedence")
    }

    func testParseServiceBindingWhenAbsentOrNull() {
        // Outer profile country US without binding => nil (Never treat profile.country_code as a binding)
        let profileNoBinding: [String: Any] = [
            "contractor_id": "c_123",
            "country_code": "US"
        ]
        XCTAssertNil(ServiceBindingParser.parse(from: profileNoBinding), "Absent service_binding must return nil, never treat profile country_code as binding")

        // Outer profile with null service_binding => nil
        let profileNullBinding: [String: Any] = [
            "contractor_id": "c_123",
            "country_code": "US",
            "service_binding": NSNull()
        ]
        XCTAssertNil(ServiceBindingParser.parse(from: profileNullBinding), "Null service_binding must return nil")
    }

    // MARK: - Parsing Direct Binding Dictionary

    func testParseBindingDirectDictionary() {
        let bindingDict: [String: Any] = [
            "country_code": "gb",
            "provider": "twilio",
            "number_type": "mobile"
        ]

        let binding = ServiceBindingParser.parseBinding(bindingDict)
        XCTAssertNotNil(binding)
        XCTAssertEqual(binding?.countryCode, "GB")
        XCTAssertEqual(binding?.provider, "twilio")
        XCTAssertEqual(binding?.numberType, "mobile")
    }

    func testParseBindingNilOrNonDictReturnsNil() {
        XCTAssertNil(ServiceBindingParser.parseBinding(nil))
        XCTAssertNil(ServiceBindingParser.parseBinding(NSNull()))
        XCTAssertNil(ServiceBindingParser.parseBinding("not a dictionary"))
        XCTAssertNil(ServiceBindingParser.parseBinding(123))
    }

    func testExplicitEmptyOrAllNullDictionaryReturnsNonNilUnknownBinding() {
        // Explicit empty dictionary
        let emptyDict: [String: Any] = [:]
        let parsedEmpty = ServiceBindingParser.parseBinding(emptyDict)
        XCTAssertNotNil(parsedEmpty, "Explicit empty dictionary must parse to non-nil ServiceBinding")
        XCTAssertNil(parsedEmpty?.countryCode)
        XCTAssertNil(parsedEmpty?.provider)
        XCTAssertNil(parsedEmpty?.numberType)
        XCTAssertNil(parsedEmpty?.capabilities)
        XCTAssertFalse(parsedEmpty?.isVoiceCapable ?? true)

        // Explicit all-null dictionary
        let nullFieldsDict: [String: Any] = [
            "country_code": NSNull(),
            "provider": NSNull(),
            "number_type": NSNull(),
            "capabilities": NSNull()
        ]
        let parsedNullFields = ServiceBindingParser.parseBinding(nullFieldsDict)
        XCTAssertNotNil(parsedNullFields, "Explicit all-null dictionary must parse to non-nil ServiceBinding")
        XCTAssertNil(parsedNullFields?.countryCode)
        XCTAssertNil(parsedNullFields?.provider)
        XCTAssertNil(parsedNullFields?.numberType)
        XCTAssertNil(parsedNullFields?.capabilities)
    }

    // MARK: - Capability Strict Boolean Identity

    func testRejectsNonBooleanInCapabilities() {
        let bindingDict: [String: Any] = [
            "country_code": "BR",
            "capabilities": [
                "voice": 1, // numeric integer masquerading as bool
                "sms": true,
                "data": "true", // string masquerading as bool
                "fax": 0 // numeric zero masquerading as bool
            ]
        ]

        let binding = ServiceBindingParser.parseBinding(bindingDict)
        XCTAssertNotNil(binding)
        XCTAssertNil(binding?.capabilities?["voice"], "Numeric 1 must not become a capability")
        XCTAssertNil(binding?.capabilities?["data"], "String 'true' must not become a capability")
        XCTAssertNil(binding?.capabilities?["fax"], "Numeric 0 must not become a capability")
        XCTAssertEqual(binding?.capabilities?["sms"], true)
        XCTAssertFalse(binding?.isVoiceCapable ?? true)
        XCTAssertTrue(binding?.isSMSCapable ?? false)
    }

    func testVoiceCapabilityDefaultsToFalse() {
        let bindingNoCaps = ServiceBinding(countryCode: "US", provider: "twilio")
        XCTAssertFalse(bindingNoCaps.isVoiceCapable)

        let bindingEmptyCaps = ServiceBinding(countryCode: "US", capabilities: [:])
        XCTAssertFalse(bindingEmptyCaps.isVoiceCapable)

        let bindingVoiceFalse = ServiceBinding(countryCode: "US", capabilities: ["voice": false])
        XCTAssertFalse(bindingVoiceFalse.isVoiceCapable)
    }

    // MARK: - Country Code Normalization

    func testCountryCodeNormalization() {
        XCTAssertEqual(ServiceBinding(countryCode: "br").countryCode, "BR")
        XCTAssertEqual(ServiceBinding(countryCode: " US \n").countryCode, "US")
        XCTAssertNil(ServiceBinding(countryCode: "invalid").countryCode)
        XCTAssertNil(ServiceBinding(countryCode: "").countryCode)
        XCTAssertNil(ServiceBinding(countryCode: "   ").countryCode)
        XCTAssertNil(ServiceBinding(countryCode: "12").countryCode)
        XCTAssertNil(ServiceBinding(countryCode: nil).countryCode)
    }

    // MARK: - Parse from Data Delegates Outer Profile

    func testParseServiceBindingFromDataDelegatesOuterProfile() throws {
        let jsonWithBinding = """
        {
            "contractor_id": "c_123",
            "service_binding": {
                "country_code": "BR",
                "provider": "twilio",
                "number_type": "mobile",
                "capabilities": {"sms": true}
            }
        }
        """.data(using: .utf8)!

        let binding = ServiceBindingParser.parse(data: jsonWithBinding)
        XCTAssertNotNil(binding)
        XCTAssertEqual(binding?.countryCode, "BR")
        XCTAssertEqual(binding?.provider, "twilio")
        XCTAssertEqual(binding?.numberType, "mobile")
        XCTAssertEqual(binding?.capabilities?["sms"], true)
        XCTAssertFalse(binding?.isVoiceCapable ?? true)

        let jsonWithoutBinding = """
        {
            "contractor_id": "c_123",
            "country_code": "US"
        }
        """.data(using: .utf8)!

        XCTAssertNil(ServiceBindingParser.parse(data: jsonWithoutBinding))
    }

    // MARK: - AppState Lifecycle and Isolation

    @MainActor
    func testAppStateServiceBindingResetOnContractorChange() {
        let state = AppState(inMemory: true)
        state.contractorId = "c_initial"
        state.serviceBinding = ServiceBinding(countryCode: "BR", provider: "twilio")
        XCTAssertEqual(state.serviceBinding?.countryCode, "BR")

        // Changing contractorId must reset serviceBinding to prevent cross-account leak
        state.contractorId = "c_different"
        XCTAssertNil(state.serviceBinding, "serviceBinding must be reset when contractorId changes")
    }

    @MainActor
    func testAppStateServiceBindingResetOnAuthChange() {
        var currentAuth = CallAuthContext(contractorId: "c_auth_test", bearerToken: "tok", generation: 1)
        let state = AppState(authProvider: { currentAuth }, inMemory: true)
        state.contractorId = "c_auth_test"
        state.serviceBinding = ServiceBinding(countryCode: "BR", provider: "twilio")

        // Invalidate auth context and apply change
        currentAuth = CallAuthContext(contractorId: "", bearerToken: "", generation: 2)
        state.applyAuthChange()
        XCTAssertNil(state.serviceBinding, "serviceBinding must be reset when auth context is cleared")
    }

    // MARK: - Precedence and Resolution Tests

    func testAssignedBindingPrecedenceOverProfileCountry() {
        let binding = ServiceBinding(countryCode: "BR", provider: "twilio")
        let profileCountry = "US"

        let resolved = ForwardingCountry.resolve(
            serviceBinding: binding,
            accountCountry: profileCountry,
            assignedNumber: "+5511987654321",
            hasAssignedNumber: true,
            locale: Locale(identifier: "en_US")
        )

        // Binding country must win over profile country
        XCTAssertEqual(resolved, "BR")
    }

    func testAssignedBindingInvalidFailsClosedWithoutFallback() {
        let badBinding = ServiceBinding(countryCode: "invalid", provider: "twilio")

        let resolved = ForwardingCountry.resolve(
            serviceBinding: badBinding,
            accountCountry: "US",
            assignedNumber: "+16505551234",
            hasAssignedNumber: true,
            locale: Locale(identifier: "en_US")
        )

        // Invalid binding present must fail closed and return empty string
        XCTAssertEqual(resolved, "")
    }

    func testAssignedNANPFallbackWhenBindingAbsent() {
        let profileCountry = "CA"

        let resolved = ForwardingCountry.resolve(
            serviceBinding: nil,
            accountCountry: profileCountry,
            assignedNumber: "+14165551234",
            hasAssignedNumber: true,
            locale: Locale(identifier: "pt_BR")
        )

        // Confirmed account country CA must be used with valid NANP number
        XCTAssertEqual(resolved, "CA")
    }

    func testAssignedNonNANPFallbackRejectedWhenBindingAbsent() {
        let profileCountry = "BR"

        let resolved = ForwardingCountry.resolve(
            serviceBinding: nil,
            accountCountry: profileCountry,
            assignedNumber: "+5511987654321",
            hasAssignedNumber: true,
            locale: Locale(identifier: "pt_BR")
        )

        // Non-NANP legacy fallback without binding must return empty string
        XCTAssertEqual(resolved, "")
    }
}
