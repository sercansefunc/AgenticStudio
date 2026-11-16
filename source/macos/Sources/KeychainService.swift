import Foundation
import Security

enum KeychainService {
    private static let service = "AgenticStudio"

    private static func load(account: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess, let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    private static func save(_ key: String, account: String) throws {
        let value = key.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else {
            throw NSError(domain: "AgenticStudio.Keychain", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "Anahtar boş olamaz."])
        }
        let data = Data(value.utf8)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let attrs: [String: Any] = [kSecValueData as String: data]
        let updateStatus = SecItemUpdate(query as CFDictionary, attrs as CFDictionary)
        if updateStatus == errSecItemNotFound {
            var add = query
            add[kSecValueData as String] = data
            let status = SecItemAdd(add as CFDictionary, nil)
            guard status == errSecSuccess else {
                throw NSError(domain: NSOSStatusErrorDomain, code: Int(status), userInfo: nil)
            }
        } else if updateStatus != errSecSuccess {
            throw NSError(domain: NSOSStatusErrorDomain, code: Int(updateStatus), userInfo: nil)
        }
    }

    private static func delete(account: String) throws {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw NSError(domain: NSOSStatusErrorDomain, code: Int(status), userInfo: nil)
        }
    }

    static func load(_ secret: Secret) -> String? { load(account: secret.rawValue) }
    static func save(_ value: String, for secret: Secret) throws { try save(value, account: secret.rawValue) }
    static func delete(_ secret: Secret) throws { try delete(account: secret.rawValue) }
}

/// Keychain accounts (raw values are stored names; keep them stable) and their bridge env.
enum Secret: String, CaseIterable, Identifiable {
    case gemini = "Gemini API Key"
    case github = "GitHub Token"
    case boardCookie = "Board Cookie"
    case boardUserAgent = "Board User-Agent"

    var id: String { rawValue }
    var environment: String {
        switch self {
        case .gemini: return "GEMINI_API_KEY"
        case .github: return "GITHUB_TOKEN"
        case .boardCookie: return "BOARD_COOKIE"
        case .boardUserAgent: return "BOARD_USER_AGENT"
        }
    }
    var title: String {
        switch self {
        case .gemini: return "Gemini API anahtarı"
        case .github: return "GitHub token (opsiyonel, kota için)"
        case .boardCookie: return "Board Cookie (isteğe bağlı)"
        case .boardUserAgent: return "Board için tarayıcı User-Agent"
        }
    }
}
