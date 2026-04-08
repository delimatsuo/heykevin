import Foundation
import Contacts
import CryptoKit

private func debugLog(_ message: String) {
    #if DEBUG
    print(message)
    #endif
}

enum SyncResult {
    case success(synced: Int, removed: Int)
    case permissionDenied
    case rateLimited
    case error(String)
}

actor ContactSyncManager {
    static let shared = ContactSyncManager()

    private let store = CNContactStore()
    private let lastSyncHashKey = "lastContactSyncHash"
    private let permissionDeniedKey = "contactsPermissionDenied"
    private let minSyncInterval: TimeInterval = 300 // 5 minutes

    private var lastSyncTime: Date?
    private var isSyncing = false

    /// Request full contacts access and sync all contacts to backend.
    func syncContacts(contractorId: String) async -> SyncResult {
        // Prevent concurrent syncs
        guard !isSyncing else {
            debugLog("Contact sync: already in progress")
            return .error("Sync already in progress")
        }

        // Skip if permission was previously denied
        if UserDefaults.standard.bool(forKey: permissionDeniedKey) {
            debugLog("Contact sync: permission previously denied")
            return .permissionDenied
        }

        // Rate limit client-side
        if let last = lastSyncTime, Date().timeIntervalSince(last) < minSyncInterval {
            debugLog("Contact sync: rate limited")
            return .rateLimited
        }

        // Check authorization
        let status = CNContactStore.authorizationStatus(for: .contacts)
        if status == .notDetermined {
            do {
                let granted = try await store.requestAccess(for: .contacts)
                if !granted {
                    UserDefaults.standard.set(true, forKey: permissionDeniedKey)
                    return .permissionDenied
                }
            } catch {
                debugLog("Contact access request failed: \(error)")
                return .error("Contact access request failed: \(error.localizedDescription)")
            }
        } else if status != .authorized {
            UserDefaults.standard.set(true, forKey: permissionDeniedKey)
            debugLog("Contact access not authorized: \(status.rawValue)")
            return .permissionDenied
        }

        isSyncing = true
        defer { isSyncing = false }

        // Fetch contacts with phone numbers — cap at 5000 to prevent memory issues
        let keys = [CNContactGivenNameKey, CNContactFamilyNameKey, CNContactPhoneNumbersKey] as [CNKeyDescriptor]
        var contacts: [(name: String, phone: String)] = []
        let maxContacts = 5000

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

        guard !contacts.isEmpty else { return .success(synced: 0, removed: 0) }

        // Compute hash for server-side comparison (normalize to digits-only)
        let sortedPhones = contacts.map { $0.phone.filter { $0.isNumber } }.sorted()
        let phoneString = sortedPhones.joined(separator: ",")
        let hash = SHA256.hash(data: Data(phoneString.utf8))
            .map { String(format: "%02x", $0) }
            .joined()

        // Check if hash matches server first (avoids sending full list)
        let lastHash = UserDefaults.standard.string(forKey: lastSyncHashKey) ?? ""
        if hash == lastHash {
            debugLog("Contact sync: hash unchanged locally, skipping")
            lastSyncTime = Date()
            return .success(synced: 0, removed: 0)
        }

        // Hash changed — send full list to backend in batches to reduce memory
        let batchSize = 200
        var totalSynced = 0
        var totalRemoved = 0

        for batchStart in stride(from: 0, to: contacts.count, by: batchSize) {
            let batchEnd = min(batchStart + batchSize, contacts.count)
            let batch = Array(contacts[batchStart..<batchEnd])
            let isLastBatch = batchEnd >= contacts.count

            let result = await APIClient.shared.bulkSyncContacts(
                contractorId: contractorId,
                contacts: batch,
                contactsHash: isLastBatch ? hash : ""
            )
            totalSynced += result.synced
            totalRemoved += result.removed
        }

        // Cache the hash locally
        UserDefaults.standard.set(hash, forKey: lastSyncHashKey)

        lastSyncTime = Date()
        debugLog("Contact sync complete: synced=\(totalSynced), removed=\(totalRemoved)")
        return .success(synced: totalSynced, removed: totalRemoved)
    }

    /// Request contacts permission (used during onboarding).
    func requestAccess() async -> Bool {
        let status = CNContactStore.authorizationStatus(for: .contacts)
        if status == .authorized { return true }
        if status == .notDetermined {
            do {
                let granted = try await store.requestAccess(for: .contacts)
                if !granted {
                    UserDefaults.standard.set(true, forKey: permissionDeniedKey)
                }
                return granted
            } catch {
                return false
            }
        }
        UserDefaults.standard.set(true, forKey: permissionDeniedKey)
        return false
    }

    /// Reset permission denied cache (e.g., if user changes in Settings).
    func resetPermissionCache() {
        UserDefaults.standard.removeObject(forKey: permissionDeniedKey)
    }
}
