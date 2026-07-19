import XCTest
import WebKit

/// End-to-end fixture tests for the injected Apply scripts (snapshot.js +
/// fill.js) in a real WKWebView — the layer the Swift mapping tests can't
/// reach. Loads a local HTML form, snapshots it, fills it with hand-built
/// items (the shape `ApplyBrowserView.buildFillItems` produces), and asserts
/// against both the reported statuses and the live DOM.
///
/// Covers the fix-plan's fixture matrix: a plain input, a controlled input
/// that reverts its value once, an input whose data-jobsmith-fid stamp is
/// dropped between snapshot and fill (human_selector re-locate), select /
/// multi-select / radio / checkbox paths, a Workday date segment, and
/// honeypot exclusion.
@MainActor
final class ApplyFillJSTests: XCTestCase {
    private var webView: WKWebView!
    private var snapshotJS = ""
    private var fillJS = ""

    private static let fixtureHTML = """
    <!DOCTYPE html><html><body>
      <form>
        <label for="name">Full name</label>
        <input id="name" name="name" type="text">

        <label for="email">Email address</label>
        <input id="email" name="email" type="email">

        <label for="reverty">Controlled field</label>
        <input id="reverty" name="reverty" type="text">

        <label for="stampless">Fallback field</label>
        <input id="stampless" name="stampless" type="text">

        <label for="state">State</label>
        <select id="state" name="state">
          <option>– Select –</option>
          <option>California</option>
          <option>Texas</option>
        </select>

        <label for="langs">Languages</label>
        <select id="langs" name="langs" multiple>
          <option>Python</option>
          <option>Go</option>
          <option>Rust</option>
        </select>

        <fieldset>
          <legend>Are you authorized to work?</legend>
          <label><input type="radio" name="auth" value="yes">Yes</label>
          <label><input type="radio" name="auth" value="no">No</label>
        </fieldset>

        <label><input id="terms" name="terms" type="checkbox">I agree</label>

        <label for="wd-month">Start date</label>
        <input id="wd-month" name="wd_month" type="text"
               data-automation-id="dateSectionMonth-input">

        <input id="trap" name="hpcsaf_field" type="text"
               style="position:absolute; left:-99999px">
      </form>
      <script>
        // Simulate a controlled input: the framework rejects the first
        // scripted set and snaps the value back — the second survives.
        (function () {
          const el = document.getElementById("reverty");
          let reverted = false;
          el.addEventListener("input", function () {
            if (!reverted && el.value) {
              reverted = true;
              setTimeout(function () { el.value = ""; }, 30);
            }
          });
        })();
      </script>
    </body></html>
    """

    override func setUpWithError() throws {
        let bundle = Bundle(for: ApplyFillJSTests.self)
        snapshotJS = try loadScript("snapshot", from: bundle)
        fillJS = try loadScript("fill", from: bundle)
        webView = WKWebView(frame: CGRect(x: 0, y: 0, width: 390, height: 844))
        webView.loadHTMLString(Self.fixtureHTML, baseURL: nil)
    }

    private func loadScript(_ name: String, from bundle: Bundle) throws -> String {
        guard let url = bundle.url(forResource: name, withExtension: "js") else {
            throw XCTSkip("\(name).js not bundled with the test target")
        }
        return try String(contentsOf: url, encoding: .utf8)
    }

    private func waitForLoad() async throws {
        // readyState is already "complete" on the initial blank document, so
        // wait for a fixture element to prove loadHTMLString has committed.
        for _ in 0..<100 {
            let loaded = (try? await webView.evaluateJavaScript(
                "!!(document.getElementById('name')) && document.readyState === 'complete'"))
                as? Bool ?? false
            if loaded { return }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        XCTFail("fixture page never finished loading")
    }

    private func snapshotFields() async throws -> [[String: Any]] {
        let raw = try await webView.evaluateJavaScript(snapshotJS)
        let dict = raw as? [String: Any] ?? [:]
        return dict["fields"] as? [[String: Any]] ?? []
    }

    private func field(_ fields: [[String: Any]], _ id: String) -> [String: Any]? {
        fields.first { ($0["field_id"] as? String) == id }
    }

    /// The minimal fill-item shape buildFillItems produces from a descriptor.
    private func item(_ d: [String: Any], value: String,
                      fieldType: String? = nil) -> [String: Any] {
        [
            "field_id": d["field_id"] as? String ?? "",
            "selector": d["_selector"] as? String ?? "",
            "human_selector": d["_human_selector"] as? String ?? "",
            "name": d["name"] as? String ?? "",
            "value": value,
            "action": "fill",
            "field_type": fieldType ?? (d["field_type"] as? String ?? "text"),
            "confidence": 1.0,
            "source": "profile",
            "options": d["options"] as? [String] ?? [],
            "required": false,
            "_combobox": d["_combobox"] as? Bool ?? false,
        ]
    }

    private func fill(_ items: [[String: Any]]) async throws -> [String: String] {
        _ = try await webView.evaluateJavaScript(fillJS)
        let out = try await webView.callAsyncJavaScript(
            "return await window.__jobsmithFillAndHighlight(items, {});",
            arguments: ["items": items],
            contentWorld: .page)
        let dict = out as? [String: Any] ?? [:]
        let results = dict["results"] as? [[String: Any]] ?? []
        var byId: [String: String] = [:]
        for r in results {
            if let fid = r["field_id"] as? String {
                byId[fid] = r["status"] as? String
            }
        }
        return byId
    }

    private func domValue(_ js: String) async throws -> String {
        (try await webView.evaluateJavaScript(js)) as? String ?? ""
    }

    // ------------------------------------------------------------------

    func testSnapshotDetectsFieldsAndExcludesHoneypot() async throws {
        try await waitForLoad()
        let fields = try await snapshotFields()
        let ids = Set(fields.compactMap { $0["field_id"] as? String })

        XCTAssertTrue(ids.isSuperset(of: ["name", "email", "reverty", "stampless",
                                          "state", "langs", "terms", "wd-month"]))
        XCTAssertFalse(ids.contains("trap"),
                       "off-screen honeypot must never be emitted")
        XCTAssertEqual(field(fields, "email")?["field_type"] as? String, "email")
        XCTAssertEqual(field(fields, "state")?["options"] as? [String],
                       ["– Select –", "California", "Texas"])
        // The radio group is captured once, under its group question.
        let radios = fields.filter { ($0["field_type"] as? String) == "radio" }
        XCTAssertEqual(radios.count, 1)
        XCTAssertTrue((radios.first?["extra_context"] as? String ?? "")
            .contains("authorized to work"))
        // Workday date segment gets a named label from its owning field.
        XCTAssertTrue((field(fields, "wd-month")?["label"] as? String ?? "")
            .contains("Month"))
    }

    func testFillCoversTextSelectRadioCheckboxAndFallbacks() async throws {
        try await waitForLoad()
        let fields = try await snapshotFields()

        // Drop the stamp on #stampless AFTER the snapshot — an SPA re-render
        // in miniature; fill.js must re-locate it via human_selector.
        _ = try await webView.evaluateJavaScript(
            "document.getElementById('stampless').removeAttribute('data-jobsmith-fid'); true")

        var items: [[String: Any]] = []
        for (id, value) in [("name", "Jane Q Doe"), ("email", "jane@example.com"),
                            ("reverty", "survives retries"), ("stampless", "found me"),
                            ("wd-month", "2023-06")] {
            guard let d = field(fields, id) else { XCTFail("missing \(id)"); return }
            items.append(item(d, value: value))
        }
        guard let state = field(fields, "state"), let langs = field(fields, "langs"),
              let auth = fields.first(where: { ($0["field_type"] as? String) == "radio" }),
              let terms = field(fields, "terms") else {
            XCTFail("missing select/radio/checkbox descriptors")
            return
        }
        items.append(item(state, value: "CA"))          // alias → "California"
        items.append(item(langs, value: "Python; Go"))  // multi-select split
        items.append(item(auth, value: "Yes"))
        items.append(item(terms, value: "Yes", fieldType: "checkbox"))

        let status = try await fill(items)

        XCTAssertEqual(status["name"], "filled")
        XCTAssertEqual(status["email"], "filled")
        XCTAssertEqual(status["reverty"], "filled",
                       "the verify-and-retry pass must beat a one-shot revert")
        XCTAssertEqual(status["stampless"], "filled",
                       "human_selector fallback must survive a dropped stamp")
        XCTAssertEqual(status["state"], "filled")
        XCTAssertEqual(status["langs"], "filled")
        XCTAssertEqual(status[auth["field_id"] as? String ?? ""], "filled")
        XCTAssertEqual(status["terms"], "filled")
        XCTAssertEqual(status["wd-month"], "filled")

        // And the DOM agrees.
        let name = try await domValue("document.getElementById('name').value")
        XCTAssertEqual(name, "Jane Q Doe")
        let reverty = try await domValue("document.getElementById('reverty').value")
        XCTAssertEqual(reverty, "survives retries")
        let stampless = try await domValue("document.getElementById('stampless').value")
        XCTAssertEqual(stampless, "found me")
        let state2 = try await domValue("document.getElementById('state').value")
        XCTAssertEqual(state2, "California")
        let langsPicked = try await domValue(
            "Array.from(document.getElementById('langs').selectedOptions)"
            + ".map(o => o.value).join(',')")
        XCTAssertEqual(langsPicked, "Python,Go")
        let authChecked = (try await webView.evaluateJavaScript(
            "document.querySelector('input[name=auth][value=yes]').checked")) as? Bool
        XCTAssertEqual(authChecked, true)
        let termsChecked = (try await webView.evaluateJavaScript(
            "document.getElementById('terms').checked")) as? Bool
        XCTAssertEqual(termsChecked, true)
        // Workday month segment reduced "2023-06" to the segment's number.
        let month = try await domValue("document.getElementById('wd-month').value")
        XCTAssertEqual(month, "6")
    }
}
