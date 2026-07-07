import Foundation
import SwiftSoup

/// LinkedIn guest search API helpers: query batching, filter-parameter
/// mapping, URL building, and search-result-fragment parsing. Pure functions
/// so fixture tests catch LinkedIn DOM changes before they silently zero out
/// the whole source.
enum LinkedInGuestAPI {
    static let searchBase = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"

    /// LinkedIn f_WT values: 1=On-site, 2=Remote, 3=Hybrid.
    static let remoteFilter = "2"

    /// Keywords are combined into boolean-OR search queries so 11 configured
    /// keywords cost ~3 searches per location instead of 11.
    static let keywordBatchSize = 4

    /// One parsed guest-search result card.
    struct SearchCard: Equatable, Sendable {
        var title: String
        var company: String
        var location: String
        var url: String
        var externalID: String
        var datePosted: String
        /// Card-level Easy Apply badge detection (footer/benefits text).
        var isEasyApply: Bool
    }

    // MARK: - Query building

    /// Group keywords into boolean-OR queries — port of `_batch_keywords`.
    /// Multi-word keywords are quoted so OR binds to the whole phrase. A
    /// batch of one keeps the bare keyword to preserve LinkedIn's fuzzy
    /// matching.
    static func batchKeywords(_ keywords: [String], size: Int = keywordBatchSize) -> [String] {
        let cleaned = keywords
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        var batches: [String] = []
        var i = 0
        while i < cleaned.count {
            let chunk = Array(cleaned[i..<min(i + size, cleaned.count)])
            if chunk.count == 1 {
                batches.append(chunk[0])
            } else {
                batches.append(chunk.map { $0.contains(" ") ? "\"\($0)\"" : $0 }
                    .joined(separator: " OR "))
            }
            i += size
        }
        return batches
    }

    /// Map max_age_days to LinkedIn's f_TPR parameter — exact Python mapping.
    static func timeFilter(maxAgeDays: Int) -> String {
        if maxAgeDays <= 1 { return "r86400" }      // Past 24 hours
        if maxAgeDays <= 7 { return "r604800" }     // Past week
        if maxAgeDays <= 30 { return "r2592000" }   // Past month
        return ""
    }

    /// Build one guest-search URL. When we have a geoId, that's the
    /// authoritative location signal — sending the text `&location=`
    /// alongside it lets LinkedIn fall back to fuzzy matching the string,
    /// which has been observed to pull in Polish/EU jobs even when geoId
    /// says US. So: geoId-only when we have one.
    static func searchURL(query: String, geoID: String, location: String,
                          isRemoteLocation: Bool, timeFilter: String,
                          start: Int) -> URL? {
        var items: [(String, String)] = [("keywords", query)]
        if !geoID.isEmpty {
            items.append(("geoId", geoID))
        } else {
            items.append(("location", location))
        }
        if isRemoteLocation {
            items.append(("f_WT", remoteFilter))
        }
        if !timeFilter.isEmpty {
            items.append(("f_TPR", timeFilter))
        }
        // distance=25 only for city geoIds — meaningless for whole states or
        // countries, where LinkedIn anchors the radius on an arbitrary centroid.
        if !isRemoteLocation, !geoID.isEmpty,
           !GeoIDResolver.stateOrCountryGeoIDs.contains(geoID) {
            items.append(("distance", "25"))
        }
        items.append(("start", String(start)))
        return HTTPClient.url(searchBase, query: items)
    }

    // MARK: - Search-fragment parsing

    /// Job id from a /jobs/view URL: `/view/[^/]+-(\d+)`.
    private static let jobIDRegex = try! NSRegularExpression(pattern: "/view/[^/]+-(\\d+)")

    /// Parse the HTML fragment the guest API returns into cards. Returns nil
    /// when the fragment has no <li> elements at all (possible auth redirect
    /// or empty results page) so the caller can distinguish that from a page
    /// where every card was an ad/spacer.
    static func parseSearchPage(html: String) -> [SearchCard]? {
        guard let doc = try? SwiftSoup.parse(html),
              let items = (try? doc.select("li"))?.array(), !items.isEmpty else { return nil }
        var cards: [SearchCard] = []
        for li in items {
            if let card = ((try? parseSearchCard(li)) ?? nil) { cards.append(card) }
        }
        return cards
    }

    /// Extract raw fields from one guest-search result card (<li> element) —
    /// port of `_parse_search_card`. Returns nil when the card lacks a title
    /// or link (ads, spacer nodes).
    static func parseSearchCard(_ card: Element) throws -> SearchCard? {
        guard let titleEl = try card.select("h3[class~=base-search-card__title]").first(),
              let linkEl = try card.select("a[class~=base-card__full-link]").first() else {
            return nil
        }
        let companyEl = try card.select("h4[class~=base-search-card__subtitle]").first()
        let locationEl = try card.select("span[class~=job-search-card__location]").first()
        let timeEl = try card.select("time").first()

        let href = try linkEl.attr("href")
        let jobURL = href.components(separatedBy: "?")[0]

        let ns = jobURL as NSString
        let match = jobIDRegex.firstMatch(in: jobURL, range: NSRange(location: 0, length: ns.length))
        let externalID = match.map { ns.substring(with: $0.range(at: 1)) } ?? jobURL

        // --- Easy Apply detection from the search card ---
        // LinkedIn search cards often have a footer or span with "Easy Apply".
        var easyApply = ((try? card.text()) ?? "").lowercased().contains("easy apply")
        if !easyApply,
           let eaEl = try card.select("[class~=(?i)easy-apply|easyApply|job-posting-benefits__text]").first(),
           (try eaEl.text()).lowercased().contains("easy apply") {
            easyApply = true
        }

        return SearchCard(
            title: try titleEl.text(),
            company: (try companyEl?.text()) ?? "",
            location: (try locationEl?.text()) ?? "",
            url: jobURL,
            externalID: externalID,
            datePosted: (try timeEl?.attr("datetime")) ?? "",
            isEasyApply: easyApply
        )
    }

    // MARK: - Location matching

    /// Check if a job's location matches any of the configured locations —
    /// port of `_is_location_match`. Returns true when jobLocation is empty:
    /// LinkedIn's guest API often omits location from search cards; the
    /// detail fetch may populate it later, and filtering on an empty string
    /// would silently drop valid jobs.
    static func isLocationMatch(_ jobLocation: String, configLocations: [String]) -> Bool {
        guard !jobLocation.isEmpty else { return true }
        let locLower = jobLocation.lowercased()
        for configLoc in configLocations {
            let cl = configLoc.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            if cl.isEmpty { continue }
            if cl == "remote" && locLower.contains("remote") { return true }
            if locLower.contains(cl) { return true }
            if locLower.hasPrefix(cl) { return true }
        }
        return false
    }
}
