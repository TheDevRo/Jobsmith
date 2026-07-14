import Foundation
import Security

/// The few config values that are live credentials rather than settings — today
/// just the LinkedIn `li_at` session cookie, which is an account-takeover token
/// if it ever leaves the device in cleartext.
public enum SecretKey: String, Sendable, CaseIterable {
    case linkedInCookie = "linkedin.li_at"
    /// LinkedIn `JSESSIONID` — a session cookie whose value is also the
    /// `csrf-token` the Voyager API requires for authenticated actions.
    case linkedInJSessionId = "linkedin.jsessionid"
}

/// Where those credentials live. An abstraction only so `ConfigStore` can be
/// tested against an in-memory double: a hostless XCTest bundle runs inside the
/// shared `xctest` process, which carries no application-identifier entitlement,
/// so `SecItemAdd` there always fails with `errSecMissingEntitlement`.
public protocol SecretStore: Sendable {
    func get(_ key: SecretKey) -> String?
    /// Store — or, for an empty value, clear — a secret. `false` means the store
    /// is unavailable, which is the caller's cue to fall back to the plaintext
    /// config file rather than silently dropping the value.
    @discardableResult
    func set(_ value: String, for key: SecretKey) -> Bool
}

/// Keychain-backed `SecretStore`.
///
/// Deliberately uses the app's *default* access group (no `kSecAttrAccessGroup`):
/// a shared group needs a team-prefixed identifier, which breaks free-account
/// sideloads — and nothing outside the app target reads these values. Items are
/// `AfterFirstUnlockThisDeviceOnly` so background fetches can still read them
/// while the phone is locked, and so they never travel in a backup.
public struct KeychainStore: SecretStore {
    public static let shared = KeychainStore()

    private let service: String

    public init(service: String = "com.thedevro.jobsmith.standalone") {
        self.service = service
    }

    private func baseQuery(_ key: SecretKey) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key.rawValue,
        ]
    }

    @discardableResult
    public func set(_ value: String, for key: SecretKey) -> Bool {
        let query = baseQuery(key)
        let deleted = SecItemDelete(query as CFDictionary)
        guard deleted == errSecSuccess || deleted == errSecItemNotFound else { return false }
        guard !value.isEmpty else { return true }

        var add = query
        add[kSecValueData as String] = Data(value.utf8)
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        return SecItemAdd(add as CFDictionary, nil) == errSecSuccess
    }

    public func get(_ key: SecretKey) -> String? {
        var query = baseQuery(key)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne

        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data, !data.isEmpty else { return nil }
        return String(decoding: data, as: UTF8.self)
    }
}
