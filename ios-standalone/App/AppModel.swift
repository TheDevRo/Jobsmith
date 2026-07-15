import SwiftUI
import UIKit
import Observation
import JobsmithKit

/// App-wide state and actions. Owns the shared database and stores; screens
/// read published snapshots and call actions.
@MainActor
@Observable
final class AppModel {
    let database: AppDatabase
    let jobStore: JobStore
    let searchRunStore: SearchRunStore
    let applicationStore: ApplicationStore
    let activityStore: ActivityStore
    let answerBank: AnswerBankStore
    let configStore: ConfigStore

    var config: AppConfig
    var inbox: [Job] = []
    var pipeline: [Job] = []
    var stats = JobStore.Stats()
    var activity: [ActivityEntry] = []
    /// The application for each job that has one, keyed by job id — lets the
    /// pipeline and detail screens show a submitted job's outcome without a
    /// per-row query.
    var applicationsByJob: [String: Application] = [:]
    /// Set when a notification tap should route to a job; the Pipeline consumes
    /// and clears it. Before this, notifications wrote a `deepLink` into userInfo
    /// that nothing ever read, so a tap went nowhere.
    var deepLinkedJobId: String?
    /// The action a shake is offering to take back; drives the undo prompt.
    var pendingUndo: UndoableAction?
    /// Normalized (title, company) keys of roles already applied to — drives the
    /// "already applied" badge. See isAlreadyApplied.
    var appliedIdentities: Set<String> = []
    /// How often each source has actually replied to you. Drives the "Best bets"
    /// sort; empty for sources with too little history to judge.
    var conversionBySource: [String: Double] = [:]
    var isFetching = false
    /// A search stopped short of finishing and is waiting to be resumed. Not an
    /// error — the jobs it did collect are already in the Inbox — so it shows as
    /// a quiet banner rather than the blocking error alert.
    var isSearchPaused = false
    /// Same, for a Score-all run: the endpoint went out of reach or the app was
    /// suspended mid-call, and the remaining jobs will be picked up later.
    var isScoringPaused = false
    /// Live per-source progress for the in-flight fetch, or nil when idle.
    /// Drives the Inbox "Searching…" banner; cleared when the fetch ends.
    var fetchProgress: FetchProgress?
    var lastError: String?

    init() {
        do {
            database = try AppDatabase.shared()
        } catch {
            // A broken shared container is unrecoverable in the field, but
            // an in-memory fallback keeps the UI alive to show the error.
            database = (try? AppDatabase.inMemory()) ?? { fatalError("no database") }()
            lastError = "Could not open the database: \(error.localizedDescription)"
        }
        jobStore = JobStore(database)
        searchRunStore = SearchRunStore(database)
        applicationStore = ApplicationStore(database)
        activityStore = ActivityStore(database)
        answerBank = AnswerBankStore(database)
        configStore = ConfigStore.shared
        config = AppConfig()
        try? AnswerBankMatcher(store: answerBank).seedIfEmpty()
        #if DEBUG
        if CommandLine.arguments.contains("-SeedDemoData") {
            seedDemoData()
        }
        #endif
        Task {
            config = await configStore.load()
            #if DEBUG
            // Test-only: "-E2EKeywords a,b,c" / "-E2ESources x,y" inject
            // search config in-memory so walkthrough tests can exercise a
            // real fetch without persisting config. DEBUG-only — the UI tests
            // run against the Debug build, and a shipped binary has no business
            // reading its search config off the command line.
            if let idx = CommandLine.arguments.firstIndex(of: "-E2EKeywords"),
               CommandLine.arguments.indices.contains(idx + 1) {
                config.search.keywords = CommandLine.arguments[idx + 1]
                    .split(separator: ",").map(String.init)
            }
            if let idx = CommandLine.arguments.firstIndex(of: "-E2ESources"),
               CommandLine.arguments.indices.contains(idx + 1) {
                config.search.enabledSources = Set(
                    CommandLine.arguments[idx + 1].split(separator: ",").map(String.init))
            }
            #endif
        }
        refresh()
    }

    #if DEBUG
    /// Deterministic state for UI tests (-SeedDemoData): wipe the jobs and
    /// applications tables and the persisted config so every run starts with
    /// exactly two untriaged demo jobs and a default (empty-profile) config,
    /// regardless of what earlier runs or manual fetches left.
    private func seedDemoData() {
        try? FileManager.default.removeItem(at: AppGroup.configURL)
        // Reset background-search prefs so UI-test runs start deterministically
        // off, uncontaminated by a prior run's toggle — including any run a
        // previous test left parked, which would otherwise resume mid-test.
        BackgroundScheduler.setEnabled(false)
        BackgroundScheduler.setIntervalHours(12)
        BackgroundScheduler.setScoresInBackground(false)
        BackgroundScheduler.clearContinuation()
        ScoringIntent.clear()
        try? database.writer.write {
            try $0.execute(sql: "DELETE FROM search_runs")
        }
        try? database.writer.write {
            try $0.execute(sql: "DELETE FROM applications")
            try $0.execute(sql: "DELETE FROM jobs")
        }
        // "-E2EJobURL <url>" points demo-1 at a real posting so UI tests can
        // drive the Apply browser against a live site (LinkedIn especially).
        var demo1URL = "https://example.com/jobs/demo-1"
        if let idx = CommandLine.arguments.firstIndex(of: "-E2EJobURL"),
           CommandLine.arguments.indices.contains(idx + 1) {
            demo1URL = CommandLine.arguments[idx + 1]
        }
        let jobs = [
            NormalizedJob(source: "demo", externalId: "demo-1",
                          title: "Senior Backend Engineer", company: "Acme Corp",
                          location: "Denver, CO",
                          url: demo1URL,
                          description: "Build high-throughput services in Python and Go.",
                          salaryMin: 140000, salaryMax: 175000, salaryPeriod: "annual"),
            NormalizedJob(source: "demo", externalId: "demo-2",
                          title: "Platform Engineer", company: "Globex",
                          location: "Remote",
                          url: "https://example.com/jobs/demo-2",
                          description: "Own the Kubernetes platform and CI/CD tooling.",
                          isRemote: true),
        ]
        _ = try? jobStore.upsert(jobs)

        // -SeedApplied additionally puts demo-1 in the submitted state with an
        // application, so the outcome UI (which only exists once you've applied)
        // is reachable without driving a real apply through a live posting.
        if CommandLine.arguments.contains("-SeedApplied"),
           let applied = try? jobStore.jobs(triage: "new").first(where: { $0.externalId == "demo-1" }) {
            try? jobStore.setTriage("shortlisted", jobId: applied.id)
            try? jobStore.setStatus("applied", jobId: applied.id)
            if let application = try? applicationStore.createOrReplace(
                jobId: applied.id, resume: "RESUME", coverLetter: "COVER",
                honestyLevel: "honest", stylePreset: "ledger") {
                try? applicationStore.updateStatus(id: application.id, status: "applied")
            }
        }
    }
    #endif

    func refresh() {
        inbox = (try? jobStore.inbox()) ?? []
        pipeline = (try? jobStore.jobs(triage: "shortlisted")) ?? []
        stats = (try? jobStore.stats()) ?? JobStore.Stats()
        activity = (try? activityStore.recent()) ?? []
        applicationsByJob = Dictionary(
            ((try? applicationStore.applications()) ?? []).map { ($0.jobId, $0) },
            uniquingKeysWith: { a, b in a.updatedAt >= b.updatedAt ? a : b })
        appliedIdentities = (try? jobStore.appliedIdentities()) ?? []
        conversionBySource = (try? applicationStore.responseRateBySource()) ?? [:]
    }

    /// True when this posting is a repost — or a cross-posting — of a role you
    /// already applied to at the same company. Computed rather than stored: a
    /// stored flag goes stale the moment you apply to something new.
    func isAlreadyApplied(_ job: Job) -> Bool {
        guard job.status != "applied",
              let key = JobStore.identityKey(title: job.title, company: job.company)
        else { return false }
        return appliedIdentities.contains(key)
    }

    // MARK: - Undo (shake)

    /// History behind the shake gesture. Not everything the app does belongs
    /// here: tailoring a resume or running a search is deliberate, slow, and
    /// visible, whereas a triage swipe is one careless flick away from throwing
    /// out the job you wanted — those are the actions worth being able to take
    /// back, so those are the ones that register.
    let undoStack = UndoStack()

    /// True while an undo closure is running, so the write it makes to put things
    /// back doesn't land on the stack as an action of its own. Without it, a
    /// reverted outcome would leave behind an "undo the undo" entry.
    private var isUndoing = false

    private func registerUndo(_ label: String, revert: @escaping () -> Void) {
        guard !isUndoing else { return }
        undoStack.register(label, revert: revert)
    }

    /// The user shook the phone. Offer the last undoable action — as a prompt,
    /// never as an immediate revert: a shake is easy to set off by accident, in a
    /// pocket or on a walk, and silently rolling back the user's last action
    /// would be a worse bug than the one this feature exists to fix.
    func requestUndo() {
        guard pendingUndo == nil, let action = undoStack.top else { return }
        pendingUndo = action
        UINotificationFeedbackGenerator().notificationOccurred(.warning)
    }

    /// The user confirmed the prompt. Undoing by id rather than "whatever is on
    /// top" keeps us honest if something landed on the stack while the prompt was
    /// up — we take back exactly what the prompt named.
    func performUndo() {
        guard let action = pendingUndo else { return }
        pendingUndo = nil
        isUndoing = true
        undoStack.undo(action.id)
        isUndoing = false
        activityStore.log("undone", action.label)
        refresh()
        UINotificationFeedbackGenerator().notificationOccurred(.success)
        Task { await syncNow() }
    }

    // MARK: outcomes

    /// Record what the employer did after you applied. This is the whole point of
    /// having the pipeline on the phone: the rejection email and the "can you do
    /// Tuesday?" arrive here, not at the desk.
    func setOutcome(jobId: String, _ outcome: ApplicationOutcome) {
        guard let application = applicationsByJob[jobId],
              application.outcome != outcome.rawValue else { return }
        let previous = ApplicationOutcome(rawValue: application.outcome)
        do {
            try applicationStore.recordOutcome(id: application.id, outcome: outcome)
        } catch {
            lastError = "Could not save the outcome: \(error.localizedDescription)"
            return
        }
        let title = pipeline.first { $0.id == jobId }?.title ?? "application"
        activityStore.log("outcome", "\(outcome.label) — \(title)", jobId: jobId)
        if let previous {
            // The history is append-only — two devices merge it as a union — so
            // taking an outcome back means recording the move *back*, not erasing
            // the move forward. The detail screen's history shows both, which is
            // what actually happened.
            let applicationId = application.id
            registerUndo("Marked \(title) as \(outcome.label).") { [weak self] in
                try? self?.applicationStore.recordOutcome(id: applicationId, outcome: previous)
            }
        }
        refresh()
        Task { await syncNow() }
    }

    /// Funnel counts over submitted applications, for the Activity screen.
    var outcomeFunnel: (applied: Int, stages: [(ApplicationOutcome, Int)]) {
        (try? applicationStore.funnel()) ?? (0, [])
    }

    /// Set or clear a reminder date, then rebuild the notification schedule.
    func setSchedule(jobId: String, followUpAt: Date? = nil, interviewAt: Date? = nil,
                     clearFollowUp: Bool = false, clearInterview: Bool = false) {
        guard let application = applicationsByJob[jobId] else { return }
        do {
            try applicationStore.setSchedule(
                id: application.id,
                followUpAt: followUpAt.map(ApplicationStore.isoMs),
                interviewAt: interviewAt.map(ApplicationStore.isoMs),
                clearFollowUp: clearFollowUp, clearInterview: clearInterview)
        } catch {
            lastError = "Could not save the reminder: \(error.localizedDescription)"
            return
        }
        refresh()
        NotificationManager.rescheduleReminders(model: self)
        Task { await syncNow() }
    }

    // MARK: foreground auto-sync

    private var autoSyncTask: Task<Void, Never>?

    /// Run one sync cycle now (if enabled and a folder is set), then refresh the
    /// UI. Silent on failure: cycles are frequent and a transient folder/iCloud
    /// hiccup shouldn't raise the blocking error alert.
    @discardableResult
    func syncNow() async -> Bool {
        guard SyncManager.shared.isEnabled(), SyncManager.shared.resolvedFolder() != nil else { return false }
        do {
            _ = try await SyncManager.shared.syncNow(
                db: database, configStore: configStore, deviceLabel: UIDevice.current.name)
            refresh()
            return true
        } catch {
            return false
        }
    }

    /// Foreground auto-sync: an immediate catch-up cycle, then one every
    /// `SyncManager.syncIntervalSeconds` while the app is active. A cadence of 0
    /// ("manual only") runs the single catch-up cycle and stops. Idempotent —
    /// cancels any existing loop first.
    func startAutoSync() {
        stopAutoSync()
        guard SyncManager.shared.isEnabled() else { return }
        autoSyncTask = Task { @MainActor [weak self] in
            await self?.syncNow()
            while !Task.isCancelled {
                let secs = SyncManager.shared.syncIntervalSeconds()
                guard secs > 0 else { break }
                try? await Task.sleep(for: .seconds(secs))
                if Task.isCancelled { break }
                await self?.syncNow()
            }
        }
    }

    func stopAutoSync() {
        autoSyncTask?.cancel()
        autoSyncTask = nil
    }

    func saveConfig(_ mutate: @escaping (inout AppConfig) -> Void) {
        Task { await saveConfigNow(mutate) }
    }

    /// Awaitable save — use when the next screen reads the config right
    /// away (e.g. profile import → profile review), where the fire-and-
    /// forget variant races the navigation.
    func saveConfigNow(_ mutate: @escaping (inout AppConfig) -> Void) async {
        do {
            config = try await configStore.update(mutate)
        } catch {
            lastError = "Could not save settings: \(error.localizedDescription)"
        }
    }

    func triage(_ job: Job, as triage: String) {
        let previous = job.triage
        try? jobStore.setTriage(triage, jobId: job.id)
        withAnimation(.snappy) {
            inbox.removeAll { $0.id == job.id }
        }
        if triage == "shortlisted" {
            activityStore.log("shortlisted", "\(job.title) at \(job.company)", jobId: job.id)
        }
        stats = (try? jobStore.stats()) ?? stats
        pipeline = (try? jobStore.jobs(triage: "shortlisted")) ?? pipeline
        // The card is gone in one flick of a thumb, and the inbox is a deck you
        // move through fast — this is the action shake-to-undo exists for.
        registerUndo(triage == "shortlisted"
                     ? "Shortlisted \(job.title) at \(job.company)."
                     : "Passed on \(job.title) at \(job.company).") { [weak self] in
            try? self?.jobStore.setTriage(previous, jobId: job.id)
        }
    }

    /// AI engine for scoring/tailoring. Mock in UI tests; otherwise routes
    /// between the configured endpoint and Apple's on-device model.
    var aiEngine: any AIEngine {
        #if DEBUG
        // Canned fixtures for UI tests / demo runs. DEBUG-only so the mock AI
        // responses can never ship in a Release/TestFlight build.
        if CommandLine.arguments.contains("-UseMockAI") {
            return MockAIEngine.standardFixtures()
        }
        #endif
        return EngineRouter()
    }

    var busyJobIds: Set<String> = []

    // MARK: - Batch scoring ("Score all")

    /// True while a Score-all run is in flight; drives the progress banner.
    var isScoringAll = false
    var scoreAllDone = 0
    var scoreAllTotal = 0
    private var scoreAllTask: Task<Void, Never>?

    /// Untriaged jobs that have no fit score yet — the Score-all candidates.
    var unscoredInboxJobs: [Job] {
        ScoreBatch.unscored(inbox)
    }

    /// Shortlisted (in-pipeline) jobs that were never scored — e.g. shortlisted
    /// straight from the Inbox before scoring. The Pipeline tab's Score-all set.
    var unscoredPipelineJobs: [Job] {
        ScoreBatch.unscored(pipeline)
    }

    /// The job whose apply flow is in progress — set while the in-app Apply
    /// browser is open, resolved by the "Did you submit?" prompt on dismiss.
    var pendingApplyJob: Job?

    /// Non-nil drives the in-app Apply browser sheet (ApplyBrowserView).
    var applyBrowserJob: Job?

    /// Market salary estimate from real data sources (Adzuna/BLS); the LLM
    /// only canonicalizes the title. Nil result means "no data", not zero.
    func estimateSalary(_ job: Job) async -> Bool {
        busyJobIds.insert(job.id)
        defer { busyJobIds.remove(job.id); refresh() }
        do {
            guard let estimate = try await SalaryEstimator().estimate(
                job: job, config: config, engine: aiEngine, database: database) else {
                return false
            }
            let json = String(data: try JSONEncoder().encode(estimate), encoding: .utf8)
            try jobStore.setSalaryEstimate(jobId: job.id, json: json)
            activityStore.log("salary_estimated",
                              "\(job.title): ~$\(estimate.p50 ?? estimate.p25)",
                              jobId: job.id)
            return true
        } catch {
            lastError = "Salary estimate failed: \(error.localizedDescription)"
            return false
        }
    }

    /// Score one job. A failure (offline endpoint, unparseable answer) leaves the
    /// job *unscored* and raises `lastError` — writing a `0` would permanently
    /// brand it a bad fit with no way to tell it apart from a real one.
    func score(_ job: Job) async {
        busyJobIds.insert(job.id)
        defer { busyJobIds.remove(job.id); refresh() }
        do {
            let result = try await scoreOne(job)
            activityStore.log("scored", "\(job.title): \(Int(result.score))/100", jobId: job.id)
        } catch {
            lastError = "Scoring failed: \(error.localizedDescription)"
        }
    }

    /// Score up to `cap` unscored jobs. Runs sequentially — never a concurrent
    /// fan-out — so a batch can't stampede the endpoint and the Stop button
    /// halts the run after at most one more in-flight call. `cap`
    /// (config.ai.scoreAllCap) is the hard ceiling on calls per run.
    /// `candidates` selects the source set — unscored inbox jobs by default,
    /// or e.g. `unscoredPipelineJobs` when scoring from the Pipeline tab.
    ///
    /// Like a search, the run holds a background assertion so leaving the app
    /// doesn't kill it outright, and it distinguishes the two ways it can end
    /// early:
    ///
    /// - **A dead endpoint aborts it.** Every remaining job would fail the same
    ///   way, so hammering the endpoint for the whole batch just burns the user's
    ///   time (and, before REL-01, poisoned every job with a `0`).
    /// - **An interruption pauses it.** The app being suspended mid-call, or the
    ///   endpoint dropping off the network — an LM Studio box is only on the LAN
    ///   while you're home — says nothing about the remaining jobs. The run is
    ///   parked and resumed later. There is no separate progress record to keep:
    ///   each score is committed as it lands, so "what's left" is simply the jobs
    ///   still unscored.
    func scoreAll(cap: Int, candidates: [Job]? = nil, isResume: Bool = false) {
        guard !isScoringAll else { return }
        let batch = ScoreBatch.plan(candidates: candidates ?? unscoredInboxJobs, cap: cap)
        guard !batch.isEmpty else {
            ScoringIntent.clear()
            return
        }
        if !isResume {
            ScoringIntent.save(cap: cap, candidates: candidates == nil ? .inbox : .pipeline)
        }
        isScoringAll = true
        isScoringPaused = false
        scoreAllTotal = batch.count
        scoreAllDone = 0
        scoreAllTask = Task { @MainActor in
            var bgTask = UIBackgroundTaskIdentifier.invalid
            bgTask = UIApplication.shared.beginBackgroundTask(withName: "jobsmith.scoring") {
                self.scoreAllTask?.cancel()
                UIApplication.shared.endBackgroundTask(bgTask)
                bgTask = .invalid
            }
            defer {
                if bgTask != .invalid {
                    UIApplication.shared.endBackgroundTask(bgTask)
                    bgTask = .invalid
                }
            }

            var failure: String?
            var paused = false
            for job in batch {
                if Task.isCancelled { break }
                do {
                    _ = try await scoreOne(job)
                } catch let error as ScoringError {
                    if case .interrupted = error { paused = true; break }
                    failure = "Scoring stopped: \(error.localizedDescription)"
                    break
                } catch {
                    failure = "Scoring stopped: \(error.localizedDescription)"
                    break
                }
                scoreAllDone += 1
            }
            let done = scoreAllDone
            // Cancellation is how the background window closes, so it parks the
            // run the same way an interruption does. The Stop button clears the
            // intent first, which is what keeps a deliberate stop from coming
            // back to life on the next foreground.
            let cancelled = Task.isCancelled
            paused = paused || cancelled

            isScoringAll = false
            scoreAllTask = nil

            if paused && ScoringIntent.isPending {
                isScoringPaused = true
            } else {
                isScoringPaused = false
                ScoringIntent.clear()
            }
            if let failure {
                lastError = failure
                ScoringIntent.clear()
            }
            refresh()
            activityStore.log("scored_batch",
                "Scored \(done) job\(done == 1 ? "" : "s")\(paused ? " (paused — will resume)" : "")")
        }
    }

    /// Halt an in-progress Score-all run — the hard kill switch. The current
    /// job's call finishes, then the loop exits before the next one starts.
    /// Clearing the intent first is what tells the run apart from a pause: a
    /// stop the user asked for must not resurrect itself on the next foreground.
    func cancelScoreAll() {
        ScoringIntent.clear()
        scoreAllTask?.cancel()
    }

    /// Pick up a Score-all run that was paused mid-batch. The remaining work is
    /// derived from the database — the jobs still unscored — so nothing beyond
    /// the user's original intent needs to have survived.
    @discardableResult
    func resumeScoringIfNeeded() -> Bool {
        guard !isScoringAll, let intent = ScoringIntent.pending else { return false }
        let candidates = intent.candidates == .pipeline ? unscoredPipelineJobs : unscoredInboxJobs
        guard !candidates.isEmpty else {
            ScoringIntent.clear()
            isScoringPaused = false
            return false
        }
        scoreAll(cap: intent.cap, candidates: candidates, isResume: true)
        return true
    }

    /// Score-all, but awaitable — a background task has to know when the work is
    /// done before it reports the window finished.
    func scoreAllAndWait(cap: Int, candidates: [Job]? = nil) async {
        scoreAll(cap: cap, candidates: candidates)
        await scoreAllTask?.value
    }

    /// Can we actually reach the AI endpoint from where the phone is right now?
    ///
    /// Worth asking before a background scoring run: a self-hosted endpoint (LM
    /// Studio on a laptop) is only on the LAN while the user is home, and the
    /// engine's own request timeout is 90 seconds — long enough to eat a whole
    /// background window discovering that. This probes the cheap `/models`
    /// endpoint and gives up quickly.
    func isAIEndpointReachable(timeout: Duration = .seconds(5)) async -> Bool {
        let engine = aiEngine
        let aiConfig = config.ai
        return await withTaskGroup(of: Bool?.self) { group in
            group.addTask { await engine.testConnection(config: aiConfig).connected }
            group.addTask {
                try? await Task.sleep(for: timeout)
                return nil  // the probe outlived its welcome
            }
            let first = await group.next() ?? nil
            group.cancelAll()
            return first ?? false
        }
    }

    /// Score one job and persist the result, without the per-job activity-log
    /// entry or UI refresh that the interactive `score(_:)` performs. Throws
    /// (leaving the job unscored) rather than persisting a placeholder.
    @discardableResult
    private func scoreOne(_ job: Job) async throws -> FitResult {
        let result = try await ScoringService.score(job: job, profile: config.profile,
                                                    config: config, engine: aiEngine)
        try? jobStore.setScore(jobId: job.id, score: result.score,
                               reasoning: result.reasoning,
                               matchReport: result.matchReportJSON)
        return result
    }

    func tailor(_ job: Job) async {
        busyJobIds.insert(job.id)
        defer { busyJobIds.remove(job.id); refresh() }
        do {
            try jobStore.setStatus("tailoring", jobId: job.id)
            let resume = try await TailoringService.tailorResume(
                job: job, profile: config.profile, config: config, engine: aiEngine)
            let coverLetter = try await TailoringService.coverLetter(
                job: job, profile: config.profile, config: config, engine: aiEngine)
            let application = try applicationStore.createOrReplace(
                jobId: job.id, resume: resume, coverLetter: coverLetter,
                honestyLevel: config.honesty.level.rawValue,
                stylePreset: config.honesty.resumeStyle.rawValue)
            try regenerateDocuments(for: application, job: job)
            try jobStore.setStatus("review", jobId: job.id)
            activityStore.log("tailored", "Resume + cover letter for \(job.title)", jobId: job.id)
        } catch {
            try? jobStore.setStatus("discovered", jobId: job.id)
            lastError = "Tailoring failed: \(error.localizedDescription)"
        }
    }

    /// Rebuild the DOCX artifacts from an application's (possibly edited)
    /// text content.
    func regenerateDocuments(for application: Application, job: Job) throws {
        let parsed = ResumeTextParser.parse(application.resumeContent)
        let content = DocResumeContent(
            summary: parsed.summary,
            skillsText: parsed.skills.joined(separator: ", "),
            experiences: parsed.experiences.map {
                DocExperience(title: $0.title, company: $0.company, dates: $0.dates, bullets: $0.bullets)
            },
            education: parsed.education.map {
                DocEducation(degree: $0.degree, school: $0.school, year: $0.year)
            },
            certifications: parsed.certifications)
        // The application row records the style it was tailored under; rows
        // written before the five-style lineup carry retired names, so map them.
        let style = HonestyConfig.Style.fromPersisted(application.stylePreset)
        let accent = config.honesty.resumeAccent
        let format = config.honesty.documentFormat

        let resumeDoc = ResumeDocxGenerator.build(content: content, profile: config.profile,
                                                  style: style, accent: accent)
        let resumeURL = try FileVault.write(render(resumeDoc, as: format),
                                            jobId: job.id, kind: .resume, format: format)
        // The cover letter shares the resume's letterhead and typography, so the
        // pair reads as one matched set.
        let coverDoc = CoverLetterDocxGenerator.build(
            content: application.coverLetterContent, profile: config.profile,
            jobTitle: job.title, company: job.company, style: style, accent: accent)
        let coverURL = try FileVault.write(render(coverDoc, as: format),
                                           jobId: job.id, kind: .coverLetter, format: format)
        try applicationStore.setDocumentPaths(id: application.id,
                                              resumePath: resumeURL.path,
                                              coverPath: coverURL.path)
    }

    /// Render the shared layout model to the user's chosen output format.
    private func render(_ doc: DocxDocument, as format: FileVault.Format) throws -> Data {
        switch format {
        case .docx: return try doc.render()
        case .pdf: return DocxPDFRenderer.render(doc)
        }
    }

    /// Open the posting in the in-app Apply browser, where we inject the
    /// autofill scripts and map fields on-device. No Safari, no extension.
    func applyInApp(_ job: Job) {
        guard URL(string: job.url) != nil else {
            lastError = "This job has no application URL."
            return
        }
        pendingApplyJob = job
        applyBrowserJob = job
        activityStore.log("apply_started", "Opened \(job.title) in the Apply browser", jobId: job.id)
    }

    /// Map a page's scanned form fields to values on-device, reusing the same
    /// pipeline the Safari extension used to reach over native messaging
    /// (FieldMapper → profile/answer-bank/LLM). `rawFields` is the snapshot.js
    /// `fields` array; the return is FieldValue dicts keyed by `field_id`.
    func mapApplyFields(_ rawFields: [[String: Any]], job: Job) async -> [[String: Any]] {
        let router = NativeMessageRouter(db: database, engine: aiEngine, configStore: configStore)
        let body: [String: Any] = ["url": job.url, "job_id": job.id, "fields": rawFields]
        let response = await router.handle(name: "scan", body: body)
        let result = response["result"] as? [String: Any]
        return (result?["fields"] as? [[String: Any]]) ?? []
    }

    /// Called when the Apply browser dismisses — asks the user what happened.
    func resolvePendingApply(applied: Bool) {
        guard let job = pendingApplyJob else { return }
        pendingApplyJob = nil
        let status = applied ? "applied" : "manual"
        try? jobStore.setStatus(status, jobId: job.id)
        if let application = try? applicationStore.application(jobId: job.id) {
            try? applicationStore.updateStatus(id: application.id, status: status)
        }
        activityStore.log(applied ? "applied" : "apply_deferred",
                          "\(job.title) at \(job.company)", jobId: job.id)
        refresh()
    }

    /// Fetch new jobs from all enabled sources.
    ///
    /// Start a fresh search across every enabled source, superseding any run
    /// that was left unfinished.
    func fetchJobs() async {
        guard !isFetching else { return }
        let sources = SourceRegistry.enabledIDs(for: config)
        // Never begin an empty run: FetchPipeline reads an empty source list
        // as "every registered source", which would turn "all sources off"
        // into a fetch of all of them.
        guard !sources.isEmpty else {
            lastError = "No job sources are enabled. Turn one on in Settings › Job sources."
            return
        }
        guard let run = try? searchRunStore.begin(sources: sources) else { return }
        await runSearch(run)
    }

    /// Carry on a search that an earlier attempt couldn't finish — the app was
    /// backgrounded and iOS reclaimed the ~30s window while LinkedIn, which
    /// budgets minutes, was still working. Called when the app returns to the
    /// foreground and from the `BGProcessingTask` continuation.
    ///
    /// Returns true if there was a run to resume.
    @discardableResult
    func resumeInterruptedSearch() async -> Bool {
        guard !isFetching else { return false }
        guard let run = try? searchRunStore.activeRun(), !run.isFinished else { return false }
        activityStore.log("search_resumed",
                          "Resuming \(run.remainingSources.count) source(s) from the last search")
        await runSearch(run)
        return true
    }

    /// One attempt at `run`.
    ///
    /// The attempt holds a `UIApplication` background-task assertion, so leaving
    /// the app doesn't suspend it on the spot — iOS grants a continued-execution
    /// window of roughly 30 seconds. That is nowhere near enough for a LinkedIn
    /// search, which is the whole reason a run is resumable: the pipeline saves
    /// jobs as it collects them and bookmarks how far each source got, so when
    /// the window closes we cancel cleanly, mark the run interrupted, and ask iOS
    /// for a long `BGProcessingTask` window to finish in. Nothing is discarded
    /// and nothing is reported as an error.
    private func runSearch(_ run: SearchRun) async {
        // A parked run's work list was written when the run BEGAN; the user may
        // have toggled sources off since (the reported case: everything off but
        // LinkedIn, yet a resumed run kept fetching the other boards). Honor
        // the current toggles here — the one place every attempt passes
        // through — and retire the run when nothing enabled remains.
        let enabled = Set(SourceRegistry.enabledIDs(for: config))
        let sources = run.remainingSources.filter { enabled.contains($0) }
        guard !sources.isEmpty else {
            try? searchRunStore.setState(id: run.id, .complete)
            isSearchPaused = false
            BackgroundScheduler.clearContinuation()
            return
        }
        isFetching = true
        try? searchRunStore.setState(id: run.id, .running)

        let pipeline = FetchPipeline()
        // Subscribe to the progress stream *before* running so the opening
        // "fetching from N sources" event isn't missed, then mirror each event
        // onto `fetchProgress` for the live Inbox banner.
        let stream = await pipeline.progressUpdates()
        let progressTask = Task { @MainActor in
            for await progress in stream { self.fetchProgress = progress }
        }

        let work = Task { @MainActor in
            await pipeline.run(config: config, sources: sources,
                               jobStore: jobStore, runStore: searchRunStore,
                               runID: run.id, cursors: run.cursors)
        }

        var bgTask = UIBackgroundTaskIdentifier.invalid
        bgTask = UIApplication.shared.beginBackgroundTask(withName: "jobsmith.search") {
            // The window is closing. Cancelling is safe now — everything the
            // pipeline collected is already committed — and it's what lets the
            // sources report themselves interrupted rather than being frozen
            // mid-request and losing the run.
            work.cancel()
            UIApplication.shared.endBackgroundTask(bgTask)
            bgTask = .invalid
        }
        defer {
            isFetching = false
            fetchProgress = nil
            refresh()
            if bgTask != .invalid {
                UIApplication.shared.endBackgroundTask(bgTask)
                bgTask = .invalid
            }
        }

        // `work` is unstructured, so it does not inherit cancellation from
        // whoever is awaiting us — and the BGProcessingTask continuation cancels
        // exactly that way when its window expires. Without this the pipeline
        // would keep running into the suspension and the run would never be
        // marked interrupted.
        let summary = await withTaskCancellationHandler {
            await work.value
        } onCancel: {
            work.cancel()
        }
        progressTask.cancel()

        if summary.isIncomplete {
            // Left unfinished, not failed. Park it and line up a long background
            // window to finish in; the app returning to the foreground will also
            // pick it up, whichever comes first.
            try? searchRunStore.setState(id: run.id, .interrupted)
            isSearchPaused = true
            BackgroundScheduler.requestContinuation()
            activityStore.log(
                "search_paused",
                "Paused with \(summary.interrupted.count) source(s) left — will finish in the background")
            return
        }

        try? searchRunStore.setState(id: run.id, .complete)
        isSearchPaused = false
        BackgroundScheduler.clearContinuation()

        let total = summary.inserted
        activityStore.log("fetched", "\(total) new job\(total == 1 ? "" : "s") from \(summary.perSource.count) sources")
        // A rejected key is actionable in a way "had trouble" is not, so it wins
        // the one error slot.
        if !summary.authFailed.isEmpty {
            let names = summary.authFailed
                .map { SourceCatalog.displayName(for: $0) }
                .sorted().joined(separator: ", ")
            lastError = "Check the API key for: \(names)"
        } else if !summary.failed.isEmpty || !summary.timedOut.isEmpty {
            let names = (summary.failed + summary.timedOut).joined(separator: ", ")
            lastError = "Some sources had trouble: \(names)"
        }
        // Only notify if the user has left the app; a foregrounded search
        // already shows its results in the Inbox.
        if UIApplication.shared.applicationState != .active {
            await NotificationManager.notifySearchComplete(summary: summary, model: self)
        }
    }

    // MARK: - Deletion

    /// Delete specific jobs (Pipeline multi-select). A soft delete — the row stays
    /// and only its triage changes — so the application row survives (the FK
    /// cascade is on a real DELETE, which this is not) and only the generated
    /// document files actually go.
    func deleteJobs(_ ids: Set<String>) {
        guard !ids.isEmpty else { return }
        // Capture each job's triage before the delete overwrites it: that value is
        // the whole of what an undo has to put back.
        let previousTriage: [(id: String, triage: String)] = ids.compactMap { id in
            guard let job = try? jobStore.job(id: id) else { return nil }
            return (id, job.triage)
        }
        for id in ids {
            try? jobStore.delete(jobId: id)
            FileVault.deleteDocuments(jobId: id)
        }
        activityStore.log("deleted", "Deleted \(ids.count) posting\(ids.count == 1 ? "" : "s")")
        refresh()

        let count = ids.count
        registerUndo("Deleted \(count) posting\(count == 1 ? "" : "s").") { [weak self] in
            guard let self else { return }
            for (id, triage) in previousTriage {
                try? self.jobStore.setTriage(triage, jobId: id)
                // The DOCX/PDF files were really deleted, but they are derived —
                // the application row holds the resume and cover-letter text they
                // were rendered from, so restoring the job can rebuild them.
                if let job = try? self.jobStore.job(id: id),
                   let application = try? self.applicationStore.application(jobId: id) {
                    try? self.regenerateDocuments(for: application, job: job)
                }
            }
        }
    }

    /// Clear every tracked posting and its tailored documents, plus the fetch
    /// caches — but keep the profile, settings, and answer bank. "Start the
    /// job list over" without a full reset.
    func deleteAllTrackedPostings() {
        wipeTables(["applications", "jobs", "activity_log",
                    "source_stats", "geo_cache", "ai_cache"])
        try? FileManager.default.removeItem(at: AppGroup.documentsDirectory)
        // These are hard deletes: every pending undo would be writing back to a
        // row that no longer exists, so the offer has to go with the rows.
        undoStack.clear()
        refresh()
    }

    /// Factory reset: wipe all database tables, generated documents, imports,
    /// and the saved config/profile, then reseed the default answer bank.
    /// Leaves the app as if freshly installed.
    func deleteAllData() {
        wipeTables(["applications", "jobs", "activity_log", "answer_bank",
                    "source_stats", "geo_cache", "ai_cache"])
        try? FileManager.default.removeItem(at: AppGroup.documentsDirectory)
        try? FileManager.default.removeItem(at: AppGroup.importsDirectory)
        try? FileManager.default.removeItem(at: AppGroup.configURL)
        try? AnswerBankMatcher(store: answerBank).seedIfEmpty()
        undoStack.clear()
        Task {
            config = await configStore.reload()
            refresh()
        }
        refresh()
    }

    private func wipeTables(_ tables: [String]) {
        try? database.writer.write { db in
            for table in tables {
                try db.execute(sql: "DELETE FROM \(table)")
            }
        }
    }
}
