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
    @State private var showDeleteAllConfirm = false
    @State private var searchQuery = ""
    @State private var showSearch = false
    /// Empty = all boards. Non-empty restricts the deck to those source slugs.
    @State private var selectedBoards: Set<String> = []
    /// Shown once, the first time the user kicks off a fetch.
    @AppStorage(AppStorageKey.hasSeenSearchTip) private var hasSeenSearchTip = false
    @State private var showSearchTip = false
    /// Tap targets grow with the user's text size (44pt HIG minimum at default).
    @ScaledMetric(relativeTo: .title2) private var triageButtonSize: CGFloat = 58

    private var sort: JobSort { JobSort(rawValue: sortRaw) ?? .bestMatch }
    private var filteredInbox: [Job] {
        JobListFilter.apply(model.inbox, query: searchQuery, boards: selectedBoards)
    }
    private var sortedInbox: [Job] { sort.sorted(filteredInbox, conversion: model.conversionBySource) }
    private var availableBoards: [String] { JobListFilter.availableBoards(in: model.inbox) }

    private var scoreCap: Int { model.config.ai.scoreAllCap }
    private var unscoredCount: Int { model.unscoredInboxJobs.count }
    /// A default run: unscored jobs, clamped to the user's standing cap.
    private var boundedCount: Int { min(unscoredCount, scoreCap) }

    var body: some View {
        NavigationStack(path: $path) {
            VStack(spacing: 0) {
              Group {
                if model.inbox.isEmpty {
                    emptyState
                } else {
                    VStack(spacing: 0) {
                        if showSearch {
                            // Cancel is omitted: the collapsed toolbar button and
                            // tap-outside both dismiss, so it would be redundant.
                            JobSearchField(text: $searchQuery, showsCancel: false, onCancel: closeSearch)
                        }
                        // Tapping anywhere in the deck's empty space (not a card,
                        // button, or the search bar) exits search — matches the
                        // "tap elsewhere to restore" behavior.
                        Group {
                            if sortedInbox.isEmpty {
                                noMatchesState
                            } else {
                                deck
                            }
                        }
                        .contentShape(Rectangle())
                        .onTapGesture { if showSearch { closeSearch() } }
                    }
                }
              }
            }
            // The one in-app progress marker: a slim strip pinned under the
            // navigation bar. It insets the content instead of overlapping it,
            // and the per-source detail lives in its tap-to-open sheet — the
            // deck keeps the screen.
            .safeAreaInset(edge: .top, spacing: 0) {
                ActivityStrip()
            }
            .overlay(alignment: .bottom) {
                if showSearchTip {
                    SearchTipToast(message: searchTipMessage, isPresented: $showSearchTip)
                }
            }
            // Title is blanked while searching so it can't overlap the search bar.
            .navigationTitle(showSearch ? "" : "Inbox")
            .navigationBarTitleDisplayMode(showSearch ? .inline : .large)
            .navigationDestination(for: String.self) { jobId in
                JobDetailView(jobId: jobId)
            }
            .toolbar { toolbarContent }
            .alert("Add job by URL", isPresented: $showAddByURL) {
                TextField("https://…", text: $pastedURL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                Button("Add") { addByURL() }
                Button("Cancel", role: .cancel) { pastedURL = "" }
            } message: {
                Text("Paste any job posting link — LinkedIn, Greenhouse, or any ATS page.")
            }
            .confirmationDialog("Delete all \(model.inbox.count) inbox posting\(model.inbox.count == 1 ? "" : "s")?",
                                isPresented: $showDeleteAllConfirm, titleVisibility: .visible) {
                Button("Delete \(model.inbox.count)", role: .destructive) {
                    model.deleteAllInboxPostings()
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("Clears every untriaged posting. Your Pipeline — shortlisted, applied, and beyond — is not affected. You can Undo right after.")
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

    /// While searching, the three trailing buttons collapse to a single
    /// "close search" button so the search bar has an uncluttered top row;
    /// tapping it (or outside the bar) restores the full toolbar.
    @ToolbarContentBuilder
    private var toolbarContent: some ToolbarContent {
        if showSearch {
            ToolbarItem(placement: .topBarTrailing) {
                Button(action: closeSearch) {
                    Label("Close search", systemImage: "magnifyingglass.circle.fill")
                }
            }
        } else {
            ToolbarItem(placement: .topBarTrailing) {
                Button(action: openSearch) {
                    Label("Search", systemImage: "magnifyingglass")
                }
            }
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
    }

    private func openSearch() {
        withAnimation(.snappy) { showSearch = true }
    }

    private func closeSearch() {
        searchQuery = ""
        withAnimation(.snappy) { showSearch = false }
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
            BoardFilterMenu(boards: availableBoards, selected: $selectedBoards)
            Divider()
            Button {
                showScoreAllConfirm = true
            } label: {
                Label("Score jobs (\(unscoredCount))", systemImage: "flame")
            }
            .disabled(unscoredCount == 0 || model.isScoringAll)
            Divider()
            Button(role: .destructive) {
                showDeleteAllConfirm = true
            } label: {
                Label("Delete all in inbox (\(model.inbox.count))", systemImage: "trash")
            }
            .disabled(model.inbox.isEmpty)
        } label: {
            Label("Sort, filter, score", systemImage: "ellipsis.circle")
        }
    }

    private var noMatchesState: some View {
        ContentUnavailableView {
            Label("No matches", systemImage: "line.3.horizontal.decrease.circle")
        } description: {
            Text("No inbox jobs match your search or board filter.")
        } actions: {
            Button("Clear filters") {
                searchQuery = ""
                selectedBoards = []
            }
        }
        .frame(maxHeight: .infinity)
    }

    private var deck: some View {
        VStack(spacing: 16) {
            HStack {
                Eyebrow(text: "\(sortedInbox.count) to scout")
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
                    .frame(width: triageButtonSize, height: triageButtonSize)
                    .background(Circle().fill(.thickMaterial))
                    .overlay(Circle().strokeBorder(tint.opacity(0.4), lineWidth: 1.5))
                    .foregroundStyle(tint)
                Text(label)
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.secondary)
            }
        }
        .buttonStyle(.plain)
        // No .accessibilityElement(children: .ignore) here: on a Button that
        // wraps the whole control in a *second* element, so "Shortlist" was
        // published twice at the same frame — VoiceOver announced it twice, and
        // any query for the button matched two identical elements. A Button is
        // already a single element; it just needs its label and hint.
        .accessibilityLabel(label)
        .accessibilityHint(topJobDescription.map { "\(label) \($0)" } ?? "")
    }

    /// Names the card the Pass/Shortlist buttons would act on, so VoiceOver
    /// says *which* job it is about to triage.
    private var topJobDescription: String? {
        sortedInbox.first.map { "\($0.title) at \($0.company.isEmpty ? SourceCatalog.displayName(for: $0.source) : $0.company)" }
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
            startFetch()
        } label: {
            if model.isFetching {
                ProgressView()
            } else {
                Label("Fetch jobs", systemImage: "arrow.down.circle")
            }
        }
        .disabled(model.isFetching)
    }

    /// Kick off a fetch, surfacing the one-time reassurance toast on the user's
    /// first-ever search. The completion notification (posted by the model when
    /// backgrounded) is what "notify you when your jobs are ready" refers to.
    private func startFetch() {
        if !hasSeenSearchTip {
            hasSeenSearchTip = true
            withAnimation(.snappy) { showSearchTip = true }
        }
        Task { await model.fetchJobs() }
    }

    private var searchTipMessage: String {
        "Searching can take \(fetchEstimateText(for: model.config)). Feel free to leave the app — we'll notify you when your jobs are ready!"
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
                startFetch()
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
            // Only the front card is reachable; the two behind it are decorative
            // stack depth and would otherwise be read out as phantom jobs.
            .accessibilityHidden(!isTop)
            .accessibilityAddTraits(.isButton)
            .accessibilityHint("Opens the full posting")
            // The drag gesture is unusable under VoiceOver, so surface the two
            // triage decisions as rotor actions instead.
            .accessibilityAction(named: Text("Shortlist")) { onSwipe(.shortlist) }
            .accessibilityAction(named: Text("Pass")) { onSwipe(.dismiss) }
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
    @Environment(AppModel.self) private var model
    /// The card must be able to grow with Dynamic Type or the title, chips, and
    /// description clip against each other at AX sizes.
    @ScaledMetric(relativeTo: .body) private var minCardHeight: CGFloat = 340
    @ScaledMetric(relativeTo: .body) private var maxCardHeight: CGFloat = 420

    /// One spoken sentence for the whole card. The fit score is included as a
    /// number, so VoiceOver users get what the heat color conveys visually.
    private var accessibilityDescription: String {
        let employer = job.company.isEmpty ? SourceCatalog.displayName(for: job.source) : job.company
        var parts = ["\(job.title) at \(employer)"]
        if let score = job.fitScore {
            parts.append("fit \(Int(score)) of 100")
        } else {
            parts.append("not scored yet")
        }
        if job.isRemote { parts.append("remote") }
        if !job.location.isEmpty { parts.append(job.location) }
        if let salary = salaryText { parts.append(salary) }
        if model.isAlreadyApplied(job) { parts.append("you already applied to this role") }
        return parts.joined(separator: ", ")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(job.title)
                        .font(.title3.weight(.semibold))
                        .multilineTextAlignment(.leading)
                        .lineLimit(3)
                    Text(job.company.isEmpty ? SourceCatalog.displayName(for: job.source) : job.company)
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

            // A repost, or the same role from another board. Worth saying out
            // loud right here — this is the screen where you decide to keep it.
            if model.isAlreadyApplied(job) {
                Label("You already applied to this role", systemImage: "arrow.uturn.left")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Theme.ember)
            }

            Text(job.description.isEmpty ? "No description captured yet." : job.description)
                .font(.callout)
                .foregroundStyle(.secondary)
                .lineLimit(6)
                .frame(maxWidth: .infinity, alignment: .leading)

            Spacer(minLength: 0)

            HStack {
                Eyebrow(text: SourceCatalog.displayName(for: job.source))
                Spacer()
                if !job.datePosted.isEmpty {
                    Text(job.datePosted.prefix(10))
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
            }
        }
        .padding(18)
        .frame(maxWidth: .infinity, minHeight: minCardHeight, maxHeight: maxCardHeight,
               alignment: .topLeading)
        .background(
            RoundedRectangle(cornerRadius: 22)
                .fill(.background)
                .shadow(color: .black.opacity(0.18), radius: 14, y: 6)
        )
        .overlay(RoundedRectangle(cornerRadius: 22).strokeBorder(.quaternary))
        .contentShape(RoundedRectangle(cornerRadius: 22))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(accessibilityDescription)
    }

    private var salaryText: String? {
        guard let max = job.salaryMax ?? job.salaryMin else { return nil }
        return max >= 1000 ? "$\(max / 1000)k" : "$\(max)/hr"
    }

    private func chip(_ text: String, system: String) -> some View {
        HStack(spacing: 3) {
            // Decorative: the adjacent text already says what the glyph means.
            Image(systemName: system)
                .font(.caption2)
                .accessibilityHidden(true)
            Text(text).lineLimit(1)
        }
        .font(.caption.weight(.medium))
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(Capsule().fill(Color.primary.opacity(0.06)))
        .foregroundStyle(.secondary)
    }
}
