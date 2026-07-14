import Foundation

/// Where a LinkedIn fetch got to, so a later attempt can carry on instead of
/// starting the whole multi-minute scrape again.
///
/// The search phase walks queries × locations × pages in a fixed order, so a
/// position in that walk is three indices. Once the walk is done, `searchDone`
/// flips and the detail phase takes over — and *its* worklist doesn't need to be
/// in the cursor at all, because it is derivable: a LinkedIn job stored with an
/// empty description is exactly a job whose detail page we never scraped.
struct LinkedInCursor: Codable, Sendable, Equatable {
    /// Bumped when the shape changes; a cursor from an older app version is
    /// discarded rather than misread.
    static let currentVersion = 1

    var v = LinkedInCursor.currentVersion
    var queryIndex = 0
    var locationIndex = 0
    var pageStart = 0
    var searchDone = false

    var encoded: String? {
        guard let data = try? JSONEncoder().encode(self) else { return nil }
        return String(data: data, encoding: .utf8)
    }

    /// A cursor we can't read — a malformed string, or one written by a version
    /// whose walk order we no longer match — is no cursor at all. Resuming from
    /// a misread position would silently skip pages; starting over merely costs
    /// time, and the upsert makes the refetch harmless.
    static func decode(_ json: String?) -> LinkedInCursor? {
        guard let json, let data = json.data(using: .utf8),
              let cursor = try? JSONDecoder().decode(LinkedInCursor.self, from: data),
              cursor.v == currentVersion else { return nil }
        return cursor
    }
}

/// LinkedIn public guest job search (no login required) — port of
/// `job_sources/linkedin.py`. Two phases: guest-API search with proper
/// filter parameters, then a detail-page fetch per new job to extract the
/// full description, salary information, and criteria tags. Jobs already in
/// the DB (`knownExternalIDs`) skip the detail fetch — it's the slowest,
/// most 429-prone part of this source.
///
/// This is the one source that cannot finish inside the ~30 seconds iOS grants a
/// backgrounded app, so it checkpoints: every search page is handed to the
/// pipeline the moment it is parsed, along with a cursor. Being cut off then
/// costs the current page, not the run.
public struct LinkedInSource: JobSource {
    public static let id = "linkedin"
    /// The pipeline cancels this source at 600s. Cancellation no longer discards
    /// results — they are checkpointed as they're collected — but the internal
    /// budgets still keep both phases under it so a run can reach a clean end.
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

    /// How many enriched jobs the detail phase delivers per checkpoint. Small
    /// enough that a suspension loses little, large enough not to write on every
    /// single page fetch.
    static let detailCheckpointSize = 5

    /// Desktop Chrome headers, no cookies (the shared preset matches the
    /// Python header dict exactly).
    static var headers: [String: String] { HTTPClient.browserHeaders }

    private let database: AppDatabase?

    /// - Parameter database: backs the persistent geoId cache and the resumed
    ///   detail worklist; nil falls back to in-memory-only resolution (plus the
    ///   seed dict) and to whatever this attempt collected itself.
    public init(database: AppDatabase? = nil) {
        self.database = database ?? (try? AppDatabase.shared())
    }

    public func fetchJobs(config: AppConfig, knownExternalIDs: Set<String>) async throws -> [NormalizedJob] {
        try await fetchJobs(config: config, knownExternalIDs: knownExternalIDs,
                            resumeCursor: nil, onCheckpoint: { _, _ in })
    }

    public func fetchJobs(config: AppConfig, knownExternalIDs: Set<String>,
                          resumeCursor: String?,
                          onCheckpoint: @escaping SourceCheckpoint) async throws -> [NormalizedJob] {
        let keywords = config.search.keywords
        guard !keywords.isEmpty else { return [] }
        let locations = config.search.locations.isEmpty ? [""] : config.search.locations
        let excludePatterns = JobFilters.compileExcludes(config.search.excludeKeywords)
        let maxAgeDays = config.search.maxAgeDays ?? 7

        let timeFilter = LinkedInGuestAPI.timeFilter(maxAgeDays: maxAgeDays)
        let hasRemote = locations.contains {
            $0.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() == "remote"
        }

        // Batched boolean-OR queries instead of one search per keyword. The order
        // of this walk is the cursor's coordinate system, so it must be
        // deterministic — `batchKeywords` and the configured locations both
        // preserve input order.
        let queries = LinkedInGuestAPI.batchKeywords(keywords)
        let resolver = GeoIDResolver(database: database)

        var cursor = LinkedInCursor.decode(resumeCursor) ?? LinkedInCursor()

        let totalBudget = TimeBudget(seconds: Self.totalBudget)
        let searchBudget = TimeBudget(seconds: Self.searchPhaseBudget)
        let searchThrottler = LinkedInThrottler(spacing: Self.searchDelay)

        var results: [NormalizedJob] = []
        var seenIDs = Set<String>()
        // Track (query, geoId) pairs already searched — multiple configured
        // locations can map to the same LinkedIn geoId ("Colorado"/"CO").
        var seenQueryGeo = Set<String>()

        // ----- Phase 1: Collect job cards from search results -----
        // Skipped wholesale when a previous attempt already finished the walk.
        if !cursor.searchDone {
            queryLoop: for (queryIndex, query) in queries.enumerated() {
                if queryIndex < cursor.queryIndex { continue }
                if searchBudget.isExpired { break }

                for (locationIndex, location) in locations.enumerated() {
                    // Only the query we resumed *into* skips locations; later
                    // queries start from the top.
                    if queryIndex == cursor.queryIndex && locationIndex < cursor.locationIndex { continue }
                    if searchBudget.isExpired { break queryLoop }

                    let locNormalized = GeoIDResolver.normalize(location)
                    let geoID = await resolver.resolve(location, headers: Self.headers)

                    let comboKey = query + "\u{1F}" + (geoID.isEmpty ? locNormalized : geoID)
                    if !seenQueryGeo.insert(comboKey).inserted { continue }

                    let isRemoteLocation = locNormalized == "remote"

                    // Resume mid-pagination only for the exact (query, location)
                    // the cursor names; every other pair starts at page 0.
                    let firstPage = (queryIndex == cursor.queryIndex
                                     && locationIndex == cursor.locationIndex) ? cursor.pageStart : 0

                    // 4 pages per query — OR-batched queries aggregate several
                    // keywords, so go one page deeper than per-keyword search did.
                    pageLoop: for start in stride(from: firstPage, to: 100, by: 25) {
                        if searchBudget.isExpired { break }
                        guard let pageURL = LinkedInGuestAPI.searchURL(
                            query: query, geoID: geoID, location: location,
                            isRemoteLocation: isRemoteLocation, timeFilter: timeFilter,
                            start: start) else { break }

                        // Throttle search requests to avoid 429s.
                        await searchThrottler.wait()
                        guard let html = try await Self.fetchWithLinkedInRetries(
                            pageURL, timeout: 30, throttler: nil) else { break }

                        guard let cards = LinkedInGuestAPI.parseSearchPage(html: html) else {
                            // No <li> elements — possible auth redirect or empty page.
                            break
                        }

                        var page: [NormalizedJob] = []
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

                            page.append(NormalizedJob(
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

                        results += page
                        // Hand the page over before fetching the next one: from
                        // here on, being cut off costs at most this page.
                        cursor.queryIndex = queryIndex
                        cursor.locationIndex = locationIndex
                        cursor.pageStart = start + 25
                        await onCheckpoint(page, cursor.encoded)

                        if page.isEmpty { break pageLoop }
                    }

                    // This (query, location) is exhausted — next one starts fresh.
                    cursor.queryIndex = queryIndex
                    cursor.locationIndex = locationIndex + 1
                    cursor.pageStart = 0
                }

                cursor.queryIndex = queryIndex + 1
                cursor.locationIndex = 0
                cursor.pageStart = 0
            }

            // The walk finished (or its budget ran out, which for resume purposes
            // is the same thing: don't re-walk it, get on with the details).
            cursor.searchDone = true
            await onCheckpoint([], cursor.encoded)
        }

        // ----- Phase 2: Fetch detail pages concurrently for descriptions -----
        // The worklist is every LinkedIn job we hold without a description: the
        // ones this attempt just collected, plus any left description-less by an
        // earlier attempt that was cut short. `knownExternalIDs` (jobs whose
        // description we already have) is what keeps this from re-scraping the
        // whole board every run.
        var pending = results.filter { !knownExternalIDs.contains($0.externalId) }
        if let database {
            let stored = (try? JobStore(database).jobsNeedingDescription(source: "linkedin")) ?? []
            let have = Set(pending.map(\.externalId))
            pending += stored.filter { !have.contains($0.externalId) && !knownExternalIDs.contains($0.externalId) }
        }

        if !pending.isEmpty {
            // Whatever remains of the overall budget, capped at the phase max.
            let detailBudget = TimeBudget(
                seconds: max(30.0, min(Self.detailPhaseBudget, totalBudget.remaining)))
            let throttler = LinkedInThrottler(spacing: Self.detailSpacing, jitter: Self.detailJitter)
            let limiter = AsyncLimiter(Self.detailConcurrency)

            let jobs = pending
            let details = await withTaskGroup(of: (Int, LinkedInDetailParser.Detail?).self,
                                              returning: [Int: LinkedInDetailParser.Detail].self) { group in
                for index in jobs.indices {
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
                var batch: [NormalizedJob] = []
                for await (index, detail) in group {
                    guard let detail else { continue }
                    collected[index] = detail
                    var enriched = jobs[index]
                    Self.apply(detail, to: &enriched)
                    batch.append(enriched)
                    // Deliver enriched jobs in small batches rather than holding
                    // them all to the end, where a suspension would lose them.
                    if batch.count >= Self.detailCheckpointSize {
                        await onCheckpoint(batch, cursor.encoded)
                        batch.removeAll()
                    }
                }
                if !batch.isEmpty { await onCheckpoint(batch, cursor.encoded) }
                return collected
            }

            // Merge back into this attempt's own results, which are what the
            // return value promises. Jobs pulled from the database belong to an
            // earlier attempt and were already delivered by the checkpoint above.
            var indexByID: [String: Int] = [:]
            for (i, job) in results.enumerated() { indexByID[job.externalId] = i }
            for (index, detail) in details {
                if let target = indexByID[jobs[index].externalId] {
                    Self.apply(detail, to: &results[target])
                }
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
    ///
    /// Unlike the search phase this never throws: a detail page we couldn't get
    /// leaves the job stored with an empty description, which is precisely the
    /// marker that puts it back on the worklist next run.
    static func fetchJobDetail(urlString: String, throttler: LinkedInThrottler,
                               budget: TimeBudget) async -> LinkedInDetailParser.Detail? {
        guard !urlString.isEmpty, let url = URL(string: urlString) else { return nil }
        for attempt in 0...maxRetries {
            await throttler.wait()
            if budget.isExpired { return nil }
            guard let (status, retryAfter, body) = try? await fetchOnce(url, timeout: 20) else {
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
    /// exponential backoff base 5s, 2 retries.
    ///
    /// Returns nil when the page is genuinely unusable (non-200, exhausted
    /// retries) — the caller stops paginating, as before. But it *throws*
    /// `SourceInterruptedError` when the request died for a reason a retry won't
    /// reproduce: the app being suspended, the network dropping. Collapsing that
    /// into nil, as this used to, made a killed socket indistinguishable from
    /// "LinkedIn has no more results" and silently truncated the search.
    static func fetchWithLinkedInRetries(_ url: URL, timeout: TimeInterval,
                                         throttler: LinkedInThrottler?) async throws -> String? {
        for attempt in 0...maxRetries {
            guard let (status, retryAfter, body) = try await fetchOnce(url, timeout: timeout) else {
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
    /// `HTTPClient.Response` doesn't surface).
    ///
    /// Throws `SourceInterruptedError` on a transient failure (suspension,
    /// cancellation, lost connection); returns nil on a fatal one.
    private static func fetchOnce(_ url: URL, timeout: TimeInterval)
        async throws -> (status: Int, retryAfter: Double, body: String)? {
        var request = URLRequest(url: url, timeoutInterval: timeout)
        for (key, value) in headers { request.setValue(value, forHTTPHeaderField: key) }
        do {
            let (data, urlResponse) = try await HTTPClient.session.data(for: request)
            let http = urlResponse as? HTTPURLResponse
            let retryAfter = Double(http?.value(forHTTPHeaderField: "Retry-After") ?? "") ?? 0
            return (http?.statusCode ?? 0, retryAfter, String(decoding: data, as: UTF8.self))
        } catch {
            if TransientNetwork.isTransient(error) {
                throw SourceInterruptedError("linkedin: \(error.localizedDescription)")
            }
            return nil
        }
    }
}
