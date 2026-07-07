import Foundation

/// WeWorkRemotely RSS feeds, covering all major job categories. The main
/// remote-jobs.rss feed is a superset; link dedup handles the overlap.
public struct WeWorkRemotelySource: JobSource {
    public static let id = "weworkremotely"
    public static let timeout: Duration = .seconds(60)

    static let concurrency = 5

    /// All available categories per weworkremotely.com/remote-job-categories
    /// as of 2026-07. Retired slugs 301 with no Location and are omitted.
    static let feeds = [
        "https://weworkremotely.com/remote-jobs.rss",
        "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
        "https://weworkremotely.com/categories/remote-front-end-programming-jobs.rss",
        "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
        "https://weworkremotely.com/categories/remote-design-jobs.rss",
        "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
        "https://weworkremotely.com/categories/remote-customer-support-jobs.rss",
        "https://weworkremotely.com/categories/remote-product-jobs.rss",
        "https://weworkremotely.com/categories/remote-sales-and-marketing-jobs.rss",
        "https://weworkremotely.com/categories/remote-management-and-finance-jobs.rss",
        "https://weworkremotely.com/categories/all-other-remote-jobs.rss",
    ]

    public init() {}

    public func fetchJobs(config: AppConfig, knownExternalIDs: Set<String>) async throws -> [NormalizedJob] {
        let keywords = config.search.keywords
        guard !keywords.isEmpty else { return [] }
        let excludePatterns = JobFilters.compileExcludes(config.search.excludeKeywords)

        let headers = [
            "User-Agent": HTTPClient.browserUserAgent,
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        ]

        let limiter = AsyncLimiter(Self.concurrency)
        var feedItems: [[RSSItem]] = []
        await withTaskGroup(of: [RSSItem].self) { group in
            for feed in Self.feeds {
                group.addTask {
                    guard let url = URL(string: feed) else { return [] }
                    await limiter.acquire()
                    let response: HTTPClient.Response?
                    do {
                        response = try await HTTPClient.fetchWithRetries(url, headers: headers, timeout: 30)
                    } catch {
                        response = nil
                    }
                    await limiter.release()
                    guard let response, response.status == 200 else { return [] }
                    return Self.parseFeed(response.data)
                }
            }
            for await items in group { feedItems.append(items) }
        }

        var seenLinks = Set<String>()
        var results: [NormalizedJob] = []
        for items in feedItems {
            results += Self.buildJobs(items: items, keywords: keywords,
                                      excludePatterns: excludePatterns, seenLinks: &seenLinks)
        }
        return results
    }

    struct RSSItem: Sendable, Equatable {
        var title = ""
        var link = ""
        var description = ""
        var pubDate = ""
    }

    static func parseFeed(_ data: Data) -> [RSSItem] {
        let delegate = FeedDelegate()
        let parser = XMLParser(data: data)
        parser.delegate = delegate
        parser.parse()
        return delegate.items
    }

    static func buildJobs(items: [RSSItem], keywords: [String],
                          excludePatterns: [NSRegularExpression],
                          seenLinks: inout Set<String>) -> [NormalizedJob] {
        var results: [NormalizedJob] = []
        for item in items {
            if seenLinks.contains(item.link) { continue }
            seenLinks.insert(item.link)

            let (title, company) = parseTitleCompany(item.title)
            let description = JobFilters.cleanDescription(item.description)

            if !JobFilters.matchesKeywords("\(title) \(description)", keywords) { continue }
            if JobFilters.matchesExclude(title, excludePatterns) { continue }

            results.append(NormalizedJob(
                source: "weworkremotely",
                externalId: item.link,
                title: title,
                company: company,
                location: "Remote",
                url: item.link,
                description: description,
                tags: [],
                datePosted: item.pubDate,
                isRemote: true))
        }
        return results
    }

    /// WWR titles are often "Company: Job Title" — split on the first colon.
    static func parseTitleCompany(_ rawTitle: String) -> (title: String, company: String) {
        guard let colon = rawTitle.firstIndex(of: ":") else { return (rawTitle, "") }
        let company = String(rawTitle[..<colon]).trimmingCharacters(in: .whitespacesAndNewlines)
        let title = String(rawTitle[rawTitle.index(after: colon)...])
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return (title, company)
    }

    private final class FeedDelegate: NSObject, XMLParserDelegate {
        var items: [RSSItem] = []
        private var current: RSSItem?
        private var buffer = ""

        func parser(_ parser: XMLParser, didStartElement elementName: String,
                    namespaceURI: String?, qualifiedName: String?,
                    attributes: [String: String] = [:]) {
            if elementName == "item" { current = RSSItem() }
            buffer = ""
        }

        func parser(_ parser: XMLParser, foundCharacters string: String) {
            buffer += string
        }

        func parser(_ parser: XMLParser, foundCDATA CDATABlock: Data) {
            buffer += String(decoding: CDATABlock, as: UTF8.self)
        }

        func parser(_ parser: XMLParser, didEndElement elementName: String,
                    namespaceURI: String?, qualifiedName: String?) {
            guard var item = current else { return }
            let text = buffer.trimmingCharacters(in: .whitespacesAndNewlines)
            switch elementName {
            case "title": item.title = text
            case "link": item.link = text
            case "description", "summary": item.description = text
            case "pubDate": item.pubDate = text
            case "item":
                items.append(item)
                current = nil
                return
            default: break
            }
            current = item
        }
    }
}
