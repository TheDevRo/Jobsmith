import Foundation

/// Shared HTTP layer for all job sources: one ephemeral session, the
/// fetch-with-retries policy ported from Python `fetch_with_retries`, and the
/// browser-like header preset RemoteOK / WeWorkRemotely / generic pages need.
public enum HTTPClient {
    /// The session every source shares. A `var` only so tests can install a
    /// `URLProtocol` stub (a custom session snapshots its protocol classes at
    /// creation, so the global `URLProtocol.registerClass` registry can't reach
    /// it); nothing in the app ever reassigns it.
    public nonisolated(unsafe) static var session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 30
        config.timeoutIntervalForResource = 300
        config.httpAdditionalHeaders = nil
        return URLSession(configuration: config)
    }()

    public static let browserUserAgent =
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " +
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    /// Header preset for sources that must look like a browser.
    public static let browserHeaders: [String: String] = [
        "User-Agent": browserUserAgent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    ]

    public struct Response: Sendable {
        public let status: Int
        public let data: Data
        public var text: String { String(decoding: data, as: UTF8.self) }
    }

    /// GET `url` and return (status, body), retrying transient failures.
    ///
    /// Retries timeouts, connection errors, 5xx, and 429 with jittered
    /// exponential backoff (429 honors Retry-After and backs off harder).
    /// Other statuses are returned as-is — semantics like 403 = blocked stay
    /// with the caller. Throws the last error when all attempts fail.
    public static func fetchWithRetries(
        _ url: URL,
        headers: [String: String] = [:],
        timeout: TimeInterval = 30,
        retries: Int = 2,
        backoffBase: Double = 1.0
    ) async throws -> Response {
        var lastError: Error?
        for attempt in 0...retries {
            var request = URLRequest(url: url, timeoutInterval: timeout)
            for (key, value) in headers { request.setValue(value, forHTTPHeaderField: key) }
            do {
                let (data, urlResponse) = try await session.data(for: request)
                let http = urlResponse as? HTTPURLResponse
                let status = http?.statusCode ?? 0
                if status == 429 && attempt < retries {
                    let retryAfter = Double(http?.value(forHTTPHeaderField: "Retry-After") ?? "") ?? 0
                    let wait = max(retryAfter, 2.0 * backoffBase * pow(2, Double(attempt)))
                        + Double.random(in: 0..<0.5)
                    try await Task.sleep(nanoseconds: UInt64(wait * 1_000_000_000))
                    continue
                }
                if status >= 500 && attempt < retries {
                    let wait = backoffBase * pow(2, Double(attempt)) + Double.random(in: 0..<0.5)
                    try await Task.sleep(nanoseconds: UInt64(wait * 1_000_000_000))
                    continue
                }
                return Response(status: status, data: data)
            } catch let error as URLError {
                if error.code == .cancelled { throw error }
                lastError = error
                if attempt < retries {
                    let wait = backoffBase * pow(2, Double(attempt)) + Double.random(in: 0..<0.5)
                    try await Task.sleep(nanoseconds: UInt64(wait * 1_000_000_000))
                }
            }
        }
        throw lastError ?? URLError(.unknown)
    }

    /// Build a URL with percent-encoded query items. Order-preserving.
    public static func url(_ base: String, query: [(String, String)] = []) -> URL? {
        guard var components = URLComponents(string: base) else { return nil }
        if !query.isEmpty {
            components.queryItems = query.map { URLQueryItem(name: $0.0, value: $0.1) }
        }
        return components.url
    }
}

/// Counting semaphore for bounding source-internal request concurrency
/// (Python asyncio.Semaphore equivalent).
actor AsyncLimiter {
    private var available: Int
    private var waiters: [CheckedContinuation<Void, Never>] = []

    init(_ count: Int) { available = count }

    func acquire() async {
        if available > 0 {
            available -= 1
            return
        }
        await withCheckedContinuation { waiters.append($0) }
    }

    func release() {
        if let next = waiters.first {
            waiters.removeFirst()
            next.resume()
        } else {
            available += 1
        }
    }
}
