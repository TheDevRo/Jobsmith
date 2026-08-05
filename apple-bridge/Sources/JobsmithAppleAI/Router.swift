import Foundation

/// The OpenAI-compatible surface: just enough of the API for the backend's
/// OpenAI client to treat the on-device model as one more endpoint.
enum Router {
    static func handle(_ request: HTTPRequest, respond: @escaping (HTTPResponse) -> Void) {
        // Query strings are irrelevant here but clients still send them.
        let path = String(request.path.split(separator: "?", maxSplits: 1).first ?? "")
        switch (request.method, path) {
        case ("GET", "/health"):
            let state = OnDeviceModel.availability()
            respond(.ok([
                "ok": true,
                "available": state.available,
                "reason": state.reason as Any? ?? NSNull(),
            ] as [String: Any]))

        case ("GET", "/v1/models"), ("GET", "/models"):
            let state = OnDeviceModel.availability()
            // The sentinel is listed only when it can actually serve a
            // request, so the app's "model missing" banner tells the truth
            // when Apple Intelligence is off.
            let data: [[String: Any]] = state.available ? [[
                "id": OnDeviceModel.modelID,
                "object": "model",
                "created": 0,
                "owned_by": "apple",
            ]] : []
            respond(.ok(["object": "list", "data": data] as [String: Any]))

        case ("POST", "/v1/chat/completions"), ("POST", "/chat/completions"):
            completions(request, respond: respond)

        case (_, "/health"), (_, "/v1/models"), (_, "/v1/chat/completions"):
            respond(.error(status: 405, message: "Method not allowed",
                           type: "invalid_request_error", code: "method_not_allowed"))

        default:
            respond(.error(status: 404, message: "Unknown path \(path)",
                           type: "invalid_request_error", code: "not_found"))
        }
    }

    private static func completions(_ request: HTTPRequest, respond: @escaping (HTTPResponse) -> Void) {
        guard let root = try? JSONSerialization.jsonObject(with: request.body) as? [String: Any] else {
            respond(.error(status: 400, message: "Request body must be a JSON object",
                           type: "invalid_request_error", code: "bad_request"))
            return
        }
        let messages = root["messages"] as? [[String: Any]] ?? []
        guard !messages.isEmpty else {
            respond(.error(status: 400, message: "messages is required",
                           type: "invalid_request_error", code: "missing_messages"))
            return
        }

        // System/developer turns become the session's instructions; everything
        // else is joined into one prompt, since the on-device session API takes
        // a single string rather than a transcript.
        var instructions: [String] = []
        var prompt: [String] = []
        for message in messages {
            let role = (message["role"] as? String ?? "user").lowercased()
            let text = content(of: message["content"])
            guard !text.isEmpty else { continue }
            switch role {
            case "system", "developer": instructions.append(text)
            case "assistant": prompt.append("Assistant: \(text)")
            default: prompt.append(text)
            }
        }
        let system = instructions.isEmpty ? nil : instructions.joined(separator: "\n\n")
        let user = prompt.joined(separator: "\n\n")
        guard !user.isEmpty else {
            respond(.error(status: 400, message: "No user content in messages",
                           type: "invalid_request_error", code: "missing_messages"))
            return
        }

        let temperature = number(root["temperature"])
        let maxTokens = number(root["max_tokens"] ?? root["max_completion_tokens"]).map { Int($0) }
        let requested = root["model"] as? String ?? OnDeviceModel.modelID

        Task {
            do {
                let text = try await OnDeviceModel.Generator.shared.respond(
                    system: system, user: user, temperature: temperature, maxTokens: maxTokens)
                respond(.ok(completion(model: requested, prompt: user, system: system, text: text)))
            } catch let failure as OnDeviceModel.Failure {
                respond(.error(status: failure.status, message: failure.message,
                               type: failure.type, code: failure.code))
            } catch {
                respond(.error(status: 503, message: error.localizedDescription,
                               type: "server_error", code: "model_unavailable"))
            }
        }
    }

    /// OpenAI allows content to be a string or an array of typed parts; the
    /// backend sends strings, but accepting parts costs three lines.
    private static func content(of value: Any?) -> String {
        if let text = value as? String { return text }
        if let parts = value as? [[String: Any]] {
            return parts.compactMap { $0["text"] as? String }.joined()
        }
        return ""
    }

    private static func number(_ value: Any?) -> Double? {
        if let number = value as? NSNumber { return number.doubleValue }
        if let text = value as? String { return Double(text) }
        return nil
    }

    private static func completion(model: String, prompt: String, system: String?, text: String) -> [String: Any] {
        // FoundationModels does not report token counts. Rather than omit
        // usage (some OpenAI-compatible clients insist on it) we report a
        // ~4-chars-per-token estimate; nothing in the app bills on it.
        let promptTokens = ((system?.count ?? 0) + prompt.count) / 4
        let completionTokens = text.count / 4
        return [
            "id": "chatcmpl-\(UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(24))",
            "object": "chat.completion",
            "created": Int(Date().timeIntervalSince1970),
            "model": model,
            "choices": [[
                "index": 0,
                "message": ["role": "assistant", "content": text] as [String: Any],
                "finish_reason": "stop",
            ] as [String: Any]],
            "usage": [
                "prompt_tokens": promptTokens,
                "completion_tokens": completionTokens,
                "total_tokens": promptTokens + completionTokens,
            ] as [String: Any],
        ]
    }
}
