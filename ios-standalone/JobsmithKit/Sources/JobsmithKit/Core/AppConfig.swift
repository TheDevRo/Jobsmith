import Foundation

/// App-wide configuration, persisted as JSON in the App Group container so
/// the Safari and Share extensions read the same settings. Mirrors the
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

    public init(keywords: [String] = [], locations: [String] = ["Remote"],
                excludeKeywords: [String] = [], minSalary: Int? = nil,
                maxAgeDays: Int? = 7, remoteOnly: Bool = false,
                greenhouseBoards: [String] = [], leverCompanies: [String] = [],
                ashbyBoards: [String] = [], workableAccounts: [String] = [],
                recruiteeCompanies: [String] = [],
                enabledSources: Set<String> = ["remoteok", "weworkremotely", "arbeitnow", "greenhouse"]) {
        self.keywords = keywords; self.locations = locations
        self.excludeKeywords = excludeKeywords; self.minSalary = minSalary
        self.maxAgeDays = maxAgeDays; self.remoteOnly = remoteOnly
        self.greenhouseBoards = greenhouseBoards; self.leverCompanies = leverCompanies
        self.ashbyBoards = ashbyBoards; self.workableAccounts = workableAccounts
        self.recruiteeCompanies = recruiteeCompanies
        self.enabledSources = enabledSources
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
    public enum Style: String, Codable, Sendable, CaseIterable {
        case standard, minimal, modern
    }

    public var level: Level
    public var coverLetterTone: Tone
    public var resumeStyle: Style
    /// nil = include all roles; otherwise cap and let the LLM pick.
    public var maxResumeExperienceEntries: Int?
    public var aiEditTier: ModelTier

    public init(level: Level = .honest, coverLetterTone: Tone = .professional,
                resumeStyle: Style = .standard, maxResumeExperienceEntries: Int? = nil,
                aiEditTier: ModelTier = .strong) {
        self.level = level; self.coverLetterTone = coverLetterTone
        self.resumeStyle = resumeStyle
        self.maxResumeExperienceEntries = maxResumeExperienceEntries
        self.aiEditTier = aiEditTier
    }
}

public struct APIKeys: Codable, Equatable, Sendable {
    public var adzunaAppID: String
    public var adzunaAppKey: String
    public var usajobsEmail: String
    public var usajobsAPIKey: String
    public var blsRegistrationKey: String
    /// LinkedIn `li_at` session cookie captured by the in-app sign-in.
    /// Stored for future authenticated features; the job scraper stays on
    /// the guest API so the user's account is never used for scraping.
    public var linkedInCookie: String

    public init(adzunaAppID: String = "", adzunaAppKey: String = "",
                usajobsEmail: String = "", usajobsAPIKey: String = "",
                blsRegistrationKey: String = "", linkedInCookie: String = "") {
        self.adzunaAppID = adzunaAppID; self.adzunaAppKey = adzunaAppKey
        self.usajobsEmail = usajobsEmail; self.usajobsAPIKey = usajobsAPIKey
        self.blsRegistrationKey = blsRegistrationKey
        self.linkedInCookie = linkedInCookie
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
    }
}
