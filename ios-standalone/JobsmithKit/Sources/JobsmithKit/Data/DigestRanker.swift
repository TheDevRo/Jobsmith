import Foundation

/// "Which of these should I actually apply to today?"
///
/// Blends fit, freshness, salary and apply-effort — and then the term that makes
/// this more than another sort order: each job is weighted by how often its
/// source has actually replied to *you*, measured from your own outcome history.
/// A board that has never once responded stops crowding out one that does.
///
/// Mirrors the desktop's `database.get_digest`. Keep the two formulas in step —
/// the same job should rank the same on both devices.
public enum DigestRanker {

    public struct Weights: Sendable {
        public var fit: Double
        public var freshness: Double
        public var salary: Double
        public var effort: Double
        public var conversion: Double

        public init(fit: Double = 1.0, freshness: Double = 0.5, salary: Double = 0.3,
                    effort: Double = 0.2, conversion: Double = 0.5) {
            self.fit = fit
            self.freshness = freshness
            self.salary = salary
            self.effort = effort
            self.conversion = conversion
        }

        public static let `default` = Weights()
    }

    /// Below this many submitted applications a source's response rate is noise,
    /// so callers leave it out of `conversion` and it scores neutral.
    public static let minConversionSample = 3

    /// A source with no measured history scores neutral rather than zero — an
    /// unproven board shouldn't be buried like a proven-silent one.
    static let neutralConversion = 0.5

    public static func score(_ job: Job, conversion: [String: Double],
                             topSalary: Int, now: Date = Date(),
                             weights: Weights = .default) -> Double {
        let fit = (job.fitScore ?? 0) / 100.0
        let pay = Double(job.salaryMax ?? job.salaryMin ?? 0)
        let salary = topSalary > 0 ? pay / Double(topSalary) : 0
        let effort = job.isEasyApply ? 1.0 : 0.0
        let conv = conversion[job.source] ?? neutralConversion

        return weights.fit * fit
            + weights.freshness * freshness(job, now: now)
            + weights.salary * salary
            + weights.effort * effort
            + weights.conversion * conv
    }

    /// Linear decay to zero over a month. An undated posting sits mid-scale:
    /// unknown age is not the same as known-stale.
    static func freshness(_ job: Job, now: Date = Date()) -> Double {
        let stamp = job.datePosted.isEmpty ? job.dateDiscovered : job.datePosted
        guard let posted = ApplicationStore.parseEventDate(stamp) else { return 0.5 }
        let days = max(now.timeIntervalSince(posted) / 86_400, 0)
        return max(0, 1 - days / 30)
    }

    /// Highest-ranked first. Only scored jobs are ranked — an unscored job has no
    /// fit signal, so it sinks to the bottom (newest first among them) rather than
    /// being scored as a zero.
    public static func rank(_ jobs: [Job], conversion: [String: Double],
                            now: Date = Date(), weights: Weights = .default) -> [Job] {
        let topSalary = jobs.compactMap { $0.salaryMax ?? $0.salaryMin }.max() ?? 0
        let (scored, unscored) = (jobs.filter { $0.fitScore != nil },
                                  jobs.filter { $0.fitScore == nil })
        let ranked = scored
            .map { ($0, score($0, conversion: conversion, topSalary: topSalary,
                              now: now, weights: weights)) }
            .sorted { ($0.1, $0.0.dateDiscovered) > ($1.1, $1.0.dateDiscovered) }
            .map(\.0)
        return ranked + unscored.sorted { $0.dateDiscovered > $1.dateDiscovered }
    }
}
