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
}
