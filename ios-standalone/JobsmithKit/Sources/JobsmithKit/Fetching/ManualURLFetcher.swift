import Foundation

/// Ingest a single job posting from a user-pasted URL (Python `manual.py`).
/// Greenhouse URLs use the board API for clean structured data; anything else
/// goes through the generic JSON-LD parser. The result is stamped
/// source="manual" with a deterministic external id so re-pasting is
/// idempotent.
///
/// TODO: LinkedIn URLs should route through LinkedInSource's detail parser
/// once that source is ported; they currently fall through to the generic
/// parser.
public struct ManualURLFetcher: Sendable {
    public enum FetchError: Error, LocalizedError, Equatable {
        case invalidURL
        case httpStatus(Int)
        case noTitle

        public var errorDescription: String? {
            switch self {
            case .invalidURL: return "URL must be an http(s) link"
            case .httpStatus(let code): return "URL returned HTTP \(code)"
            case .noTitle: return "Could not extract a job title from that URL"
            }
        }
    }

    public init() {}

    public func fetchJob(from urlString: String) async throws -> NormalizedJob {
        let trimmed = urlString.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty,
              let url = URL(string: trimmed),
              let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https",
              let host = url.host, !host.isEmpty
        else { throw FetchError.invalidURL }

        var job: NormalizedJob
        if host.lowercased().contains("greenhouse.io"),
           let (slug, jobID) = Self.greenhouseSlugAndID(from: trimmed) {
            job = try await fetchViaGreenhouse(slug: slug, jobID: jobID, originalURL: trimmed)
        } else {
            job = try await fetchGeneric(url: url)
        }

        // Without a title the row is useless — surface a clear error.
        guard !job.title.isEmpty else { throw FetchError.noTitle }
        job.source = "manual"
        job.externalId = GenericJobParser.manualExternalID(for: trimmed)
        if job.url.isEmpty { job.url = trimmed }
        return job
    }

    /// Match boards.greenhouse.io and job-boards.greenhouse.io URLs.
    static func greenhouseSlugAndID(from url: String) -> (slug: String, id: String)? {
        guard let regex = try? NSRegularExpression(pattern: "greenhouse\\.io/([^/]+)/jobs/(\\d+)"),
              let match = regex.firstMatch(in: url, range: NSRange(url.startIndex..., in: url)),
              let slugRange = Range(match.range(at: 1), in: url),
              let idRange = Range(match.range(at: 2), in: url)
        else { return nil }
        return (String(url[slugRange]), String(url[idRange]))
    }

    private func fetchViaGreenhouse(slug: String, jobID: String,
                                    originalURL: String) async throws -> NormalizedJob {
        guard let apiURL = URL(string: "https://boards-api.greenhouse.io/v1/boards/\(slug)/jobs/\(jobID)")
        else { throw FetchError.invalidURL }
        var request = URLRequest(url: apiURL, timeoutInterval: 30)
        request.httpMethod = "GET"
        let (data, response) = try await HTTPClient.session.data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard status == 200 else { throw FetchError.httpStatus(status) }

        let detail = jsonDict(try JSONSerialization.jsonObject(with: data))
        let locationName = jsonString(jsonDict(detail["location"])["name"])
        var url = jsonString(detail["absolute_url"])
        if url.isEmpty { url = originalURL }

        return NormalizedJob(
            source: "manual",
            externalId: GenericJobParser.manualExternalID(for: originalURL),
            title: jsonString(detail["title"]),
            company: titleCased(slug: slug),
            location: locationName,
            url: url,
            description: JobFilters.cleanDescription(jsonString(detail["content"])),
            tags: jsonArray(detail["departments"])
                .map { jsonString(jsonDict($0)["name"]) }.filter { !$0.isEmpty },
            datePosted: jsonString(detail["updated_at"]),
            isRemote: locationName.lowercased().contains("remote"))
    }

    private func fetchGeneric(url: URL) async throws -> NormalizedJob {
        var request = URLRequest(url: url, timeoutInterval: 30)
        for (key, value) in HTTPClient.browserHeaders {
            request.setValue(value, forHTTPHeaderField: key)
        }
        let (data, response) = try await HTTPClient.session.data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard status == 200 else { throw FetchError.httpStatus(status) }
        let html = String(decoding: data, as: UTF8.self)
        guard let job = GenericJobParser.parse(html: html, url: url) else {
            throw FetchError.noTitle
        }
        return job
    }
}
