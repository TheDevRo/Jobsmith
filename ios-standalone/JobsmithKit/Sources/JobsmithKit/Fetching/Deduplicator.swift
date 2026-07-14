import Foundation

/// Cross-source duplicate removal, ported from `fetch_all_jobs`: URL dedup
/// first, then a normalized (title, company, location) identity key so the
/// same posting on multiple boards with different URLs collapses. Location is
/// part of the key so multi-office postings of the same role survive.
public enum Deduplicator {
    public static func dedupe(_ jobs: [NormalizedJob]) -> [NormalizedJob] {
        // URL pass — first occurrence wins. Jobs without a URL are never
        // dropped here (Python: `if url and url in seen`); the identity-key
        // pass below still gets a chance to merge them.
        var seenURLs = Set<String>()
        var combined: [NormalizedJob] = []
        for job in jobs {
            if !job.url.isEmpty {
                if seenURLs.contains(job.url) { continue }
                seenURLs.insert(job.url)
            }
            combined.append(job)
        }

        var seenKeys = Set<IdentityKey>()
        var unique: [NormalizedJob] = []
        for job in combined {
            if let key = identityKey(job) {
                if seenKeys.contains(key) { continue }
                seenKeys.insert(key)
            }
            unique.append(job)
        }
        return unique
    }

    struct IdentityKey: Hashable {
        let title: String
        let company: String
        let location: String
    }

    /// Normalized identity, or nil when title or company is missing — we
    /// never merge on incomplete identity.
    static func identityKey(_ job: NormalizedJob) -> IdentityKey? {
        let title = norm(job.title)
        let company = norm(job.company)
        guard !title.isEmpty, !company.isEmpty else { return nil }
        return IdentityKey(title: title, company: company, location: norm(job.location))
    }

    /// Lowercase and collapse non-word-character runs to single spaces
    /// (Python `re.sub(r"\W+", " ", s.lower()).strip()`).
    static func norm(_ s: String) -> String {
        s.lowercased()
            .replacingOccurrences(of: "\\W+", with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespaces)
    }
}

/// The same dedup rules as `Deduplicator`, applied to a stream of batches
/// instead of one merged array — what the pipeline needs now that it upserts a
/// source's jobs the moment they arrive rather than pooling every source first.
///
/// The one rule the batch version can't express: a job may legitimately arrive
/// **twice from its own source**. LinkedIn delivers a posting from the search
/// page with an empty description, then re-delivers it once the detail page has
/// been scraped. Keying only on URL/identity would discard that second, richer
/// copy as a duplicate. So each claim records *who* made it — a re-delivery
/// under the same `source:externalId` passes through (the upsert backfills the
/// description), while the same posting from a *different* board is dropped, as
/// before.
public struct IncrementalDeduplicator {
    private var urlOwner: [String: String] = [:]
    private var identityOwner: [Deduplicator.IdentityKey: String] = [:]

    public init() {}

    /// The subset of `jobs` this run may persist, claiming each one's URL and
    /// identity for its owning posting.
    public mutating func admit(_ jobs: [NormalizedJob]) -> [NormalizedJob] {
        var admitted: [NormalizedJob] = []
        for job in jobs {
            let owner = job.source + ":" + job.externalId
            if !job.url.isEmpty, let claimed = urlOwner[job.url], claimed != owner { continue }
            let identity = Deduplicator.identityKey(job)
            if let identity, let claimed = identityOwner[identity], claimed != owner { continue }

            if !job.url.isEmpty { urlOwner[job.url] = owner }
            if let identity { identityOwner[identity] = owner }
            admitted.append(job)
        }
        return admitted
    }
}
