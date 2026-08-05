import Foundation

// Cross-language verification tool for the LLM score-response parser.
// Compiled on the host together with the REAL JobsmithKit sources
// (LenientJSON.swift + ScoreResponseParser.swift) so it exercises the actual
// iOS parse chain — not a reimplementation.
//
//   ai-crosslang <fixtures.json>
//
// Reads tests/fixtures/ai_score_responses.json and prints, for each case,
// what the Swift parser produced:
//
//   {"cases": [{"name": ..., "parsed": null | {"score": Double,
//               "reasoning": String, "report": null | {…}}}]}
//
// The Python side (tests/test_crosslang_ai.py) runs the same fixtures through
// backend.ai_engine.parse_score_response and requires identical output.

func die(_ msg: String) -> Never { FileHandle.standardError.write(Data((msg + "\n").utf8)); exit(2) }

guard CommandLine.arguments.count == 2 else { die("usage: ai-crosslang <fixtures.json>") }

guard let raw = FileManager.default.contents(atPath: CommandLine.arguments[1]),
      let doc = try? JSONSerialization.jsonObject(with: raw) as? [String: Any],
      let cases = doc["cases"] as? [[String: Any]] else {
    die("could not read fixtures JSON")
}

var out: [[String: Any]] = []
for c in cases {
    guard let name = c["name"] as? String, let response = c["response"] as? String else {
        die("malformed fixture case")
    }
    var entry: [String: Any] = ["name": name]
    if let parsed = ScoreResponseParser.parse(response) {
        var p: [String: Any] = ["score": parsed.score, "reasoning": parsed.reasoning]
        if let reportJSON = parsed.matchReportJSON,
           let reportData = reportJSON.data(using: .utf8),
           let report = try? JSONSerialization.jsonObject(with: reportData) {
            p["report"] = report
        } else {
            p["report"] = NSNull()
        }
        entry["parsed"] = p
    } else {
        entry["parsed"] = NSNull()
    }
    out.append(entry)
}

let result = try! JSONSerialization.data(withJSONObject: ["cases": out], options: [.sortedKeys])
print(String(data: result, encoding: .utf8)!)
