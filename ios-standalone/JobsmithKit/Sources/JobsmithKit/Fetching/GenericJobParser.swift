import Foundation
import CryptoKit
import SwiftSoup

/// Source-agnostic JobPosting parser (Python `_generic.py` + the manual-id
/// stamping from `manual.py`). Looks for schema.org JobPosting JSON-LD first
/// (covered by Lever, Workable, Ashby, and many ATS platforms), then falls
/// back to <title> / <meta> tags. Returns nil when no title can be found.
public enum GenericJobParser {
    public static func parse(html: String, url: URL) -> NormalizedJob? {
        guard let doc = try? SwiftSoup.parse(html) else { return nil }

        var title = ""
        var company = ""
        var location = ""
        var description = ""
        var salaryMin: Int?
        var salaryMax: Int?
        var salaryPeriod: String?
        var tags: [String] = []
        var datePosted = ""
        var isRemote = false

        var ld: [String: Any]?
        for script in (try? doc.select("script[type=application/ld+json]").array()) ?? [] {
            let raw = script.data()
            guard !raw.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                  let json = try? JSONSerialization.jsonObject(with: Data(raw.utf8))
            else { continue }
            if let hit = findJobPosting(json) {
                ld = hit
                break
            }
        }

        if let ld {
            title = jsonString(ld["title"]).trimmingCharacters(in: .whitespacesAndNewlines)

            if let org = ld["hiringOrganization"] as? [String: Any] {
                company = jsonString(org["name"]).trimmingCharacters(in: .whitespacesAndNewlines)
            } else if let org = ld["hiringOrganization"] as? String {
                company = org.trimmingCharacters(in: .whitespacesAndNewlines)
            }

            let desc = jsonString(ld["description"])
            if !desc.isEmpty { description = stripHTML(desc) }

            let (sMin, sMax, period) = parseSalary(ld)
            if let sMin, sMin != 0 { salaryMin = sMin }
            if let sMax, sMax != 0 { salaryMax = sMax }
            if period != "unknown" && (salaryMin != nil || salaryMax != nil) {
                salaryPeriod = period
            }

            let jl = ld["jobLocation"]
            let entries: [[String: Any]]
            if let list = jl as? [Any] { entries = list.compactMap { $0 as? [String: Any] } }
            else if let dict = jl as? [String: Any] { entries = [dict] }
            else { entries = [] }
            for entry in entries {
                let addr = jsonDict(entry["address"])
                let parts = [jsonString(addr["addressLocality"]), jsonString(addr["addressRegion"])]
                    .filter { !$0.isEmpty }
                if !parts.isEmpty {
                    location = parts.joined(separator: ", ")
                    break
                }
            }

            datePosted = jsonString(ld["datePosted"])

            if let empList = ld["employmentType"] as? [Any] {
                tags = empList.map { jsonString($0) }
            } else {
                let emp = jsonString(ld["employmentType"])
                if !emp.isEmpty { tags = [emp] }
            }

            if jsonString(ld["jobLocationType"]) == "TELECOMMUTE" { isRemote = true }
        }

        // Fallbacks: og:title / <title>, og:site_name, meta description.
        if title.isEmpty {
            title = (try? doc.select("meta[property=og:title]").first()?.attr("content")) ?? ""
            if title.isEmpty { title = (try? doc.title()) ?? "" }
            title = title.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        if company.isEmpty {
            company = ((try? doc.select("meta[property=og:site_name]").first()?.attr("content")) ?? "")
                .trimmingCharacters(in: .whitespacesAndNewlines)
        }
        if description.isEmpty {
            description = ((try? doc.select("meta[name=description]").first()?.attr("content")) ?? "")
                .trimmingCharacters(in: .whitespacesAndNewlines)
        }

        guard !title.isEmpty else { return nil }

        return NormalizedJob(
            source: "manual",
            externalId: manualExternalID(for: url.absoluteString),
            title: title,
            company: company,
            location: location,
            url: url.absoluteString,
            description: description,
            salaryMin: salaryMin,
            salaryMax: salaryMax,
            salaryPeriod: salaryPeriod,
            tags: tags,
            datePosted: datePosted,
            isRemote: isRemote)
    }

    /// Deterministic id so re-pasting the same URL is idempotent
    /// ("manual:" + first 16 hex chars of SHA1(url)).
    public static func manualExternalID(for url: String) -> String {
        let digest = Insecure.SHA1.hash(data: Data(url.utf8))
        let hex = digest.map { String(format: "%02x", $0) }.joined()
        return "manual:" + hex.prefix(16)
    }

    /// Recurse a JSON-LD node looking for @type == JobPosting (handles
    /// @graph nesting and top-level arrays).
    static func findJobPosting(_ node: Any) -> [String: Any]? {
        if let dict = node as? [String: Any] {
            if let t = dict["@type"] as? String, t == "JobPosting" { return dict }
            if let t = dict["@type"] as? [Any], t.contains(where: { jsonString($0) == "JobPosting" }) {
                return dict
            }
            if let graph = dict["@graph"] as? [Any] {
                for item in graph {
                    if let hit = findJobPosting(item) { return hit }
                }
            }
        } else if let list = node as? [Any] {
            for item in list {
                if let hit = findJobPosting(item) { return hit }
            }
        }
        return nil
    }

    static func parseSalary(_ ld: [String: Any]) -> (Int?, Int?, String) {
        guard let base = ld["baseSalary"] as? [String: Any],
              let value = base["value"] as? [String: Any] else { return (nil, nil, "unknown") }
        // Python `minValue or value` falls back on missing/zero/empty.
        func truthy(_ v: Any?) -> Any? {
            if let s = v as? String { return s.isEmpty ? nil : v }
            if let n = v as? NSNumber { return n.doubleValue == 0 ? nil : v }
            return nil
        }
        let sMin = jsonInt(truthy(value["minValue"]) ?? value["value"])
        let sMax = jsonInt(truthy(value["maxValue"]) ?? value["value"])
        let unit = jsonString(value["unitText"]).uppercased()
        var period = "unknown"
        if unit == "HOUR" { period = "hourly" }
        else if unit == "YEAR" || unit == "ANNUAL" { period = "annual" }
        if period == "unknown", let sMin, sMin < 1000 { period = "hourly" }
        return (sMin, sMax, period)
    }

    /// Approximation of BeautifulSoup's get_text(separator="\n"): tags become
    /// newlines, entities decode, 3+ blank lines collapse.
    static func stripHTML(_ html: String) -> String {
        guard !html.isEmpty else { return "" }
        var text = html.replacingOccurrences(of: "<[^>]+>", with: "\n", options: .regularExpression)
        text = (try? Entities.unescape(text)) ?? text
        text = text.replacingOccurrences(of: "\\n{3,}", with: "\n\n", options: .regularExpression)
        return text.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
