import SwiftUI
import JobsmithKit

/// Swipe-to-triage deck: right shortlists, left dismisses, tap opens detail.
struct InboxView: View {
    @Environment(AppModel.self) private var model
    @State private var dragOffset: CGSize = .zero
    @State private var showAddByURL = false
    @State private var pastedURL = ""
    @AppStorage(AppStorageKey.jobSort) private var sortRaw = JobSort.bestMatch.rawValue
    @State private var showScoreAllConfirm = false

    private var sort: JobSort { JobSort(rawValue: sortRaw) ?? .bestMatch }
    private var sortedInbox: [Job] { sort.sorted(model.inbox) }
    /// How many jobs a Score-all run would actually touch: unscored, capped.
    private var scoreAllCount: Int {
        min(model.unscoredInboxJobs.count, model.config.ai.scoreAllCap)
    }

    var body: some View {
        NavigationStack {
            Group {
                if model.inbox.isEmpty {
                    emptyState
                } else {
                    deck
                }
            }
            .navigationTitle("Inbox")
            .navigationDestination(for: String.self) { jobId in
                JobDetailView(jobId: jobId)
            }
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    sortAndScoreMenu
                }
                ToolbarItem(placement: .topBarTrailing) {
                    fetchButton
                }
                ToolbarItem(placement: .topBarLeading) {
                    Button {
                        showAddByURL = true
                    } label: {
                        Label("Add job by URL", systemImage: "link.badge.plus")
                    }
                }
            }
            .alert("Add job by URL", isPresented: $showAddByURL) {
                TextField("https://…", text: $pastedURL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                Button("Add") { addByURL() }
                Button("Cancel", role: .cancel) { pastedURL = "" }
            } message: {
                Text("Paste any job posting link — LinkedIn, Greenhouse, or any ATS page.")
            }
            .confirmationDialog("Score \(scoreAllCount) job\(scoreAllCount == 1 ? "" : "s")?",
                                isPresented: $showScoreAllConfirm, titleVisibility: .visible) {
                Button("Score \(scoreAllCount)") {
                    model.scoreAll(cap: model.config.ai.scoreAllCap)
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("Makes one AI call per job (hard cap \(model.config.ai.scoreAllCap)). You can Stop anytime.")
            }
        }
    }

    /// Sort options plus the batch "Score all" action, folded into one
    /// overflow menu to keep the toolbar uncluttered on iPhone.
    private var sortAndScoreMenu: some View {
        Menu {
            Picker("Sort by", selection: $sortRaw) {
                ForEach(JobSort.allCases) { option in
                    Label(option.label, systemImage: option.systemImage).tag(option.rawValue)
                }
            }
            Divider()
            Button {
                showScoreAllConfirm = true
            } label: {
                Label("Score all (\(scoreAllCount))", systemImage: "flame")
            }
            .disabled(scoreAllCount == 0 || model.isScoringAll)
        } label: {
            Label("Sort and score", systemImage: "ellipsis.circle")
        }
    }

    /// Live progress for a Score-all run, with the hard-stop button.
    private var scoreAllBanner: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Scoring \(model.scoreAllDone) of \(model.scoreAllTotal)…")
                    .font(.subheadline.weight(.semibold))
                ProgressView(value: Double(model.scoreAllDone),
                             total: Double(max(model.scoreAllTotal, 1)))
                    .tint(Theme.ember)
            }
            Button(role: .destructive) {
                model.cancelScoreAll()
            } label: {
                Text("Stop")
            }
            .buttonStyle(.bordered)
            .tint(.red)
        }
        .padding(.horizontal, 24)
    }

    private var deck: some View {
        VStack(spacing: 16) {
            if model.isScoringAll {
                scoreAllBanner
            }
            HStack {
                Eyebrow(text: "\(model.inbox.count) to triage · \(sort.label)")
                Spacer()
            }
            .padding(.horizontal, 24)

            ZStack {
                ForEach(Array(sortedInbox.prefix(3).enumerated().reversed()), id: \.element.id) { index, job in
                    JobCardView(job: job)
                        .scaleEffect(1 - CGFloat(index) * 0.04)
                        .offset(y: CGFloat(index) * 10)
                        .opacity(index == 2 ? 0.4 : 1)
                        .zIndex(Double(3 - index))
                        .allowsHitTesting(index == 0)
                        .modifier(index == 0
                            ? TriageDragModifier(offset: $dragOffset, onDecision: decide)
                            : TriageDragModifier(offset: .constant(.zero), onDecision: { _ in }))
                }
            }
            .padding(.horizontal, 20)

            HStack(spacing: 44) {
                triageButton(icon: "xmark", label: "Pass", tint: .secondary) {
                    if let top = sortedInbox.first { model.triage(top, as: "dismissed") }
                }
                triageButton(icon: "star.fill", label: "Shortlist", tint: Theme.ember) {
                    if let top = sortedInbox.first { model.triage(top, as: "shortlisted") }
                }
            }
            .padding(.bottom, 6)
        }
        .padding(.vertical, 8)
    }

    private func decide(_ direction: TriageDirection) {
        guard let top = sortedInbox.first else { return }
        dragOffset = .zero
        model.triage(top, as: direction == .shortlist ? "shortlisted" : "dismissed")
    }

    private func triageButton(icon: String, label: String, tint: Color, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            VStack(spacing: 6) {
                Image(systemName: icon)
                    .font(.title2.weight(.semibold))
                    .frame(width: 58, height: 58)
                    .background(Circle().fill(.thickMaterial))
                    .overlay(Circle().strokeBorder(tint.opacity(0.4), lineWidth: 1.5))
                    .foregroundStyle(tint)
                Text(label)
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.secondary)
            }
        }
        .buttonStyle(.plain)
    }

    private func addByURL() {
        guard let url = URL(string: pastedURL.trimmingCharacters(in: .whitespaces)),
              url.scheme?.hasPrefix("http") == true else {
            model.lastError = "That doesn't look like a valid link."
            pastedURL = ""
            return
        }
        pastedURL = ""
        Task {
            do {
                let job = try await ManualURLFetcher().fetchJob(from: url.absoluteString)
                try model.jobStore.upsert([job])
                model.activityStore.log("saved", "Added by URL: \(job.title)")
                model.refresh()
            } catch {
                model.lastError = "Couldn't read that posting: \(error.localizedDescription)"
            }
        }
    }

    private var fetchButton: some View {
        Button {
            Task { await model.fetchJobs() }
        } label: {
            if model.isFetching {
                ProgressView()
            } else {
                Label("Fetch jobs", systemImage: "arrow.down.circle")
            }
        }
        .disabled(model.isFetching)
    }

    private var emptyState: some View {
        ContentUnavailableView {
            Label("Inbox clear", systemImage: "tray")
        } description: {
            Text(model.stats.totalJobs == 0
                 ? "Fetch jobs from your configured sources to start triaging."
                 : "You've triaged everything. Fetch again for fresh listings.")
        } actions: {
            Button {
                Task { await model.fetchJobs() }
            } label: {
                if model.isFetching {
                    HStack(spacing: 8) { ProgressView(); Text("Fetching…") }
                } else {
                    Text("Fetch new jobs")
                }
            }
            .buttonStyle(.borderedProminent)
            .tint(Theme.ember)
            .disabled(model.isFetching)
        }
    }
}

enum TriageDirection { case shortlist, dismiss }

/// Drag-to-decide for the top card, with directional color feedback.
struct TriageDragModifier: ViewModifier {
    @Binding var offset: CGSize
    let onDecision: (TriageDirection) -> Void

    func body(content: Content) -> some View {
        content
            .offset(x: offset.width, y: offset.height * 0.2)
            .rotationEffect(.degrees(Double(offset.width) / 24))
            .overlay(alignment: offset.width >= 0 ? .topLeading : .topTrailing) {
                if abs(offset.width) > 24 {
                    Text(offset.width > 0 ? "SHORTLIST" : "PASS")
                        .font(.caption.weight(.heavy))
                        .fontWidth(.expanded)
                        .foregroundStyle(offset.width > 0 ? Theme.ember : .secondary)
                        .padding(8)
                        .overlay(RoundedRectangle(cornerRadius: 6)
                            .strokeBorder(offset.width > 0 ? Theme.ember : Color.secondary, lineWidth: 2))
                        .rotationEffect(.degrees(offset.width > 0 ? -12 : 12))
                        .padding(18)
                        .opacity(min(Double(abs(offset.width)) / 90, 1))
                }
            }
            .gesture(
                DragGesture()
                    .onChanged { offset = $0.translation }
                    .onEnded { value in
                        if value.translation.width > 110 {
                            withAnimation(.snappy) { onDecision(.shortlist) }
                        } else if value.translation.width < -110 {
                            withAnimation(.snappy) { onDecision(.dismiss) }
                        } else {
                            withAnimation(.bouncy) { offset = .zero }
                        }
                    }
            )
    }
}

struct JobCardView: View {
    let job: Job

    var body: some View {
        NavigationLink(value: job.id) {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(job.title)
                            .font(.title3.weight(.semibold))
                            .multilineTextAlignment(.leading)
                            .lineLimit(3)
                        Text(job.company.isEmpty ? job.source : job.company)
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    HeatChip(score: job.fitScore)
                }

                HStack(spacing: 6) {
                    if job.isRemote {
                        chip("Remote", system: "wifi")
                    }
                    if !job.location.isEmpty {
                        chip(job.location, system: "mappin")
                    }
                    if let salary = salaryText {
                        chip(salary, system: "dollarsign")
                    }
                }

                Text(job.description.isEmpty ? "No description captured yet." : job.description)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .lineLimit(6)
                    .frame(maxWidth: .infinity, alignment: .leading)

                Spacer(minLength: 0)

                HStack {
                    Eyebrow(text: job.source)
                    Spacer()
                    if !job.datePosted.isEmpty {
                        Text(job.datePosted.prefix(10))
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                }
            }
            .padding(18)
            .frame(maxWidth: .infinity, minHeight: 340, maxHeight: 420, alignment: .topLeading)
            .background(
                RoundedRectangle(cornerRadius: 22)
                    .fill(.background)
                    .shadow(color: .black.opacity(0.18), radius: 14, y: 6)
            )
            .overlay(RoundedRectangle(cornerRadius: 22).strokeBorder(.quaternary))
        }
        .buttonStyle(.plain)
    }

    private var salaryText: String? {
        guard let max = job.salaryMax ?? job.salaryMin else { return nil }
        return max >= 1000 ? "$\(max / 1000)k" : "$\(max)/hr"
    }

    private func chip(_ text: String, system: String) -> some View {
        HStack(spacing: 3) {
            Image(systemName: system).font(.system(size: 10))
            Text(text).lineLimit(1)
        }
        .font(.caption.weight(.medium))
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(Capsule().fill(Color.primary.opacity(0.06)))
        .foregroundStyle(.secondary)
    }
}
