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
                    model.applyInSafari(job)
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
