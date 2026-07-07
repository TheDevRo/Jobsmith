import Foundation

/// Describes a single form field as detected from the DOM.
/// Port of `backend/auto_apply/models.py` FieldDescriptor. The CodingKeys are
/// the JS wire format — snake_case, exactly as the extension sends them.
public struct FieldDescriptor: Codable, Equatable, Sendable {
    /// Stable key used to reference this field.
    public var fieldId: String
    /// Text from <label>, aria-label, or nearby heading.
    public var label: String
    public var placeholder: String
    /// text|number|email|tel|url|select|textarea|checkbox|radio|file|date|password
    public var fieldType: String
    public var name: String
    /// For <select> / radio groups.
    public var options: [String]?
    public var required: Bool
    /// Nearby text snippet (e.g. fieldset legend) for ambiguous fields.
    public var extraContext: String
    /// HTML autocomplete attribute — high-precision matching hint.
    public var autocomplete: String

    enum CodingKeys: String, CodingKey {
        case fieldId = "field_id"
        case label
        case placeholder
        case fieldType = "field_type"
        case name
        case options
        case required
        case extraContext = "extra_context"
        case autocomplete
    }

    public init(fieldId: String, label: String = "", placeholder: String = "",
                fieldType: String = "text", name: String = "",
                options: [String]? = nil, required: Bool = false,
                extraContext: String = "", autocomplete: String = "") {
        self.fieldId = fieldId; self.label = label; self.placeholder = placeholder
        self.fieldType = fieldType; self.name = name; self.options = options
        self.required = required; self.extraContext = extraContext
        self.autocomplete = autocomplete
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        fieldId = try c.decode(String.self, forKey: .fieldId)
        label = try c.decodeIfPresent(String.self, forKey: .label) ?? ""
        placeholder = try c.decodeIfPresent(String.self, forKey: .placeholder) ?? ""
        fieldType = try c.decodeIfPresent(String.self, forKey: .fieldType) ?? "text"
        name = try c.decodeIfPresent(String.self, forKey: .name) ?? ""
        options = try c.decodeIfPresent([String].self, forKey: .options)
        required = try c.decodeIfPresent(Bool.self, forKey: .required) ?? false
        extraContext = try c.decodeIfPresent(String.self, forKey: .extraContext) ?? ""
        autocomplete = try c.decodeIfPresent(String.self, forKey: .autocomplete) ?? ""
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(fieldId, forKey: .fieldId)
        try c.encode(label, forKey: .label)
        try c.encode(placeholder, forKey: .placeholder)
        try c.encode(fieldType, forKey: .fieldType)
        try c.encode(name, forKey: .name)
        // Encode nil as explicit null, matching Pydantic model_dump().
        try c.encode(options, forKey: .options)
        try c.encode(required, forKey: .required)
        try c.encode(extraContext, forKey: .extraContext)
        try c.encode(autocomplete, forKey: .autocomplete)
    }
}

/// The mapped answer for a single field.
/// Port of `backend/auto_apply/models.py` FieldValue.
public struct FieldValue: Codable, Equatable, Sendable {
    public var fieldId: String
    /// The value to set (empty string → skip).
    public var value: String
    /// fill|select|check|upload|skip
    public var action: String
    public var confidence: Double
    /// profile|answer_bank|llm_generated|skip
    public var source: String

    enum CodingKeys: String, CodingKey {
        case fieldId = "field_id"
        case value
        case action
        case confidence
        case source
    }

    public init(fieldId: String, value: String, action: String = "fill",
                confidence: Double = 1.0, source: String = "profile") {
        self.fieldId = fieldId; self.value = value; self.action = action
        self.confidence = confidence; self.source = source
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        fieldId = try c.decode(String.self, forKey: .fieldId)
        value = try c.decode(String.self, forKey: .value)
        action = try c.decodeIfPresent(String.self, forKey: .action) ?? "fill"
        confidence = try c.decodeIfPresent(Double.self, forKey: .confidence) ?? 1.0
        source = try c.decodeIfPresent(String.self, forKey: .source) ?? "profile"
    }
}

/// Job context handed to the field mapper — the Swift twin of the Python
/// JobApplicationRequest fields the mapping prompt actually uses.
public struct ApplyJobContext: Codable, Equatable, Sendable {
    public var jobId: String
    public var title: String
    public var company: String
    public var url: String
    public var description: String

    enum CodingKeys: String, CodingKey {
        case jobId = "job_id"
        case title, company, url, description
    }

    public init(jobId: String = "", title: String = "", company: String = "",
                url: String = "", description: String = "") {
        self.jobId = jobId; self.title = title; self.company = company
        self.url = url; self.description = description
    }

    public init(job: Job) {
        self.init(jobId: job.id, title: job.title, company: job.company,
                  url: job.url, description: job.description)
    }

    /// Stub when the extension scans a page with no bound job.
    public static func stub(url: String, jobId: String = "") -> ApplyJobContext {
        ApplyJobContext(jobId: jobId, url: url)
    }
}
