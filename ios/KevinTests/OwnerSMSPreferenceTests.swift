import XCTest
@testable import Kevin

final class OwnerSMSPreferenceTests: XCTestCase {

    func testStrictBooleanAndIntegerHelper() {
        // Strict booleans
        XCTAssertEqual(OwnerSMSParsingHelper.strictBool(NSNumber(value: true)), true)
        XCTAssertEqual(OwnerSMSParsingHelper.strictBool(NSNumber(value: false)), false)
        XCTAssertNil(OwnerSMSParsingHelper.strictBool(1))
        XCTAssertNil(OwnerSMSParsingHelper.strictBool(0))
        XCTAssertNil(OwnerSMSParsingHelper.strictBool("true"))
        XCTAssertNil(OwnerSMSParsingHelper.strictBool("false"))
        XCTAssertNil(OwnerSMSParsingHelper.strictBool(nil))

        // Strict integers
        XCTAssertEqual(OwnerSMSParsingHelper.strictInt(NSNumber(value: 42)), 42)
        XCTAssertEqual(OwnerSMSParsingHelper.strictInt(NSNumber(value: 0)), 0)
        XCTAssertNil(OwnerSMSParsingHelper.strictInt(NSNumber(value: true)))
        XCTAssertNil(OwnerSMSParsingHelper.strictInt(NSNumber(value: false)))
        XCTAssertNil(OwnerSMSParsingHelper.strictInt("42"))
        XCTAssertNil(OwnerSMSParsingHelper.strictInt(nil))
        for invalid in [1.7, Double.nan, Double.infinity, -Double.infinity, Double(Int.max)] {
            XCTAssertNil(OwnerSMSParsingHelper.strictInt(NSNumber(value: invalid)))
        }
    }

    func testParseProfileDefaultMissingFields() {
        let emptyDoc: [String: Any] = [
            "contractor_id": "c123",
            "business_name": "Test Co",
        ]
        let parsed = OwnerSMSResponseParser.parseProfile(dict: emptyDoc)
        // Absent owner_sms_enabled defaults true
        XCTAssertTrue(parsed.enabled)
        // Absent owner_sms_opted_out defaults false
        XCTAssertFalse(parsed.optedOut)
        // Absent revision defaults 0
        XCTAssertEqual(parsed.revision, 0)
        XCTAssertFalse(parsed.isVersioned)
    }

    func testParseProfileMalformedFieldsFailClosed() {
        let malformedDoc: [String: Any] = [
            "owner_sms_enabled": "not_a_bool",
            "owner_sms_opted_out": 1, // numeric 1 is not strict boolean
            "owner_sms_opt_out_revision": "invalid_rev",
        ]
        let parsed = OwnerSMSResponseParser.parseProfile(dict: malformedDoc)
        // Malformed enabled fails closed (false)
        XCTAssertFalse(parsed.enabled)
        // Malformed opted_out fails closed (true / carrier blocked)
        XCTAssertTrue(parsed.optedOut)
        XCTAssertEqual(parsed.revision, 0)
        XCTAssertFalse(parsed.isVersioned)
    }

    func testParseProfileExplicitValues() {
        let doc: [String: Any] = [
            "owner_sms_enabled": NSNumber(value: false),
            "owner_sms_opted_out": NSNumber(value: true),
            "owner_sms_opt_out_revision": NSNumber(value: 5),
            "owner_sms_settings_version": NSNumber(value: 1),
        ]
        let parsed = OwnerSMSResponseParser.parseProfile(dict: doc)
        XCTAssertFalse(parsed.enabled)
        XCTAssertTrue(parsed.optedOut)
        XCTAssertEqual(parsed.revision, 5)
        XCTAssertEqual(parsed.version, 1)
        XCTAssertTrue(parsed.isVersioned)
    }

    func testParsePatchResponseSuccess() {
        let json: [String: Any] = [
            "status": "ok",
            "owner_sms_enabled": NSNumber(value: false),
            "owner_sms_opted_out": NSNumber(value: false),
            "owner_sms_opt_out_revision": NSNumber(value: 2),
            "owner_sms_settings_version": NSNumber(value: 1),
        ]
        let data = try! JSONSerialization.data(withJSONObject: json)
        let response = HTTPURLResponse(url: URL(string: "https://api.test/api/contractors/c123")!, statusCode: 200, httpVersion: nil, headerFields: nil)!

        let result = OwnerSMSResponseParser.parsePatchResponse(data: data, response: response)
        XCTAssertEqual(
            result,
            .success(enabled: false, optedOut: false, revision: 2, version: 1)
        )
    }

    func testParsePatchResponseUnsupportedOldBackend() {
        // Legacy backend returning generic status: ok without version 1 or explicit booleans
        let legacyJson: [String: Any] = ["status": "ok"]
        let data = try! JSONSerialization.data(withJSONObject: legacyJson)
        let response = HTTPURLResponse(url: URL(string: "https://api.test/api/contractors/c123")!, statusCode: 200, httpVersion: nil, headerFields: nil)!

        let result = OwnerSMSResponseParser.parsePatchResponse(data: data, response: response)
        XCTAssertEqual(result, .unsupported)
    }

    func testParsePatchResponseHttpError() {
        let errorJson: [String: Any] = ["detail": "Failed to persist preferences"]
        let data = try! JSONSerialization.data(withJSONObject: errorJson)
        let response = HTTPURLResponse(url: URL(string: "https://api.test/api/contractors/c123")!, statusCode: 500, httpVersion: nil, headerFields: nil)!

        let result = OwnerSMSResponseParser.parsePatchResponse(data: data, response: response)
        XCTAssertEqual(result, .failure("Could not save SMS notifications. Please try again."))
    }

    func testStatePolicySaveSuccessAndFailedSaveRollback() {
        let auth = CallAuthContext(contractorId: "c123", bearerToken: "token123", generation: 1)
        var state = OwnerSMSPreferenceState(boundAuth: auth, confirmedEnabled: true, carrierOptedOut: false, isLoaded: true)

        // 1. Begin save to disable SMS
        guard let (token1, previous) = state.beginSave(targetEnabled: false, auth: auth) else {
            XCTFail("beginSave should succeed")
            return
        }
        XCTAssertEqual(previous, true)
        XCTAssertTrue(state.isSaving)
        XCTAssertEqual(state.saveError, "")

        // 2. Failed save simulation
        state.finishSave(result: .failure("Network error"), operationToken: token1, auth: auth)
        XCTAssertFalse(state.isSaving)
        XCTAssertEqual(state.confirmedEnabled, true) // rolled back to previous confirmed value
        XCTAssertEqual(state.saveError, "Network error")
        XCTAssertEqual(state.requestedEnabled, false)

        // 3. Retry with successful save
        guard let (token2, _) = state.beginSave(targetEnabled: state.requestedEnabled!, auth: auth) else {
            XCTFail("beginSave should succeed for retry")
            return
        }
        state.finishSave(
            result: .success(enabled: false, optedOut: false, revision: 1, version: 1),
            operationToken: token2,
            auth: auth
        )
        XCTAssertFalse(state.isSaving)
        XCTAssertEqual(state.confirmedEnabled, false)
        XCTAssertEqual(state.saveError, "")
        XCTAssertEqual(state.optOutRevision, 1)
        XCTAssertNil(state.requestedEnabled)
    }

    func testStaleSaveResponseIgnored() {
        let auth = CallAuthContext(contractorId: "c123", bearerToken: "token123", generation: 1)
        var state = OwnerSMSPreferenceState(boundAuth: auth, confirmedEnabled: true, isLoaded: true)

        let (token1, _) = state.beginSave(targetEnabled: false, auth: auth)!
        // Simulate save 1 failing/timing out, clearing isSaving
        state.finishSave(result: .failure("Timeout"), operationToken: token1, auth: auth)
        XCTAssertFalse(state.isSaving)

        // Save 2 begins, incrementing fenceRevision
        let (token2, _) = state.beginSave(targetEnabled: true, auth: auth)!
        XCTAssertTrue(state.isSaving)

        // Old delayed response for token1 arrives late
        state.finishSave(
            result: .success(enabled: false, optedOut: false, revision: 1, version: 1),
            operationToken: token1,
            auth: auth
        )
        // Must be ignored because token1 != current fenceRevision (token2)
        XCTAssertTrue(state.isSaving) // still waiting for token2

        state.finishSave(
            result: .success(enabled: true, optedOut: false, revision: 2, version: 1),
            operationToken: token2,
            auth: auth
        )
        XCTAssertFalse(state.isSaving)
        XCTAssertEqual(state.confirmedEnabled, true)
        XCTAssertEqual(state.optOutRevision, 2)
    }

    func testStaleProfileGetIgnoredAfterToggle() {
        let auth = CallAuthContext(contractorId: "c123", bearerToken: "token123", generation: 1)
        var state = OwnerSMSPreferenceState(boundAuth: auth, confirmedEnabled: true, isLoaded: true)

        // 1. Capture load token before initiating GET
        let loadToken = state.captureLoadToken(auth: auth)!

        // 2. User toggles the preference switch before GET returns
        let (saveToken, _) = state.beginSave(targetEnabled: false, auth: auth)!
        state.finishSave(
            result: .success(enabled: false, optedOut: false, revision: 1, version: 1),
            operationToken: saveToken,
            auth: auth
        )
        XCTAssertEqual(state.confirmedEnabled, false)

        // 3. Stale GET response arrives reporting owner_sms_enabled = true
        let staleServerDoc: [String: Any] = [
            "owner_sms_enabled": NSNumber(value: true),
            "owner_sms_opted_out": NSNumber(value: false),
            "owner_sms_opt_out_revision": NSNumber(value: 0),
        ]
        state.applyHydration(dict: staleServerDoc, token: loadToken, auth: auth)

        // Stale GET must NOT revert the user's toggle!
        XCTAssertEqual(state.confirmedEnabled, false)
        XCTAssertEqual(state.optOutRevision, 1)
    }

    func testCrossAccountReset() {
        let authA = CallAuthContext(contractorId: "contractor_A", bearerToken: "token_A", generation: 1)
        var state = OwnerSMSPreferenceState(boundAuth: authA, confirmedEnabled: false, carrierOptedOut: true, optOutRevision: 3)

        // Switch to Account B
        let authB = CallAuthContext(contractorId: "contractor_B", bearerToken: "token_B", generation: 2)
        state.reset(auth: authB)

        XCTAssertEqual(state.boundAuth, authB)
        XCTAssertEqual(state.confirmedEnabled, true) // reset to default
        XCTAssertEqual(state.carrierOptedOut, false)
        XCTAssertEqual(state.optOutRevision, 0)
        XCTAssertFalse(state.isLoaded)
        XCTAssertFalse(state.isSaving)

        // Late response for Account A must be rejected
        let docA: [String: Any] = [
            "owner_sms_enabled": NSNumber(value: false),
            "owner_sms_opted_out": NSNumber(value: true),
        ]
        state.applyHydration(dict: docA, token: 1, auth: authA)
        XCTAssertEqual(state.confirmedEnabled, true) // unchanged
    }

    func testProviderCarrierBlockAndSTARTIndependence() {
        let auth = CallAuthContext(contractorId: "c123", bearerToken: "token123", generation: 1)
        var state = OwnerSMSPreferenceState(boundAuth: auth, confirmedEnabled: false, carrierOptedOut: true)

        // 1. App switch is false AND carrier blocked is true
        XCTAssertFalse(state.displayedEnabled)
        XCTAssertTrue(state.isCarrierBlocked)

        // 2. Profile GET arrives after user replied START (cleared carrier block, but app switch remains false)
        let token = state.captureLoadToken(auth: auth)!
        let docAfterStart: [String: Any] = [
            "owner_sms_enabled": NSNumber(value: false),
            "owner_sms_opted_out": NSNumber(value: false),
            "owner_sms_opt_out_revision": NSNumber(value: 2),
            "owner_sms_settings_version": NSNumber(value: 1),
        ]
        state.applyHydration(dict: docAfterStart, token: token, auth: auth)

        XCTAssertFalse(state.isCarrierBlocked) // carrier block cleared
        XCTAssertFalse(state.displayedEnabled)  // app switch remains false (independent!)
    }

    func testUnsupportedOldBackendState() {
        let auth = CallAuthContext(contractorId: "c123", bearerToken: "token123", generation: 1)
        var state = OwnerSMSPreferenceState(boundAuth: auth, confirmedEnabled: true, isLoaded: true)

        let (saveToken, _) = state.beginSave(targetEnabled: false, auth: auth)!
        state.finishSave(result: .unsupported, operationToken: saveToken, auth: auth)

        XCTAssertTrue(state.isUnsupportedBackend)
        XCTAssertFalse(state.saveError.isEmpty)
    }

    private let auth = CallAuthContext(contractorId: "test_owner", bearerToken: "test_token", generation: 1)

    private func profile(enabled: Bool = true, optedOut: Bool = false) -> [String: Any] {
        ["owner_sms_enabled": enabled, "owner_sms_opted_out": optedOut,
         "owner_sms_opt_out_revision": 3, "owner_sms_settings_version": 1]
    }

    func testColdLoadBindsCurrentAccountAndLoadsConfirmedPreference() {
        var state = OwnerSMSPreferenceState()
        XCTAssertNil(state.beginSave(targetEnabled: false, auth: auth))
        let token = state.captureLoadToken(auth: auth)!
        state.applyHydration(dict: profile(enabled: false), token: token, auth: auth)
        XCTAssertEqual(state.boundAuth, auth)
        XCTAssertTrue(state.isLoaded)
        XCTAssertFalse(state.displayedEnabled)
        XCTAssertNotNil(state.beginSave(targetEnabled: true, auth: auth))
        XCTAssertTrue(state.displayedEnabled)
    }

    func testMalformedOrLegacyProfileNeverEnablesControl() {
        var documents: [[String: Any]] = [[:]]
        for key in ["owner_sms_enabled", "owner_sms_opted_out", "owner_sms_opt_out_revision", "owner_sms_settings_version"] {
            var missing = profile()
            missing.removeValue(forKey: key)
            documents.append(missing)
            var malformed = profile()
            malformed[key] = "1"
            documents.append(malformed)
        }
        for key in ["owner_sms_opt_out_revision", "owner_sms_settings_version"] {
            var fractional = profile()
            fractional[key] = 1.7
            documents.append(fractional)
        }
        for document in documents {
            var state = OwnerSMSPreferenceState()
            let token = state.captureLoadToken(auth: auth)!
            state.applyHydration(dict: document, token: token, auth: auth)
            XCTAssertFalse(state.isLoaded)
            XCTAssertTrue(state.isUnsupportedBackend)
            XCTAssertFalse(state.loadError.isEmpty)
            XCTAssertNil(state.beginSave(targetEnabled: false, auth: auth))
        }
    }

    func testLoadsDuringSaveAndLateLoadsCannotRevertAcknowledgedOff() {
        var state = OwnerSMSPreferenceState(boundAuth: auth, isLoaded: true)
        let earlierLoad = state.captureLoadToken(auth: auth)!
        let (save, _) = state.beginSave(targetEnabled: false, auth: auth)!
        XCTAssertNil(state.captureLoadToken(auth: auth))
        state.applyHydration(dict: profile(), token: earlierLoad, auth: auth)
        XCTAssertTrue(state.isSaving)
        state.finishSave(result: .success(enabled: false, optedOut: false, revision: 3, version: 1), operationToken: save, auth: auth)
        state.applyHydration(dict: profile(), token: earlierLoad, auth: auth)
        XCTAssertFalse(state.confirmedEnabled)
    }

    func testLatestLoadWinsAndStaleLoadFailureIsIgnored() {
        var state = OwnerSMSPreferenceState()
        let first = state.captureLoadToken(auth: auth)!
        let second = state.captureLoadToken(auth: auth)!
        state.applyHydration(dict: profile(enabled: false, optedOut: true), token: second, auth: auth)
        state.applyHydration(dict: profile(), token: first, auth: auth)
        state.recordLoadFailure(token: first, auth: auth)
        XCTAssertFalse(state.confirmedEnabled)
        XCTAssertTrue(state.isCarrierBlocked)
        XCTAssertTrue(state.loadError.isEmpty)
    }

    func testInitialLoadFailureAndRetryAreVisibleAndAccountBound() {
        var state = OwnerSMSPreferenceState()
        let first = state.captureLoadToken(auth: auth)!
        state.recordLoadFailure(token: first, auth: auth)
        XCTAssertFalse(state.isLoaded)
        XCTAssertFalse(state.loadError.isEmpty)
        let retry = state.captureLoadToken(auth: auth)!
        state.applyHydration(dict: profile(), token: retry, auth: auth)
        XCTAssertTrue(state.isLoaded)
        XCTAssertTrue(state.loadError.isEmpty)
        let replacement = CallAuthContext(contractorId: "other", bearerToken: "other_token", generation: 2)
        state.reset(auth: replacement)
        state.recordLoadFailure(token: retry, auth: auth)
        XCTAssertTrue(state.loadError.isEmpty)
        XCTAssertFalse(state.isLoaded)
    }

    func testCrossAccountLateSaveCannotAcknowledgeNewAccount() {
        var state = OwnerSMSPreferenceState(boundAuth: auth, isLoaded: true)
        let (save, _) = state.beginSave(targetEnabled: false, auth: auth)!
        let other = CallAuthContext(contractorId: "other", bearerToken: "other_token", generation: 2)
        state.reset(auth: other)
        state.finishSave(result: .success(enabled: false, optedOut: true, revision: 4, version: 1), operationToken: save, auth: auth)
        XCTAssertFalse(state.isLoaded)
        XCTAssertFalse(state.isSaving)
        XCTAssertNil(state.requestedEnabled)
        XCTAssertFalse(state.isCarrierBlocked)
    }

    func testAcknowledgedDifferentPreferenceRetainsRequestedRetry() {
        var state = OwnerSMSPreferenceState(boundAuth: auth, isLoaded: true)
        let (save, _) = state.beginSave(targetEnabled: false, auth: auth)!
        state.finishSave(result: .success(enabled: true, optedOut: false, revision: 3, version: 1), operationToken: save, auth: auth)
        XCTAssertTrue(state.confirmedEnabled)
        XCTAssertEqual(state.requestedEnabled, false)
        XCTAssertFalse(state.saveError.isEmpty)
    }

    func testPatchRequiresAcknowledgementAndValidRevision() throws {
        let response = HTTPURLResponse(url: URL(string: "https://example.invalid")!, statusCode: 200, httpVersion: nil, headerFields: nil)!
        var valid = profile(enabled: false)
        valid["status"] = "ok"
        var missingRevision = valid
        missingRevision.removeValue(forKey: "owner_sms_opt_out_revision")
        var negativeRevision = valid
        negativeRevision["owner_sms_opt_out_revision"] = -1
        var numericBool = valid
        numericBool["owner_sms_enabled"] = 0
        for json in [missingRevision, negativeRevision, numericBool] {
            let data = try JSONSerialization.data(withJSONObject: json)
            XCTAssertEqual(OwnerSMSResponseParser.parsePatchResponse(data: data, response: response), .unsupported)
        }
        valid["status"] = "error"
        let data = try JSONSerialization.data(withJSONObject: valid)
        if case .failure = OwnerSMSResponseParser.parsePatchResponse(data: data, response: response) {} else {
            XCTFail("A 200 response without successful acknowledgement must not confirm an opt-out")
        }
    }
}


extension OwnerSMSPreferenceTests {
    private var canadianAuth: CallAuthContext {
        .init(contractorId: "synthetic-ca", bearerToken: "synthetic-token", generation: 1)
    }

    private func canadianProfile(required: Any? = true, owner: String = "+14165550100",
                                  sender: String = "+14165550101", revision: Int = 3,
                                  optedOut: Bool = false) -> [String: Any] {
        var dict: [String: Any] = ["owner_sms_enabled": true, "owner_sms_opted_out": optedOut,
            "owner_sms_opt_out_revision": revision, "owner_sms_settings_version": 1,
            "country_code": "CA", "service_binding": ["country_code": "CA"],
            "owner_phone": owner, "twilio_number": sender]
        dict["owner_sms_consent_required"] = required
        return dict
    }

    private func hydratedCanadian(_ profile: [String: Any]? = nil) -> OwnerSMSPreferenceState {
        var state = OwnerSMSPreferenceState()
        let token = state.captureLoadToken(auth: canadianAuth)!
        state.applyHydration(dict: profile ?? canadianProfile(), token: token, auth: canadianAuth)
        return state
    }

    func testCanadianExplicitConsentRequirementCannotUseGenericToggle() {
        var state = hydratedCanadian(canadianProfile(required: true))
        XCTAssertTrue(state.confirmedEnabled)
        XCTAssertTrue(state.consentRequired)
        XCTAssertFalse(state.displayedEnabled)
        XCTAssertNotNil(state.consentSnapshot(auth: canadianAuth))
        XCTAssertNil(state.beginSave(targetEnabled: true, auth: canadianAuth))
    }

    func testCanadianMissingOrMalformedConsentCapabilityDoesNotOfferBrokenOptIn() {
        for value: Any? in [nil, "false", 0] {
            var state = hydratedCanadian(canadianProfile(required: value))
            XCTAssertFalse(state.isLoaded)
            XCTAssertTrue(state.isUnsupportedBackend)
            XCTAssertEqual(state.loadError, OwnerSMSPreferenceState.unavailableNotice)
            XCTAssertNil(state.consentSnapshot(auth: canadianAuth))
            XCTAssertNil(state.beginSave(targetEnabled: false, auth: canadianAuth))
            XCTAssertNil(state.beginSave(targetEnabled: true, auth: canadianAuth))
        }
    }

    func testCanadianConsentSnapshotPinsExactPayload() throws {
        let state = hydratedCanadian()
        let snapshot = try XCTUnwrap(state.consentSnapshot(auth: canadianAuth))
        XCTAssertEqual(Set(snapshot.requestBody.keys), ["disclosure_version", "owner_phone_e164", "twilio_number", "expected_revision"])
        XCTAssertEqual(snapshot.requestBody["disclosure_version"] as? String, "ca-owner-sms-v1")
        XCTAssertEqual(snapshot.requestBody["owner_phone_e164"] as? String, "+14165550100")
        XCTAssertEqual(snapshot.requestBody["twilio_number"] as? String, "+14165550101")
        XCTAssertEqual(snapshot.requestBody["expected_revision"] as? Int, 3)
        for text in ["Hey Kevin", "call summaries", "voicemail", "account notices", "frequency varies", "rates may apply", "STOP", "HELP"] {
            XCTAssertTrue(snapshot.disclosure.contains(text))
        }
    }

    func testCanonicalOwnerIdentityWinsOverFormattedOrDifferentRawPhone() throws {
        for raw in ["(604) 555-0100", "+14165550100"] {
            var profile = canadianProfile(owner: raw)
            profile["owner_phone_e164"] = "+16045550100"
            let state = hydratedCanadian(profile)
            let snapshot = try XCTUnwrap(state.consentSnapshot(auth: canadianAuth))
            XCTAssertEqual(state.ownerPhoneE164, "+16045550100")
            XCTAssertEqual(snapshot.ownerPhoneE164, "+16045550100")
            XCTAssertEqual(snapshot.requestBody["owner_phone_e164"] as? String, "+16045550100")
        }
    }

    func testPresentMalformedCanonicalOwnerNeverFallsBackToValidRawPhone() {
        for canonical: Any in ["", "(604) 555-0100", " +16045550100", "+06045550100", "+1604555010０", true, 16045550100, NSNull()] {
            var profile = canadianProfile(owner: "+14165550100")
            profile["owner_phone_e164"] = canonical
            let state = hydratedCanadian(profile)
            XCTAssertEqual(state.ownerPhoneE164, "")
            XCTAssertNil(state.consentSnapshot(auth: canadianAuth))
        }
    }

    func testAbsentCanonicalOwnerUsesOnlyStrictE164RawPhone() throws {
        let valid = hydratedCanadian(canadianProfile(owner: "+16045550100"))
        XCTAssertEqual(try XCTUnwrap(valid.consentSnapshot(auth: canadianAuth)).ownerPhoneE164, "+16045550100")
        XCTAssertNil(hydratedCanadian(canadianProfile(owner: "(604) 555-0100")).consentSnapshot(auth: canadianAuth))
    }

    func testCanonicalOwnerHydrationChangeInvalidatesOpenConfirmation() throws {
        var profile = canadianProfile(owner: "(604) 555-0100")
        profile["owner_phone_e164"] = "+16045550100"
        var state = hydratedCanadian(profile)
        let snapshot = try XCTUnwrap(state.consentSnapshot(auth: canadianAuth))
        profile["owner_phone_e164"] = "+16045550102"
        let token = try XCTUnwrap(state.captureLoadToken(auth: canadianAuth))
        state.applyHydration(dict: profile, token: token, auth: canadianAuth)
        XCTAssertNil(state.beginConsent(snapshot, auth: canadianAuth))
    }

    func testCanadianConsentRejectsStopMalformedIdentityAndHydrationDrift() throws {
        for profile in [canadianProfile(optedOut: true), canadianProfile(owner: "invalid"), canadianProfile(sender: "")] {
            XCTAssertNil(hydratedCanadian(profile).consentSnapshot(auth: canadianAuth))
        }
        for profile in [canadianProfile(owner: "+14165550102"), canadianProfile(sender: "+14165550103"), canadianProfile(revision: 4), canadianProfile(optedOut: true)] {
            var state = hydratedCanadian()
            let snapshot = try XCTUnwrap(state.consentSnapshot(auth: canadianAuth))
            let token = state.captureLoadToken(auth: canadianAuth)!
            state.applyHydration(dict: profile, token: token, auth: canadianAuth)
            XCTAssertNil(state.beginConsent(snapshot, auth: canadianAuth))
        }
        var state = hydratedCanadian()
        let snapshot = try XCTUnwrap(state.consentSnapshot(auth: canadianAuth))
        let changed = CallAuthContext(contractorId: canadianAuth.contractorId, bearerToken: "new-token", generation: 2)
        XCTAssertNil(state.beginConsent(snapshot, auth: changed))
        state.reset(auth: changed)
        XCTAssertNil(state.beginConsent(snapshot, auth: changed))
    }

    func testCanadianConsentRequiresExplicitAckAndUnchangedRevision() throws {
        let response = HTTPURLResponse(url: URL(string: "https://example.com")!, statusCode: 200, httpVersion: nil, headerFields: nil)!
        var ack = canadianProfile(required: false)
        ack["status"] = "ok"
        let parsed = OwnerSMSResponseParser.parseConsentResponse(data: try JSONSerialization.data(withJSONObject: ack), response: response)
        XCTAssertEqual(parsed, .success(enabled: true, optedOut: false, revision: 3, version: 1))
        for value: Any? in [nil, true, "false", 0] {
            ack["owner_sms_consent_required"] = value
            let result = OwnerSMSResponseParser.parseConsentResponse(data: try JSONSerialization.data(withJSONObject: ack), response: response)
            if case .success = result { XCTFail("Malformed consent acknowledgment accepted") }
        }
        var state = hydratedCanadian()
        let snapshot = try XCTUnwrap(state.consentSnapshot(auth: canadianAuth))
        let token = try XCTUnwrap(state.beginConsent(snapshot, auth: canadianAuth))
        state.finishConsent(result: .success(enabled: true, optedOut: false, revision: 4, version: 1), snapshot: snapshot, operationToken: token, auth: canadianAuth)
        XCTAssertTrue(state.consentRequired)
        XCTAssertFalse(state.isSaving)
        let nextSnapshot = try XCTUnwrap(state.consentSnapshot(auth: canadianAuth))
        let nextToken = try XCTUnwrap(state.beginConsent(nextSnapshot, auth: canadianAuth))
        state.finishConsent(result: parsed, snapshot: nextSnapshot, operationToken: nextToken, auth: canadianAuth)
        XCTAssertFalse(state.consentRequired)
        XCTAssertTrue(state.displayedEnabled)
        XCTAssertNil(state.consentSnapshot(auth: canadianAuth))
    }

    func testConsentReplyCannotCrossAccountFence() throws {
        var state = hydratedCanadian()
        let snapshot = try XCTUnwrap(state.consentSnapshot(auth: canadianAuth))
        let token = try XCTUnwrap(state.beginConsent(snapshot, auth: canadianAuth))
        let other = CallAuthContext(contractorId: "other", bearerToken: "other-token", generation: 2)
        state.reset(auth: other)
        state.finishConsent(result: .success(enabled: true, optedOut: false, revision: 3, version: 1), snapshot: snapshot, operationToken: token, auth: canadianAuth)
        XCTAssertEqual(state.boundAuth, other)
        XCTAssertFalse(state.isLoaded)
    }
}
