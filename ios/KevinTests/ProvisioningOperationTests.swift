import Foundation
import XCTest
@testable import Kevin

@MainActor
final class ProvisioningSequenceSpy {
    enum Stage: String, Equatable, CustomStringConvertible {
        case patch
        case fetchProfileInitial
        case provision
        case syncContacts
        case fetchProfileUUID
        case commit

        var description: String { rawValue }
    }

    private(set) var events: [Stage] = []
    var currentAuth: CallAuthContext
    var currentAppleUserId: String
    var committedOutput: ProvisioningOperation.Output?

    var onPatch: (() async throws -> Bool)?
    var onFetchProfileInitial: (() async -> [String: Any]?)?
    var onProvision: (() async -> [String: Any]?)?
    var onSyncContacts: (() async -> Int?)?
    var onFetchProfileUUID: (() async -> [String: Any]?)?
    private var profileFetchCount = 0

    init(initialAuth: CallAuthContext, initialAppleUserId: String = "apple-user-123") {
        self.currentAuth = initialAuth
        self.currentAppleUserId = initialAppleUserId
    }

    func isCurrent(capturedAuth: CallAuthContext, capturedAppleUserId: String) -> Bool {
        currentAuth == capturedAuth && currentAppleUserId == capturedAppleUserId
    }

    func patchProfile() async throws -> Bool {
        events.append(.patch)
        if let onPatch {
            return try await onPatch()
        }
        return true
    }

    func fetchProfile() async -> [String: Any]? {
        profileFetchCount += 1
        if profileFetchCount == 1 {
            events.append(.fetchProfileInitial)
            if let onFetchProfileInitial {
                return await onFetchProfileInitial()
            }
            return [:]
        } else {
            events.append(.fetchProfileUUID)
            if let onFetchProfileUUID {
                return await onFetchProfileUUID()
            }
            return [:]
        }
    }

    func provisionNumber() async -> [String: Any]? {
        events.append(.provision)
        if let onProvision {
            return await onProvision()
        }
        return [
            "status": "ok",
            "phone_number": "+5511987654321",
            "country_code": "BR",
            "service_binding": [
                "country_code": "BR",
                "provider": "twilio",
                "number_type": "mobile"
            ]
        ]
    }

    func syncContacts() async -> Int? {
        events.append(.syncContacts)
        if let onSyncContacts {
            return await onSyncContacts()
        }
        return 10
    }

    func commit(output: ProvisioningOperation.Output) {
        events.append(.commit)
        committedOutput = output
    }
}

@MainActor
final class ProvisioningOperationTests: XCTestCase {
    private let validAuth = CallAuthContext(
        contractorId: "test-contractor-1",
        bearerToken: "test-token-1",
        generation: 1
    )
    private let otherAuth = CallAuthContext(
        contractorId: "test-contractor-2",
        bearerToken: "test-token-2",
        generation: 2
    )
    private let appleUserId = "apple-user-123"

    // MARK: - Stale / Invalid Entry

    func testNoEffectsOnInvalidAuthEntry() async {
        let invalidAuth = CallAuthContext(contractorId: "", bearerToken: "", generation: 1)
        let spy = ProvisioningSequenceSpy(initialAuth: invalidAuth, initialAppleUserId: appleUserId)

        let result = await ProvisioningOperation.run(
            capturedAuth: invalidAuth,
            isCurrent: { spy.isCurrent(capturedAuth: invalidAuth, capturedAppleUserId: self.appleUserId) },
            patchProfile: { try await spy.patchProfile() },
            fetchProfile: { await spy.fetchProfile() },
            provisionNumber: { await spy.provisionNumber() },
            syncContacts: { await spy.syncContacts() },
            commit: { output in spy.commit(output: output) }
        )

        XCTAssertEqual(result, .superseded)
        XCTAssertEqual(spy.events, [])
        XCTAssertNil(spy.committedOutput)
    }

    func testNoEffectsOnStaleEntry() async {
        let spy = ProvisioningSequenceSpy(initialAuth: otherAuth, initialAppleUserId: appleUserId)

        let result = await ProvisioningOperation.run(
            capturedAuth: validAuth,
            isCurrent: { spy.isCurrent(capturedAuth: self.validAuth, capturedAppleUserId: self.appleUserId) },
            patchProfile: { try await spy.patchProfile() },
            fetchProfile: { await spy.fetchProfile() },
            provisionNumber: { await spy.provisionNumber() },
            syncContacts: { await spy.syncContacts() },
            commit: { output in spy.commit(output: output) }
        )

        XCTAssertEqual(result, .superseded)
        XCTAssertEqual(spy.events, [])
        XCTAssertNil(spy.committedOutput)
    }

    // MARK: - Suspended Stage Identity Switch

    func testIdentitySwitchDuringPatch_supersedesAndStopsLaterEffects() async {
        let spy = ProvisioningSequenceSpy(initialAuth: validAuth, initialAppleUserId: appleUserId)
        spy.onPatch = {
            spy.currentAuth = self.otherAuth
            return true
        }

        let result = await ProvisioningOperation.run(
            capturedAuth: validAuth,
            isCurrent: { spy.isCurrent(capturedAuth: self.validAuth, capturedAppleUserId: self.appleUserId) },
            patchProfile: { try await spy.patchProfile() },
            fetchProfile: { await spy.fetchProfile() },
            provisionNumber: { await spy.provisionNumber() },
            syncContacts: { await spy.syncContacts() },
            commit: { output in spy.commit(output: output) }
        )

        XCTAssertEqual(result, .superseded)
        XCTAssertEqual(spy.events, [.patch])
        XCTAssertNil(spy.committedOutput)
    }

    func testIdentitySwitchDuringInitialProfileFetch_supersedesAndStopsLaterEffects() async {
        let spy = ProvisioningSequenceSpy(initialAuth: validAuth, initialAppleUserId: appleUserId)
        spy.onFetchProfileInitial = {
            spy.currentAppleUserId = "switched-apple-user"
            return [:]
        }

        let result = await ProvisioningOperation.run(
            capturedAuth: validAuth,
            isCurrent: { spy.isCurrent(capturedAuth: self.validAuth, capturedAppleUserId: self.appleUserId) },
            patchProfile: { try await spy.patchProfile() },
            fetchProfile: { await spy.fetchProfile() },
            provisionNumber: { await spy.provisionNumber() },
            syncContacts: { await spy.syncContacts() },
            commit: { output in spy.commit(output: output) }
        )

        XCTAssertEqual(result, .superseded)
        XCTAssertEqual(spy.events, [.patch, .fetchProfileInitial])
        XCTAssertNil(spy.committedOutput)
    }

    func testIdentitySwitchDuringProvision_supersedesAndStopsLaterEffects() async {
        let spy = ProvisioningSequenceSpy(initialAuth: validAuth, initialAppleUserId: appleUserId)
        spy.onProvision = {
            spy.currentAuth = self.otherAuth
            return [
                "status": "ok",
                "phone_number": "+5511987654321"
            ]
        }

        let result = await ProvisioningOperation.run(
            capturedAuth: validAuth,
            isCurrent: { spy.isCurrent(capturedAuth: self.validAuth, capturedAppleUserId: self.appleUserId) },
            fetchProfile: { await spy.fetchProfile() },
            provisionNumber: { await spy.provisionNumber() },
            syncContacts: { await spy.syncContacts() },
            commit: { output in spy.commit(output: output) }
        )

        XCTAssertEqual(result, .superseded)
        XCTAssertEqual(spy.events, [.fetchProfileInitial, .provision])
        XCTAssertNil(spy.committedOutput)
    }

    func testIdentitySwitchDuringContactSync_supersedesAndStopsLaterEffects() async {
        let spy = ProvisioningSequenceSpy(initialAuth: validAuth, initialAppleUserId: appleUserId)
        spy.onSyncContacts = {
            spy.currentAuth = self.otherAuth
            return 25
        }

        let result = await ProvisioningOperation.run(
            capturedAuth: validAuth,
            isCurrent: { spy.isCurrent(capturedAuth: self.validAuth, capturedAppleUserId: self.appleUserId) },
            fetchProfile: { await spy.fetchProfile() },
            provisionNumber: { await spy.provisionNumber() },
            syncContacts: { await spy.syncContacts() },
            commit: { output in spy.commit(output: output) }
        )

        XCTAssertEqual(result, .superseded)
        XCTAssertEqual(spy.events, [.fetchProfileInitial, .provision, .syncContacts])
        XCTAssertNil(spy.committedOutput)
    }

    func testIdentitySwitchDuringUUIDFetch_supersedesAndStopsCommit() async {
        let spy = ProvisioningSequenceSpy(initialAuth: validAuth, initialAppleUserId: appleUserId)
        spy.onFetchProfileUUID = {
            spy.currentAuth = self.otherAuth
            return ["subscription_uuid": "sub-uuid-1"]
        }

        let result = await ProvisioningOperation.run(
            capturedAuth: validAuth,
            isCurrent: { spy.isCurrent(capturedAuth: self.validAuth, capturedAppleUserId: self.appleUserId) },
            fetchProfile: { await spy.fetchProfile() },
            provisionNumber: { await spy.provisionNumber() },
            syncContacts: { await spy.syncContacts() },
            commit: { output in spy.commit(output: output) }
        )

        XCTAssertEqual(result, .superseded)
        XCTAssertEqual(spy.events, [.fetchProfileInitial, .provision, .syncContacts, .fetchProfileUUID])
        XCTAssertNil(spy.committedOutput)
    }

    // MARK: - Happy Paths

    func testHappyExistingUnassignedContinuation_commitsWithBRBinding() async {
        let spy = ProvisioningSequenceSpy(initialAuth: validAuth, initialAppleUserId: appleUserId)
        spy.onFetchProfileInitial = {
            ["owner_phone": "+5511999998888"]
        }
        spy.onProvision = {
            [
                "status": "ok",
                "phone_number": "+5511987654321",
                "country_code": "BR",
                "service_binding": [
                    "country_code": "BR",
                    "provider": "twilio",
                    "number_type": "mobile",
                    "capabilities": ["voice": true, "sms": true]
                ]
            ]
        }
        spy.onSyncContacts = { 42 }
        spy.onFetchProfileUUID = {
            ["subscription_uuid": "uuid-br-sub-123"]
        }

        let result = await ProvisioningOperation.run(
            capturedAuth: validAuth,
            isCurrent: { spy.isCurrent(capturedAuth: self.validAuth, capturedAppleUserId: self.appleUserId) },
            patchProfile: { try await spy.patchProfile() },
            fetchProfile: { await spy.fetchProfile() },
            provisionNumber: { await spy.provisionNumber() },
            syncContacts: { await spy.syncContacts() },
            commit: { output in spy.commit(output: output) }
        )

        XCTAssertEqual(result, .completed)
        XCTAssertEqual(spy.events, [.patch, .fetchProfileInitial, .provision, .syncContacts, .fetchProfileUUID, .commit])
        XCTAssertEqual(spy.committedOutput?.number, "+5511987654321")
        XCTAssertEqual(spy.committedOutput?.country, "BR")
        XCTAssertEqual(spy.committedOutput?.binding?.countryCode, "BR")
        XCTAssertEqual(spy.committedOutput?.binding?.provider, "twilio")
        XCTAssertEqual(spy.committedOutput?.binding?.numberType, "mobile")
        XCTAssertEqual(spy.committedOutput?.binding?.isVoiceCapable, true)
        XCTAssertEqual(spy.committedOutput?.binding?.isSMSCapable, true)
        XCTAssertEqual(spy.committedOutput?.subscriptionUUID, "uuid-br-sub-123")
        XCTAssertEqual(spy.committedOutput?.syncedContacts, 42)
    }

    func testHappyNewAccountContinuation_commitsWithoutPatch() async {
        let spy = ProvisioningSequenceSpy(initialAuth: validAuth, initialAppleUserId: appleUserId)
        spy.onFetchProfileInitial = { [:] }
        spy.onProvision = {
            [
                "status": "ok",
                "phone_number": "+5511987654321",
                "country_code": "BR",
                "service_binding": [
                    "country_code": "BR",
                    "provider": "twilio"
                ]
            ]
        }
        spy.onFetchProfileUUID = {
            ["subscription_uuid": "sub-uuid-new-456"]
        }

        let result = await ProvisioningOperation.run(
            capturedAuth: validAuth,
            isCurrent: { spy.isCurrent(capturedAuth: self.validAuth, capturedAppleUserId: self.appleUserId) },
            patchProfile: nil,
            fetchProfile: { await spy.fetchProfile() },
            provisionNumber: { await spy.provisionNumber() },
            syncContacts: nil,
            commit: { output in spy.commit(output: output) }
        )

        XCTAssertEqual(result, .completed)
        XCTAssertEqual(spy.events, [.fetchProfileInitial, .provision, .fetchProfileUUID, .commit])
        XCTAssertEqual(spy.committedOutput?.number, "+5511987654321")
        XCTAssertEqual(spy.committedOutput?.country, "BR")
        XCTAssertEqual(spy.committedOutput?.binding?.countryCode, "BR")
        XCTAssertEqual(spy.committedOutput?.binding?.provider, "twilio")
        XCTAssertEqual(spy.committedOutput?.subscriptionUUID, "sub-uuid-new-456")
        XCTAssertNil(spy.committedOutput?.syncedContacts)
    }

    func testAssignedProfileReusesNumber_withoutProvisioning() async {
        let spy = ProvisioningSequenceSpy(initialAuth: validAuth, initialAppleUserId: appleUserId)
        spy.onFetchProfileInitial = {
            [
                "twilio_number": "+14155551234",
                "country_code": "US",
                "service_binding": [
                    "country_code": "US",
                    "provider": "twilio"
                ]
            ]
        }

        let result = await ProvisioningOperation.run(
            capturedAuth: validAuth,
            isCurrent: { spy.isCurrent(capturedAuth: self.validAuth, capturedAppleUserId: self.appleUserId) },
            fetchProfile: { await spy.fetchProfile() },
            provisionNumber: { await spy.provisionNumber() },
            syncContacts: { await spy.syncContacts() },
            commit: { output in spy.commit(output: output) }
        )

        XCTAssertEqual(result, .completed)
        XCTAssertEqual(spy.events, [.fetchProfileInitial, .syncContacts, .fetchProfileUUID, .commit])
        XCTAssertEqual(spy.committedOutput?.number, "+14155551234")
        XCTAssertEqual(spy.committedOutput?.country, "US")
        XCTAssertEqual(spy.committedOutput?.binding?.countryCode, "US")
    }

    func testCachedNumberReusedWhenProfileUnassigned() async {
        let spy = ProvisioningSequenceSpy(initialAuth: validAuth, initialAppleUserId: appleUserId)
        spy.onFetchProfileInitial = {
            ["country_code": "US"]
        }

        let cachedBinding = ServiceBinding(countryCode: "US", provider: "twilio")
        let result = await ProvisioningOperation.run(
            capturedAuth: validAuth,
            isCurrent: { spy.isCurrent(capturedAuth: self.validAuth, capturedAppleUserId: self.appleUserId) },
            cachedNumber: "+16505559876",
            cachedCountry: "US",
            cachedBinding: cachedBinding,
            fetchProfile: { await spy.fetchProfile() },
            provisionNumber: { await spy.provisionNumber() },
            syncContacts: { await spy.syncContacts() },
            commit: { output in spy.commit(output: output) }
        )

        XCTAssertEqual(result, .completed)
        XCTAssertEqual(spy.events, [.fetchProfileInitial, .syncContacts, .fetchProfileUUID, .commit])
        XCTAssertEqual(spy.committedOutput?.number, "+16505559876")
        XCTAssertEqual(spy.committedOutput?.country, "US")
        XCTAssertEqual(spy.committedOutput?.binding, cachedBinding)
    }

    // MARK: - Failures

    func testFailedProfileFetchWithoutCachedNumber_neverProvisions() async {
        let spy = ProvisioningSequenceSpy(initialAuth: validAuth, initialAppleUserId: appleUserId)
        spy.onFetchProfileInitial = { nil }

        let result = await ProvisioningOperation.run(
            capturedAuth: validAuth,
            isCurrent: { spy.isCurrent(capturedAuth: self.validAuth, capturedAppleUserId: self.appleUserId) },
            cachedNumber: "",
            fetchProfile: { await spy.fetchProfile() },
            provisionNumber: { await spy.provisionNumber() },
            commit: { output in spy.commit(output: output) }
        )

        XCTAssertEqual(result, .failed(message: "Could not reconnect your account. Please try again."))
        XCTAssertEqual(spy.events, [.fetchProfileInitial])
        XCTAssertNil(spy.committedOutput)
    }

    func testFailedProvision_noContactsPushOrCommit() async {
        let spy = ProvisioningSequenceSpy(initialAuth: validAuth, initialAppleUserId: appleUserId)
        spy.onFetchProfileInitial = { [:] }
        spy.onProvision = {
            ["status": "error", "error": "Insufficient inventory"]
        }

        let result = await ProvisioningOperation.run(
            capturedAuth: validAuth,
            isCurrent: { spy.isCurrent(capturedAuth: self.validAuth, capturedAppleUserId: self.appleUserId) },
            fetchProfile: { await spy.fetchProfile() },
            provisionNumber: { await spy.provisionNumber() },
            syncContacts: { await spy.syncContacts() },
            commit: { output in spy.commit(output: output) }
        )

        XCTAssertEqual(result, .failed(message: "Insufficient inventory"))
        XCTAssertEqual(spy.events, [.fetchProfileInitial, .provision])
        XCTAssertNil(spy.committedOutput)
    }

    func testFailedPatch_returnsErrorWithoutProfileFetchOrProvision() async {
        let spy = ProvisioningSequenceSpy(initialAuth: validAuth, initialAppleUserId: appleUserId)
        spy.onPatch = { false }

        let result = await ProvisioningOperation.run(
            capturedAuth: validAuth,
            isCurrent: { spy.isCurrent(capturedAuth: self.validAuth, capturedAppleUserId: self.appleUserId) },
            patchProfile: { try await spy.patchProfile() },
            patchFailureMessage: "Failed to update profile. Please try again.",
            fetchProfile: { await spy.fetchProfile() },
            provisionNumber: { await spy.provisionNumber() },
            commit: { output in spy.commit(output: output) }
        )

        XCTAssertEqual(result, .failed(message: "Failed to update profile. Please try again."))
        XCTAssertEqual(spy.events, [.patch])
        XCTAssertNil(spy.committedOutput)
    }

    func testFailedUUIDFetch_leavesUUIDNilAndCompletes() async {
        let spy = ProvisioningSequenceSpy(initialAuth: validAuth, initialAppleUserId: appleUserId)
        spy.onFetchProfileInitial = { [:] }
        spy.onFetchProfileUUID = { nil }

        let result = await ProvisioningOperation.run(
            capturedAuth: validAuth,
            isCurrent: { spy.isCurrent(capturedAuth: self.validAuth, capturedAppleUserId: self.appleUserId) },
            fetchProfile: { await spy.fetchProfile() },
            provisionNumber: { await spy.provisionNumber() },
            commit: { output in spy.commit(output: output) }
        )

        XCTAssertEqual(result, .completed)
        XCTAssertNil(spy.committedOutput?.subscriptionUUID)
    }

    // MARK: - APIClient Bearer Token Preservation

    func testAPIClientProvisionNumber_usesExplicitCapturedBearerToken() async throws {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [MockURLProtocol.self]
        let session = URLSession(configuration: config)
        let client = APIClient(session: session)

        var capturedAuthHeader: String? = nil
        MockURLProtocol.requestHandler = { request in
            capturedAuthHeader = request.value(forHTTPHeaderField: "Authorization")
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: nil
            )!
            let data = try JSONSerialization.data(withJSONObject: [
                "status": "ok",
                "phone_number": "+14155559999"
            ])
            return (response, data)
        }

        let explicitToken = "explicit-bearer-token-12345"
        let result = await client.provisionNumber(contractorId: "test-cid", bearerToken: explicitToken)

        XCTAssertEqual(capturedAuthHeader, "Bearer \(explicitToken)")
        XCTAssertEqual(result?["status"] as? String, "ok")
        XCTAssertEqual(result?["phone_number"] as? String, "+14155559999")

        MockURLProtocol.requestHandler = nil
    }

    // MARK: - Cross-Account Profile Rejection & OnboardingBusinessDraftCoordinator Tests

    func testProvisioningOperation_ignoresInitialAndFinalProfilesFromDifferentContractorId() async {
        let auth = CallAuthContext(contractorId: "owner-A", bearerToken: "token-A", generation: 1)
        let cachedBinding = ServiceBindingParser.parseBinding(["country_code": "US", "provider": "twilio"])

        // 1. Cached number present: wrong-account initial & final profiles must not override cached fields or inject UUID
        var committedCached: ProvisioningOperation.Output?
        let resultWithCache = await ProvisioningOperation.run(
            capturedAuth: auth,
            isCurrent: { true },
            cachedNumber: "+14155550101",
            cachedCountry: "US",
            cachedBinding: cachedBinding,
            fetchProfile: {
                [
                    "contractor_id": "owner-B",
                    "twilio_number": "+16045559999",
                    "country_code": "CA",
                    "service_binding": ["country_code": "CA", "provider": "twilio"],
                    "subscription_uuid": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                ]
            },
            provisionNumber: {
                XCTFail("provisionNumber should not be called when cachedNumber is used")
                return nil
            },
            commit: { committedCached = $0 }
        )
        XCTAssertEqual(resultWithCache, .completed)
        XCTAssertEqual(committedCached?.number, "+14155550101")
        XCTAssertEqual(committedCached?.country, "US")
        XCTAssertEqual(committedCached?.binding?.countryCode, "US")
        XCTAssertNil(committedCached?.subscriptionUUID)

        // 2. Empty cachedNumber + wrong-account initial profile fails closed without provisioning or adopting wrong number
        var provisionCalled = false
        let failedWrongInitial = await ProvisioningOperation.run(
            capturedAuth: auth,
            isCurrent: { true },
            fetchProfile: {
                [
                    "contractor_id": "owner-B",
                    "twilio_number": "+16045559999",
                ]
            },
            provisionNumber: {
                provisionCalled = true
                return nil
            },
            commit: { _ in XCTFail("Must not commit wrong-account initial profile") }
        )
        if case .failed = failedWrongInitial {
            // Expected fail-closed behavior
        } else {
            XCTFail("Expected failure when initial profile has mismatched contractor_id, got \(failedWrongInitial)")
        }
        XCTAssertFalse(provisionCalled)

        // 3. Matching initial profile (no number yet) + wrong-account final profile: provisions cleanly without adopting wrong subscription_uuid
        var fetchStep = 0
        var committedProvisioned: ProvisioningOperation.Output?
        let resultWrongFinal = await ProvisioningOperation.run(
            capturedAuth: auth,
            isCurrent: { true },
            fetchProfile: {
                fetchStep += 1
                if fetchStep == 1 {
                    return ["contractor_id": "owner-A", "twilio_number": ""]
                }
                return [
                    "contractor_id": "owner-B",
                    "subscription_uuid": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                ]
            },
            provisionNumber: {
                [
                    "status": "ok",
                    "phone_number": "+14155550199",
                    "country_code": "US",
                    "service_binding": ["country_code": "US", "provider": "twilio"],
                ]
            },
            commit: { committedProvisioned = $0 }
        )
        XCTAssertEqual(resultWrongFinal, .completed)
        XCTAssertEqual(committedProvisioned?.number, "+14155550199")
        XCTAssertEqual(committedProvisioned?.country, "US")
        XCTAssertEqual(committedProvisioned?.binding?.countryCode, "US")
        XCTAssertNil(committedProvisioned?.subscriptionUUID)
    }

    func testOnboardingBusinessDraftCoordinator_existingContinuationRestoresMergesPatchesAndHydratesMetadata() async throws {
        let store = RecoveryTestKeychain()
        let state = AppState(inMemory: true, secureStore: store.client)
        state.isOnboarded = false
        state.contractorId = ""
        state.appleUserId = "apple-1"
        state.appleIdentityToken = "apple-jwt-1"

        let draftUUID = "550e8400-e29b-41d4-a716-4466554400aa"
        let continuation = SignupContinuation(contractorId: "draft-owner", appleUserId: "apple-1")

        let coordinator = AccountRestoreCoordinator(
            state: state,
            lookupHandler: { appleUser, identityToken in
                XCTAssertEqual(appleUser, "apple-1")
                XCTAssertEqual(identityToken, "apple-jwt-1")
                return .found(contractorId: "draft-owner", apiToken: "verified-bearer-token")
            },
            profileFetcher: { id, token in
                XCTAssertEqual(id, "draft-owner")
                XCTAssertEqual(token, "verified-bearer-token")
                return [
                    "contractor_id": "draft-owner",
                    "active": true,
                    "subscription_status": "trial",
                    "subscription_tier": "none",
                    "twilio_number": "",
                    "knowledge": "Existing Hours Note",
                ]
            },
            notificationReconciler: {},
            contactSyncHandler: { _ in }
        )

        var patchedContractorId: String?
        var patchedBearerToken: String?
        var patchedBody: [String: Any]?
        var fetchCalls = 0

        let outcome = await OnboardingBusinessDraftCoordinator.prepare(
            appState: state,
            ownerName: "Alice Smith",
            businessName: "Smith Plumbing",
            serviceType: "plumbing",
            servicesPayload: [["name": "Drain Cleaning", "min_price": 150]],
            knowledgeToSave: "## Services & Pricing\n- Drain Cleaning: $150+",
            createContractor: {
                .success([
                    "status": "ok",
                    "contractor_id": "draft-owner",
                    "api_token": "untrusted-bootstrap-token",
                    "existing": true,
                ])
            },
            restoreBootstrap: { response in
                let rev = coordinator.beginAttempt()!
                return await coordinator.restoreBootstrapResponse(
                    response,
                    appleUserId: state.appleUserId,
                    appleIdentityToken: state.appleIdentityToken,
                    signupContinuation: continuation,
                    attemptRevision: rev
                )
            },
            fetchProfile: { cid, token in
                fetchCalls += 1
                XCTAssertEqual(cid, "draft-owner")
                XCTAssertEqual(token, "verified-bearer-token")
                if fetchCalls == 1 {
                    return [
                        "contractor_id": "draft-owner",
                        "knowledge": "Existing Hours Note",
                    ]
                }
                return [
                    "contractor_id": "draft-owner",
                    "subscription_uuid": draftUUID,
                    "service_binding": ["country_code": "CA", "provider": "twilio"],
                ]
            },
            patchContractor: { cid, body, token in
                patchedContractorId = cid
                patchedBody = body
                patchedBearerToken = token
                return true
            }
        )

        XCTAssertEqual(outcome, .readyForNextStep)
        XCTAssertEqual(patchedContractorId, "draft-owner")
        XCTAssertEqual(patchedBearerToken, "verified-bearer-token")
        XCTAssertEqual(patchedBody?["owner_name"] as? String, "Alice Smith")
        XCTAssertEqual(patchedBody?["business_name"] as? String, "Smith Plumbing")
        XCTAssertEqual(patchedBody?["service_type"] as? String, "plumbing")
        let mergedKnowledge = try XCTUnwrap(patchedBody?["knowledge"] as? String)
        XCTAssertTrue(mergedKnowledge.contains("Existing Hours Note"))
        XCTAssertTrue(mergedKnowledge.contains("Drain Cleaning"))
        XCTAssertEqual(state.userName, "Alice Smith")
        XCTAssertEqual(state.businessName, "Smith Plumbing")
        XCTAssertEqual(state.serviceType, "plumbing")
        XCTAssertEqual(state.subscriptionUUID, draftUUID)
        XCTAssertEqual(state.serviceBinding?.countryCode, "CA")
    }

    func testOnboardingBusinessDraftCoordinator_returningOnboardedAccountStopsWithoutPatchingOrOverwritingFields() async {
        let store = RecoveryTestKeychain()
        let state = AppState(inMemory: true, secureStore: store.client)
        state.isOnboarded = false
        state.contractorId = ""
        state.appleUserId = "apple-returning"
        state.appleIdentityToken = "apple-jwt-returning"

        let coordinator = AccountRestoreCoordinator(
            state: state,
            lookupHandler: { _, _ in
                .found(contractorId: "returning-owner", apiToken: "returning-bearer")
            },
            profileFetcher: { _, _ in
                [
                    "contractor_id": "returning-owner",
                    "active": true,
                    "subscription_status": "active",
                    "subscription_tier": "business",
                    "twilio_number": "+14155550188",
                    "owner_name": "Restored Owner",
                    "business_name": "Restored Electric",
                    "service_type": "electrical",
                ]
            },
            notificationReconciler: {},
            contactSyncHandler: { _ in }
        )

        var patchCalled = false
        var fetchCalled = false

        let outcome = await OnboardingBusinessDraftCoordinator.prepare(
            appState: state,
            ownerName: "Typed Form Name",
            businessName: "Typed Form Biz",
            serviceType: "plumbing",
            knowledgeToSave: "## New Draft Knowledge",
            createContractor: {
                .success([
                    "status": "ok",
                    "contractor_id": "returning-owner",
                    "api_token": "bootstrap-hint",
                    "existing": true,
                ])
            },
            restoreBootstrap: { response in
                let rev = coordinator.beginAttempt()!
                return await coordinator.restoreBootstrapResponse(
                    response,
                    appleUserId: state.appleUserId,
                    appleIdentityToken: state.appleIdentityToken,
                    attemptRevision: rev
                )
            },
            fetchProfile: { _, _ in
                fetchCalled = true
                return nil
            },
            patchContractor: { _, _, _ in
                patchCalled = true
                return true
            }
        )

        XCTAssertEqual(outcome, .restoredCompletedAccount)
        XCTAssertFalse(patchCalled)
        XCTAssertFalse(fetchCalled)
        XCTAssertTrue(state.isOnboarded)
        XCTAssertEqual(state.sessionState, .ready)
        XCTAssertEqual(state.userName, "Restored Owner")
        XCTAssertEqual(state.businessName, "Restored Electric")
        XCTAssertEqual(state.serviceType, "electrical")
    }

    func testOnboardingBusinessDraftCoordinator_missingApiTokenOrPendingRecoveryFailsClosedWithZeroWrites() async {
        // 1. Missing or empty api_token on new contractor creation
        for response in [
            ["status": "ok", "contractor_id": "new-c1"] as [String: Any],
            ["status": "ok", "contractor_id": "new-c1", "api_token": ""] as [String: Any],
        ] {
            let state = AppState(inMemory: true)
            state.appleUserId = "apple-new"
            var writtenTokens: [String] = []
            var patchCalled = false

            let outcome = await OnboardingBusinessDraftCoordinator.prepare(
                appState: state,
                ownerName: "New Owner",
                businessName: "New Roofing",
                serviceType: "roofing",
                createContractor: { .success(response) },
                writeBearerToken: { writtenTokens.append($0) },
                fetchProfile: { _, _ in nil },
                patchContractor: { _, _, _ in
                    patchCalled = true
                    return true
                }
            )

            guard case .failed = outcome else {
                return XCTFail("Expected failure for missing/empty api_token, got \(outcome)")
            }
            XCTAssertEqual(state.contractorId, "")
            XCTAssertEqual(state.currentAuthContext().bearerToken, "")
            XCTAssertTrue(writtenTokens.isEmpty)
            XCTAssertFalse(patchCalled)
        }

        // 2. Pending account recovery blocks brand-new contractor commit with zero writes
        let recoveryStore = RecoveryTestKeychain()
        let recoveryState = AppState(inMemory: true, secureStore: recoveryStore.client)
        recoveryState.beginAccountRecovery(
            checkpoint: AccountRecoveryCheckpoint(
                contractorId: "checkpoint-owner",
                appleUserId: "apple-checkpoint",
                allowsUnfinishedSetup: true
            )
        )
        var writtenRecoveryTokens: [String] = []

        let recoveryOutcome = await OnboardingBusinessDraftCoordinator.prepare(
            appState: recoveryState,
            ownerName: "New Owner",
            businessName: "New Roofing",
            serviceType: "roofing",
            createContractor: {
                .success([
                    "status": "ok",
                    "contractor_id": "brand-new-id",
                    "api_token": "brand-new-token",
                ])
            },
            writeBearerToken: { writtenRecoveryTokens.append($0) },
            fetchProfile: { _, _ in nil },
            patchContractor: { _, _, _ in
                XCTFail("patchContractor must not be called while pendingAccountRecovery != nil")
                return true
            }
        )

        guard case .failed = recoveryOutcome else {
            return XCTFail("Expected failure when pendingAccountRecovery != nil, got \(recoveryOutcome)")
        }
        XCTAssertEqual(recoveryState.contractorId, "")
        XCTAssertTrue(writtenRecoveryTokens.isEmpty)
    }

    func testOnboardingBusinessDraftCoordinator_midFlightAuthOrAppleSwitchAbortsWithoutMutatingReplacementAccount() async {
        let replacementBinding = ServiceBindingParser.parseBinding(["country_code": "US", "provider": "twilio"])

        // 1. Mid-flight auth switch during patchContractor
        do {
            var activeAuth = CallAuthContext(contractorId: "draft-A", bearerToken: "token-A", generation: 1)
            let state = AppState(authProvider: { activeAuth }, inMemory: true)
            state.contractorId = "draft-A"
            state.appleUserId = "apple-A"
            state.userName = "Original User"
            state.businessName = "Original Biz"
            state.subscriptionUUID = "11111111-1111-4111-8111-111111111111"
            state.serviceBinding = replacementBinding

            let outcome = await OnboardingBusinessDraftCoordinator.prepare(
                appState: state,
                ownerName: "Stale Draft Name",
                businessName: "Stale Draft Biz",
                serviceType: "hvac",
                fetchProfile: { _, _ in
                    XCTFail("Final fetchProfile must not execute after auth switch during patch")
                    return nil
                },
                patchContractor: { _, _, _ in
                    // Switch to replacement Account B mid-flight
                    state.contractorId = "account-B"
                    state.appleUserId = "apple-B"
                    state.userName = "Replacement Owner"
                    state.businessName = "Replacement Biz"
                    state.subscriptionUUID = "22222222-2222-4222-8222-222222222222"
                    activeAuth = CallAuthContext(contractorId: "account-B", bearerToken: "token-B", generation: 2)
                    state.serviceBinding = replacementBinding
                    return true
                }
            )

            XCTAssertEqual(outcome, .aborted)
            XCTAssertEqual(state.userName, "Replacement Owner")
            XCTAssertEqual(state.businessName, "Replacement Biz")
            XCTAssertEqual(state.subscriptionUUID, "22222222-2222-4222-8222-222222222222")
            XCTAssertEqual(state.serviceBinding, replacementBinding)
        }

        // 2. Mid-flight Apple-ID switch during final profile fetch
        do {
            let activeAuth = CallAuthContext(contractorId: "draft-A", bearerToken: "token-A", generation: 1)
            let state = AppState(authProvider: { activeAuth }, inMemory: true)
            state.contractorId = "draft-A"
            state.appleUserId = "apple-A"
            state.userName = "Replacement Owner"
            state.businessName = "Replacement Biz"
            state.subscriptionUUID = "22222222-2222-4222-8222-222222222222"
            state.serviceBinding = replacementBinding

            let outcome = await OnboardingBusinessDraftCoordinator.prepare(
                appState: state,
                ownerName: "Stale Draft Name",
                businessName: "Stale Draft Biz",
                serviceType: "hvac",
                fetchProfile: { _, _ in
                    // Apple ID changes while final profile fetch is suspended
                    state.appleUserId = "apple-B"
                    return [
                        "contractor_id": "draft-A",
                        "subscription_uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                        "service_binding": ["country_code": "CA", "provider": "twilio"],
                    ]
                },
                patchContractor: { _, _, _ in true }
            )

            XCTAssertEqual(outcome, .aborted)
            XCTAssertEqual(state.userName, "Replacement Owner")
            XCTAssertEqual(state.businessName, "Replacement Biz")
            XCTAssertEqual(state.subscriptionUUID, "22222222-2222-4222-8222-222222222222")
            XCTAssertEqual(state.serviceBinding, replacementBinding)
        }
    }
}
