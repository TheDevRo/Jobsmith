import Foundation

/// Pure-logic apply-type classification, ported from the Python per-source
/// detectors (no network calls). Classifies a stored job's URL as:
/// - "easy_apply": the apply flow lives on the ATS's own domain and is
///   automatable in-app.
/// - "external": the URL points somewhere else (custom-domain boards, agency
///   sites) — the Applicant Assist flow handles those.
/// - "unknown": no URL stored; cannot classify.
public enum ApplyTypeDetector {
    /// Detector registry by job source. Sources without a detector (or the
    /// not-yet-ported linkedin/indeed ones) return "unknown".
    public static func detect(source: String, url: String?) -> String {
        switch source {
        case "usajobs": return usajobsApplyType(url: url)
        case "greenhouse": return greenhouseApplyType(url: url)
        case "lever": return leverApplyType(url: url)
        case "ashby": return classify(url: url, domain: "ashbyhq.com")
        case "workable": return classify(url: url, domain: "workable.com")
        case "recruitee": return classify(url: url, domain: "recruitee.com")
        default: return "unknown"
        }
    }

    public static func usajobsApplyType(url: String?) -> String {
        classify(url: url, domain: "usajobs.gov")
    }

    public static func greenhouseApplyType(url: String?) -> String {
        classify(url: url, domain: "greenhouse.io")
    }

    public static func leverApplyType(url: String?) -> String {
        classify(url: url, domain: "lever.co")
    }

    private static func classify(url: String?, domain: String) -> String {
        let trimmed = (url ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return "unknown" }
        let host = hostname(of: trimmed)
        if host == domain || host.hasSuffix("." + domain) { return "easy_apply" }
        return host.isEmpty ? "unknown" : "external"
    }

    /// Lowercased hostname with any leading "www." stripped (Python
    /// urlparse().netloc semantics, minus the port).
    static func hostname(of url: String) -> String {
        var host = (URL(string: url)?.host ?? "").lowercased()
        if host.hasPrefix("www.") { host = String(host.dropFirst(4)) }
        return host
    }
}
