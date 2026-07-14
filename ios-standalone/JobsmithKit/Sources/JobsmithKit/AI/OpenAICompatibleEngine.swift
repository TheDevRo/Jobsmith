import Foundation

public enum AIEngineError: Error, Equatable, Sendable, LocalizedError {
    case invalidBaseURL(String)
    case unreachable(String)
    /// The call was cut off by something a later attempt won't reproduce: the
    /// app suspended mid-request, the task cancelled, the network dropped, or
    /// the endpoint went out of reach (a laptop running LM Studio is only on the
    /// LAN while you're home). Distinct from `unreachable`, which means the
    /// server answered the door and said no — a refused connection, a bad host —
    /// and will say the same thing on every retry.
    case interrupted(String)
    case httpStatus(Int, String)
    case emptyResponse

    public var errorDescription: String? {
        switch self {
        case .invalidBaseURL(let url):
            return url.isEmpty ? "No endpoint URL set" : "Invalid endpoint URL: \(url)"
        case .unreachable(let detail):
            return "Could not reach the server: \(detail)"
        case .interrupted(let detail):
            return "The connection to the server was interrupted: \(detail)"
        case .httpStatus(let code, let body):
            let detail = body.prefix(120)
            return detail.isEmpty ? "Server returned HTTP \(code)" : "HTTP \(code): \(detail)"
        case .emptyResponse:
            return "The server returned an empty or malformed response"
        }
    }
}

/// Chat backend for any OpenAI-compatible endpoint (LM Studio, OpenRouter…).
public struct OpenAICompatibleEngine: AIEngine {
    public init() {}

    public func complete(_ req: CompletionRequest, config: AIConfig) async throws -> String {
        var messages: [[String: Any]] = []
        if let system = req.system, !system.isEmpty {
            messages.append(["role": "system", "content": system])
        }
        messages.append(["role": "user", "content": req.user])
        let body: [String: Any] = [
            "model": config.endpointModel(for: req.tier),
            "messages": messages,
            "temperature": req.temperature,
            "max_tokens": req.maxTokens,
        ]
        let data = try await send(path: "chat/completions", method: "POST", body: body, config: config)
        guard let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let choices = obj["choices"] as? [[String: Any]],
              let message = choices.first?["message"] as? [String: Any],
              let content = message["content"] as? String,
              !content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw AIEngineError.emptyResponse
        }
        return content
    }

    public func listModels(config: AIConfig) async throws -> [String] {
        let data = try await send(path: "models", method: "GET", body: nil, config: config)
        guard let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let rows = obj["data"] as? [[String: Any]] else {
            throw AIEngineError.emptyResponse
        }
        return rows.compactMap { $0["id"] as? String }
    }

    private func send(path: String, method: String, body: [String: Any]?,
                      config: AIConfig) async throws -> Data {
        var base = config.baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        while base.hasSuffix("/") { base.removeLast() }
        guard !base.isEmpty, let url = URL(string: base + "/" + path) else {
            throw AIEngineError.invalidBaseURL(config.baseURL)
        }
        var request = URLRequest(url: url, timeoutInterval: 90)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if !config.apiKey.isEmpty {
            request.setValue("Bearer \(config.apiKey)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        }

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await URLSession.shared.data(for: request)
        } catch {
            // Keep the two apart: a cut-off call is worth resuming, a refused one
            // is worth reporting. Flattening both into `unreachable` is what made
            // a backgrounded scoring run look like a dead endpoint and abort.
            if TransientNetwork.isTransient(error) {
                throw AIEngineError.interrupted(error.localizedDescription)
            }
            throw AIEngineError.unreachable(error.localizedDescription)
        }
        guard let http = response as? HTTPURLResponse else {
            throw AIEngineError.unreachable("Non-HTTP response from \(url)")
        }
        guard (200..<300).contains(http.statusCode) else {
            let snippet = String(String(data: data, encoding: .utf8)?.prefix(500) ?? "")
            throw AIEngineError.httpStatus(http.statusCode, snippet)
        }
        return data
    }
}
