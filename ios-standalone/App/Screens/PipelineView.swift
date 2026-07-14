import SwiftUI
import JobsmithKit

/// Shortlisted jobs grouped by pipeline stage, with a hold-to-select mode for
/// bulk deletion.
struct PipelineView: View {
    @Environment(AppModel.self) private var model
    @AppStorage(AppStorageKey.jobSort) private var sortRaw = JobSort.bestMatch.rawValue

    @State private var path: [String] = []
    @State private var isSelecting = false
    @State private var selection: Set<String> = []
    @State private var showDeleteConfirm = false
    @State private var showScoreAllConfirm = false
    @State private var searchQuery = ""
    @State private var showSearch = false
    /// Empty = all boards. Non-empty restricts the list to those source slugs.
    @State private var selectedBoards: Set<String> = []

    private var scoreCap: Int { model.config.ai.scoreAllCap }
    private var unscoredCount: Int { model.unscoredPipelineJobs.count }
    /// A default run: unscored pipeline jobs, clamped to the user's cap.
    private var boundedCount: Int { min(unscoredCount, scoreCap) }

    private var sort: JobSort { JobSort(rawValue: sortRaw) ?? .bestMatch }

    /// Pipeline jobs after the search + board filters, before stage grouping.
    private var filteredPipeline: [Job] {
        JobListFilter.apply(model.pipeline, query: searchQuery, boards: selectedBoards)
    }
    private var availableBoards: [String] { JobListFilter.availableBoards(in: model.pipeline) }

    private var stages: [(String, [Job])] {
        let jobs = filteredPipeline
        // Stage is derived from job.status + application state; the store
        // keeps status on the job row (discovered → tailoring → review →
        // applied | manual).
        let order = ["discovered": 0, "tailoring": 1, "review": 2, "applied": 3, "manual": 4]
        let grouped = Dictionary(grouping: jobs) { $0.status }
        let labels = ["discovered": "Shortlisted", "tailoring": "Tailoring",
                      "review": "Ready to review", "applied": "Applied", "manual": "Manual"]
        return grouped
            .sorted { (order[$0.key] ?? 9) < (order[$1.key] ?? 9) }
            .map { (labels[$0.key] ?? $0.key.capitalized,
                     sort.sorted($0.value, conversion: model.conversionBySource)) }
    }

    // Select-all targets only what's currently visible (filtered) set.
    private var allIDs: Set<String> { Set(filteredPipeline.map(\.id)) }
    private var allSelected: Bool { !allIDs.isEmpty && selection == allIDs }

    var body: some View {
        NavigationStack(path: $path) {
            Group {
                if model.pipeline.isEmpty {
                    ContentUnavailableView {
                        Label("Nothing in flight", systemImage: "list.bullet.rectangle")
                    } description: {
                        Text("Shortlist jobs from the Inbox and they land here for scoring, tailoring, and applying.")
                    }
                } else {
                    VStack(spacing: 0) {
                        if showSearch {
                            JobSearchField(text: $searchQuery) {
                                withAnimation(.snappy) { showSearch = false }
                            }
                        }
                        if model.isScoringAll {
                            ScoreAllBanner(done: model.scoreAllDone, total: model.scoreAllTotal) {
                                model.cancelScoreAll()
                            }
                            .padding(.vertical, 12)
                        }
                        if filteredPipeline.isEmpty {
                            noMatchesState
                        } else {
                            List {
                                ForEach(stages, id: \.0) { stage, jobs in
                                    Section {
                                        ForEach(jobs) { job in
                                            row(job)
                                        }
                                    } header: {
                                        Eyebrow(text: "\(stage) · \(jobs.count)")
                                    }
                                }
                            }
                            .listStyle(.insetGrouped)
                        }
                    }
                }
            }
            .navigationTitle(isSelecting ? "\(selection.count) selected" : "Pipeline")
            .navigationBarTitleDisplayMode(isSelecting ? .inline : .large)
            .navigationDestination(for: String.self) { jobId in
                JobDetailView(jobId: jobId)
            }
            .toolbar { toolbarContent }
            .sensoryFeedback(.selection, trigger: isSelecting)
            .refreshable { model.refresh() }
            // A tapped reminder routes here (NotificationDelegate sets the id).
            .onChange(of: model.deepLinkedJobId) { _, jobId in
                guard let jobId else { return }
                path.append(jobId)
                model.deepLinkedJobId = nil
            }
            .confirmationDialog("Delete \(selection.count) posting\(selection.count == 1 ? "" : "s")?",
                                isPresented: $showDeleteConfirm, titleVisibility: .visible) {
                Button("Delete \(selection.count)", role: .destructive) {
                    let ids = selection
                    exitSelection()
                    model.deleteJobs(ids)
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("Removes each posting and its tailored documents. Shake your phone straight after to undo it.")
            }
            .confirmationDialog("\(unscoredCount) unscored job\(unscoredCount == 1 ? "" : "s")",
                                isPresented: $showScoreAllConfirm, titleVisibility: .visible) {
                Button("Score \(boundedCount)") {
                    model.scoreAll(cap: scoreCap, candidates: model.unscoredPipelineJobs)
                }
                if unscoredCount > scoreCap {
                    Button("Score all \(unscoredCount)") {
                        model.scoreAll(cap: unscoredCount, candidates: model.unscoredPipelineJobs)
                    }
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                if unscoredCount > scoreCap {
                    Text("One AI call per job. “Score \(boundedCount)” respects your cap of \(scoreCap); “Score all \(unscoredCount)” scores every unscored job. You can Stop anytime.")
                } else {
                    Text("One AI call per job. You can Stop anytime.")
                }
            }
        }
    }

    /// The outcome chip for a submitted job, if it has an application.
    private func outcome(_ job: Job) -> ApplicationOutcome? {
        guard job.status == "applied", let app = model.applicationsByJob[job.id] else { return nil }
        return ApplicationOutcome(rawValue: app.outcome)
    }

    private func row(_ job: Job) -> some View {
        let isChecked = selection.contains(job.id)
        // The chip sits outside the combined a11y element and the tap gestures —
        // it is its own control, and folding it in would make the menu
        // unreachable to VoiceOver and swallow its taps.
        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 12) {
                if isSelecting {
                    // Decorative: selection is announced via the `.isSelected` trait
                    // on the row, so the checkmark itself stays out of the a11y tree.
                    Image(systemName: isChecked ? "checkmark.circle.fill" : "circle")
                        .font(.title3)
                        .foregroundStyle(isChecked ? Theme.ember : .secondary)
                        .transition(.scale.combined(with: .opacity))
                        .accessibilityHidden(true)
                }
                JobRowView(job: job)
            }
            .contentShape(Rectangle())
            .accessibilityElement(children: .combine)
            .accessibilityAddTraits(isSelecting && isChecked ? [.isButton, .isSelected] : .isButton)
            .accessibilityHint(isSelecting ? (isChecked ? "Deselect" : "Select") : "Opens the full posting")
            .onTapGesture {
                if isSelecting { toggle(job.id) } else { path.append(job.id) }
            }
            // simultaneousGesture (not onLongPressGesture) so the enclosing List
            // doesn't swallow the press before it's recognized.
            .simultaneousGesture(
                LongPressGesture(minimumDuration: 0.45).onEnded { _ in
                    guard !isSelecting else { return }
                    withAnimation(.snappy) {
                        isSelecting = true
                        selection = [job.id]
                    }
                }
            )

            if !isSelecting, let current = outcome(job) {
                OutcomeChip(outcome: current) { model.setOutcome(jobId: job.id, $0) }
            }
        }
    }

    @ToolbarContentBuilder
    private var toolbarContent: some ToolbarContent {
        if isSelecting {
            ToolbarItem(placement: .topBarLeading) {
                Button("Cancel") { exitSelection() }
            }
            ToolbarItem(placement: .topBarTrailing) {
                Button(allSelected ? "Deselect all" : "Select all") {
                    withAnimation(.snappy) { selection = allSelected ? [] : allIDs }
                }
            }
            ToolbarItem(placement: .topBarTrailing) {
                Button(role: .destructive) {
                    showDeleteConfirm = true
                } label: {
                    Label("Delete", systemImage: "trash")
                }
                .tint(.red)
                .disabled(selection.isEmpty)
            }
        } else {
            ToolbarItem(placement: .topBarTrailing) {
                Button {
                    withAnimation(.snappy) { showSearch.toggle() }
                } label: {
                    Label("Search", systemImage: showSearch ? "magnifyingglass.circle.fill" : "magnifyingglass")
                }
            }
            ToolbarItem(placement: .topBarTrailing) {
                Menu {
                    Picker("Sort by", selection: $sortRaw) {
                        ForEach(JobSort.allCases) { option in
                            Label(option.label, systemImage: option.systemImage).tag(option.rawValue)
                        }
                    }
                    Divider()
                    BoardFilterMenu(boards: availableBoards, selected: $selectedBoards)
                    Divider()
                    Button {
                        showScoreAllConfirm = true
                    } label: {
                        Label("Score jobs (\(unscoredCount))", systemImage: "flame")
                    }
                    .disabled(unscoredCount == 0 || model.isScoringAll)
                } label: {
                    Label("Sort, filter, score", systemImage: "ellipsis.circle")
                }
            }
        }
    }

    private var noMatchesState: some View {
        ContentUnavailableView {
            Label("No matches", systemImage: "line.3.horizontal.decrease.circle")
        } description: {
            Text("No shortlisted jobs match your search or board filter.")
        } actions: {
            Button("Clear filters") { clearFilters() }
        }
        .frame(maxHeight: .infinity)
    }

    private func clearFilters() {
        searchQuery = ""
        selectedBoards = []
    }

    private func toggle(_ id: String) {
        if selection.contains(id) { selection.remove(id) } else { selection.insert(id) }
    }

    private func exitSelection() {
        withAnimation(.snappy) {
            isSelecting = false
            selection = []
        }
    }
}

struct JobRowView: View {
    let job: Job

    /// Spoken as one phrase, with the fit score as a number — the heat color
    /// alone can't carry it.
    private var accessibilityDescription: String {
        let source = SourceCatalog.displayName(for: job.source)
        var parts = ["\(job.title) at \(job.company.isEmpty ? source : job.company)"]
        parts.append(job.fitScore.map { "fit \(Int($0)) of 100" } ?? "not scored yet")
        if job.isRemote { parts.append("remote") }
        if !job.company.isEmpty { parts.append("from \(source)") }
        return parts.joined(separator: ", ")
    }

    var body: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(job.title)
                    .font(.subheadline.weight(.semibold))
                    .lineLimit(2)
                HStack(spacing: 6) {
                    let source = SourceCatalog.displayName(for: job.source)
                    Text(job.company.isEmpty ? source : job.company)
                    if job.isRemote { Text("· Remote") }
                    // Always surface the source so the "Source" sort is visible.
                    if !job.company.isEmpty { Text("· \(source)") }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
            Spacer()
            HeatChip(score: job.fitScore)
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(accessibilityDescription)
    }
}
