import XCTest
@testable import JobsmithKit

// MARK: - Transient vs. fatal

final class TransientNetworkTests: XCTestCase {
    /// The whole resume architecture hangs off this call: a transient error parks
    /// a run and retries it, a fatal one stops and tells the user.
    func testTransientCodes() {
        for code: URLError.Code in [.cancelled, .networkConnectionLost, .timedOut,
                                    .notConnectedToInternet, .dataNotAllowed] {
            XCTAssertTrue(TransientNetwork.isTransient(URLError(code)),
                          "\(code) should be transient")
        }
        XCTAssertTrue(TransientNetwork.isTransient(CancellationError()))
        XCTAssertTrue(TransientNetwork.isTransient(SourceInterruptedError("x")))
        XCTAssertTrue(TransientNetwork.isTransient(AIEngineError.interrupted("x")))
        XCTAssertTrue(TransientNetwork.isTransient(ScoringError.interrupted("x")))
    }

    /// A refused connection or a bad host fails identically on every retry —
    /// resuming forever would just hide a broken endpoint from the user.
    func testFatalCodes() {
        for code: URLError.Code in [.cannotConnectToHost, .cannotFindHost, .badURL,
                                    .userAuthenticationRequired, .badServerResponse] {
            XCTAssertFalse(TransientNetwork.isTransient(URLError(code)),
                           "\(code) should be fatal")
        }
        XCTAssertFalse(TransientNetwork.isTransient(AIEngineError.unreachable("refused")))
        XCTAssertFalse(TransientNetwork.isTransient(AIEngineError.emptyResponse))
        XCTAssertFalse(TransientNetwork.isTransient(SourceBlockedError("blocked")))
    }
}

// MARK: - Incremental dedup

final class IncrementalDeduplicatorTests: XCTestCase {
    private func job(_ source: String, _ id: String, title: String = "Engineer",
                     company: String = "Acme", url: String = "", description: String = "") -> NormalizedJob {
        NormalizedJob(source: source, externalId: id, title: title, company: company,
                      location: "Denver, CO", url: url, description: description)
    }

    /// The reason this type exists: LinkedIn hands over a posting twice — once
    /// from the search page with no description, again once the detail page has
    /// been scraped. The second copy must not be mistaken for a duplicate, or the
    /// description never lands.
    func testSameSourceRedeliveryIsAdmitted() {
        var dedup = IncrementalDeduplicator()
        let bare = job("linkedin", "li-1", url: "https://li/1")
        let enriched = job("linkedin", "li-1", url: "https://li/1", description: "Full text")

        XCTAssertEqual(dedup.admit([bare]).count, 1)
        let second = dedup.admit([enriched])
        XCTAssertEqual(second.count, 1, "the enriched re-delivery must survive")
        XCTAssertEqual(second.first?.description, "Full text")
    }

    /// Cross-source duplicates are still collapsed — that behavior is unchanged.
    func testCrossSourceDuplicateIsDroppedByURL() {
        var dedup = IncrementalDeduplicator()
        XCTAssertEqual(dedup.admit([job("greenhouse", "g-1", url: "https://same/1")]).count, 1)
        XCTAssertTrue(dedup.admit([job("linkedin", "li-9", url: "https://same/1")]).isEmpty)
    }

    func testCrossSourceDuplicateIsDroppedByIdentity() {
        var dedup = IncrementalDeduplicator()
        XCTAssertEqual(dedup.admit([job("greenhouse", "g-1", url: "https://a")]).count, 1)
        // Different URL, same (title, company, location) — the identity pass.
        XCTAssertTrue(dedup.admit([job("linkedin", "li-9", url: "https://b")]).isEmpty)
    }

    /// Matches `Deduplicator.dedupe` for a single batch, which is the contract
    /// the pooled version used to provide.
    func testAgreesWithPooledDedupeOnOneBatch() {
        let batch = [job("a", "1", url: "https://x"),
                     job("b", "2", url: "https://x"),
                     job("c", "3", title: "Other", url: "https://y")]
        var dedup = IncrementalDeduplicator()
        XCTAssertEqual(dedup.admit(batch).count, Deduplicator.dedupe(batch).count)
    }
}

// MARK: - Run record

final class SearchRunStoreTests: XCTestCase {
    func testRemainingSourcesShrinkAsTheyComplete() throws {
        let store = SearchRunStore(try AppDatabase.inMemory())
        let run = try store.begin(sources: ["remoteok", "linkedin", "greenhouse"])
        XCTAssertEqual(run.remainingSources, ["remoteok", "linkedin", "greenhouse"])

        try store.markSourceComplete(id: run.id, source: "remoteok")
        try store.markSourceComplete(id: run.id, source: "greenhouse")

        let reloaded = try XCTUnwrap(store.run(id: run.id))
        XCTAssertEqual(reloaded.remainingSources, ["linkedin"],
                       "only the source that never finished is still owed")
        XCTAssertFalse(reloaded.isFinished)
    }

    func testCursorRoundTrip() throws {
        let store = SearchRunStore(try AppDatabase.inMemory())
        let run = try store.begin(sources: ["linkedin"])
        try store.saveCursor(id: run.id, source: "linkedin", cursor: #"{"v":1,"pageStart":50}"#)

        let reloaded = try XCTUnwrap(store.run(id: run.id))
        XCTAssertEqual(reloaded.cursors["linkedin"], #"{"v":1,"pageStart":50}"#)
    }

    /// A run spans attempts, and each attempt counts only its own inserts. The
    /// resumed attempt that merely backfills descriptions inserts nothing new —
    /// assigning rather than accumulating here would reset the run's total to
    /// zero and report "0 new jobs" for a search that found forty.
    func testInsertedTotalAccumulatesAcrossAttempts() throws {
        let store = SearchRunStore(try AppDatabase.inMemory())
        let run = try store.begin(sources: ["linkedin"])

        try store.addInserted(id: run.id, delta: 25)   // first attempt, page one
        try store.addInserted(id: run.id, delta: 15)   // first attempt, page two
        try store.addInserted(id: run.id, delta: 0)    // resumed attempt: details only

        XCTAssertEqual(try store.run(id: run.id)?.insertedSoFar, 40)
    }

    func testActiveRunReturnsAnInterruptedRun() throws {
        let store = SearchRunStore(try AppDatabase.inMemory())
        let run = try store.begin(sources: ["linkedin", "remoteok"])
        try store.markSourceComplete(id: run.id, source: "remoteok")
        try store.setState(id: run.id, .interrupted)

        let active = try XCTUnwrap(store.activeRun())
        XCTAssertEqual(active.id, run.id)
        XCTAssertEqual(active.remainingSources, ["linkedin"])
    }

    /// A run with nothing left to do is not something to resume — otherwise every
    /// foreground return would kick off a pointless empty pipeline pass.
    func testFinishedRunIsNotActive() throws {
        let store = SearchRunStore(try AppDatabase.inMemory())
        let run = try store.begin(sources: ["remoteok"])
        try store.markSourceComplete(id: run.id, source: "remoteok")
        try store.setState(id: run.id, .interrupted)
        XCTAssertNil(try store.activeRun())
    }

    /// Yesterday's cursor points into a search page that has since moved, and its
    /// listings are stale. Starting over is both cheaper and more correct.
    func testStaleRunIsAbandoned() throws {
        let db = try AppDatabase.inMemory()
        let store = SearchRunStore(db)
        let run = try store.begin(sources: ["linkedin"])
        let old = ISO8601DateFormatter().string(
            from: Date(timeIntervalSinceNow: -(SearchRunStore.maxAge + 60)))
        try db.writer.write {
            try $0.execute(sql: "UPDATE search_runs SET state = 'interrupted', startedAt = ? WHERE id = ?",
                           arguments: [old, run.id])
        }
        XCTAssertNil(try store.activeRun())
    }

    /// Tapping Search is an instruction to search *now*. The old run's results are
    /// already saved; only its unfinished tail is dropped, and the new run covers
    /// that anyway.
    func testBeginSupersedesAnUnfinishedRun() throws {
        let store = SearchRunStore(try AppDatabase.inMemory())
        let first = try store.begin(sources: ["linkedin"])
        try store.setState(id: first.id, .interrupted)

        let second = try store.begin(sources: ["remoteok"])
        let active = try XCTUnwrap(store.activeRun())
        XCTAssertEqual(active.id, second.id)
        XCTAssertEqual(try store.run(id: first.id)?.state, .complete)
    }
}

// MARK: - LinkedIn cursor

final class LinkedInCursorTests: XCTestCase {
    func testRoundTrip() throws {
        let cursor = LinkedInCursor(v: 1, queryIndex: 2, locationIndex: 1,
                                    pageStart: 50, searchDone: false)
        let decoded = try XCTUnwrap(LinkedInCursor.decode(cursor.encoded))
        XCTAssertEqual(decoded, cursor)
    }

    /// A cursor we can't trust must be discarded, not guessed at: resuming from a
    /// misread position would silently skip pages, while starting over merely
    /// costs time.
    func testWrongVersionIsRejected() {
        XCTAssertNil(LinkedInCursor.decode(#"{"v":99,"queryIndex":3,"pageStart":75}"#))
    }

    func testGarbageIsRejected() {
        XCTAssertNil(LinkedInCursor.decode("not json"))
        XCTAssertNil(LinkedInCursor.decode(nil))
    }
}

// MARK: - Deterministic walk order

final class LinkedInWalkOrderTests: XCTestCase {
    /// The cursor is three indices into a queries × locations × pages walk. If
    /// that walk isn't reproducible, a resumed run skips or repeats pages — so
    /// the order of the batched queries is load-bearing, not incidental.
    func testKeywordBatchingIsOrderPreservingAndStable() {
        let keywords = ["security engineer", "soc analyst", "devops", "sre", "platform engineer"]
        let first = LinkedInGuestAPI.batchKeywords(keywords)
        XCTAssertEqual(first, LinkedInGuestAPI.batchKeywords(keywords),
                       "same input must produce the same walk, run to run")
        // The first batch must lead with the first keyword: indices from an
        // earlier attempt have to still mean the same thing.
        XCTAssertTrue(first.first?.contains("security engineer") == true)
    }
}

// MARK: - Detail-phase worklist

final class DetailWorklistTests: XCTestCase {
    private func linkedInJob(_ id: String, description: String) -> NormalizedJob {
        NormalizedJob(source: "linkedin", externalId: id, title: "Engineer \(id)",
                      company: "Acme", location: "Remote",
                      url: "https://linkedin.com/jobs/view/\(id)",
                      description: description, isRemote: true)
    }

    /// The bug this prevents: using plain `knownExternalIDs` to skip detail
    /// fetches would strand a job whose detail fetch was cut short — it's
    /// "known", so every later run skips it, and its description stays empty
    /// forever. Only a job we actually *have* the description for may be skipped.
    func testOnlyJobsWithDescriptionsCountAsKnown() throws {
        let db = try AppDatabase.inMemory()
        let store = JobStore(db)
        try store.upsert([linkedInJob("li-1", description: "Full description"),
                          linkedInJob("li-2", description: "")])

        XCTAssertEqual(try store.externalIDsWithDescription(source: "linkedin"), ["li-1"])
        XCTAssertEqual(try store.knownExternalIDs(source: "linkedin"), ["li-1", "li-2"],
                       "both are 'known' — which is exactly why that set is the wrong one to skip on")
    }

    /// A resumed run rebuilds its detail worklist from the database rather than
    /// re-running the search phase to reconstruct it.
    func testJobsNeedingDescriptionAreTheLeftovers() throws {
        let db = try AppDatabase.inMemory()
        let store = JobStore(db)
        try store.upsert([linkedInJob("li-1", description: "Full description"),
                          linkedInJob("li-2", description: ""),
                          linkedInJob("li-3", description: "")])

        let pending = try store.jobsNeedingDescription(source: "linkedin")
        XCTAssertEqual(Set(pending.map(\.externalId)), ["li-2", "li-3"])
        XCTAssertEqual(pending.first?.source, "linkedin")
        XCTAssertTrue(pending.allSatisfy { !$0.url.isEmpty }, "URLs survive the round trip")
    }

    /// A job whose detail page has failed `maxAttempts` times is written off:
    /// retrying it every run forever is how a dead backlog ate the whole
    /// detail budget. Least-tried jobs come first, so retries rotate through
    /// the backlog instead of hammering the same few.
    func testWorklistWritesOffRepeatedFailuresAndOrdersByAttempts() throws {
        let db = try AppDatabase.inMemory()
        let store = JobStore(db)
        try store.upsert([linkedInJob("li-1", description: ""),
                          linkedInJob("li-2", description: ""),
                          linkedInJob("li-3", description: "")])

        try store.recordDetailAttempts(source: "linkedin", externalIds: ["li-1", "li-2", "li-1"])
        try store.recordDetailAttempts(source: "linkedin", externalIds: ["li-1"])

        let pending = try store.jobsNeedingDescription(source: "linkedin", maxAttempts: 3)
        XCTAssertEqual(pending.map(\.externalId), ["li-3", "li-2"],
                       "li-1 hit the cap and is written off; the untried job goes first")

        let capped = try store.jobsNeedingDescription(source: "linkedin", maxAttempts: 3, limit: 1)
        XCTAssertEqual(capped.map(\.externalId), ["li-3"], "limit bounds one run's worklist")
    }
}

// MARK: - LinkedIn checkpointing over a stubbed network

/// Serves LinkedIn search pages and detail pages, recording every URL it saw so a
/// test can assert which pages a resumed run did — and didn't — refetch.
final class LinkedInStubProtocol: URLProtocol {
    nonisolated(unsafe) static var requested: [String] = []
    nonisolated(unsafe) static var cardsPerPage = 2
    /// Pages at or beyond this `start` come back empty, ending pagination.
    nonisolated(unsafe) static var emptyFromStart = 50
    /// Overrides the detail-page body — set to something without the guest
    /// markup to simulate LinkedIn serving the logged-in SPA shell.
    nonisolated(unsafe) static var detailBody: String?
    /// Set when any request arrived carrying a Cookie header.
    nonisolated(unsafe) static var sawCookieHeader = false
    private static let lock = NSLock()

    static func reset() {
        lock.lock(); requested = []; lock.unlock()
        cardsPerPage = 2
        emptyFromStart = 50
        detailBody = nil
        sawCookieHeader = false
    }

    static func record(_ url: String) {
        lock.lock(); requested.append(url); lock.unlock()
    }

    static var searchStarts: [Int] {
        lock.lock(); defer { lock.unlock() }
        return requested.compactMap { url in
            guard url.contains("search"),
                  let comps = URLComponents(string: url),
                  let start = comps.queryItems?.first(where: { $0.name == "start" })?.value else { return nil }
            return Int(start)
        }
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let url = request.url!
        Self.record(url.absoluteString)
        if request.value(forHTTPHeaderField: "Cookie") != nil {
            Self.sawCookieHeader = true
        }

        let body: String
        if url.absoluteString.contains("search") {
            let comps = URLComponents(url: url, resolvingAgainstBaseURL: false)
            let start = Int(comps?.queryItems?.first { $0.name == "start" }?.value ?? "0") ?? 0
            body = start >= Self.emptyFromStart ? "<ul></ul>" : Self.searchHTML(start: start)
        } else {
            body = Self.detailBody ?? Self.detailHTML()
        }

        let response = HTTPURLResponse(url: url, statusCode: 200,
                                       httpVersion: "HTTP/1.1", headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(body.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    /// Posted today — otherwise the pipeline's `maxAgeDays` filter drops every
    /// card before it reaches the database, and these tests would be asserting
    /// on an empty store for the wrong reason.
    static var postedToday: String {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter.string(from: Date())
    }

    /// Cards whose ids encode their page, so a test can tell which page a job
    /// came from.
    static func searchHTML(start: Int) -> String {
        let items = (0..<cardsPerPage).map { index -> String in
            let id = 3900000000 + start + index
            return """
            <li>
              <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/engineer-at-acme-\(id)?trk=x"></a>
              <h3 class="base-search-card__title">Senior Engineer \(id)</h3>
              <h4 class="base-search-card__subtitle">Acme Corp</h4>
              <span class="job-search-card__location">Remote</span>
              <time datetime="\(postedToday)"></time>
            </li>
            """
        }
        return "<ul>\(items.joined())</ul>"
    }

    static func detailHTML() -> String {
        """
        <div class="show-more-less-html__markup">Detailed description of the role.</div>
        """
    }
}

final class LinkedInCheckpointTests: XCTestCase {
    private var realSession: URLSession!

    override func setUp() {
        super.setUp()
        LinkedInStubProtocol.reset()
        realSession = HTTPClient.session
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [LinkedInStubProtocol.self]
        HTTPClient.session = URLSession(configuration: config)
    }

    override func tearDown() {
        HTTPClient.session = realSession
        LinkedInStubProtocol.reset()
        super.tearDown()
    }

    private func config() -> AppConfig {
        var config = AppConfig()
        config.search.keywords = ["engineer"]
        config.search.locations = ["Remote"]
        config.search.maxAgeDays = 7
        config.search.linkedInEnabled = true  // opt-in default is now off
        return config
    }

    /// The heart of Phase 1: jobs reach the caller *as each search page is
    /// parsed*, not only when the whole multi-minute scrape finishes. Being cut
    /// off after this point costs the current page, not the run.
    func testCheckpointFiresPerSearchPage() async throws {
        let db = try AppDatabase.inMemory()
        let source = LinkedInSource(database: db)

        actor Recorder {
            var batches: [[NormalizedJob]] = []
            var cursors: [String?] = []
            func add(_ jobs: [NormalizedJob], _ cursor: String?) {
                batches.append(jobs); cursors.append(cursor)
            }
        }
        let recorder = Recorder()

        _ = try await source.fetchJobs(config: config(), knownExternalIDs: [],
                                       resumeCursor: nil) { jobs, cursor in
            await recorder.add(jobs, cursor)
        }

        let batches = await recorder.batches
        let searchBatches = batches.prefix(2)
        XCTAssertGreaterThanOrEqual(batches.count, 2,
                                    "one checkpoint per search page, at minimum")
        XCTAssertTrue(searchBatches.allSatisfy { !$0.isEmpty },
                      "each search page hands over its cards immediately")

        // Every checkpoint carries a cursor the pipeline can store.
        let cursors = await recorder.cursors
        XCTAssertTrue(cursors.allSatisfy { LinkedInCursor.decode($0) != nil })
    }

    /// A resumed run must not re-walk pages an earlier attempt already delivered.
    func testResumeSkipsCompletedPages() async throws {
        let db = try AppDatabase.inMemory()
        let source = LinkedInSource(database: db)

        // Pretend the first page (start=0) is already done: resume at start=25.
        let cursor = LinkedInCursor(queryIndex: 0, locationIndex: 0, pageStart: 25,
                                    searchDone: false)
        _ = try await source.fetchJobs(config: config(), knownExternalIDs: [],
                                       resumeCursor: cursor.encoded) { _, _ in }

        let starts = LinkedInStubProtocol.searchStarts
        XCTAssertFalse(starts.contains(0), "page 0 was already collected — refetching it wastes the window")
        XCTAssertTrue(starts.contains(25), "the run picks up exactly where it left off")
    }

    /// With the search walk already finished, a resumed run goes straight to the
    /// detail phase and enriches the jobs left description-less — reading its
    /// worklist from the database rather than re-running the search.
    func testResumeWithSearchDoneOnlyFetchesDetails() async throws {
        let db = try AppDatabase.inMemory()
        let store = JobStore(db)
        try store.upsert([NormalizedJob(source: "linkedin", externalId: "li-3900000001",
                                        title: "Senior Engineer", company: "Acme",
                                        location: "Remote",
                                        url: "https://www.linkedin.com/jobs/view/engineer-at-acme-3900000001",
                                        description: "", isRemote: true)])

        let source = LinkedInSource(database: db)
        let cursor = LinkedInCursor(searchDone: true)

        actor Box { var jobs: [NormalizedJob] = []
                    func add(_ j: [NormalizedJob]) { jobs += j } }
        let box = Box()

        _ = try await source.fetchJobs(config: config(), knownExternalIDs: [],
                                       resumeCursor: cursor.encoded) { jobs, _ in
            await box.add(jobs)
        }

        XCTAssertTrue(LinkedInStubProtocol.searchStarts.isEmpty,
                      "the search walk was already done — don't do it again")
        let delivered = await box.jobs
        let enriched = delivered.first { $0.externalId == "li-3900000001" }
        XCTAssertEqual(enriched?.description, "Detailed description of the role.",
                       "the leftover job gets its description on the resumed run")
    }
}

// MARK: - Guest-only fetching and the blocked-detail breaker

/// LinkedIn answers cookie-bearing guest requests with redirect loops, 429s,
/// or the logged-in SPA shell (observed 2026-07-15: 0/102 descriptions).
/// These tests pin the two defenses: the session cookie never rides along,
/// and a detail phase that yields nothing trips a breaker instead of
/// grinding out its whole budget.
final class LinkedInGuestModeTests: XCTestCase {
    private var realSession: URLSession!

    override func setUp() {
        super.setUp()
        LinkedInStubProtocol.reset()
        realSession = HTTPClient.session
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [LinkedInStubProtocol.self]
        HTTPClient.session = URLSession(configuration: config)
    }

    override func tearDown() {
        HTTPClient.session = realSession
        LinkedInStubProtocol.reset()
        super.tearDown()
    }

    private func config() -> AppConfig {
        var config = AppConfig()
        config.search.keywords = ["engineer"]
        config.search.locations = ["Remote"]
        config.search.maxAgeDays = 7
        config.search.linkedInEnabled = true  // opt-in default is now off
        return config
    }

    private func linkedInJob(_ id: String) -> NormalizedJob {
        NormalizedJob(source: "linkedin", externalId: id, title: "Engineer \(id)",
                      company: "Acme", location: "Remote",
                      url: "https://www.linkedin.com/jobs/view/engineer-at-acme-\(id)",
                      description: "", isRemote: true)
    }

    /// Even a signed-in user is fetched as a guest: the cookie flips LinkedIn
    /// into blocking/SPA behavior on exactly these endpoints, and it would put
    /// the user's own account in front of LinkedIn's automation detection.
    func testSessionCookieNeverSentToGuestEndpoints() async throws {
        var config = config()
        config.apiKeys.linkedInCookie = "AQEDAtestcookievalue"

        let source = LinkedInSource(database: try AppDatabase.inMemory())
        _ = try await source.fetchJobs(config: config, knownExternalIDs: [],
                                       resumeCursor: nil) { _, _ in }

        XCTAssertFalse(LinkedInStubProtocol.requested.isEmpty, "the fetch actually ran")
        XCTAssertFalse(LinkedInStubProtocol.sawCookieHeader,
                       "no request may carry the user's session")
    }

    /// When every early detail page comes back description-less (SPA shell,
    /// bot wall, markup change), the run aborts as blocked instead of failing
    /// identically through minutes of remaining worklist — and the probed jobs
    /// are counted against their retry cap.
    func testDetailPhaseTripsBreakerWhenNothingYieldsDescriptions() async throws {
        let db = try AppDatabase.inMemory()
        let store = JobStore(db)
        try store.upsert((1...6).map { linkedInJob("li-390000000\($0)") })
        LinkedInStubProtocol.detailBody = "<html><body><div id=\"app\"></div></body></html>"

        let source = LinkedInSource(database: db)
        let cursor = LinkedInCursor(searchDone: true)
        do {
            _ = try await source.fetchJobs(config: config(), knownExternalIDs: [],
                                           resumeCursor: cursor.encoded) { _, _ in }
            XCTFail("expected SourceBlockedError")
        } catch is SourceBlockedError {
            // expected
        }

        let unprobed = try store.jobsNeedingDescription(source: "linkedin", maxAttempts: 1)
        XCTAssertEqual(unprobed.count, 1,
                       "the five probes were recorded; only the sixth job is still untried")
    }
}

// MARK: - Pipeline: incremental persistence and interruption

/// Drives the real pipeline over the LinkedIn stub, which is the only registered
/// source that runs long enough to be cut in half.
final class PipelineInterruptionTests: XCTestCase {
    private var realSession: URLSession!

    override func setUp() {
        super.setUp()
        LinkedInStubProtocol.reset()
        realSession = HTTPClient.session
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [LinkedInStubProtocol.self]
        HTTPClient.session = URLSession(configuration: config)
    }

    override func tearDown() {
        HTTPClient.session = realSession
        LinkedInStubProtocol.reset()
        super.tearDown()
    }

    private func config() -> AppConfig {
        var config = AppConfig()
        config.search.keywords = ["engineer"]
        config.search.locations = ["Remote"]
        config.search.maxAgeDays = 7
        config.search.linkedInEnabled = true  // opt-in default is now off
        return config
    }

    /// The single most important guarantee of the whole change.
    ///
    /// The old pipeline pooled every source's jobs and upserted once, at the very
    /// end — so a run killed partway (which is what iOS does to a backgrounded
    /// app after ~30 seconds) committed *nothing*, and LinkedIn's minutes of
    /// scraping were thrown away. Now each page is committed as it lands, so the
    /// same kill leaves the collected jobs in the database.
    func testCancelledRunKeepsTheJobsItAlreadyCollected() async throws {
        let db = try AppDatabase.inMemory()
        let store = JobStore(db)
        let pipeline = FetchPipeline()

        let work = Task {
            await pipeline.run(config: config(), sources: ["linkedin"], jobStore: store)
        }
        // The first search page is delivered straight away; the next is a 1.5s
        // throttle behind it. Cut the run off in between.
        try await Task.sleep(for: .milliseconds(600))
        work.cancel()
        let summary = await work.value

        let stored = try store.jobs()
        XCTAssertFalse(stored.isEmpty,
                       "jobs checkpointed before the cancellation must survive it")
        XCTAssertTrue(stored.allSatisfy { $0.source == "linkedin" })

        // And it reports as unfinished work, not as a failure.
        XCTAssertEqual(summary.interrupted, ["linkedin"])
        XCTAssertTrue(summary.failed.isEmpty, "cancellation is not failure")
        XCTAssertTrue(summary.isIncomplete)
    }

    /// An interrupted source stays on the run's books so the resume picks it back
    /// up, and its cursor is bookmarked. This is what a resumed attempt reads.
    func testInterruptedSourceStaysOnTheRunWithACursor() async throws {
        let db = try AppDatabase.inMemory()
        let runStore = SearchRunStore(db)
        let run = try runStore.begin(sources: ["linkedin"])

        let work = Task {
            await FetchPipeline().run(config: config(), sources: ["linkedin"],
                                      jobStore: JobStore(db), runStore: runStore,
                                      runID: run.id)
        }
        try await Task.sleep(for: .milliseconds(600))
        work.cancel()
        _ = await work.value

        let reloaded = try XCTUnwrap(runStore.run(id: run.id))
        XCTAssertEqual(reloaded.remainingSources, ["linkedin"],
                       "a source cut off mid-fetch is still owed")
        XCTAssertFalse(reloaded.isFinished)
        let cursor = try XCTUnwrap(LinkedInCursor.decode(reloaded.cursors["linkedin"]))
        XCTAssertGreaterThan(cursor.pageStart, 0, "it bookmarked how far it got")
    }

    /// A source that reaches a terminal state is struck off, so a resume doesn't
    /// re-run it. Greenhouse with no boards configured returns [] without
    /// touching the network — a clean, terminal, zero-job finish.
    func testFinishedSourceIsStruckOffTheRun() async throws {
        let db = try AppDatabase.inMemory()
        let runStore = SearchRunStore(db)
        let run = try runStore.begin(sources: ["greenhouse"])

        _ = await FetchPipeline().run(config: AppConfig(), sources: ["greenhouse"],
                                      jobStore: JobStore(db), runStore: runStore,
                                      runID: run.id)

        let reloaded = try XCTUnwrap(runStore.run(id: run.id))
        XCTAssertEqual(reloaded.completedSources, ["greenhouse"])
        XCTAssertTrue(reloaded.isFinished, "a source that reached the end is not resumed")
    }

    /// The cross-source dedup that the pooled version did in one pass still holds
    /// when jobs arrive a page at a time — and LinkedIn's own re-delivery of a
    /// job (search card first, then the detail page's description) lands as an
    /// update rather than being dropped as a duplicate.
    func testDetailPhaseEnrichesTheAlreadyStoredJob() async throws {
        let db = try AppDatabase.inMemory()
        let store = JobStore(db)

        _ = await FetchPipeline().run(config: config(), sources: ["linkedin"],
                                      jobStore: store)

        let stored = try store.jobs()
        XCTAssertFalse(stored.isEmpty)
        XCTAssertTrue(stored.allSatisfy { !$0.description.isEmpty },
                      "every job ends up with the description from its detail page")
        // No duplicate rows: the re-delivery updated in place.
        XCTAssertEqual(Set(stored.map(\.externalId)).count, stored.count)
    }
}
