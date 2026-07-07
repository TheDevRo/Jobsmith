import Foundation
import SwiftSoup

/// Parses a LinkedIn job detail page — port of the extraction half of
/// `_fetch_job_detail`. JSON-LD structured data is the primary extraction
/// method (most reliable); HTML selectors are the fallback.
enum LinkedInDetailParser {
    /// Everything a detail page can contribute to a search-phase job.
    struct Detail: Equatable, Sendable {
        var description: String?
        var salaryMin: Int?
        var salaryMax: Int?
        /// "hourly" | "annual" — only set when known and a value exists.
        var salaryPeriod: String?
        /// Backfill for cards that omitted location (from JSON-LD address).
        var location: String?
        var tags: [String] = []
        var isEasyApply: Bool = false
    }

    static func parse(html: String) -> Detail {
        var detail = Detail()
        guard let doc = try? SwiftSoup.parse(html) else { return detail }

        // --- Primary: JSON-LD structured data ---
        let ld = parseJSONLD(doc)
        if let ld {
            let desc = jsonString(ld["description"])
            if !desc.isEmpty {
                detail.description = JobFilters.cleanDescription(desc)
            }

            let (sMin, sMax, sPeriod) = extractSalaryFromLD(ld)
            if let sMin { detail.salaryMin = sMin }
            if let sMax { detail.salaryMax = sMax }
            if sPeriod != "unknown" && (sMin != nil || sMax != nil) {
                detail.salaryPeriod = sPeriod
            }

            // Location from JSON-LD (caller backfills only when the card had none).
            let jl = ld["jobLocation"]
            let entries: [[String: Any]]
            if let list = jl as? [Any] {
                entries = list.compactMap { $0 as? [String: Any] }
            } else if let dict = jl as? [String: Any] {
                entries = [dict]
            } else {
                entries = []
            }
            for entry in entries {
                let addr = jsonDict(entry["address"])
                let city = jsonString(addr["addressLocality"])
                let region = jsonString(addr["addressRegion"])
                let parts = [city, region].filter { !$0.isEmpty }
                if !parts.isEmpty {
                    detail.location = parts.joined(separator: ", ")
                    break
                }
            }

            // Employment type as tag
            if let empList = ld["employmentType"] as? [Any] {
                detail.tags = empList.map { jsonString($0) }
            } else {
                let emp = jsonString(ld["employmentType"])
                if !emp.isEmpty { detail.tags = [emp] }
            }
        }

        // --- Fallback: HTML selectors (if JSON-LD didn't give a description) ---
        if detail.description == nil || detail.description!.isEmpty {
            let descEl = firstMatch(doc, selectors: [
                "div[class~=show-more-less-html__markup]",
                "div[class~=description__text]",
                "section[class~=description]",
            ])
            if let descEl, let outer = try? descEl.outerHtml() {
                detail.description = JobFilters.cleanDescription(outer)
            }
        }

        // --- Fallback: HTML salary ---
        if detail.salaryMin == nil {
            var salaryEl = firstMatch(doc, selectors: [
                "div[class~=salary-main-rail__data-body]",
                "div[class~=compensation__salary]",
            ])
            if salaryEl == nil {
                for li in criteriaItems(doc) {
                    if let header = try? li.select("h3").first(),
                       ((try? header.text()) ?? "").lowercased().contains("salary") {
                        salaryEl = li
                        break
                    }
                }
            }
            if let salaryEl, let text = try? salaryEl.text() {
                let (sMin, sMax, sPeriod) = parseSalaryText(text)
                if let sMin { detail.salaryMin = sMin }
                if let sMax { detail.salaryMax = sMax }
                if sPeriod != "unknown" && (sMin != nil || sMax != nil) {
                    detail.salaryPeriod = sPeriod
                }
            }
        }

        // --- Fallback: HTML tags from job criteria ---
        if detail.tags.isEmpty {
            var tags: [String] = []
            for item in criteriaItems(doc) {
                if let valEl = try? item.select("span[class~=description__job-criteria-text]").first(),
                   let val = try? valEl.text(),
                   !val.isEmpty, val.lowercased() != "other" {
                    tags.append(val)
                }
            }
            detail.tags = tags
        }

        detail.isEasyApply = detectEasyApply(doc, ld: ld)
        return detail
    }

    private static func criteriaItems(_ doc: Document) -> [Element] {
        (try? doc.select("li[class~=description__job-criteria-item]"))?.array() ?? []
    }

    private static func firstMatch(_ doc: Document, selectors: [String]) -> Element? {
        for selector in selectors {
            if let el = try? doc.select(selector).first() { return el }
        }
        return nil
    }

    // MARK: - JSON-LD

    /// Extract the JSON-LD JobPosting blob embedded in the page, if present —
    /// port of `_parse_json_ld` (single object, list, or @graph wrapper).
    static func parseJSONLD(_ doc: Document) -> [String: Any]? {
        let scripts = (try? doc.select("script[type=application/ld+json]"))?.array() ?? []
        for script in scripts {
            var raw = script.data()
            if raw.isEmpty { raw = (try? script.html()) ?? "" }
            guard let data = decodeJSONLD(raw) else { continue }
            if let list = data as? [Any] {
                for case let item as [String: Any] in list
                where jsonString(item["@type"]) == "JobPosting" {
                    return item
                }
            } else if let dict = data as? [String: Any] {
                if jsonString(dict["@type"]) == "JobPosting" { return dict }
                for case let item as [String: Any] in jsonArray(dict["@graph"])
                where jsonString(item["@type"]) == "JobPosting" {
                    return item
                }
            }
        }
        return nil
    }

    private static func decodeJSONLD(_ raw: String) -> Any? {
        // SwiftSoup entity-escapes script text when reserialized; unescape
        // both variants before JSON parsing.
        let candidates = [raw, (try? Entities.unescape(raw)) ?? raw]
        for text in candidates {
            if let obj = try? JSONSerialization.jsonObject(with: Data(text.utf8)) {
                return obj
            }
        }
        return nil
    }

    /// Pull salary from JSON-LD baseSalary — port of `_extract_salary_from_ld`.
    /// Schema.org `unitText` specifies the period: HOUR, DAY, WEEK, MONTH,
    /// YEAR. Returns (min, max, period) where period is
    /// 'hourly' | 'annual' | 'unknown'. Day/week/month not handled distinctly.
    static func extractSalaryFromLD(_ ld: [String: Any]) -> (Int?, Int?, String) {
        guard let base = ld["baseSalary"] as? [String: Any],
              let value = base["value"] as? [String: Any] else {
            return (nil, nil, "unknown")
        }

        let unit = jsonString(value["unitText"]).uppercased()
        var period: String
        switch unit {
        case "HOUR": period = "hourly"
        case "YEAR": period = "annual"
        default: period = "unknown"
        }

        // Python truthiness: 0 / "" / null all mean "no value".
        var sMin = jsonInt(value["minValue"]).flatMap { $0 == 0 ? nil : $0 }
        var sMax = jsonInt(value["maxValue"]).flatMap { $0 == 0 ? nil : $0 }

        // Don't drop low values when LinkedIn explicitly says it's hourly.
        if period != "hourly" {
            if let lo = sMin, lo < 15000 { sMin = nil }
            if let hi = sMax, hi < 15000 { sMax = nil }
        }

        // If period unknown but values look hourly-shaped, infer.
        if period == "unknown", let lo = sMin, lo < 1000 {
            period = "hourly"
        }

        return (sMin, sMax, period)
    }

    // MARK: - Free-text salary

    private static let amountRegex = try! NSRegularExpression(
        pattern: "\\$\\s*([\\d,]+(?:\\.\\d+)?)(?:\\s*([kK])\\b)?")

    /// Extract a salary range from a string like '$80,000/yr - $120,000/yr'
    /// or '$25.00/hr - $30.00/hr' — port of `_parse_salary`. Returns
    /// (min, max, period); hourly values are preserved raw. The k-suffix is
    /// captured per amount ("$80k") instead of checking the whole string for
    /// the letter k — "401k" elsewhere in the text must not turn an hourly
    /// "$25" into "$25,000".
    static func parseSalaryText(_ text: String) -> (Int?, Int?, String) {
        let period = JobFilters.detectPayPeriod(text)

        let ns = text as NSString
        var nums: [Int] = []
        for m in amountRegex.matches(in: text, range: NSRange(location: 0, length: ns.length)) {
            let cleaned = ns.substring(with: m.range(at: 1)).replacingOccurrences(of: ",", with: "")
            guard var val = Double(cleaned) else { continue }
            if m.range(at: 2).location != NSNotFound { val *= 1000 }
            nums.append(Int(val))
        }
        guard !nums.isEmpty else { return (nil, nil, period) }

        // For annual postings, drop values that are obviously hourly rates
        // that leaked in as standalone numbers. For hourly keep everything.
        if period != "hourly" {
            nums = nums.filter { $0 >= 15000 }
        }

        if nums.count >= 2 { return (nums.min(), nums.max(), period) }
        if nums.count == 1 { return (nums[0], nil, period) }
        return (nil, nil, period)
    }

    // MARK: - Easy Apply detection (multi-signal)

    static func detectEasyApply(_ doc: Document, ld: [String: Any]?) -> Bool {
        // 1. JSON-LD: directApply field (most reliable)
        if let ld {
            if ld["directApply"] as? Bool == true { return true }
            // Some listings use applyMethod or potentialAction
            let applyMethod = ld["applyMethod"] ?? ld["potentialAction"]
            if let am = applyMethod as? [String: Any],
               jsonString(am["@type"]) == "ApplyAction" {
                return true
            }
            if let list = applyMethod as? [Any] {
                for case let am as [String: Any] in list
                where jsonString(am["@type"]) == "ApplyAction" {
                    return true
                }
            }
        }

        // 2. HTML: apply button with Easy Apply specific classes or text.
        // LinkedIn's apply button without "easy apply" text means external,
        // so only mark when the text explicitly says easy apply.
        let buttonClassSelector = "[class~=(?i)jobs-apply-button--top-card"
            + "|easy-apply|easyApply|jobs-s-apply|jobs-apply-button]"
        if let btn = ((try? doc.select(buttonClassSelector).first()) ?? nil),
           ((try? btn.text()) ?? "").lowercased().contains("easy apply") {
            return true
        }

        // 3. HTML: any short span/div/li/button/a saying "Easy Apply"
        // (badges, footers, labels) — not whole-page text, which would
        // false-positive on descriptions mentioning "easy apply".
        for tag in ["span", "div", "li", "button", "a"] {
            for el in (try? doc.select(tag))?.array() ?? [] {
                let elText = ((try? el.text()) ?? "").lowercased()
                if elText == "easy apply" || elText == "be an early applicant · easy apply" {
                    return true
                }
                if elText.count < 40 && elText.contains("easy apply") {
                    return true
                }
            }
        }

        // 4. HTML: data attributes LinkedIn uses for Easy Apply
        if ((try? doc.select("[data-is-easy-apply=true]").first()) ?? nil) != nil {
            return true
        }
        if ((try? doc.select("[data-job-apply-type=EASY_APPLY]").first()) ?? nil) != nil {
            return true
        }

        return false
    }
}
