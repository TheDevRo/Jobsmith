import Foundation

/// Turn a company *name* into live ATS board slugs so users never have to know
/// slugs. Port of desktop `_ATS_PROBES` / `_slug_candidates` / `_detect_boards_for`
/// (backend/routers/jobs.py): derive slug guesses from a name, probe every ATS's
/// public unauthenticated API concurrently, and keep the live board with the most
/// jobs per ATS.
public enum BoardDetector {

    /// One supported applicant-tracking system and how to probe it.
    public enum ATS: String, CaseIterable, Sendable {
        case greenhouse, lever, ashby, workable, recruitee

        /// Probe URL for a candidate slug.
        func probeURL(slug: String) -> URL? {
            switch self {
            case .greenhouse: return URL(string: "https://boards-api.greenhouse.io/v1/boards/\(slug)/jobs")
            case .lever:      return URL(string: "https://api.lever.co/v0/postings/\(slug)?mode=json")
            case .ashby:      return URL(string: "https://api.ashbyhq.com/posting-api/job-board/\(slug)")
            case .workable:   return URL(string: "https://apply.workable.com/api/v1/widget/accounts/\(slug)")
            case .recruitee:  return URL(string: "https://\(slug).recruitee.com/api/offers/")
            }
        }

        /// Human-facing board link for a slug.
        public func boardURL(slug: String) -> String {
            switch self {
            case .greenhouse: return "https://boards.greenhouse.io/\(slug)"
            case .lever:      return "https://jobs.lever.co/\(slug)"
            case .ashby:      return "https://jobs.ashbyhq.com/\(slug)"
            case .workable:   return "https://apply.workable.com/\(slug)"
            case .recruitee:  return "https://\(slug).recruitee.com"
            }
        }

        /// The `SearchConfig` slug array a match for this ATS is written into.
        public var keyPath: WritableKeyPath<SearchConfig, [String]> {
            switch self {
            case .greenhouse: return \.greenhouseBoards
            case .lever:      return \.leverCompanies
            case .ashby:      return \.ashbyBoards
            case .workable:   return \.workableAccounts
            case .recruitee:  return \.recruiteeCompanies
            }
        }

        /// The `enabledSources` id to turn on so this board is actually fetched.
        /// Lever is fetched by `GreenhouseSource`, so it shares the "greenhouse" id.
        public var enabledSourceID: String {
            switch self {
            case .greenhouse, .lever: return "greenhouse"
            case .ashby:              return "ashby"
            case .workable:           return "workable"
            case .recruitee:          return "recruitee"
            }
        }

        /// Short label for the UI ("Greenhouse", "Lever", …).
        public var label: String { rawValue.prefix(1).uppercased() + rawValue.dropFirst() }

        /// Extract the number of open jobs from a probe payload, or nil if the
        /// shape doesn't look like a real board for this ATS.
        func jobCount(from json: Any) -> Int? {
            switch self {
            case .greenhouse, .ashby:
                return (json as? [String: Any]).flatMap { ($0["jobs"] as? [Any])?.count }
            case .lever:
                return (json as? [Any])?.count
            case .workable:
                return (json as? [String: Any]).flatMap { ($0["jobs"] as? [Any])?.count }
            case .recruitee:
                return (json as? [String: Any]).flatMap { ($0["offers"] as? [Any])?.count }
            }
        }

        /// The company's real name if the payload carries one (Workable/Recruitee do).
        func companyName(from json: Any) -> String? {
            switch self {
            case .workable:
                return (json as? [String: Any])?["name"] as? String
            case .recruitee:
                let offers = (json as? [String: Any])?["offers"] as? [Any]
                return ((offers?.first) as? [String: Any])?["company_name"] as? String
            default:
                return nil
            }
        }
    }

    /// A live board discovered for a company name.
    public struct BoardMatch: Identifiable, Sendable, Equatable {
        public let ats: ATS
        public let slug: String
        public let jobs: Int
        public let companyName: String?
        public var boardURL: String { ats.boardURL(slug: slug) }
        public var id: String { "\(ats.rawValue):\(slug)" }

        public init(ats: ATS, slug: String, jobs: Int, companyName: String? = nil) {
            self.ats = ats; self.slug = slug; self.jobs = jobs; self.companyName = companyName
        }
    }

    private static let headers = ["User-Agent": "Jobsmith/1.0"]
    private static let probeConcurrency = 6

    /// Likely board slugs for a company name: "Notion Labs" → ["notionlabs",
    /// "notion-labs", "notion"]. Strips punctuation and common legal/brand
    /// suffixes. Pure and network-free — the unit-tested core.
    public static func slugCandidates(_ company: String) -> [String] {
        let lowered = company.lowercased()
        let base = lowered.unicodeScalars.map { scalar -> Character in
            CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyz0123456789 ").contains(scalar)
                ? Character(scalar) : " "
        }
        let words = String(base).split(separator: " ").map(String.init)
        guard !words.isEmpty else { return [] }

        var candidates = [words.joined(), words.joined(separator: "-")]
        let suffixes: Set<String> = ["inc", "labs", "hq", "io", "co", "ai", "gmbh", "ltd", "llc"]
        if words.count > 1, let last = words.last, suffixes.contains(last) {
            let trimmed = Array(words.dropLast())
            candidates.append(trimmed.joined())
            candidates.append(trimmed.joined(separator: "-"))
        }

        var seen = Set<String>(), out: [String] = []
        for candidate in candidates where !candidate.isEmpty && !seen.contains(candidate) {
            seen.insert(candidate)
            out.append(candidate)
        }
        return out
    }

    /// Probe every ATS with every slug guess for one company; return the best
    /// hit (most jobs) per ATS, sorted by job count descending.
    public static func detectBoards(company: String) async -> [BoardMatch] {
        let slugs = slugCandidates(company)
        guard !slugs.isEmpty else { return [] }

        let limiter = AsyncLimiter(probeConcurrency)
        var matches: [BoardMatch] = []
        await withTaskGroup(of: BoardMatch?.self) { group in
            for ats in ATS.allCases {
                for slug in slugs {
                    group.addTask {
                        await limiter.acquire()
                        defer { Task { await limiter.release() } }
                        return await probe(ats: ats, slug: slug)
                    }
                }
            }
            for await match in group {
                if let match { matches.append(match) }
            }
        }

        // Best hit per ATS.
        var best: [ATS: BoardMatch] = [:]
        for match in matches where best[match.ats] == nil || match.jobs > best[match.ats]!.jobs {
            best[match.ats] = match
        }
        return best.values.sorted { $0.jobs > $1.jobs }
    }

    /// One (ATS, slug) probe: 200 + a parseable board payload → a match.
    static func probe(ats: ATS, slug: String) async -> BoardMatch? {
        guard let url = ats.probeURL(slug: slug) else { return nil }
        do {
            let response = try await HTTPClient.fetchWithRetries(url, headers: headers, timeout: 8, retries: 0)
            return parseProbe(ats: ats, slug: slug, status: response.status, data: response.data)
        } catch {
            return nil
        }
    }

    /// Pure response-parsing half of a probe, separated so it is testable with
    /// fixture data (mirrors `GreenhouseSource.parseBoard` vs `fetch…`).
    static func parseProbe(ats: ATS, slug: String, status: Int, data: Data) -> BoardMatch? {
        guard status == 200,
              let json = try? JSONSerialization.jsonObject(with: data),
              let count = ats.jobCount(from: json) else { return nil }
        return BoardMatch(ats: ats, slug: slug, jobs: count, companyName: ats.companyName(from: json))
    }
}
