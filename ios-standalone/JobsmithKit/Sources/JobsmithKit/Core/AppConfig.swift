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
    /// Setup Assistant choice: "local" | "cloud" | "advanced" ("" = not chosen).
    /// Device-local: not in the settings-sync registry.
    public var setupMode: String
    /// Set when the setup wizard is finished or dismissed; gates the wizard on
    /// launch. Device-local. Configs from before the flag existed decode it as
    /// "has a profile", so existing users are never re-prompted.
    public var onboardingComplete: Bool

    public init(profile: Profile = Profile(), search: SearchConfig = SearchConfig(),
                ai: AIConfig = AIConfig(), honesty: HonestyConfig = HonestyConfig(),
                apiKeys: APIKeys = APIKeys(), promptOverrides: [String: String] = [:],
                setupMode: String = "", onboardingComplete: Bool = false) {
        self.profile = profile; self.search = search; self.ai = ai
        self.honesty = honesty; self.apiKeys = apiKeys
        self.promptOverrides = promptOverrides
        self.setupMode = setupMode
        self.onboardingComplete = onboardingComplete
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
        setupMode = c.lenient(String.self, .setupMode, "")
        onboardingComplete = c.lenient(Bool.self, .onboardingComplete, !profile.isEmpty)
    }

    enum CodingKeys: String, CodingKey {
        case profile, search, ai, honesty, apiKeys, promptOverrides, setupMode, onboardingComplete
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
    /// Strict companion to `minSalary`: also hide postings that state no pay
    /// (or a pay with an unknown period). Display-time only — the postings
    /// stay stored, so flipping this off brings them all back. Meaningless
    /// without a `minSalary` floor; the filter ignores it when the floor is
    /// off.
    public var requireStatedPay: Bool
    /// The Inbox swipe-deck sort order, stored in canonical snake_case
    /// (best_bets/best_match/newest/salary/company) so it round-trips through
    /// the settings-sync `inbox.sort` key without translation. The UI maps it
    /// to `JobSort`; an unrecognized value falls back to best_match at read.
    public var inboxSort: String
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
                requireStatedPay: Bool = false,
                inboxSort: String = "best_match",
                maxAgeDays: Int? = 7, remoteOnly: Bool = false,
                greenhouseBoards: [String] = [], leverCompanies: [String] = [],
                ashbyBoards: [String] = [], workableAccounts: [String] = [],
                recruiteeCompanies: [String] = [],
                enabledSources: Set<String> = ["remoteok", "weworkremotely", "arbeitnow", "greenhouse"],
                linkedInEnabled: Bool = false) {
        self.keywords = keywords; self.locations = locations
        self.excludeKeywords = excludeKeywords; self.minSalary = minSalary
        self.requireStatedPay = requireStatedPay
        self.inboxSort = inboxSort
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
        requireStatedPay = c.lenient(Bool.self, .requireStatedPay, d.requireStatedPay)
        inboxSort = c.lenient(String.self, .inboxSort, d.inboxSort)
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
        case keywords, locations, excludeKeywords, minSalary, requireStatedPay, inboxSort, maxAgeDays, remoteOnly
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

    /// Sentinel for the fast tier ("Scoring & form-fill") only: score jobs with
    /// the local NLI model (`LocalNLI`). It is NOT an LLM — the tier chain skips
    /// it, so every generative `.fast` call (essays, field mapping, retries)
    /// resolves to the next model down the chain (the strong tier) and the
    /// sentinel can never reach an engine. Device-local: never synced.
    public static let localMatchModelID = "local-match-model"

    /// Tier-model sentinels that name no endpoint model.
    static let sentinelModelIDs: Set<String> = [onDeviceModelID, localMatchModelID]

    /// A saved AI connection the user can switch to in one tap: endpoint, key,
    /// and the per-tier model assignments. Models belong to the preset because
    /// they are endpoint-specific — an LM Studio model name means nothing to
    /// OpenRouter, so switching the URL without the models would break routing.
    /// Device-local: not in the settings-sync registry, so saved keys never
    /// land in the sync folder.
    public struct SavedEndpoint: Codable, Equatable, Identifiable, Sendable {
        public var id: String
        public var name: String
        public var baseURL: String
        /// Bearer token. Like the live `AIConfig.apiKey`, `ConfigStore` keeps it
        /// out of the plaintext JSON, in the Keychain under
        /// `SecretKey.savedEndpointAPIKey(id)`.
        public var apiKey: String
        public var strongModel: String
        public var fastModel: String
        public var utilityModel: String

        public init(id: String = UUID().uuidString, name: String,
                    baseURL: String, apiKey: String,
                    strongModel: String = "", fastModel: String = "",
                    utilityModel: String = "") {
            self.id = id; self.name = name
            self.baseURL = baseURL; self.apiKey = apiKey
            self.strongModel = strongModel; self.fastModel = fastModel
            self.utilityModel = utilityModel
        }
    }

    public var engine: EngineKind
    /// OpenAI-compatible endpoint, e.g. http://192.168.1.x:1234/v1 (a server on
    /// your network) or https://openrouter.ai/api/v1. Empty on a new install.
    public var baseURL: String
    /// The Setup Assistant preset `baseURL` came from (`AIProviderPreset.name`),
    /// or "custom". Synced with `baseURL` (settings registry `ai.provider`).
    public var provider: String
    /// Bearer token for the live endpoint. A live credential, so `ConfigStore`
    /// round-trips it through the device Keychain (`SecretKey.aiAPIKey`) rather
    /// than this struct's JSON — see `SecretStore`. It stays a plain property so
    /// callers (and the opt-in settings sync, which reads the in-memory value)
    /// don't have to care; the Keychain redirect is at-rest only.
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
    /// The user's saved connections, switchable from the AI settings screen.
    public var savedEndpoints: [SavedEndpoint]
    /// "Local AI model (beta)": Apply Assist answers leftover fields from the
    /// profile with an on-device NLI model, and scoring falls back to it when the
    /// scoring LLM is unavailable. Device-local (the model lives on this device),
    /// so it is not in the settings-sync registry. See `LocalNLI`.
    public var nliBetaEnabled: Bool
    /// Run the Local match model on the Neural Engine instead of the CPU (experimental;
    /// Core ML falls back to the CPU for anything the Neural Engine can't run). Never the
    /// GPU: it aborts in Metal on an A17. Device-local.
    public var nliUseNeuralEngine: Bool
    /// "Refine top matches with the detailed model": after a scoring run, the NLI model
    /// re-scores the top 15% of Quick match scores (`ScoringService.refineTop`). Device-local.
    public var triageRefine: Bool
    /// Run Quick match's embedding model on the Neural Engine instead of the CPU (experimental,
    /// same rules as `nliUseNeuralEngine`). Device-local.
    public var triageUseNeuralEngine: Bool

    public init(engine: EngineKind = .openAICompatible,
                baseURL: String = "", apiKey: String = "", provider: String = "",
                utilityModel: String = "", fastModel: String = "", strongModel: String = "",
                temperature: Double = 0.7, maxTokens: Int = 16384,
                preferOnDeviceForLightTasks: Bool = false,
                scoreAllCap: Int = 25,
                savedEndpoints: [SavedEndpoint] = [],
                nliBetaEnabled: Bool = false, nliUseNeuralEngine: Bool = false,
                triageRefine: Bool = false, triageUseNeuralEngine: Bool = false) {
        self.engine = engine; self.baseURL = baseURL; self.apiKey = apiKey
        self.provider = provider
        self.utilityModel = utilityModel; self.fastModel = fastModel
        self.strongModel = strongModel
        self.temperature = temperature; self.maxTokens = maxTokens
        self.preferOnDeviceForLightTasks = preferOnDeviceForLightTasks
        self.scoreAllCap = scoreAllCap
        self.savedEndpoints = savedEndpoints
        self.nliBetaEnabled = nliBetaEnabled
        self.nliUseNeuralEngine = nliUseNeuralEngine
        self.triageRefine = triageRefine
        self.triageUseNeuralEngine = triageUseNeuralEngine
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
        provider = c.lenient(String.self, .provider, "")
        utilityModel = try c.decodeIfPresent(String.self, forKey: .utilityModel) ?? ""
        fastModel = try c.decodeIfPresent(String.self, forKey: .fastModel) ?? ""
        strongModel = try c.decodeIfPresent(String.self, forKey: .strongModel) ?? ""
        temperature = try c.decodeIfPresent(Double.self, forKey: .temperature) ?? d.temperature
        maxTokens = try c.decodeIfPresent(Int.self, forKey: .maxTokens) ?? d.maxTokens
        preferOnDeviceForLightTasks = try c.decodeIfPresent(Bool.self, forKey: .preferOnDeviceForLightTasks) ?? false
        scoreAllCap = try c.decodeIfPresent(Int.self, forKey: .scoreAllCap) ?? d.scoreAllCap
        savedEndpoints = try c.decodeIfPresent([SavedEndpoint].self, forKey: .savedEndpoints) ?? []
        nliBetaEnabled = c.lenient(Bool.self, .nliBetaEnabled, false)
        nliUseNeuralEngine = c.lenient(Bool.self, .nliUseNeuralEngine, false)
        triageRefine = c.lenient(Bool.self, .triageRefine, false)
        triageUseNeuralEngine = c.lenient(Bool.self, .triageUseNeuralEngine, false)
        migrateLegacyOnDeviceRouting()
        // Retired "Use it for all job scoring" toggle → the fast-tier picker choice.
        let legacy = try decoder.container(keyedBy: LegacyKeys.self)
        if nliBetaEnabled, legacy.lenient(Bool.self, .nliScoringPreferLocal, false) {
            fastModel = AIConfig.localMatchModelID
        }
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
    /// May return the on-device sentinel, never the local-match one.
    public func model(for tier: ModelTier) -> String {
        chain(for: tier).first { !$0.isEmpty } ?? "local-model"
    }

    /// Whether the fast tier is set to the local match model (scoring only).
    public var usesLocalMatchModel: Bool { fastModel == AIConfig.localMatchModelID }

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

    /// The LLM models a tier may resolve to, in order. The local-match sentinel
    /// is dropped here, centrally, so no engine or label ever sees it.
    private func chain(for tier: ModelTier) -> [String] {
        let models: [String]
        switch tier {
        case .utility: models = [utilityModel, fastModel, strongModel]
        case .fast: models = [fastModel, strongModel]
        case .strong: models = [strongModel, fastModel]
        }
        return models.filter { $0 != AIConfig.localMatchModelID }
    }

    /// Make `endpoint` the live connection. Sentinel tier assignments
    /// (on-device, local match model) are kept: they route to the device, not
    /// to any endpoint, so they survive an endpoint switch.
    public mutating func apply(_ endpoint: SavedEndpoint) {
        baseURL = endpoint.baseURL
        apiKey = endpoint.apiKey
        func keep(_ current: String, _ new: String) -> String {
            AIConfig.sentinelModelIDs.contains(current) ? current : new
        }
        strongModel = keep(strongModel, endpoint.strongModel)
        fastModel = keep(fastModel, endpoint.fastModel)
        utilityModel = keep(utilityModel, endpoint.utilityModel)
    }

    /// Snapshot the live connection as a named preset.
    public func capture(name: String, id: String = UUID().uuidString) -> SavedEndpoint {
        SavedEndpoint(id: id, name: name, baseURL: baseURL, apiKey: apiKey,
                      strongModel: strongModel, fastModel: fastModel,
                      utilityModel: utilityModel)
    }

    /// The saved preset the live connection currently matches, if any —
    /// compared on what a switch changes (URL + key), not on model tweaks.
    public func activeSavedEndpoint() -> SavedEndpoint? {
        savedEndpoints.first { $0.baseURL == baseURL && $0.apiKey == apiKey }
    }

    private enum CodingKeys: String, CodingKey {
        case engine, baseURL, apiKey, provider, utilityModel, fastModel, strongModel
        case temperature, maxTokens, preferOnDeviceForLightTasks, scoreAllCap
        case savedEndpoints, nliBetaEnabled, nliUseNeuralEngine, triageRefine, triageUseNeuralEngine
    }

    /// Keys still read (never written) so old configs migrate.
    private enum LegacyKeys: String, CodingKey { case nliScoringPreferLocal }
}

/// Cloud provider presets for the Setup Assistant — name, base URL, API-key
/// page. Twin of desktop `backend/ai_providers.json` (same rows, same order;
/// KitTests.SetupAssistantTests reads that file to enforce it). "Custom" is
/// not a row: the UI appends it.
public struct AIProviderPreset: Equatable, Sendable, Identifiable {
    public let name: String
    public let baseURL: String
    public let keyURL: String
    public var id: String { name }

    public static let all: [AIProviderPreset] = [
        .init(name: "OpenAI", baseURL: "https://api.openai.com/v1", keyURL: "https://platform.openai.com/api-keys"),
        .init(name: "Anthropic", baseURL: "https://api.anthropic.com/v1", keyURL: "https://console.anthropic.com/settings/keys"),
        .init(name: "Google Gemini", baseURL: "https://generativelanguage.googleapis.com/v1beta/openai", keyURL: "https://aistudio.google.com/apikey"),
        .init(name: "xAI (Grok)", baseURL: "https://api.x.ai/v1", keyURL: "https://console.x.ai"),
        .init(name: "Mistral", baseURL: "https://api.mistral.ai/v1", keyURL: "https://console.mistral.ai/api-keys"),
        .init(name: "Groq", baseURL: "https://api.groq.com/openai/v1", keyURL: "https://console.groq.com/keys"),
        .init(name: "DeepSeek", baseURL: "https://api.deepseek.com/v1", keyURL: "https://platform.deepseek.com/api_keys"),
        .init(name: "Together AI", baseURL: "https://api.together.xyz/v1", keyURL: "https://api.together.ai/settings/api-keys"),
        .init(name: "Fireworks", baseURL: "https://api.fireworks.ai/inference/v1", keyURL: "https://fireworks.ai/account/api-keys"),
        .init(name: "Cerebras", baseURL: "https://api.cerebras.ai/v1", keyURL: "https://cloud.cerebras.ai"),
        .init(name: "NVIDIA NIM", baseURL: "https://integrate.api.nvidia.com/v1", keyURL: "https://build.nvidia.com"),
        .init(name: "OpenRouter", baseURL: "https://openrouter.ai/api/v1", keyURL: "https://openrouter.ai/keys"),
    ]

    /// Custom-URL clean-up (desktop `obNormalizeUrl`): add https:// when there
    /// is no scheme, drop a pasted /chat/completions and trailing slashes.
    public static func normalize(_ raw: String) -> String {
        var u = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !u.isEmpty else { return "" }
        if u.range(of: "^[a-zA-Z][a-zA-Z0-9+.-]*://", options: .regularExpression) == nil { u = "https://" + u }
        while u.hasSuffix("/") { u.removeLast() }
        if u.lowercased().hasSuffix("/chat/completions") { u.removeLast("/chat/completions".count) }
        while u.hasSuffix("/") { u.removeLast() }
        return u
    }

    /// Custom addresses warn (never block) without /v1. Presets never warn.
    public static func lacksV1(_ raw: String) -> Bool {
        let u = normalize(raw)
        return !u.isEmpty && !u.hasSuffix("/v1")
    }

    /// Model ids that are obviously not chat models (desktop OB_NON_CHAT).
    public static func isNonChat(_ id: String) -> Bool {
        id.range(of: "embed|whisper|tts|rerank|moderation|dall-e|image",
                 options: [.regularExpression, .caseInsensitive]) != nil
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

    /// Workday ATS account email. Not secret, but a per-tenant credential
    /// identifier, so it lives here (out of `profile`) and is deliberately NOT
    /// synced — the sync profile map never carries it.
    public var workdayEmail: String

    /// Workday ATS account password. A live credential like `linkedInCookie`:
    /// `ConfigStore` round-trips it through the Keychain instead of this
    /// struct's JSON, and it is never synced.
    public var workdayPassword: String

    public init(adzunaAppID: String = "", adzunaAppKey: String = "",
                usajobsEmail: String = "", usajobsAPIKey: String = "",
                blsRegistrationKey: String = "", linkedInCookie: String = "",
                linkedInJSessionId: String = "",
                workdayEmail: String = "", workdayPassword: String = "") {
        self.adzunaAppID = adzunaAppID; self.adzunaAppKey = adzunaAppKey
        self.usajobsEmail = usajobsEmail; self.usajobsAPIKey = usajobsAPIKey
        self.blsRegistrationKey = blsRegistrationKey
        self.linkedInCookie = linkedInCookie
        self.linkedInJSessionId = linkedInJSessionId
        self.workdayEmail = workdayEmail
        self.workdayPassword = workdayPassword
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
        workdayEmail = try c.decodeIfPresent(String.self, forKey: .workdayEmail) ?? ""
        workdayPassword = try c.decodeIfPresent(String.self, forKey: .workdayPassword) ?? ""
    }
}
