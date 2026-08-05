import Foundation
import Network

/// A parsed HTTP/1.1 request. Only what an OpenAI-compatible JSON API needs —
/// method, path, and a Content-Length body. No chunked encoding: every client
/// of this bridge is our own backend posting small JSON blobs.
struct HTTPRequest {
    let method: String
    let path: String
    let body: Data
}

struct HTTPResponse {
    let status: Int
    let json: Any

    static func ok(_ json: Any) -> HTTPResponse { HTTPResponse(status: 200, json: json) }

    /// OpenAI-shaped error body — the backend talks to this server with an
    /// OpenAI client, so failures have to arrive in the shape that client
    /// already knows how to surface.
    static func error(status: Int, message: String, type: String, code: String) -> HTTPResponse {
        HTTPResponse(status: status, json: [
            "error": [
                "message": message,
                "type": type,
                "code": code,
            ] as [String: Any]
        ])
    }
}

/// Minimal HTTP/1.1 server on 127.0.0.1, built straight on NWListener so the
/// bridge stays dependency-free (a sidecar shipped inside the .app should not
/// drag a web framework along).
final class HTTPServer {
    private let listener: NWListener
    private let queue = DispatchQueue(label: "jobsmith.apple-ai.http")
    private let handler: (HTTPRequest, @escaping (HTTPResponse) -> Void) -> Void

    /// - Parameter port: 0 asks the OS for a free port; the caller reads the
    ///   real one back from `boundPort` once the listener is ready.
    init(port: UInt16,
         handler: @escaping (HTTPRequest, @escaping (HTTPResponse) -> Void) -> Void) throws {
        self.handler = handler
        let parameters = NWParameters.tcp
        parameters.allowLocalEndpointReuse = true
        // Loopback only. There is no auth on this server; the OS refusing
        // off-machine connections IS the security boundary.
        parameters.requiredLocalEndpoint = .hostPort(host: .ipv4(.loopback),
                                                     port: NWEndpoint.Port(rawValue: port) ?? .any)
        self.listener = try NWListener(using: parameters)
    }

    var boundPort: UInt16? { listener.port?.rawValue }

    /// Calls `onReady` once the socket is bound (that is when the port is
    /// known); throws-by-exit on a listener failure, since a bridge that
    /// cannot listen has nothing to offer.
    func start(onReady: @escaping (UInt16) -> Void) {
        listener.stateUpdateHandler = { [weak self] state in
            switch state {
            case .ready:
                if let port = self?.boundPort { onReady(port) }
            case .failed(let error):
                FileHandle.standardError.write(
                    Data("jobsmith-apple-ai: listener failed: \(error)\n".utf8))
                exit(1)
            default:
                break
            }
        }
        listener.newConnectionHandler = { [weak self] connection in
            self?.accept(connection)
        }
        listener.start(queue: queue)
    }

    private func accept(_ connection: NWConnection) {
        connection.start(queue: queue)
        receive(connection, buffer: Data())
    }

    /// Accumulate until the headers are complete and the declared body has
    /// arrived — a single `receive` is not guaranteed to hand over a whole
    /// request, and tailoring prompts are far bigger than one TCP segment.
    private func receive(_ connection: NWConnection, buffer: Data) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 64 * 1024) { [weak self] data, _, isComplete, error in
            guard let self else { return }
            var buffer = buffer
            if let data { buffer.append(data) }
            if error != nil {
                connection.cancel()
                return
            }
            switch Self.parse(buffer) {
            case .incomplete:
                if isComplete {
                    connection.cancel()
                } else {
                    self.receive(connection, buffer: buffer)
                }
            case .malformed:
                self.respond(connection, .error(status: 400,
                                                message: "Malformed HTTP request",
                                                type: "invalid_request_error",
                                                code: "bad_request"))
            case .request(let request):
                self.handler(request) { response in
                    self.respond(connection, response)
                }
            }
        }
    }

    private func respond(_ connection: NWConnection, _ response: HTTPResponse) {
        let body: Data
        do {
            body = try JSONSerialization.data(withJSONObject: response.json, options: [])
        } catch {
            body = Data(#"{"error":{"message":"response encoding failed","type":"server_error"}}"#.utf8)
        }
        var head = "HTTP/1.1 \(response.status) \(Self.reason(response.status))\r\n"
        head += "Content-Type: application/json\r\n"
        head += "Content-Length: \(body.count)\r\n"
        // One request per connection: closing is simpler than a keep-alive
        // state machine and every HTTP client handles it.
        head += "Connection: close\r\n\r\n"
        var out = Data(head.utf8)
        out.append(body)
        connection.send(content: out, completion: .contentProcessed { _ in
            connection.cancel()
        })
    }

    private static func reason(_ status: Int) -> String {
        switch status {
        case 200: return "OK"
        case 400: return "Bad Request"
        case 404: return "Not Found"
        case 405: return "Method Not Allowed"
        case 429: return "Too Many Requests"
        case 503: return "Service Unavailable"
        default: return "Error"
        }
    }

    // MARK: - Parsing

    private enum ParseResult {
        case incomplete
        case malformed
        case request(HTTPRequest)
    }

    private static let headerTerminator = Data("\r\n\r\n".utf8)

    private static func parse(_ buffer: Data) -> ParseResult {
        guard let headerEnd = buffer.range(of: headerTerminator) else { return .incomplete }
        let headerData = buffer[buffer.startIndex..<headerEnd.lowerBound]
        guard let headerText = String(data: headerData, encoding: .utf8) else { return .malformed }
        let lines = headerText.components(separatedBy: "\r\n")
        let requestLine = lines.first?.split(separator: " ") ?? []
        guard requestLine.count >= 2 else { return .malformed }

        var contentLength = 0
        for line in lines.dropFirst() {
            let parts = line.split(separator: ":", maxSplits: 1)
            guard parts.count == 2 else { continue }
            if parts[0].lowercased() == "content-length" {
                contentLength = Int(parts[1].trimmingCharacters(in: .whitespaces)) ?? 0
            }
        }

        let bodyStart = headerEnd.upperBound
        let available = buffer.distance(from: bodyStart, to: buffer.endIndex)
        guard available >= contentLength else { return .incomplete }
        let bodyEnd = buffer.index(bodyStart, offsetBy: contentLength)
        return .request(HTTPRequest(method: String(requestLine[0]).uppercased(),
                                    path: String(requestLine[1]),
                                    body: Data(buffer[bodyStart..<bodyEnd])))
    }
}
