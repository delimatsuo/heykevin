import Foundation
import Contacts
import CryptoKit

private func debugLog(_ message: String) {
    #if DEBUG
    print(message)
    #endif
}

enum SyncResult: Equatable {
    case success(synced: Int, removed: Int)
    case permissionDenied
    case rateLimited
    case error(String)

    /// Shown in red. Decided by the case, not the message text, which is localized.
    var isFailure: Bool {
        switch self {
        case .permissionDenied, .error: return true
        case .success, .rateLimited: return false
        }
    }
}

/// What iOS's Contacts permission allows right now.
enum ContactsAccess: Equatable {
    case readable
    case notAsked
    case denied
}

actor ContactSyncManager {
    static let shared = ContactSyncManager()

    private let store = CNContactStore()
    // v2: builds before this one cached the hash after uploading in 200-number
    // requests that the server mostly dropped, so that hash does not prove the
    // server has the full list. New keys make every device upload once more,
    // without waiting out the 24-hour window an earlier build started.
    static let lastSyncHashKey = "lastContactSyncHash.v2"
    static let lastSyncAttemptKey = "lastContactSyncAttemptAt.v2"
    /// The most numbers one upload carries; the server accepts no more per request.
    static let maxContacts = 5000
    private let minSyncInterval: TimeInterval = 300 // 5 minutes
    private let automaticSyncInterval: TimeInterval = 24 * 3600

    private var lastSyncTime: Date?
    private var isSyncing = false

    /// Request full contacts access and sync all contacts to backend.
    func syncContacts(contractorId: String, force: Bool = false) async -> SyncResult {
        guard !contractorId.isEmpty else {
            return .error("Missing contractor ID")
        }

        // Prevent concurrent syncs
        guard !isSyncing else {
            debugLog("Contact sync: already in progress")
            return .error("Sync already in progress")
        }

        if !force {
            let now = Date()
            // Rate limit client-side within this process.
            if let last = lastSyncTime, now.timeIntervalSince(last) < minSyncInterval {
                debugLog("Contact sync: rate limited")
                return .rateLimited
            }

            // Persist the automatic sync window so cold launches do not
            // repeatedly enumerate the entire address book.
            let persistedLast = UserDefaults.standard.double(forKey: Self.lastSyncAttemptKey)
            if persistedLast > 0,
               now.timeIntervalSince(Date(timeIntervalSince1970: persistedLast)) < automaticSyncInterval {
                debugLog("Contact sync: automatic sync recently completed")
                return .rateLimited
            }
        }

        // Check authorization
        let status = CNContactStore.authorizationStatus(for: .contacts)
        switch Self.access(for: status) {
        case .readable:
            break
        case .notAsked:
            do {
                guard try await store.requestAccess(for: .contacts) else {
                    return .permissionDenied
                }
            } catch {
                debugLog("Contact access request failed: \(error)")
                return .error("Contact access request failed: \(error.localizedDescription)")
            }
        case .denied:
            debugLog("Contact access not authorized: \(status.rawValue)")
            return .permissionDenied
        }

        isSyncing = true
        defer { isSyncing = false }

        // Fetch contacts with phone numbers — cap at 5000 to prevent memory issues
        let keys = [CNContactGivenNameKey, CNContactFamilyNameKey, CNContactPhoneNumbersKey] as [CNKeyDescriptor]
        var contacts: [(name: String, phone: String)] = []
        let maxContacts = Self.maxContacts

        do {
            let request = CNContactFetchRequest(keysToFetch: keys)
            request.sortOrder = .givenName
            try store.enumerateContacts(with: request) { contact, stop in
                let name = "\(contact.givenName) \(contact.familyName)".trimmingCharacters(in: .whitespaces)
                for phone in contact.phoneNumbers {
                    let number = phone.value.stringValue
                    if !number.isEmpty {
                        contacts.append((name: name, phone: number))
                    }
                }
                if contacts.count >= maxContacts {
                    stop.pointee = true
                }
            }
        } catch {
            debugLog("Contact fetch failed: \(error)")
            return .error("Contact fetch failed: \(error.localizedDescription)")
        }

        let (result, completed) = await Self.upload(contacts, force: force, defaults: .standard) { list, hash in
            await APIClient.shared.bulkSyncContacts(contractorId: contractorId, contacts: list, contactsHash: hash)
        }
        if completed {
            lastSyncTime = Date()
        }
        return result
    }

    typealias Uploader = (_ contacts: [(name: String, phone: String)], _ hash: String) async -> ContactSyncUploadResult

    /// Everything after reading the address book: cap the list, skip it if this
    /// exact list already reached the server, otherwise upload it, and remember
    /// it only when the server confirms it holds it. `completed` says whether
    /// the attempt was recorded, which starts the automatic-sync window.
    static func upload(
        _ contacts: [(name: String, phone: String)],
        force: Bool,
        defaults: UserDefaults,
        uploader: Uploader
    ) async -> (result: SyncResult, completed: Bool) {
        // A contact's numbers are added together, so the last one can overshoot.
        let contacts = Array(contacts.prefix(maxContacts))
        let recordAttempt = { defaults.set(Date().timeIntervalSince1970, forKey: lastSyncAttemptKey) }

        guard !contacts.isEmpty else {
            recordAttempt()
            return (.success(synced: 0, removed: 0), true)
        }

        // Skip the upload if this exact list already reached the server. A
        // manual sync always uploads; the server skips an unchanged list.
        let hash = listHash(contacts)
        if !force, hash == defaults.string(forKey: lastSyncHashKey) {
            debugLog("Contact sync: hash unchanged locally, skipping")
            recordAttempt()
            return (.success(synced: 0, removed: 0), true)
        }

        // Send the whole list in one request. The server un-whitelists contacts
        // missing from a complete list, so splitting it would drop contacts.
        let outcome = await uploader(contacts, hash)
        debugLog("Contact sync finished: \(outcome)")
        let (result, serverHasList) = Self.result(for: outcome, uploadedCount: contacts.count)
        if serverHasList {
            defaults.set(hash, forKey: lastSyncHashKey)
            recordAttempt()
        }
        return (result, serverHasList)
    }

    /// The list's contacts_hash, which the server recomputes to verify it holds
    /// the whole list: SHA-256 of the digits-only numbers, sorted and comma-joined.
    static func listHash(_ contacts: [(name: String, phone: String)]) -> String {
        let sortedPhones = contacts.map { $0.phone.filter { $0.isNumber } }.sorted()
        return SHA256.hash(data: Data(sortedPhones.joined(separator: ",").utf8))
            .map { String(format: "%02x", $0) }
            .joined()
    }

    /// Maps the server's reply to the caller's result, and says whether the
    /// server now holds this exact list, so its hash may be cached. A list the
    /// server did not take is uploaded again on the next sync.
    static func result(
        for outcome: ContactSyncUploadResult,
        uploadedCount: Int
    ) -> (result: SyncResult, serverHasList: Bool) {
        switch outcome {
        case .synced(let synced, let removed):
            return (.success(synced: synced, removed: removed), true)
        case .unchanged:
            return (.success(synced: uploadedCount, removed: 0), true)
        case .rateLimited:
            return (.rateLimited, false)
        case .failed:
            return (.error("Sync failed"), false)
        }
    }

    /// What a Contacts authorization status allows. Read from iOS on every
    /// sync and never remembered: builds that cached a denial kept refusing
    /// after the owner granted access in iOS Settings. Limited access (iOS 18)
    /// syncs the contacts the owner chose to share; since each upload is the
    /// complete list, contacts no longer shared stop ringing through.
    static func access(for status: CNAuthorizationStatus) -> ContactsAccess {
        switch status {
        case .authorized, .limited:
            return .readable
        case .notDetermined:
            return .notAsked
        case .denied, .restricted:
            return .denied
        @unknown default:
            return .denied
        }
    }

    /// Request contacts permission (used during onboarding).
    func requestAccess() async -> Bool {
        switch Self.access(for: CNContactStore.authorizationStatus(for: .contacts)) {
        case .readable:
            return true
        case .notAsked:
            return (try? await store.requestAccess(for: .contacts)) ?? false
        case .denied:
            return false
        }
    }
}
