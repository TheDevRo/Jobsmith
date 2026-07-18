import XCTest
@testable import JobsmithKit

/// Guards the prompt registry's UI metadata (ported from the desktop
/// `prompt_registry.py`) and the override plumbing the AI Prompts editor relies
/// on.
final class PromptRegistryMetadataTests: XCTestCase {

    /// The lowercase-identifier placeholder pattern shared by the Python and
    /// Swift registries. JSON braces like `{"score": ...}` never match.
    private static let placeholderRE =
        try! NSRegularExpression(pattern: "\\{([a-z][a-z0-9_]*)\\}")

    private func placeholders(in template: String) -> [String] {
        let ns = template as NSString
        var seen: [String] = []
        for m in Self.placeholderRE.matches(
            in: template, range: NSRange(location: 0, length: ns.length)) {
            let name = ns.substring(with: m.range(at: 1))
            if !seen.contains(name) { seen.append(name) }
        }
        return seen
    }

    func testOrderedInfosMirrorTemplateIds() {
        XCTAssertEqual(PromptRegistry.orderedInfos.map { $0.id },
                       PromptRegistry.templateIds,
                       "orderedInfos must be in the same order as the template ids")
    }

    func testEveryPromptHasCompleteMetadata() throws {
        for id in PromptRegistry.templateIds {
            let info = try XCTUnwrap(PromptRegistry.info(id), "missing PromptInfo for \(id)")
            XCTAssertEqual(info.id, id)
            XCTAssertFalse(info.label.trimmingCharacters(in: .whitespaces).isEmpty,
                           "\(id) has an empty label")
            XCTAssertFalse(info.group.trimmingCharacters(in: .whitespaces).isEmpty,
                           "\(id) has an empty group")
            XCTAssertFalse(info.description.trimmingCharacters(in: .whitespaces).isEmpty,
                           "\(id) has an empty description")
        }
    }

    func testEveryTemplatePlaceholderIsDocumented() throws {
        for id in PromptRegistry.templateIds {
            let template = try XCTUnwrap(PromptRegistry.defaultTemplate(id))
            let info = try XCTUnwrap(PromptRegistry.info(id))
            let declared = Set(info.variables.map { $0.name })
            for token in placeholders(in: template) {
                XCTAssertTrue(declared.contains(token),
                              "\(id) template uses {\(token)} but it isn't declared in variables")
            }
        }
    }

    func testEveryUnknownIdHasNoInfo() {
        XCTAssertNil(PromptRegistry.info("not_a_real_prompt"))
        XCTAssertNil(PromptRegistry.defaultTemplate("not_a_real_prompt"))
    }

    func testOverrideRoundTripsThroughAppConfig() throws {
        let id = "score_job_fit"
        let defaultTemplate = try XCTUnwrap(PromptRegistry.defaultTemplate(id))

        var config = AppConfig()
        // No override → the effective template is the default.
        XCTAssertEqual(PromptRegistry.template(id, config: config), defaultTemplate)

        // Setting an override wins.
        let custom = "Custom scoring prompt using {job_title} and {profile_summary}."
        config.promptOverrides[id] = custom
        XCTAssertEqual(PromptRegistry.template(id, config: config), custom)

        // Removing it falls back to the default.
        config.promptOverrides.removeValue(forKey: id)
        XCTAssertEqual(PromptRegistry.template(id, config: config), defaultTemplate)

        // A blank override is treated as no override (mirrors get_template).
        config.promptOverrides[id] = "   \n  "
        XCTAssertEqual(PromptRegistry.template(id, config: config), defaultTemplate)
    }
}
