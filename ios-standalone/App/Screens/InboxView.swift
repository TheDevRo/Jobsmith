import SwiftUI
import JobsmithKit

/// Swipe-to-triage deck: right shortlists, left dismisses, tap opens detail.
struct InboxView: View {
    @Environment(AppModel.self) private var model
    @State private var path: [String] = []
    /// Button-driven swipe: targets the top card by id so only that card
    /// flings (the deck's own gesture handles finger swipes directly).
    @State private var swipeCommand: SwipeCommand?
    @State private var swipeToken = 0
    @State private var showAddByURL = false
    @State private var pastedURL = ""
    @AppStorage(AppStorageKey.jobSort) private var sortRaw = JobSort.bestMatch.rawValue
    @State private var showScoreAllConfirm = false

    private var sort: JobSort { JobSort(rawValue: sortRaw) ?? .bestMatch }
    private var sortedInbox: [Job] { sort.sorted(model.inbox) }

    private var scoreCap: Int { model.config.ai.scoreAllCap }
    private var unscoredCount: Int { model.unscoredInboxJobs.count }
    /// A default run: unscored jobs, clamped to the user's standing cap.
    private var boundedCount: Int { min(unscoredCount, scoreCap) }

    var body: some View {
        NavigationStack(path: $path) {
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
            .confirmationDialog("\(unscoredCount) unscored job\(unscoredCount == 1 ? "" : "s")",
                                isPresented: $showScoreAllConfirm, titleVisibility: .visible) {
                Button("Score \(boundedCount)") {
                    model.scoreAll(cap: scoreCap)
                }
                if unscoredCount > scoreCap {
                    Button("Score all \(unscoredCount)") {
                        model.scoreAll(cap: unscoredCount)
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
                Label("Score jobs (\(unscoredCount))", systemImage: "flame")
            }
            .disabled(unscoredCount == 0 || model.isScoringAll)
        } label: {
            Label("Sort and score", systemImage: "ellipsis.circle")
        }
    }

    private var deck: some View {
        VStack(spacing: 16) {
            if model.isScoringAll {
                ScoreAllBanner(done: model.scoreAllDone, total: model.scoreAllTotal) {
                    model.cancelScoreAll()
                }
            }
            HStack {
                Eyebrow(text: "\(model.inbox.count) to scout")
                Spacer()
                Label(sort.label, systemImage: sort.systemImage)
                    .font(.caption2.weight(.medium))
                    .foregroundStyle(.secondary)
            }
            .padding(.horizontal, 24)

            ZStack {
                ForEach(Array(sortedInbox.prefix(3).enumerated().reversed()), id: \.element.id) { index, job in
                    SwipeCard(
                        job: job,
                        depth: index,
                        command: swipeCommand,
                        onTap: { path.append(job.id) },
                        onSwipe: { direction in
                            model.triage(job, as: direction == .shortlist ? "shortlisted" : "dismissed")
                        }
                    )
                }
            }
            .padding(.horizontal, 20)

            HStack(spacing: 44) {
                triageButton(icon: "xmark", label: "Pass", tint: .secondary) {
                    commandSwipe(.dismiss)
                }
                triageButton(icon: "star.fill", label: "Shortlist", tint: Theme.ember) {
                    commandSwipe(.shortlist)
                }
            }
            .padding(.bottom, 6)
        }
        .padding(.vertical, 8)
    }

    /// Fire a swipe on the current top card from the Pass/Shortlist buttons.
    /// The bumped token makes repeated same-direction taps register as changes.
    private func commandSwipe(_ direction: TriageDirection) {
        guard let top = sortedInbox.first else { return }
        swipeToken += 1
        swipeCommand = SwipeCommand(targetId: top.id, direction: direction, token: swipeToken)
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
                 ? "Fetch jobs from your configured sources to start scouting."
                 : "You've scouted the whole board — fetch again for fresh listings.")
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

/// A button-initiated swipe on a specific card. `token` bumps on every press
/// so repeated same-direction taps are seen as distinct changes.
struct SwipeCommand: Equatable {
    var targetId: String
    var direction: TriageDirection
    var token: Int
}

/// One triage card. It owns its own drag offset, so the swipe stamp and the
/// off-screen fling are bound to *this* card and never bleed onto the card
/// behind it. Only the top card (`depth == 0`) is interactive; the cards
/// behind simply rest at their stacked positions and raise up (via `depth`
/// shrinking) when the top card leaves.
struct SwipeCard: View {
    let job: Job
    /// 0 = top/front, 1 and 2 = stacked behind.
    let depth: Int
    /// Button-driven swipe target; acts only when it names this card.
    let command: SwipeCommand?
    let onTap: () -> Void
    let onSwipe: (TriageDirection) -> Void

    @State private var offset: CGSize = .zero
    /// True once this card is flinging off-screen — locks further input and
    /// hides the stamp so a mid-flight card can't be re-grabbed.
    @State private var leaving = false

    private var isTop: Bool { depth == 0 }
    /// Rotation stays gentle by clamping the input before scaling.
    private var tilt: Double { Double(min(max(offset.width, -300), 300)) / 18 }

    var body: some View {
        JobCardView(job: job)
            .overlay(alignment: offset.width >= 0 ? .topLeading : .topTrailing) { stamp }
            .scaleEffect(1 - CGFloat(depth) * 0.04)
            .offset(y: CGFloat(depth) * 10)              // resting stack position
            .offset(x: offset.width, y: offset.height * 0.35)  // live drag / fling
            .rotationEffect(.degrees(tilt))
            .opacity(depth == 2 ? 0.4 : 1)
            .zIndex(Double(3 - depth))
            .allowsHitTesting(isTop && !leaving)
            .onTapGesture { if isTop && !leaving { onTap() } }
            .gesture(dragGesture)
            .onChange(of: command) { _, cmd in
                if let cmd, cmd.targetId == job.id, isTop, !leaving {
                    fling(cmd.direction)
                }
            }
    }

    @ViewBuilder private var stamp: some View {
        if isTop && !leaving && abs(offset.width) > 24 {
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

    private var dragGesture: some Gesture {
        DragGesture(minimumDistance: 12)
            .onChanged { if isTop && !leaving { offset = $0.translation } }
            .onEnded { value in
                guard isTop, !leaving else { return }
                if value.translation.width > 110 {
                    fling(.shortlist)
                } else if value.translation.width < -110 {
                    fling(.dismiss)
                } else {
                    withAnimation(.bouncy) { offset = .zero }
                }
            }
    }

    /// Animate this card fully off-screen, then hand the decision up. Removal
    /// happens after the card has left, so the card behind stays put and only
    /// raises once this one is gone.
    private func fling(_ direction: TriageDirection) {
        leaving = true
        let exitX: CGFloat = direction == .shortlist ? 900 : -900
        withAnimation(.easeOut(duration: 0.3)) {
            offset = CGSize(width: exitX, height: offset.height + 40)
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.26) {
            onSwipe(direction)
        }
    }
}

struct JobCardView: View {
    let job: Job

    var body: some View {
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
        .contentShape(RoundedRectangle(cornerRadius: 22))
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
