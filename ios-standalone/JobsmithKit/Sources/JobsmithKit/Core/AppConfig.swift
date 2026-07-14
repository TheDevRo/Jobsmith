import Foundation

/// App-wide configuration, persisted as JSON in the App Group container so
/// the Share extension reads the same settings. Mirrors the
/// desktop config.yaml sections.
public struct AppConfig: Codable, Equatable, Sendable {
    public var profile: Profile
    public var search: SearchConfig
    public var ai: AIConfig
    public var honesty: HonestyConfig
    public var apiKeys: APIKeys
    /// Prompt template overrides keyed by template id; defaults live in code.
    public var promptOverrides: [String: String]

    public init(profile: Profile = Profile(), search: SearchConfig = SearchConfig(),
                ai: AIConfig = AIConfig(), honesty: HonestyConfig = HonestyConfig(),
                apiKeys: APIKeys = APIKeys(), promptOverrides: [String: String] = [:]) {
        self.profile = profile; self.search = search; self.ai = ai
        self.honesty = honesty; self.apiKeys = apiKeys
        self.promptOverrides = promptOverrides
    }

    // Tolerant decoding, mirroring the sub-structs. Without it a single new
    // required section — or one section that fails to decode — would fail the
    // whole AppConfig and silently reset the user's profile, settings, and keys
    // on upgrade. Each section independently falls back to its defaults instead.
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        profile = c.lenient(Profile.self, .profile, Profile())
        search = c.lenient(SearchConfig.self, .search, SearchConfig())
        ai = c.lenient(AIConfig.self, .ai, AIConfig())
        honesty = c.lenient(HonestyConfig.self, .honesty, HonestyConfig())
        apiKeys = c.lenient(APIKeys.self, .apiKeys, APIKeys())
        promptOverrides = c.lenient([String: String].self, .promptOverrides, [:])
    }

    enum CodingKeys: String, CodingKey {
        case profile, search, ai, honesty, apiKeys, promptOverrides
    }
}

extension KeyedDecodingContainer {
    /// Decode a value, falling back to `fallback` when the key is missing *or*
    /// its payload is malformed. The building block of the tolerant decoders:
    /// one bad field must never take the whole config down with it.
    func lenient<T: Decodable>(_ type: T.Type, _ key: Key, _ fallback: @autoclosure () -> T) -> T {
        ((try? decodeIfPresent(type, forKey: key)) ?? nil) ?? fallback()
    }
}

public struct SearchConfig: Codable, Equatable, Sendable {
    public var keywords: [String]
    public var locations: [String]
    public var excludeKeywords: [String]
    public var minSalary: Int?
    public var maxAgeDays: Int?
    public var remoteOnly: Bool
    /// Per-company ATS watchlists (board slugs).
    public var greenhouseBoards: [String]
    public var leverCompanies: [String]
    public var ashbyBoards: [String]
    public var workableAccounts: [String]
    public var recruiteeCompanies: [String]
    /// Which sources are enabled for fetching.
    public var enabledSources: Set<String>
    /// Master switch for LinkedIn sourcing — see `LinkedInFeature`. Separate
    /// from `enabledSources` because it's the one source whose availability is
    /// a policy question, not a preference: turning it off here takes it out of
    /// the sources list and out of every fetch, foreground or background.
    public var linkedInEnabled: Bool

    public init(keywords: [String] = [], locations: [String] = ["Remote"],
                excludeKeywords: [String] = [], minSalary: Int? = nil,
                maxAgeDays: Int? = 7, remoteOnly: Bool = false,
                greenhouseBoards: [String] = [], leverCompanies: [String] = [],
                ashbyBoards: [String] = [], workableAccounts: [String] = [],
                recruiteeCompanies: [String] = [],
                enabledSources: Set<String> = ["remoteok", "weworkremotely", "arbeitnow", "greenhouse"],
                linkedInEnabled: Bool = true) {
        self.keywords = keywords; self.locations = locations
        self.excludeKeywords = excludeKeywords; self.minSalary = minSalary
        self.maxAgeDays = maxAgeDays; self.remoteOnly = remoteOnly
        self.greenhouseBoards = greenhouseBoards; self.leverCompanies = leverCompanies
        self.ashbyBoards = ashbyBoards; self.workableAccounts = workableAccounts
        self.recruiteeCompanies = recruiteeCompanies
        self.enabledSources = enabledSources
        self.linkedInEnabled = linkedInEnabled
    }

    // Tolerant decoding — a watchlist added in a later build must not reset the
    // user's keywords and sources.
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let d = SearchConfig()
        keywords = c.lenient([String].self, .keywords, d.keywords)
        locations = c.lenient([String].self, .locations, d.locations)
        excludeKeywords = c.lenient([String].self, .excludeKeywords, d.excludeKeywords)
        // Explicit null means "no limit" — distinct from an absent key, which
        // means "this build didn't write it", so decodeIfPresent won't do.
        minSalary = c.contains(.minSalary) ? ((try? c.decode(Int?.self, forKey: .minSalary)) ?? nil) : nil
        maxAgeDays = c.contains(.maxAgeDays)
            ? ((try? c.decode(Int?.self, forKey: .maxAgeDays)) ?? nil)
            : d.maxAgeDays
        remoteOnly = c.lenient(Bool.self, .remoteOnly, d.remoteOnly)
        greenhouseBoards = c.lenient([String].self, .greenhouseBoards, [])
        leverCompanies = c.lenient([String].self, .leverCompanies, [])
        ashbyBoards = c.lenient([String].self, .ashbyBoards, [])
        workableAccounts = c.lenient([String].self, .workableAccounts, [])
        recruiteeCompanies = c.lenient([String].self, .recruiteeCompanies, [])
        enabledSources = c.lenient(Set<String>.self, .enabledSources, d.enabledSources)
        linkedInEnabled = c.lenient(Bool.self, .linkedInEnabled, d.linkedInEnabled)
    }

    enum CodingKeys: String, CodingKey {
        case keywords, locations, excludeKeywords, minSalary, maxAgeDays, remoteOnly
        case greenhouseBoards, leverCompanies, ashbyBoards, workableAccounts
        case recruiteeCompanies, enabledSources, linkedInEnabled
    }
}

public struct AIConfig: Codable, Equatable, Sendable {
    public enum EngineKind: String, Codable, Sendable {
        case openAICompatible
        case appleOnDevice
    }

    /// Sentinel model id that routes a tier to Apple's on-device model
    /// instead of the OpenAI-compatible endpoint. Matches the id returned by
    /// `AppleOnDeviceEngine.listModels`, so it flows through the same per-tier
    /// model fields as any endpoint model name.
    public static let onDeviceModelID = "apple-on-device"

    public var engine: EngineKind
    /// OpenAI-compatible endpoint, e.g. http://192.168.1.7:1234/v1 (LM Studio)
    /// or https://openrouter.ai/api/v1.
    public var baseURL: String
    /// Bearer token. Stored here (App Group JSON) rather than a shared
    /// Keychain group: keychain sharing needs team-prefixed access groups
    /// that break free-account sideloads, and the desktop app keeps the same
    /// key in plaintext config.yaml. The container is app-sandboxed.
    public var apiKey: String
    /// Per-tier model assignment. Each holds an endpoint model name, the
    /// on-device sentinel (`onDeviceModelID`), or "" to fall back down the
    /// chain (utility → fast → strong).
    public var utilityModel: String
    public var fastModel: String
    public var strongModel: String
    public var temperature: Double
    public var maxTokens: Int
    /// Legacy flag from the old three-mode engine switch. Retained only so
    /// pre-existing configs decode; routing is now driven entirely by the
    /// per-tier models (see `migrateLegacyOnDeviceRouting`).
    public var preferOnDeviceForLightTasks: Bool
    /// Hard cap on how many jobs a single "Score all" run may process, so a
    /// batch can never fan out into unbounded API calls.
    public var scoreAllCap: Int

    public init(engine: EngineKind = .openAICompatible,
                baseURL: String = "http://localhost:1234/v1", apiKey: String = "",
                utilityModel: String = "", fastModel: String = "", strongModel: String = "",
                temperature: Double = 0.7, maxTokens: Int = 16384,
                preferOnDeviceForLightTasks: Bool = false,
                scoreAllCap: Int = 25) {
        self.engine = engine; self.baseURL = baseURL; self.apiKey = apiKey
        self.utilityModel = utilityModel; self.fastModel = fastModel
        self.strongModel = strongModel
        self.temperature = temperature; self.maxTokens = maxTokens
        self.preferOnDeviceForLightTasks = preferOnDeviceForLightTasks
        self.scoreAllCap = scoreAllCap
    }

    // Tolerant decoding: fields added or removed across builds must not fail
    // (and thereby reset) the whole config. Missing keys fall back to
    // defaults; unknown keys (e.g. a retired `scoringTier`) are ignored.
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let d = AIConfig()
        engine = try c.decodeIfPresent(EngineKind.self, forKey: .engine) ?? d.engine
        baseURL = try c.decodeIfPresent(String.self, forKey: .baseURL) ?? d.baseURL
        apiKey = try c.decodeIfPresent(String.self, forKey: .apiKey) ?? d.apiKey
        utilityModel = try c.decodeIfPresent(String.self, forKey: .utilityModel) ?? ""
        fastModel = try c.decodeIfPresent(String.self, forKey: .fastModel) ?? ""
        strongModel = try c.decodeIfPresent(String.self, forKey: .strongModel) ?? ""
        temperature = try c.decodeIfPresent(Double.self, forKey: .temperature) ?? d.temperature
        maxTokens = try c.decodeIfPresent(Int.self, forKey: .maxTokens) ?? d.maxTokens
        preferOnDeviceForLightTasks = try c.decodeIfPresent(Bool.self, forKey: .preferOnDeviceForLightTasks) ?? false
        scoreAllCap = try c.decodeIfPresent(Int.self, forKey: .scoreAllCap) ?? d.scoreAllCap
        migrateLegacyOnDeviceRouting()
    }

    /// Older builds routed on-device through the engine kind plus a
    /// "light tasks" flag. Translate that intent into per-tier on-device
    /// assignments once, then retire the flags so the model fields are the
    /// single source of truth.
    private mutating func migrateLegacyOnDeviceRouting() {
        let alreadyMigrated = [strongModel, fastModel, utilityModel]
            .contains(AIConfig.onDeviceModelID)
        if !alreadyMigrated {
            if engine == .appleOnDevice {
                strongModel = AIConfig.onDeviceModelID
                fastModel = AIConfig.onDeviceModelID
                utilityModel = AIConfig.onDeviceModelID
            } else if preferOnDeviceForLightTasks {
                if fastModel.isEmpty { fastModel = AIConfig.onDeviceModelID }
                if utilityModel.isEmpty { utilityModel = AIConfig.onDeviceModelID }
            }
        }
        engine = .openAICompatible
        preferOnDeviceForLightTasks = false
    }

    /// Model name for a tier, walking the fallback chain
    /// (utility → fast → strong, then any non-empty, then "local-model").
    /// May return the on-device sentinel.
    public func model(for tier: ModelTier) -> String {
        chain(for: tier).first { !$0.isEmpty } ?? "local-model"
    }

    /// Whether this tier resolves to Apple's on-device model.
    public func usesOnDevice(for tier: ModelTier) -> Bool {
        model(for: tier) == AIConfig.onDeviceModelID
    }

    /// Endpoint model for a tier, skipping the on-device sentinel and empty
    /// slots — so a tier assigned on-device still resolves to a real endpoint
    /// model when the device model is unavailable or errors and we fall back.
    public func endpointModel(for tier: ModelTier) -> String {
        chain(for: tier).first { !$0.isEmpty && $0 != AIConfig.onDeviceModelID } ?? "local-model"
    }

    private func chain(for tier: ModelTier) -> [String] {
        switch tier {
        case .utility: return [utilityModel, fastModel, strongModel]
        case .fast: return [fastModel, strongModel]
        case .strong: return [strongModel, fastModel]
        }
    }

    private enum CodingKeys: String, CodingKey {
        case engine, baseURL, apiKey, utilityModel, fastModel, strongModel
        case temperature, maxTokens, preferOnDeviceForLightTasks, scoreAllCap
    }
}

public enum ModelTier: String, Codable, Sendable, CaseIterable {
    case utility, fast, strong
}

public struct HonestyConfig: Codable, Equatable, Sendable {
    public enum Level: String, Codable, Sendable, CaseIterable {
        case honest, tailored, embellished, fabricated
    }
    public enum Tone: String, Codable, Sendable, CaseIterable {
        case professional, conversational, enthusiastic
    }
    /// Visual preset for the generated resume *and* its matching cover letter.
    /// Mirrors resume_generator.py `_STYLES`.
    public enum Style: String, Codable, Sendable, CaseIterable {
        case executive, ledger, banner, compact, swiss

        public static let `default`: Style = .ledger

        /// Persisted configs (and `applications.style_preset` rows) written
        /// before the five-style lineup carry retired names — map them instead
        /// of failing to decode. Mirrors `LEGACY_STYLE_ALIASES`.
        public static func fromPersisted(_ raw: String) -> Style {
            switch raw.lowercased() {
            case "standard", "modern": return .ledger
            case "minimal": return .swiss
            default: return Style(rawValue: raw.lowercased()) ?? .default
            }
        }

        public var label: String { rawValue.capitalized }

        /// Executive and Swiss are deliberately monochrome — they ignore the
        /// user's accent choice (`accent_locked` in the Python presets).
        public var isMonochrome: Bool { self == .executive || self == .swiss }

        public var blurb: String {
            switch self {
            case .executive: return "Georgia serif, centered small-caps name over a double rule; monochrome."
            case .ledger:    return "Bold sans, accent stub bar, accent company names (recommended)."
            case .banner:    return "A solid ink band behind your name; the boldest look here."
            case .compact:   return "9.5pt and tight margins; fits a deep work history on one page."
            case .swiss:     return "No rules, no color; hierarchy from spacing and weight alone; monochrome."
            }
        }
    }

    /// User-selectable accent palette. `.default` keeps each preset's own
    /// accent. Mirrors resume_generator.py `ACCENT_CHOICES`.
    public enum ResumeAccent: String, Codable, Sendable, CaseIterable {
        case `default`, navy, burgundy, forest, plum, charcoal

        /// nil for `.default` — the preset's own accent stands.
        public var hex: String? {
            switch self {
            case .default:  return nil
            case .navy:     return "1F3A5F"
            case .burgundy: return "6D1F2C"
            case .forest:   return "1F4D3A"
            case .plum:     return "3D3A4F"
            case .charcoal: return "37404A"
            }
        }

        public var label: String { rawValue.capitalized }
    }

    public var level: Level
    public var coverLetterTone: Tone
    public var resumeStyle: Style
    /// Accent recolor for the accent-driven styles; ignored by monochrome ones.
    public var resumeAccent: ResumeAccent
    /// nil = include all roles; otherwise cap and let the LLM pick.
    public var maxResumeExperienceEntries: Int?
    public var aiEditTier: ModelTier
    /// Output format for generated resume/cover-letter documents.
    public var documentFormat: FileVault.Format

    public init(level: Level = .honest, coverLetterTone: Tone = .professional,
                resumeStyle: Style = .ledger, resumeAccent: ResumeAccent = .default,
                maxResumeExperienceEntries: Int? = nil,
                aiEditTier: ModelTier = .strong, documentFormat: FileVault.Format = .pdf) {
        self.level = level; self.coverLetterTone = coverLetterTone
        self.resumeStyle = resumeStyle
        self.resumeAccent = resumeAccent
        self.maxResumeExperienceEntries = maxResumeExperienceEntries
        self.aiEditTier = aiEditTier
        self.documentFormat = documentFormat
    }

    // Tolerant decoding: documentFormat and resumeAccent were added later, so
    // configs written by older builds (which lack the keys) must still decode
    // with their defaults rather than failing and resetting the whole config.
    // resumeStyle is decoded as a raw string so retired style names
    // (standard/minimal/modern) map forward instead of throwing.
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        level = try c.decodeIfPresent(Level.self, forKey: .level) ?? .honest
        coverLetterTone = try c.decodeIfPresent(Tone.self, forKey: .coverLetterTone) ?? .professional
        resumeStyle = Style.fromPersisted(
            try c.decodeIfPresent(String.self, forKey: .resumeStyle) ?? Style.default.rawValue)
        resumeAccent = ResumeAccent(
            rawValue: (try c.decodeIfPresent(String.self, forKey: .resumeAccent) ?? "default").lowercased()
        ) ?? .default
        maxResumeExperienceEntries = try c.decodeIfPresent(Int.self, forKey: .maxResumeExperienceEntries)
        aiEditTier = try c.decodeIfPresent(ModelTier.self, forKey: .aiEditTier) ?? .strong
        documentFormat = try c.decodeIfPresent(FileVault.Format.self, forKey: .documentFormat) ?? .pdf
    }
}

public struct APIKeys: Codable, Equatable, Sendable {
    public var adzunaAppID: String
    public var adzunaAppKey: String
    public var usajobsEmail: String
    public var usajobsAPIKey: String
    public var blsRegistrationKey: String
    /// LinkedIn `li_at` session cookie captured by the in-app sign-in. When it
    /// is set, the LinkedIn source and profile import run as the signed-in user
    /// — the preferred mode (`LinkedInFeature`). Guest scraping is what runs
    /// when it is empty.
    ///
    /// Unlike every other field here, this one is a live credential (it is
    /// account takeover if it leaks), so `ConfigStore` round-trips it through
    /// the Keychain instead of this struct's JSON — see `SecretStore`. It stays
    /// a plain property so callers don't have to care.
    public var linkedInCookie: String

    /// LinkedIn `JSESSIONID` session cookie captured alongside `li_at`. Its value
    /// doubles as LinkedIn's `csrf-token`, which the Voyager API (Easy Apply,
    /// authenticated actions) requires — `li_at` alone renders logged-in but the
    /// action POSTs 401. It is a session cookie (evicted sooner than the
    /// persistent `li_at`), so it is captured/re-injected each sign-in. Like
    /// `linkedInCookie` it is a live credential and is round-tripped through the
    /// Keychain by `ConfigStore` rather than this struct's JSON.
    public var linkedInJSessionId: String

    public init(adzunaAppID: String = "", adzunaAppKey: String = "",
                usajobsEmail: String = "", usajobsAPIKey: String = "",
                blsRegistrationKey: String = "", linkedInCookie: String = "",
                linkedInJSessionId: String = "") {
        self.adzunaAppID = adzunaAppID; self.adzunaAppKey = adzunaAppKey
        self.usajobsEmail = usajobsEmail; self.usajobsAPIKey = usajobsAPIKey
        self.blsRegistrationKey = blsRegistrationKey
        self.linkedInCookie = linkedInCookie
        self.linkedInJSessionId = linkedInJSessionId
    }

    // Tolerant decoding: fields added over time must not fail (and thereby
    // reset) configs written by older builds.
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        adzunaAppID = try c.decodeIfPresent(String.self, forKey: .adzunaAppID) ?? ""
        adzunaAppKey = try c.decodeIfPresent(String.self, forKey: .adzunaAppKey) ?? ""
        usajobsEmail = try c.decodeIfPresent(String.self, forKey: .usajobsEmail) ?? ""
        usajobsAPIKey = try c.decodeIfPresent(String.self, forKey: .usajobsAPIKey) ?? ""
        blsRegistrationKey = try c.decodeIfPresent(String.self, forKey: .blsRegistrationKey) ?? ""
        linkedInCookie = try c.decodeIfPresent(String.self, forKey: .linkedInCookie) ?? ""
        linkedInJSessionId = try c.decodeIfPresent(String.self, forKey: .linkedInJSessionId) ?? ""
    }
}
