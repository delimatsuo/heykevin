import Foundation
import Security

enum KeychainReadResult: Equatable {
    case value(String)
    case missing
    case error(OSStatus)

    var stringValue: String? {
        if case .value(let str) = self {
            return str
        }
        return nil
    }
}

class KeychainManager {
    static let shared = KeychainManager()

    private let serviceName = "com.kevin.callscreen"
    private let defaultAccessibility = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly

    private let reader: ((String) -> KeychainReadResult)?
    private let writer: ((String, String) -> Bool)?
    private let remover: ((String) -> Void)?

    init(reader: ((String) -> KeychainReadResult)? = nil,
         writer: ((String, String) -> Bool)? = nil,
         remover: ((String) -> Void)? = nil) {
        self.reader = reader
        self.writer = writer
        self.remover = remover
    }

    @discardableResult
    func save(_ key: String, value: String) -> Bool {
        if let writer { return writer(key, value) }
        guard let data = value.data(using: .utf8) else { return false }

        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: serviceName,
            kSecAttrAccount as String: key,
        ]

        let attributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: defaultAccessibility,
        ]

        let updateStatus = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if updateStatus == errSecSuccess {
            return true
        }
        guard updateStatus == errSecItemNotFound else {
            return false
        }

        var addQuery = query
        addQuery[kSecValueData as String] = data
        addQuery[kSecAttrAccessible as String] = defaultAccessibility

        let addStatus = SecItemAdd(addQuery as CFDictionary, nil)
        return addStatus == errSecSuccess
    }

    func read(_ key: String) -> KeychainReadResult {
        if let reader { return reader(key) }
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: serviceName,
            kSecAttrAccount as String: key,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]

        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)

        if status == errSecItemNotFound {
            return .missing
        }
        guard status == errSecSuccess else {
            return .error(status)
        }
        guard let data = result as? Data, let str = String(data: data, encoding: .utf8) else {
            return .error(errSecDecode)
        }
        return .value(str)
    }

    func retrieve(_ key: String) -> String? {
        read(key).stringValue
    }

    /// An unavailable read is not absence, and a failed migration is not a
    /// usable secure credential. Keep the legacy value until readback succeeds.
    func readMigratingLegacy(_ key: String, defaults: UserDefaults = .standard) -> KeychainReadResult {
        let result = read(key)
        guard result == .missing,
              let legacy = defaults.string(forKey: key), !legacy.isEmpty else { return result }
        guard save(key, value: legacy) else { return .error(errSecNotAvailable) }
        let confirmed = read(key)
        guard confirmed == .value(legacy) else { return .error(errSecNotAvailable) }
        defaults.removeObject(forKey: key)
        return confirmed
    }

    func delete(_ key: String) {
        if let remover {
            remover(key)
            return
        }
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: serviceName,
            kSecAttrAccount as String: key,
        ]
        SecItemDelete(query as CFDictionary)
    }

    @discardableResult
    func migrateAccessibility(for keys: [String]) -> Bool {
        var readable: [(String, String)] = []
        for key in keys {
            switch read(key) {
            case .value(let value): if !value.isEmpty { readable.append((key, value)) }
            case .missing: break
            case .error: return false
            }
        }
        for (key, value) in readable {
            guard save(key, value: value), read(key) == .value(value) else { return false }
        }
        return true
    }
}
