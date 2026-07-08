import SwiftUI
import Observation
import UIKit
import JobsmithKit

/// App-wide state and actions. Owns the shared database and stores; screens
/// read published snapshots and call actions.
@MainActor
@Observable
final class AppModel {
    let database: AppDatabase
    let jobStore: JobStore
    let applicationStore: ApplicationStore
    let activityStore: ActivityStore
    let answerBank: AnswerBankStore
    let configStore: ConfigStore

    var config: AppConfig
    var inbox: [Job] = []
    var pipeline: [Job] = []
    var stats = JobStore.Stats()
    var activity: [ActivityEntry] = []
    var isFetching = false
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
        applicationStore = ApplicationStore(database)
        activityStore = ActivityStore(database)
        answerBank = AnswerBankStore(database)
        configStore = ConfigStore.shared
        config = AppConfig()
        try? AnswerBankMatcher(store: answerBank).seedIfEmpty()
        if CommandLine.arguments.contains("-SeedDemoData") {
            seedDemoData()
        }
        Task {
            config = await configStore.load()
            // Test-only: "-E2EKeywords a,b,c" / "-E2ESources x,y" inject
            // search config in-memory so walkthrough tests can exercise a
            // real fetch without persisting config.
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
        }
        refresh()
    }

    /// Deterministic state for UI tests (-SeedDemoData): wipe the jobs and
    /// applications tables and the persisted config so every run starts with
    /// exactly two untriaged demo jobs and a default (empty-profile) config,
    /// regardless of what earlier runs or manual fetches left.
    private func seedDemoData() {
        try? FileManager.default.removeItem(at: AppGroup.configURL)
        try? database.writer.write {
            try $0.execute(sql: "DELETE FROM applications")
            try $0.execute(sql: "DELETE FROM jobs")
        }
        let jobs = [
            NormalizedJob(source: "demo", externalId: "demo-1",
                          title: "Senior Backend Engineer", company: "Acme Corp",
                          location: "Denver, CO",
                          url: "https://example.com/jobs/demo-1",
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
    }

    func refresh() {
        inbox = (try? jobStore.inbox()) ?? []
        pipeline = (try? jobStore.jobs(triage: "shortlisted")) ?? []
        stats = (try? jobStore.stats()) ?? JobStore.Stats()
        activity = (try? activityStore.recent()) ?? []
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
        try? jobStore.setTriage(triage, jobId: job.id)
        withAnimation(.snappy) {
            inbox.removeAll { $0.id == job.id }
        }
        if triage == "shortlisted" {
            activityStore.log("shortlisted", "\(job.title) at \(job.company)", jobId: job.id)
        }
        stats = (try? jobStore.stats()) ?? stats
        pipeline = (try? jobStore.jobs(triage: "shortlisted")) ?? pipeline
    }

    /// AI engine for scoring/tailoring. Mock in UI tests; otherwise routes
    /// between the configured endpoint and Apple's on-device model.
    var aiEngine: any AIEngine {
        if CommandLine.arguments.contains("-UseMockAI") {
            return MockAIEngine.standardFixtures()
        }
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
        inbox.filter { ($0.fitScore ?? 0) <= 0 }
    }

    /// The job currently handed off to Safari for Apply Assist.
    var pendingApplyJob: Job?

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

    func score(_ job: Job) async {
        busyJobIds.insert(job.id)
        defer { busyJobIds.remove(job.id); refresh() }
        let result = await ScoringService.score(job: job, profile: config.profile,
                                                config: config, engine: aiEngine)
        try? jobStore.setScore(jobId: job.id, score: result.score,
                               reasoning: result.reasoning,
                               matchReport: result.matchReportJSON)
        activityStore.log("scored", "\(job.title): \(Int(result.score))/100", jobId: job.id)
    }

    /// Score up to `cap` unscored inbox jobs. Runs sequentially — never a
    /// concurrent fan-out — so a batch can't stampede the endpoint and the
    /// Stop button halts the run after at most one more in-flight call. `cap`
    /// (config.ai.scoreAllCap) is the hard ceiling on calls per run.
    func scoreAll(cap: Int) {
        guard !isScoringAll else { return }
        let batch = Array(unscoredInboxJobs.prefix(max(0, cap)))
        guard !batch.isEmpty else { return }
        isScoringAll = true
        scoreAllTotal = batch.count
        scoreAllDone = 0
        scoreAllTask = Task { @MainActor in
            for job in batch {
                if Task.isCancelled { break }
                await scoreOne(job)
                scoreAllDone += 1
            }
            let done = scoreAllDone
            let stopped = Task.isCancelled
            isScoringAll = false
            scoreAllTask = nil
            refresh()
            activityStore.log("scored_batch",
                "Scored \(done) job\(done == 1 ? "" : "s")\(stopped ? " (stopped early)" : "")")
        }
    }

    /// Halt an in-progress Score-all run — the hard kill switch. The current
    /// job's call finishes, then the loop exits before the next one starts.
    func cancelScoreAll() {
        scoreAllTask?.cancel()
    }

    /// Score one job and persist the result, without the per-job activity-log
    /// entry or UI refresh that the interactive `score(_:)` performs.
    private func scoreOne(_ job: Job) async {
        let result = await ScoringService.score(job: job, profile: config.profile,
                                                config: config, engine: aiEngine)
        try? jobStore.setScore(jobId: job.id, score: result.score,
                               reasoning: result.reasoning,
                               matchReport: result.matchReportJSON)
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
        let style = HonestyConfig.Style(rawValue: application.stylePreset) ?? .standard
        let resumeData = try ResumeDocxGenerator.generate(content: content,
                                                          profile: config.profile, style: style)
        let resumeURL = try FileVault.write(resumeData, jobId: job.id, kind: .resume, format: .docx)
        let coverData = try CoverLetterDocxGenerator.generate(
            content: application.coverLetterContent, profile: config.profile,
            jobTitle: job.title, company: job.company)
        let coverURL = try FileVault.write(coverData, jobId: job.id, kind: .coverLetter, format: .docx)
        try applicationStore.setDocumentPaths(id: application.id,
                                              resumePath: resumeURL.path,
                                              coverPath: coverURL.path)
    }

    /// Hand the job to Safari, where the Jobsmith Assist extension picks it
    /// up via the App Group active-job file.
    func applyInSafari(_ job: Job) {
        guard let url = URL(string: job.url) else {
            lastError = "This job has no application URL."
            return
        }
        try? ActiveJobStore().write(ActiveJob(job: job))
        pendingApplyJob = job
        activityStore.log("apply_started", "Opened \(job.title) in Safari", jobId: job.id)
        UIApplication.shared.open(url)
    }

    /// Called when the app returns to the foreground with an apply handoff
    /// outstanding — asks the user what happened.
    func resolvePendingApply(applied: Bool) {
        guard let job = pendingApplyJob else { return }
        pendingApplyJob = nil
        ActiveJobStore().clear()
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
    func fetchJobs() async {
        guard !isFetching else { return }
        isFetching = true
        defer { isFetching = false; refresh() }
        let summary = await FetchPipeline().run(
            config: config,
            sources: Array(config.search.enabledSources),
            jobStore: jobStore
        )
        let total = summary.inserted
        activityStore.log("fetched", "\(total) new job\(total == 1 ? "" : "s") from \(summary.perSource.count) sources")
        if !summary.failed.isEmpty || !summary.timedOut.isEmpty {
            let names = (summary.failed + summary.timedOut).joined(separator: ", ")
            lastError = "Some sources had trouble: \(names)"
        }
    }

    // MARK: - Deletion

    /// Delete specific jobs (Pipeline multi-select). The FK cascade removes
    /// each job's application row; document files are cleaned up separately.
    func deleteJobs(_ ids: Set<String>) {
        guard !ids.isEmpty else { return }
        for id in ids {
            try? jobStore.delete(jobId: id)
            FileVault.deleteDocuments(jobId: id)
        }
        activityStore.log("deleted", "Deleted \(ids.count) posting\(ids.count == 1 ? "" : "s")")
        refresh()
    }

    /// Clear every tracked posting and its tailored documents, plus the fetch
    /// caches — but keep the profile, settings, and answer bank. "Start the
    /// job list over" without a full reset.
    func deleteAllTrackedPostings() {
        wipeTables(["applications", "jobs", "activity_log",
                    "source_stats", "geo_cache", "ai_cache"])
        try? FileManager.default.removeItem(at: AppGroup.documentsDirectory)
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
