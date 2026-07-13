import SwiftUI
import JobsmithKit

struct JobDetailView: View {
    @Environment(AppModel.self) private var model
    let jobId: String

    @State private var job: Job?

    var body: some View {
        Group {
            if let job {
                content(job)
            } else {
                ContentUnavailableView("Job not found", systemImage: "questionmark.circle")
            }
        }
        .onAppear { job = try? model.jobStore.job(id: jobId) }
        .navigationTitle(job?.company ?? "Job")
        .navigationBarTitleDisplayMode(.inline)
    }

    private func content(_ job: Job) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                HStack(alignment: .top, spacing: 16) {
                    VStack(alignment: .leading, spacing: 6) {
                        Text(job.title)
                            .font(.title2.weight(.bold))
                        Text(job.company)
                            .font(.headline)
                            .foregroundStyle(.secondary)
                        HStack(spacing: 6) {
                            if job.isRemote { detailChip("Remote") }
                            if !job.location.isEmpty { detailChip(job.location) }
                            detailChip(job.source)
                        }
                    }
                    Spacer()
                    if let score = job.fitScore {
                        HeatRing(score: score)
                    }
                }

                if let reasoning = job.fitReasoning, !reasoning.isEmpty {
                    VStack(alignment: .leading, spacing: 6) {
                        Eyebrow(text: "Why this score")
                        Text(reasoning)
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                    .padding(14)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 14).fill(Color.primary.opacity(0.04)))
                }

                salarySection(job)

                actionRow(job)

                if job.status == "applied", let application = model.applicationsByJob[job.id] {
                    outcomeSection(job, application)
                }

                VStack(alignment: .leading, spacing: 8) {
                    Eyebrow(text: "Description")
                    Text(job.description.isEmpty ? "No description captured." : job.description)
                        .font(.callout)
                        .textSelection(.enabled)
                }

                if !job.url.isEmpty, let url = URL(string: job.url) {
                    Link(destination: url) {
                        Label("View original posting", systemImage: "safari")
                            .font(.callout.weight(.medium))
                    }
                }
            }
            .padding(20)
        }
    }

    private func actionRow(_ job: Job) -> some View {
        let busy = model.busyJobIds.contains(job.id)
        return VStack(spacing: 10) {
            HStack(spacing: 10) {
                actionButton(busy ? "Working…" : "Score", system: "flame") {
                    Task { await model.score(job); reload() }
                }
                .disabled(busy)
                actionButton("Tailor", system: "wand.and.stars") {
                    Task { await model.tailor(job); reload() }
                }
                .disabled(busy)
                actionButton("Apply", system: "paperplane.fill", prominent: true) {
                    model.applyInApp(job)
                }
                .disabled(job.url.isEmpty)
            }
            if job.status == "review" || job.status == "applied" {
                NavigationLink {
                    DocumentReviewView(jobId: job.id)
                } label: {
                    Label("Review documents", systemImage: "doc.text")
                        .font(.callout.weight(.semibold))
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 10)
                }
                .buttonStyle(.bordered)
            }
        }
    }

    /// Where the application stands, plus the history that got it there. The
    /// history is worth showing: a rejection after an interview is a very
    /// different result from a rejection after silence, and the current outcome
    /// alone can't tell them apart.
    private func outcomeSection(_ job: Job, _ application: Application) -> some View {
        let current = ApplicationOutcome(rawValue: application.outcome) ?? .awaiting
        let history = (try? model.applicationStore.events(id: application.id)) ?? []
        return VStack(alignment: .leading, spacing: 10) {
            Eyebrow(text: "Outcome")
            Menu {
                OutcomeMenuItems(current: current) { model.setOutcome(jobId: job.id, $0) }
            } label: {
                HStack {
                    Label(current.label, systemImage: current.systemImage)
                        .font(.callout.weight(.semibold))
                        .foregroundStyle(current.tint)
                    Spacer()
                    Image(systemName: "chevron.up.chevron.down")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .padding(.vertical, 10)
                .padding(.horizontal, 12)
                .background(Theme.slate, in: RoundedRectangle(cornerRadius: 10))
            }
            .accessibilityLabel("Outcome: \(current.label)")
            .accessibilityHint("Change what the employer did")

            if history.count > 1 {
                VStack(alignment: .leading, spacing: 3) {
                    ForEach(history) { event in
                        let stage = ApplicationOutcome(rawValue: event.toOutcome)
                        Text("\(stage?.label ?? event.toOutcome) · \(shortDate(event.occurredAt))"
                             + (event.source == "rule" ? " · automatic" : ""))
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }
            }
        }
    }

    /// Events arrive from both platforms and the two stamp time differently —
    /// Swift emits `…060Z`, the desktop `…060761+00:00`. Neither ISO8601
    /// formatter accepts both, so parse the fixed `yyyy-MM-dd'T'HH:mm:ss` prefix
    /// they do share (always UTC) and ignore the fractional/zone tail.
    private static let eventDateParser: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        return f
    }()

    private func shortDate(_ iso: String) -> String {
        guard let date = Self.eventDateParser.date(from: String(iso.prefix(19))) else { return iso }
        return date.formatted(.dateTime.month(.abbreviated).day())
    }

    private func reload() {
        job = try? model.jobStore.job(id: jobId)
    }

    /// Stated salary when the posting has one; otherwise the market estimate
    /// (clearly labeled) or a button to fetch one.
    @ViewBuilder
    private func salarySection(_ job: Job) -> some View {
        if let min = job.salaryMin {
            VStack(alignment: .leading, spacing: 4) {
                Eyebrow(text: "Salary")
                Text(salaryRangeText(min: min, max: job.salaryMax, period: job.salaryPeriod))
                    .font(.callout.weight(.semibold))
            }
        } else if let raw = job.salaryEstimate,
                  let estimate = try? JSONDecoder().decode(SalaryEstimate.self,
                                                           from: Data(raw.utf8)) {
            VStack(alignment: .leading, spacing: 4) {
                Eyebrow(text: "Market estimate")
                Text(salaryRangeText(min: estimate.p25, max: estimate.p75, period: "annual"))
                    .font(.callout.weight(.semibold))
                Text("Estimated from \(estimate.source == "bls_oews" ? "BLS wage data" : "Adzuna market data") · \(estimate.confidence) confidence — not from the posting")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        } else {
            Button {
                Task {
                    let found = await model.estimateSalary(job)
                    reload()
                    if !found { model.lastError = "No salary data available for this role." }
                }
            } label: {
                Label("Estimate salary", systemImage: "dollarsign.circle")
                    .font(.callout.weight(.medium))
            }
            .disabled(model.busyJobIds.contains(job.id))
        }
    }

    private func salaryRangeText(min: Int, max: Int?, period: String) -> String {
        let fmt = { (v: Int) -> String in
            period == "hourly" ? "$\(v)/hr" : "$\(v.formatted(.number.grouping(.automatic)))"
        }
        if let max, max != min { return "\(fmt(min)) – \(fmt(max))" }
        return fmt(min)
    }

    private func actionButton(_ label: String, system: String, prominent: Bool = false,
                              action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(label, systemImage: system)
                .font(.callout.weight(.semibold))
                .frame(maxWidth: .infinity)
                .padding(.vertical, 10)
        }
        .buttonStyle(.bordered)
        .tint(prominent ? Theme.ember : nil)
    }

    private func detailChip(_ text: String) -> some View {
        Text(text)
            .font(.caption.weight(.medium))
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(Capsule().fill(Color.primary.opacity(0.06)))
            .foregroundStyle(.secondary)
    }
}
