import Foundation

/// LinkedIn public guest job search (no login required) — port of
/// `job_sources/linkedin.py`. Two phases: guest-API search with proper
/// filter parameters, then a detail-page fetch per new job to extract the
/// full description, salary information, and criteria tags. Jobs already in
/// the DB (`knownExternalIDs`) skip the detail fetch — it's the slowest,
/// most 429-prone part of this source.
public struct LinkedInSource: JobSource {
    public static let id = "linkedin"
    /// The pipeline cancels this source at 600s and cancellation discards
    /// EVERYTHING collected, so the internal budgets keep both phases under
    /// it with margin.
    public static let timeout: Duration = .seconds(600)

    // Throttle requests to avoid 429s — Python constants.
    static let searchDelay = 1.5        // seconds between search page requests
    static let detailConcurrency = 2    // parallel detail workers (starts paced by throttler)
    static let detailSpacing = 1.2      // min seconds between detail starts, across workers
    static let detailJitter = 0.3
    static let maxRetries = 2
    static let retryBase = 5.0          // base seconds for exponential backoff on 429

    // Internal budgets (seconds): search phase, detail phase, and the total.
    static let totalBudget = 560.0
    static let searchPhaseBudget = 240.0
    static let detailPhaseBudget = 420.0

    /// Desktop Chrome headers, no cookies (the shared preset matches the
    /// Python header dict exactly).
    static var headers: [String: String] { HTTPClient.browserHeaders }

    private let database: AppDatabase?

    /// - Parameter database: backs the persistent geoId cache; nil falls back
    ///   to in-memory-only resolution (plus the seed dict).
    public init(database: AppDatabase? = nil) {
        self.database = database ?? (try? AppDatabase.shared())
    }

    public func fetchJobs(config: AppConfig, knownExternalIDs: Set<String>) async throws -> [NormalizedJob] {
        let keywords = config.search.keywords
        guard !keywords.isEmpty else { return [] }
        let locations = config.search.locations.isEmpty ? [""] : config.search.locations
        let excludePatterns = JobFilters.compileExcludes(config.search.excludeKeywords)
        let maxAgeDays = config.search.maxAgeDays ?? 7

        let timeFilter = LinkedInGuestAPI.timeFilter(maxAgeDays: maxAgeDays)
        let hasRemote = locations.contains {
            $0.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() == "remote"
        }

        // Batched boolean-OR queries instead of one search per keyword.
        let queries = LinkedInGuestAPI.batchKeywords(keywords)
        let resolver = GeoIDResolver(database: database)

        let totalBudget = TimeBudget(seconds: Self.totalBudget)
        let searchBudget = TimeBudget(seconds: Self.searchPhaseBudget)
        let searchThrottler = LinkedInThrottler(spacing: Self.searchDelay)

        var results: [NormalizedJob] = []
        var seenIDs = Set<String>()
        // Track (query, geoId) pairs already searched — multiple configured
        // locations can map to the same LinkedIn geoId ("Colorado"/"CO").
        var seenQueryGeo = Set<String>()

        // ----- Phase 1: Collect job cards from search results -----
        queryLoop: for query in queries {
            if searchBudget.isExpired { break }
            for location in locations {
                if searchBudget.isExpired { break queryLoop }
                let locNormalized = GeoIDResolver.normalize(location)
                let geoID = await resolver.resolve(location, headers: Self.headers)

                let comboKey = query + "\u{1F}" + (geoID.isEmpty ? locNormalized : geoID)
                if !seenQueryGeo.insert(comboKey).inserted { continue }

                let isRemoteLocation = locNormalized == "remote"

                // 4 pages per query — OR-batched queries aggregate several
                // keywords, so go one page deeper than per-keyword search did.
                pageLoop: for start in stride(from: 0, to: 100, by: 25) {
                    if searchBudget.isExpired { break }
                    guard let pageURL = LinkedInGuestAPI.searchURL(
                        query: query, geoID: geoID, location: location,
                        isRemoteLocation: isRemoteLocation, timeFilter: timeFilter,
                        start: start) else { break }

                    // Throttle search requests to avoid 429s.
                    await searchThrottler.wait()
                    guard let html = await Self.fetchWithLinkedInRetries(
                        pageURL, timeout: 30, throttler: nil) else { break }

                    guard let cards = LinkedInGuestAPI.parseSearchPage(html: html) else {
                        // No <li> elements — possible auth redirect or empty page.
                        break
                    }

                    var pageCount = 0
                    for card in cards {
                        if seenIDs.contains(card.externalID) { continue }
                        seenIDs.insert(card.externalID)

                        if JobFilters.matchesExclude(card.title, excludePatterns)
                            || JobFilters.matchesExclude(card.company, excludePatterns) {
                            continue
                        }

                        // Cards from an f_WT=2 (remote-filtered) search are
                        // remote by construction — LinkedIn shows the
                        // employer's location ("United States", "New York,
                        // NY") without the word "remote", so text matching
                        // alone would drop every result of a remote search.
                        let isRemote = isRemoteLocation
                            || card.location.lowercased().contains("remote")
                            || card.title.lowercased().contains("remote")
                        var locationOK = isRemote && hasRemote
                        if !locationOK {
                            locationOK = LinkedInGuestAPI.isLocationMatch(
                                card.location, configLocations: locations)
                        }
                        if !locationOK { continue }

                        pageCount += 1
                        results.append(NormalizedJob(
                            source: "linkedin",
                            externalId: "li-\(card.externalID)",
                            title: card.title,
                            company: card.company,
                            location: card.location,
                            url: card.url,
                            description: "",
                            salaryMin: nil,
                            salaryMax: nil,
                            salaryPeriod: "unknown",
                            tags: [],
                            datePosted: card.datePosted,
                            isRemote: isRemote,
                            isEasyApply: card.isEasyApply
                        ))
                    }

                    if pageCount == 0 { break pageLoop }
                }
            }
        }

        // ----- Phase 2: Fetch detail pages concurrently for descriptions -----
        // Whatever remains of the overall budget, capped at the phase max —
        // overruns return search-phase results (empty descriptions for
        // stragglers) instead of losing everything to the outer timeout.
        // Jobs already in the DB keep their stored description — skip them.
        let toFetch = results.indices.filter { !knownExternalIDs.contains(results[$0].externalId) }
        if !toFetch.isEmpty {
            let detailBudget = TimeBudget(
                seconds: max(30.0, min(Self.detailPhaseBudget, totalBudget.remaining)))
            let throttler = LinkedInThrottler(spacing: Self.detailSpacing, jitter: Self.detailJitter)
            let limiter = AsyncLimiter(Self.detailConcurrency)

            let jobs = results
            let details = await withTaskGroup(of: (Int, LinkedInDetailParser.Detail?).self,
                                              returning: [Int: LinkedInDetailParser.Detail].self) { group in
                for index in toFetch {
                    let url = jobs[index].url
                    group.addTask {
                        await limiter.acquire()
                        var detail: LinkedInDetailParser.Detail?
                        if !detailBudget.isExpired {
                            detail = await Self.fetchJobDetail(
                                urlString: url, throttler: throttler, budget: detailBudget)
                        }
                        await limiter.release()
                        return (index, detail)
                    }
                }
                var collected: [Int: LinkedInDetailParser.Detail] = [:]
                for await (index, detail) in group {
                    if let detail { collected[index] = detail }
                }
                return collected
            }

            for (index, detail) in details {
                Self.apply(detail, to: &results[index])
            }
        }

        return results
    }

    /// Merge a parsed detail page into a search-phase job — the in-place
    /// update half of Python `_fetch_job_detail`.
    static func apply(_ detail: LinkedInDetailParser.Detail, to job: inout NormalizedJob) {
        if let desc = detail.description, !desc.isEmpty { job.description = desc }
        if let sMin = detail.salaryMin { job.salaryMin = sMin }
        if let sMax = detail.salaryMax { job.salaryMax = sMax }
        if let period = detail.salaryPeriod { job.salaryPeriod = period }
        if job.location.isEmpty, let loc = detail.location { job.location = loc }
        if job.tags.isEmpty, !detail.tags.isEmpty { job.tags = detail.tags }
        // Keep an existing true from the search card.
        job.isEasyApply = job.isEasyApply || detail.isEasyApply
    }

    /// Fetch one job's detail page — the network half of `_fetch_job_detail`.
    /// Retries on 429 with exponential backoff shared across all workers via
    /// the throttler; any other failure returns nil.
    static func fetchJobDetail(urlString: String, throttler: LinkedInThrottler,
                               budget: TimeBudget) async -> LinkedInDetailParser.Detail? {
        guard !urlString.isEmpty, let url = URL(string: urlString) else { return nil }
        for attempt in 0...maxRetries {
            await throttler.wait()
            if budget.isExpired { return nil }
            guard let (status, retryAfter, body) = await fetchOnce(url, timeout: 20) else {
                return nil
            }
            if status == 429 {
                let wait = max(retryAfter, retryBase * pow(2, Double(attempt)))
                if attempt < maxRetries {
                    await throttler.backoff(wait)
                    continue
                }
                return nil
            }
            guard status == 200 else { return nil }
            return LinkedInDetailParser.parse(html: body)
        }
        return nil
    }

    /// Search-page fetch with the Python 429 policy: honor Retry-After,
    /// exponential backoff base 5s, 2 retries. Non-200 or network failure
    /// returns nil (the caller stops paginating).
    static func fetchWithLinkedInRetries(_ url: URL, timeout: TimeInterval,
                                         throttler: LinkedInThrottler?) async -> String? {
        for attempt in 0...maxRetries {
            guard let (status, retryAfter, body) = await fetchOnce(url, timeout: timeout) else {
                return nil
            }
            if status == 429 {
                let wait = max(retryAfter, retryBase * pow(2, Double(attempt)))
                if attempt < maxRetries {
                    if let throttler {
                        await throttler.backoff(wait)
                    } else {
                        try? await Task.sleep(nanoseconds: UInt64(wait * 1_000_000_000))
                    }
                    continue
                }
                return nil
            }
            guard status == 200 else { return nil }
            return body
        }
        return nil
    }

    /// One GET with the LinkedIn headers, exposing Retry-After (which
    /// `HTTPClient.Response` doesn't surface). nil on network failure.
    private static func fetchOnce(_ url: URL, timeout: TimeInterval)
        async -> (status: Int, retryAfter: Double, body: String)? {
        var request = URLRequest(url: url, timeoutInterval: timeout)
        for (key, value) in headers { request.setValue(value, forHTTPHeaderField: key) }
        do {
            let (data, urlResponse) = try await HTTPClient.session.data(for: request)
            let http = urlResponse as? HTTPURLResponse
            let retryAfter = Double(http?.value(forHTTPHeaderField: "Retry-After") ?? "") ?? 0
            return (http?.statusCode ?? 0, retryAfter, String(decoding: data, as: UTF8.self))
        } catch {
            return nil
        }
    }
}
