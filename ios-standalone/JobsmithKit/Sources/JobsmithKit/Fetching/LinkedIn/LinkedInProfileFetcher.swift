import Foundation
import SwiftSoup

/// Fetches a public LinkedIn profile from just its URL (no sign-in) and
/// reduces the page to the text the `linkedin_import` prompt expects: the
/// JSON-LD Person blob (name, titles, employers, education with dates) plus
/// the visible profile sections. LinkedIn authwalls guest profile requests
/// on some networks — that surfaces as `AuthwallError` so the UI can steer
/// the user to the in-app sign-in instead.
public enum LinkedInProfileFetcher {
    public struct AuthwallError: LocalizedError {
        public var errorDescription: String? {
            "LinkedIn wants a sign-in before showing this profile. Use "
                + "\"Sign in with LinkedIn\" instead."
        }
    }

    public struct BadURLError: LocalizedError {
        public var errorDescription: String? {
            "That doesn't look like a LinkedIn profile link — expected "
                + "something like linkedin.com/in/yourname."
        }
    }

    /// Accepts a full URL, a schemeless "linkedin.com/in/x" / "in/x", or a
    /// bare username, and produces the canonical profile URL.
    public static func normalizeProfileURL(_ input: String) -> URL? {
        var s = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !s.isEmpty else { return nil }
        // Strip query/fragment noise from copied links.
        s = s.components(separatedBy: "?")[0].components(separatedBy: "#")[0]
        while s.hasSuffix("/") { s.removeLast() }

        if let range = s.range(of: "linkedin.com/in/") {
            let slug = String(s[range.upperBound...])
            return profileURL(slug: slug)
        }
        if s.lowercased().hasPrefix("in/") {
            return profileURL(slug: String(s.dropFirst(3)))
        }
        // Bare username: no slashes, no spaces, no dots that suggest a
        // different domain.
        if !s.contains("/") && !s.contains(" ") && !s.contains(".") {
            return profileURL(slug: s)
        }
        return nil
    }

    private static func profileURL(slug: String) -> URL? {
        let cleaned = slug.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleaned.isEmpty, !cleaned.contains("/"),
              let encoded = cleaned.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed)
        else { return nil }
        return URL(string: "https://www.linkedin.com/in/\(encoded)")
    }

    /// Fetch and reduce a profile. With the user's `li_at` cookie it reads the
    /// page as they see it themselves — which is why signing in is the path the
    /// wizard offers first, and why the authwall below is mostly a guest
    /// problem. Throws `BadURLError`, `AuthwallError`, or a transport error.
    public static func fetchPublicProfile(_ input: String,
                                          cookie: String? = nil) async throws -> String {
        guard let url = normalizeProfileURL(input) else { throw BadURLError() }

        var request = URLRequest(url: url, timeoutInterval: 30)
        for (key, value) in LinkedInSource.headers(cookie: cookie) {
            request.setValue(value, forHTTPHeaderField: key)
        }
        let (data, response) = try await HTTPClient.session.data(for: request)
        let http = response as? HTTPURLResponse
        let status = http?.statusCode ?? 0
        let finalURL = http?.url

        // 999 is LinkedIn's bot-block status; redirects to authwall/login
        // mean the profile isn't guest-visible from this network right now.
        if status == 999 { throw AuthwallError() }
        guard status == 200 else { throw AuthwallError() }
        if let path = finalURL?.path.lowercased(),
           path.contains("authwall") || path.contains("/signup") || path.contains("/login") {
            throw AuthwallError()
        }

        let html = String(decoding: data, as: UTF8.self)
        let text = extractProfileText(html: html)
        guard text.count > 200 else { throw AuthwallError() }
        return text
    }

    /// Reduce profile HTML to LLM input: JSON-LD Person first (the densest,
    /// most reliable data), then the page's visible text. Capped well under
    /// the parser's 16k limit leaves room for the prompt itself.
    public static func extractProfileText(html: String, cap: Int = 14000) -> String {
        var parts: [String] = []
        guard let doc = try? SwiftSoup.parse(html) else { return "" }

        for script in (try? doc.select("script[type=application/ld+json]"))?.array() ?? [] {
            var raw = script.data()
            if raw.isEmpty { raw = (try? script.html()) ?? "" }
            if raw.contains("\"Person\"") {
                parts.append(raw)
                break
            }
        }

        // Visible page text — prefer <main> (the profile body) over the
        // whole document to skip nav/footer boilerplate.
        let mainEl = (try? doc.select("main").first()) ?? nil
        if let mainText = try? (mainEl ?? doc.body())?.text() {
            parts.append(mainText)
        }

        let joined = parts.joined(separator: "\n\n")
        return String(joined.prefix(cap))
    }
}
