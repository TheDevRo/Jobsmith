import Foundation
import GRDB

/// Resolves location strings to LinkedIn geoIds — port of `_resolve_geo_id`.
///
/// Resolution order:
///   1. `geo_cache` table (instant after first run).
///   2. Seed dict of common US locations (offline fallback).
///   3. Live lookup against LinkedIn's /jobs/search page, which embeds the
///      resolved geoId in a hidden form field. Cached in `geo_cache`.
///
/// Returns "" when the location can't be resolved (caller falls back to
/// sending text-only location, which is LinkedIn's pre-fix behavior).
/// Being an actor serializes live lookups, like the Python asyncio.Lock.
public actor GeoIDResolver {
    /// Seed dict of verified LinkedIn geoIds, ported verbatim.
    /// Only entries confirmed via reverse-lookup against linkedin.com belong
    /// here — an earlier version had "denver" mapped to 105072130, which is
    /// actually *Poland*. That single bad entry caused every "Denver" search
    /// to silently return Polish job postings. Do NOT add unverified geoIds.
    /// The live resolver is authoritative; this dict is only for offline use.
    static let seedGeoIDs: [String: String] = [
        "united states": "103644278",
        "us": "103644278",
        "usa": "103644278",
        "remote": "92000001",       // LinkedIn's "Remote anywhere" pseudo-geo
        // Bare "denver" resolves to a different (smaller, likely Denver, NC)
        // geoId on LinkedIn. The disambiguated form lands on Denver, CO. We
        // map both spellings to the CO geoId because that's the common intent
        // in this tool's primary use case.
        "denver": "103736294",
        "denver co": "103736294",
        "denver colorado": "103736294",
        "colorado": "105763813",
        "co": "105763813",
    ]

    /// GeoIds that represent entire states or countries — distance=25 is
    /// meaningless (or harmful) for these because LinkedIn anchors the radius
    /// on an arbitrary centroid rather than any city center.
    static let stateOrCountryGeoIDs: Set<String> = [
        "103644278",  // United States
        "105763813",  // Colorado (state)
    ]

    private let database: AppDatabase?
    private var memoryCache: [String: String] = [:]

    public init(database: AppDatabase?) {
        self.database = database
    }

    /// Python `_normalize_location`.
    public static func normalize(_ location: String) -> String {
        location.trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
            .replacingOccurrences(of: ",", with: "")
            .replacingOccurrences(of: "  ", with: " ")
    }

    public func resolve(_ location: String,
                        headers: [String: String] = HTTPClient.browserHeaders) async -> String {
        let norm = Self.normalize(location)
        guard !norm.isEmpty else { return "" }

        if let cached = memoryCache[norm] { return cached }
        if let stored = readCache(norm) {
            memoryCache[norm] = stored
            return stored
        }
        if let seeded = Self.seedGeoIDs[norm] { return seeded }

        // Live lookup — serialized by the actor so concurrent fetches don't
        // all race for the same location at startup.
        guard let url = HTTPClient.url("https://www.linkedin.com/jobs/search",
                                       query: [("location", location)]) else { return "" }
        let html: String
        do {
            let response = try await HTTPClient.fetchWithRetries(
                url, headers: headers, timeout: 15, retries: 0)
            guard response.status == 200 else { return "" }
            html = response.text
        } catch {
            return ""
        }

        // Hidden form field LinkedIn embeds in /jobs/search?location=... pages.
        guard let match = html.range(of: "geoId\"\\s*value=\"(\\d+)\"",
                                     options: .regularExpression) else { return "" }
        let fragment = String(html[match])
        guard let digits = fragment.range(of: "\\d+", options: .regularExpression) else { return "" }
        let geo = String(fragment[digits])

        memoryCache[norm] = geo
        writeCache(norm, geoID: geo)
        return geo
    }

    private func readCache(_ norm: String) -> String? {
        guard let database else { return nil }
        return try? database.writer.read { db in
            try String.fetchOne(db, sql: "SELECT geoId FROM geo_cache WHERE location = ?",
                                arguments: [norm])
        }
    }

    private func writeCache(_ norm: String, geoID: String) {
        guard let database else { return }
        try? database.writer.write { db in
            try db.execute(sql: "INSERT OR REPLACE INTO geo_cache (location, geoId) VALUES (?, ?)",
                           arguments: [norm, geoID])
        }
    }
}
