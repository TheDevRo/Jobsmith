import Foundation

/// Pure dispatcher for the Apply browser's message names
/// ({name, body} → {result} | {error, status}), the on-device stand-in for the
/// desktop's `/api/ext/*` HTTP surface — the shapes still mirror
/// backend/extension_api.py.
///
/// It is named for the Safari Web Extension it was originally written for. That
/// extension is gone: `ApplyBrowserView` now injects snapshot.js/fill.js into an
/// in-app WKWebView and calls `handle(name:body:)` directly, in-process
/// (`AppModel.mapApplyFields`). The message-name indirection is kept because it
/// is the same contract the desktop extension speaks.
///
/// Everything except `scan` is fast local IO — safe to run inline from the
/// caller.
public struct NativeMessageRouter: Sendable {
    let db: AppDatabase
    let engine: any AIEngine
    let loadConfig: @Sendable () async -> AppConfig
    let activeJobs: ActiveJobStore?
    let readDocument: (@Sendable (String, FileVault.Kind) -> Data?)?
    let version: String

    /// - Parameters:
    ///   - config: AppConfig provider (e.g. `{ await ConfigStore.shared.load() }`).
    ///   - activeJobStore: injectable for tests; defaults to the App Group file.
    ///   - readDocument: injectable document loader; defaults to FileVault DOCX.
    public init(db: AppDatabase,
                engine: any AIEngine,
                config: @escaping @Sendable () async -> AppConfig,
                activeJobStore: ActiveJobStore? = nil,
                readDocument: (@Sendable (String, FileVault.Kind) -> Data?)? = nil,
                version: String = (Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String) ?? "0") {
        self.db = db
        self.engine = engine
        self.loadConfig = config
        self.activeJobs = activeJobStore
        self.readDocument = readDocument
        self.version = version
    }

    public init(db: AppDatabase, engine: any AIEngine, configStore: ConfigStore) {
        self.init(db: db, engine: engine, config: { await configStore.load() })
    }

    // ------------------------------------------------------------------
    // Dispatch
    // ------------------------------------------------------------------

    struct RouterError: Error {
        let message: String
        let status: Int
    }

    public func handle(name: String, body: [String: Any]) async -> [String: Any] {
        do {
            switch name {
            case "health":
                return ok(["ok": true, "service": "jobsmith", "version": version])
            case "noop":
                return ok(["ok": true])
            case "getProfile":
                return ok(profilePayload(await loadConfig().profile))
            case "getJob":
                return ok(try getJob(body))
            case "getResumeFile":
                return ok(try fileFor(body, kind: .resume))
            case "getCoverFile":
                return ok(try fileFor(body, kind: .coverLetter))
            case "scan":
                return ok(try await scan(body))
            case "getActiveJob":
                return ok(activeJobPayload())
            case "markApplied":
                return ok(try markApplied(body))
            case "answer":
                return ok(try answer(body))
            case "saveAnswer":
                return ok(try saveAnswer(body))
            default:
                return err("Unknown message: \(name)", 404)
            }
        } catch let e as RouterError {
            return err(e.message, e.status)
        } catch {
            return err(String(describing: error), 500)
        }
    }

    private func ok(_ result: Any) -> [String: Any] {
        ["result": result]
    }

    private func err(_ message: String, _ status: Int) -> [String: Any] {
        ["error": message, "status": status]
    }

    private func requireString(_ body: [String: Any], _ keys: String...) throws -> String {
        for key in keys {
            if let value = body[key] as? String, !value.isEmpty { return value }
        }
        throw RouterError(message: "\(keys[0]) required", status: 400)
    }

    // ------------------------------------------------------------------
    // Routes
    // ------------------------------------------------------------------

    /// The autofill profile block served to the popup — same 16 snake_case
    /// keys as backend/extension_api.py GET /api/ext/profile.
    func profilePayload(_ p: Profile) -> [String: Any] {
        [
            "full_name": p.fullName,
            "email": p.email,
            "phone": p.phone,
            "linkedin": p.linkedin,
            "github": p.github,
            "portfolio": p.portfolio,
            "location": p.location,
            "street_address": p.streetAddress,
            "city": p.city,
            "state": p.state,
            "zip_code": p.zipCode,
            "desired_salary": p.desiredSalary,
            "work_authorization": p.workAuthorization,
            "sponsorship_required": p.sponsorshipRequired,
            "available_start": p.availableStart,
            "notice_period": p.noticePeriod,
        ]
    }

    private func getJob(_ body: [String: Any]) throws -> [String: Any] {
        let jobId = try requireString(body, "jobId", "job_id")
        guard let job = try JobStore(db).job(id: jobId) else {
            throw RouterError(message: "Job not found", status: 404)
        }
        return [
            "id": job.id,
            "title": job.title,
            "company": job.company,
            "url": job.url,
            "description": job.description,
        ]
    }

    private func fileFor(_ body: [String: Any], kind: FileVault.Kind) throws -> [String: Any] {
        let jobId = try requireString(body, "jobId", "job_id")
        let read = readDocument ?? { FileVault.read(jobId: $0, kind: $1, format: .docx) }
        guard let data = read(jobId, kind) else {
            let what = kind == .resume ? "Resume" : "Cover letter"
            throw RouterError(message: "\(what) not found — tailor the job first", status: 404)
        }
        return [
            "filename": "\(jobId)_\(kind.rawValue).docx",
            "base64": data.base64EncodedString(),
            "mime": FileVault.Format.docx.mime,
        ]
    }

    private func scan(_ body: [String: Any]) async throws -> [String: Any] {
        let url = body["url"] as? String ?? ""
        let jobId = (body["job_id"] as? String) ?? (body["jobId"] as? String)
        let rawFields = body["fields"] as? [Any] ?? []
        let fields: [FieldDescriptor]
        do {
            let data = try JSONSerialization.data(withJSONObject: rawFields)
            fields = try JSONDecoder().decode([FieldDescriptor].self, from: data)
        } catch {
            throw RouterError(message: "Invalid fields payload: \(error)", status: 400)
        }

        let jobStore = JobStore(db)
        var job = ApplyJobContext.stub(url: url, jobId: jobId ?? "")
        var storedJob: Job?
        if let jobId, !jobId.isEmpty, let row = try jobStore.job(id: jobId) {
            storedJob = row
            job = ApplyJobContext(job: row)
        }

        let config = await loadConfig()
        var profile = config.profile
        // Per-job desired-salary override (port of _contextual_desired_salary):
        // prefer the posting's stated max, else blank so the field shows up
        // unfilled instead of quoting a default that may not fit this job.
        profile.desiredSalary = Self.contextualDesiredSalary(storedJob)

        let mapper = FieldMapper(engine: engine, bank: AnswerBankMatcher(db))
        let values = await mapper.map(fields: fields, profile: profile,
                                      job: job, config: config)

        let encoder = JSONEncoder()
        let data = try encoder.encode(values)
        let dicts = (try JSONSerialization.jsonObject(with: data) as? [[String: Any]]) ?? []
        return ["fields": dicts, "count": dicts.count]
    }

    /// "" forces blank/skip; a "$NNN,NNN" string quotes the posting's high end.
    static func contextualDesiredSalary(_ job: Job?) -> String {
        guard let job else { return "" }
        var cand = job.salaryMax
        if cand == nil, let raw = job.salaryEstimate,
           let est = LenientJSON.parseObject(raw) {
            for key in ["salary_max", "estimated_salary_max", "max"] {
                if let n = LenientJSON.doubleValue(est[key]) {
                    cand = Int(n)
                    break
                }
            }
        }
        guard let n = cand, n > 0 else { return "" }
        let formatter = NumberFormatter()
        formatter.numberStyle = .decimal
        formatter.groupingSeparator = ","
        let grouped = formatter.string(from: NSNumber(value: n)) ?? String(n)
        return "$\(grouped)"
    }

    private func activeJobPayload() -> [String: Any] {
        let store = activeJobs ?? ActiveJobStore()
        guard let job = store.read() else {
            return ["job_id": NSNull()]
        }
        return [
            "job_id": job.jobId,
            "title": job.title,
            "company": job.company,
            "url": job.url,
            "saved_at": job.savedAt,
        ]
    }

    private func markApplied(_ body: [String: Any]) throws -> [String: Any] {
        let jobId = try requireString(body, "jobId", "job_id")
        let jobStore = JobStore(db)
        guard try jobStore.job(id: jobId) != nil else {
            throw RouterError(message: "Job not found", status: 404)
        }
        try jobStore.setStatus("applied", jobId: jobId)
        let applications = ApplicationStore(db)
        if let app = try applications.application(jobId: jobId) {
            try applications.updateStatus(id: app.id, status: "applied")
        }
        return ["ok": true, "job_id": jobId]
    }

    private func answer(_ body: [String: Any]) throws -> [String: Any] {
        let question = try requireString(body, "question")
        if let match = AnswerBankMatcher(db).findBestMatch(question: question) {
            return ["value": match.value, "source": "answer_bank", "confidence": 1.0]
        }
        return ["value": "", "source": "skip", "confidence": 0.0]
    }

    private func saveAnswer(_ body: [String: Any]) throws -> [String: Any] {
        let key = try requireString(body, "key")
        let label = body["label"] as? String ?? ""
        let value = body["value"] as? String ?? ""
        let keywords = (body["keywords"] as? [Any] ?? []).compactMap { $0 as? String }
        try AnswerBankStore(db).upsert(
            AnswerBankEntry(key: key, label: label, keywords: keywords, value: value))
        return ["ok": true, "key": key]
    }
}
