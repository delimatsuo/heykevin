import XCTest
import Security
@testable import Kevin

final class RecoveryTestKeychain {
    var values: [String: String] = [:]
    var readError: OSStatus?
    var failingKey: String?
    var writes: [String] = []
    var deletes: [String] = []
    lazy var client = KeychainManager(reader: { [unowned self] key in
        if let error = readError { return .error(error) }
        return values[key].map(KeychainReadResult.value) ?? .missing
    }, writer: { [unowned self] key, value in
        writes.append(key)
        guard failingKey != key else { return false }
        values[key] = value
        return true
    }, remover: { [unowned self] key in deletes.append(key); values.removeValue(forKey: key) })
}

@MainActor
final class AccountSessionRecoveryTests: XCTestCase {
    func testMissingIdTokenBothAndEmptyValuesRequireRecoveryWithoutClearingDefaults() {
        for credentials in [[String: String](), ["contractorId": "owner"], ["contractorApiToken": "token"],
                            ["contractorId": "owner", "contractorApiToken": ""]] {
            let store = RecoveryTestKeychain(); store.values = credentials
            let state = AppState(inMemory: true, secureStore: store.client)
            state.isOnboarded = true; state.kevinNumber = "+15005550006"
            state.refreshSecureStorageForActiveUse()
            XCTAssertEqual(state.sessionState, .needsRecovery)
            XCTAssertTrue(state.isOnboarded)
            XCTAssertEqual(state.kevinNumber, "+15005550006")
            XCTAssertTrue(store.deletes.isEmpty)
        }
    }

    func testUnavailableStorageDoesNotWriteThenUnlockMigratesReadableCredentials() {
        let store = RecoveryTestKeychain()
        store.values = ["contractorId": "owner", "contractorApiToken": "token", "subscriptionStatus": "active"]
        store.readError = errSecInteractionNotAllowed
        let state = AppState(inMemory: true, secureStore: store.client)
        state.isOnboarded = true; state.kevinNumber = "+15005550006"
        state.refreshSecureStorageForActiveUse()
        XCTAssertTrue(state.sessionState.isUnavailable)
        XCTAssertTrue(store.writes.isEmpty)
        store.readError = nil
        state.refreshSecureStorageForActiveUse()
        XCTAssertEqual(state.sessionState, .ready)
        XCTAssertTrue(state.currentAuthContext().isValid)
        XCTAssertEqual(state.contractorId, "owner")
        XCTAssertEqual(state.subscriptionStatus, "active")
        XCTAssertEqual(state.kevinNumber, "+15005550006")
        XCTAssertTrue(store.writes.contains("contractorApiToken"))
        XCTAssertTrue(store.writes.contains("contractorId"))
        XCTAssertEqual(store.values["contractorApiToken"], "token")
        XCTAssertTrue(store.deletes.isEmpty)
    }

    func testFailedAccessibilityMigrationKeepsSessionUnavailable() {
        let store = RecoveryTestKeychain()
        store.values = ["contractorId": "owner", "contractorApiToken": "token"]
        store.failingKey = "contractorApiToken"
        let state = AppState(inMemory: true, secureStore: store.client); state.isOnboarded = true
        state.refreshSecureStorageForActiveUse()
        XCTAssertTrue(state.sessionState.isUnavailable)
        XCTAssertEqual(store.values["contractorApiToken"], "token")
        XCTAssertTrue(store.deletes.isEmpty)
    }

    func testUnlockWithMissingTokenHydratesIdentityAndRejectsDifferentRecoveryAccount() async {
        let store = RecoveryTestKeychain()
        store.values = ["contractorId": "original", "appleUserId": "original-apple"]
        store.readError = errSecInteractionNotAllowed
        let state = AppState(inMemory: true, secureStore: store.client); state.isOnboarded = true
        state.refreshSecureStorageForActiveUse()
        XCTAssertTrue(state.sessionState.isUnavailable); XCTAssertEqual(state.contractorId, "")
        store.readError = nil
        state.refreshSecureStorageForActiveUse()
        XCTAssertEqual(state.sessionState, .needsRecovery)
        XCTAssertEqual(state.contractorId, "original"); XCTAssertEqual(state.appleUserId, "original-apple")
        var lookups = 0
        let coordinator = AccountRestoreCoordinator(state: state,
            lookupHandler: { _, _ in lookups += 1; return .found(contractorId: "different", apiToken: "token") },
            profileFetcher: { _, _ in XCTFail("Mismatched account must not load"); return nil },
            notificationReconciler: { XCTFail("Mismatched account must not register") }, contactSyncHandler: { _ in })
        for identity in ["different-apple", "original-apple"] {
            let outcome = await coordinator.restoreAccount(appleUserId: identity, appleIdentityToken: "fresh",
                isRecoveryMode: true, retainedContractorId: state.contractorId,
                retainedAppleUserId: state.appleUserId, attemptRevision: coordinator.beginAttempt()!)
            guard case .failed = outcome else { return XCTFail("Mismatched identity accepted") }
        }
        XCTAssertEqual(lookups, 1)
        XCTAssertEqual(state.contractorId, "original")
        XCTAssertTrue(store.writes.isEmpty)
    }

    func testLegacyMigrationKeepsValueOnReadOrWriteFailureAndRemovesOnlyAfterReadback() {
        let name = "Kevin.RecoveryTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: name)!
        defer { defaults.removePersistentDomain(forName: name) }
        let store = RecoveryTestKeychain()
        defaults.set("legacy", forKey: "contractorApiToken")
        store.readError = errSecInteractionNotAllowed
        XCTAssertEqual(store.client.readMigratingLegacy("contractorApiToken", defaults: defaults), .error(errSecInteractionNotAllowed))
        XCTAssertTrue(store.writes.isEmpty)
        store.readError = nil; store.failingKey = "contractorApiToken"
        XCTAssertEqual(store.client.readMigratingLegacy("contractorApiToken", defaults: defaults), .error(errSecNotAvailable))
        XCTAssertEqual(defaults.string(forKey: "contractorApiToken"), "legacy")
        store.failingKey = nil
        XCTAssertEqual(store.client.readMigratingLegacy("contractorApiToken", defaults: defaults), .value("legacy"))
        XCTAssertNil(defaults.string(forKey: "contractorApiToken"))
    }

    func testPartialCredentialCommitAndFailedCompletionStayInRecoveryAfterRestart() {
        for failingKey in ["appleUserId", "contractorId", "contractorApiToken", "subscriptionStatus"] {
            let store = RecoveryTestKeychain()
            store.values = ["contractorId": "owner", "contractorApiToken": "old"]
            store.failingKey = failingKey
            let state = AppState(inMemory: true, secureStore: store.client); state.isOnboarded = true
            XCTAssertFalse(state.persistRecoveredCredentials(["appleUserId": "apple", "contractorId": "owner",
                "contractorApiToken": "new", "subscriptionStatus": "active"]))
            let restarted = AppState(inMemory: true, secureStore: store.client); restarted.isOnboarded = true
            restarted.refreshSecureStorageForActiveUse()
            XCTAssertEqual(restarted.sessionState, .needsRecovery, failingKey)
        }
        let store = RecoveryTestKeychain()
        let state = AppState(inMemory: true, secureStore: store.client); state.isOnboarded = true
        XCTAssertTrue(state.persistRecoveredCredentials(["contractorId": "owner", "contractorApiToken": "token"]))
        store.failingKey = "accountRecoveryCommit"
        XCTAssertFalse(state.finishAccountRecovery(bearerToken: "token"))
        let restarted = AppState(inMemory: true, secureStore: store.client); restarted.isOnboarded = true
        restarted.refreshSecureStorageForActiveUse()
        XCTAssertEqual(restarted.sessionState, .needsRecovery)
    }

    func testExplicitReauthenticationSurvivesActivationAndSuccessfulCommitClearsIt() {
        let store = RecoveryTestKeychain(); store.values = ["contractorId": "owner", "contractorApiToken": "old"]
        let state = AppState(inMemory: true, secureStore: store.client); state.isOnboarded = true
        state.refreshSecureStorageForActiveUse(); state.beginAccountRecovery()
        state.refreshSecureStorageForActiveUse()
        XCTAssertEqual(state.sessionState, .needsRecovery)
        XCTAssertTrue(state.persistRecoveredCredentials(["contractorId": "owner", "contractorApiToken": "new"]))
        XCTAssertTrue(state.finishAccountRecovery(bearerToken: "new"))
        state.refreshSecureStorageForActiveUse()
        XCTAssertEqual(state.sessionState, .ready)
        XCTAssertEqual(state.currentAuthContext().bearerToken, "new")
    }

    func testFreshInstallRemainsOnboarding() {
        let store = RecoveryTestKeychain(); let state = AppState(inMemory: true, secureStore: store.client)
        state.refreshSecureStorageForActiveUse()
        XCTAssertEqual(state.sessionState, .notOnboarded)
    }
}
