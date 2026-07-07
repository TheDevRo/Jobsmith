import SwiftUI
import JobsmithKit

/// Shortlisted jobs grouped by pipeline stage.
struct PipelineView: View {
    @Environment(AppModel.self) private var model
    @AppStorage(AppStorageKey.jobSort) private var sortRaw = JobSort.bestMatch.rawValue

    private var sort: JobSort { JobSort(rawValue: sortRaw) ?? .bestMatch }

    private var stages: [(String, [Job])] {
        let jobs = model.pipeline
        // Stage is derived from job.status + application state; the store
        // keeps status on the job row (discovered → tailoring → review →
        // applied | manual).
        let order = ["discovered": 0, "tailoring": 1, "review": 2, "applied": 3, "manual": 4]
        let grouped = Dictionary(grouping: jobs) { $0.status }
        let labels = ["discovered": "Shortlisted", "tailoring": "Tailoring",
                      "review": "Ready to review", "applied": "Applied", "manual": "Manual"]
        return grouped
            .sorted { (order[$0.key] ?? 9) < (order[$1.key] ?? 9) }
            .map { (labels[$0.key] ?? $0.key.capitalized, sort.sorted($0.value)) }
    }

    var body: some View {
        NavigationStack {
            Group {
                if model.pipeline.isEmpty {
                    ContentUnavailableView {
                        Label("Nothing in flight", systemImage: "list.bullet.rectangle")
                    } description: {
                        Text("Shortlist jobs from the Inbox and they land here for scoring, tailoring, and applying.")
                    }
                } else {
                    List {
                        ForEach(stages, id: \.0) { stage, jobs in
                            Section {
                                ForEach(jobs) { job in
                                    NavigationLink(value: job.id) {
                                        JobRowView(job: job)
                                    }
                                }
                            } header: {
                                Eyebrow(text: "\(stage) · \(jobs.count)")
                            }
                        }
                    }
                    .listStyle(.insetGrouped)
                }
            }
            .navigationTitle("Pipeline")
            .navigationDestination(for: String.self) { jobId in
                JobDetailView(jobId: jobId)
            }
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Menu {
                        Picker("Sort by", selection: $sortRaw) {
                            ForEach(JobSort.allCases) { option in
                                Label(option.label, systemImage: option.systemImage).tag(option.rawValue)
                            }
                        }
                    } label: {
                        Label("Sort", systemImage: "arrow.up.arrow.down")
                    }
                }
            }
            .refreshable { model.refresh() }
        }
    }
}

struct JobRowView: View {
    let job: Job

    var body: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(job.title)
                    .font(.subheadline.weight(.semibold))
                    .lineLimit(2)
                HStack(spacing: 6) {
                    Text(job.company.isEmpty ? job.source : job.company)
                    if job.isRemote { Text("· Remote") }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
            Spacer()
            HeatChip(score: job.fitScore)
        }
        .padding(.vertical, 2)
    }
}
