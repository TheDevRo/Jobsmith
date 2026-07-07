import Foundation

/// Where the self-hosted Jobsmith backend lives. The iOS app is a thin shell
/// (like the Tauri desktop window) around the web frontend that the backend
/// serves — but on iOS the backend cannot be bundled, so the user points the
/// app at the server they already run (desktop app, Docker, or uvicorn).
@MainActor
final class ServerConfig: ObservableObject {
    private static let defaultsKey = "jobsmith.serverURL"

    @Published private(set) var serverURL: URL?

    init() {
        // UI-test hook: force the first-run setup screen.
        if CommandLine.arguments.contains("--reset-server") {
            UserDefaults.standard.removeObject(forKey: Self.defaultsKey)
        }
        if let raw = UserDefaults.standard.string(forKey: Self.defaultsKey) {
            serverURL = URL(string: raw)
        }
    }

    func setServer(_ url: URL) {
        serverURL = url
        UserDefaults.standard.set(url.absoluteString, forKey: Self.defaultsKey)
    }

    func clearServer() {
        serverURL = nil
        UserDefaults.standard.removeObject(forKey: Self.defaultsKey)
    }

    /// Normalize what the user typed into a base URL.
    /// - No scheme → assume `http://` (self-hosted LAN default).
    /// - No scheme *and* no port → assume `:8888` (the Jobsmith default port).
    ///   An explicit scheme means the user knows their setup (reverse proxy,
    ///   https) — leave the port alone.
    static func normalize(_ input: String) -> URL? {
        var text = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        let hadScheme = text.contains("://")
        if !hadScheme { text = "http://" + text }
        guard var components = URLComponents(string: text),
              let host = components.host, !host.isEmpty else { return nil }
        if !hadScheme && components.port == nil {
            components.port = 8888
        }
        components.path = ""
        components.query = nil
        components.fragment = nil
        guard components.scheme == "http" || components.scheme == "https" else { return nil }
        return components.url
    }

    /// Probe `/api/stats` — a cheap, auth-free endpoint every Jobsmith backend
    /// serves — to confirm the URL is actually a Jobsmith server.
    static func testConnection(to base: URL) async -> Result<Void, ConnectionError> {
        var request = URLRequest(url: base.appendingPathComponent("api/stats"))
        request.timeoutInterval = 8
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                return .failure(.notJobsmith)
            }
            guard http.statusCode == 200 else {
                return .failure(.badStatus(http.statusCode))
            }
            guard (try? JSONSerialization.jsonObject(with: data)) != nil else {
                return .failure(.notJobsmith)
            }
            return .success(())
        } catch {
            return .failure(.unreachable(error.localizedDescription))
        }
    }

    enum ConnectionError: Error {
        case unreachable(String)
        case badStatus(Int)
        case notJobsmith

        var message: String {
            switch self {
            case .unreachable(let detail):
                return "Could not reach the server. Check the address and that the Jobsmith server is running.\n(\(detail))"
            case .badStatus(let code):
                return "The server responded with HTTP \(code) — is this really your Jobsmith server?"
            case .notJobsmith:
                return "That address responded, but it doesn't look like a Jobsmith server."
            }
        }
    }
}
