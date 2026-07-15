import SwiftUI
import JobsmithKit

/// A friendly, honest upper bound on how long a fetch will take, derived from
/// the slowest enabled source's timeout (sources run in parallel).
func fetchEstimateText(for config: AppConfig) -> String {
    let duration = SourceRegistry.estimatedDuration(for: SourceRegistry.enabledIDs(for: config))
    let seconds = Int(duration.components.seconds)
    if seconds <= 90 { return "up to a minute" }
    let minutes = Int((Double(seconds) / 60).rounded(.up))
    if minutes <= 2 { return "a minute or two" }
    return "up to about \(minutes) minutes"
}

/// Unified snapshot of whatever long-running run is in flight. One surface for
/// search, scoring, and paused states — strips can never stack the way the old
/// three banners could. Search outranks scoring while both run; the detail
/// sheet shows every active section regardless.
enum RunStatus {
    case searching(FetchProgress?)
    case scoring(done: Int, total: Int)
    case paused(search: Bool, scoring: Bool)

    @MainActor
    init?(model: AppModel) {
        if model.isFetching {
            self = .searching(model.fetchProgress)
        } else if model.isScoringAll {
            self = .scoring(done: model.scoreAllDone, total: model.scoreAllTotal)
        } else if model.isSearchPaused || model.isScoringPaused {
            self = .paused(search: model.isSearchPaused, scoring: model.isScoringPaused)
        } else {
            return nil
        }
    }
}

/// The one in-app progress marker: a slim capsule pinned under the navigation
/// bar (via `safeAreaInset` at the call sites) that never displaces the deck.
/// One line of status, a hairline of progress, a chevron — the per-source
/// ledger the old banner forced on-screen lives in the tap-to-open sheet.
struct ActivityStrip: View {
    @Environment(AppModel.self) private var model
    @State private var showDetail = false

    private let amber = Color(hex: 0xF5A623)

    var body: some View {
        if let status = RunStatus(model: model) {
            Button {
                showDetail = true
            } label: {
                label(for: status)
            }
            .buttonStyle(.plain)
            .padding(.horizontal, 16)
            .padding(.top, 4)
            .padding(.bottom, 8)
            .sheet(isPresented: $showDetail) {
                RunDetailSheet()
                    .environment(model)
            }
            .transition(.move(edge: .top).combined(with: .opacity))
            .accessibilityLabel(text(for: status))
            .accessibilityHint("Shows run details and controls")
        }
    }

    private func label(for status: RunStatus) -> some View {
        VStack(spacing: 6) {
            HStack(spacing: 9) {
                icon(for: status)
                Text(text(for: status))
                    .font(.footnote.weight(.semibold))
                    .monospacedDigit()
                    .lineLimit(1)
                Spacer(minLength: 8)
                Image(systemName: "chevron.right")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.secondary)
            }
            if let fraction = fraction(for: status) {
                hairline(fraction: fraction, paused: isPaused(status))
            }
        }
        .padding(.horizontal, 14)
        .padding(.top, 9)
        .padding(.bottom, 8)
        .background(Capsule().fill(.thickMaterial))
        .overlay(Capsule().strokeBorder(.quaternary))
        .contentShape(Capsule())
    }

    @ViewBuilder
    private func icon(for status: RunStatus) -> some View {
        switch status {
        case .searching:
            ProgressView().controlSize(.mini).tint(Theme.ember)
        case .scoring:
            Image(systemName: "flame.fill")
                .font(.caption)
                .foregroundStyle(Theme.ember)
        case .paused:
            Image(systemName: "pause.circle.fill")
                .font(.caption)
                .foregroundStyle(amber)
        }
    }

    private func text(for status: RunStatus) -> String {
        switch status {
        case .searching(let p):
            guard let p, p.sourcesTotal > 0 else { return "Starting search…" }
            return "Searching · \(p.sourcesDone) of \(p.sourcesTotal) boards · \(p.jobsFound) found"
        case .scoring(let done, let total):
            return "Scoring \(done) of \(total)"
        case .paused(let search, let scoring):
            switch (search, scoring) {
            case (true, true): return "Search and scoring paused · resumes in background"
            case (true, false): return "Search paused · resumes in background"
            default: return "Scoring paused · resumes when the AI is reachable"
            }
        }
    }

    private func fraction(for status: RunStatus) -> Double? {
        switch status {
        case .searching(let p):
            guard let p, p.sourcesTotal > 0 else { return nil }
            return Double(p.sourcesDone) / Double(p.sourcesTotal)
        case .scoring(let done, let total):
            guard total > 0 else { return nil }
            return Double(done) / Double(total)
        case .paused:
            return nil
        }
    }

    private func isPaused(_ status: RunStatus) -> Bool {
        if case .paused = status { return true }
        return false
    }

    private func hairline(fraction: Double, paused: Bool) -> some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.primary.opacity(0.08))
                Capsule()
                    .fill(paused
                          ? LinearGradient(colors: [amber, amber],
                                           startPoint: .leading, endPoint: .trailing)
                          : LinearGradient(colors: [Theme.steel, Theme.ember],
                                           startPoint: .leading, endPoint: .trailing))
                    .frame(width: max(6, geo.size.width * min(max(fraction, 0), 1)))
            }
        }
        .frame(height: 3)
        .animation(.snappy, value: fraction)
    }
}

/// The full run ledger: per-source rows while searching, scoring progress, the
/// pause explanation, and the Stop/Resume controls. Medium detent — glance,
/// act, dismiss.
struct RunDetailSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss

    private var isIdle: Bool { RunStatus(model: model) == nil }

    var body: some View {
        NavigationStack {
            List {
                if model.isFetching {
                    searchSection
                }
                if model.isScoringAll {
                    scoringSection
                }
                if !model.isFetching && !model.isScoringAll
                    && (model.isSearchPaused || model.isScoringPaused) {
                    pausedSection
                }
            }
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }
                }
            }
        }
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
        // The run finishing closes the sheet — its content just vanished.
        .onChange(of: isIdle) { _, idle in
            if idle { dismiss() }
        }
    }

    private var title: String {
        if model.isFetching { return "Search in progress" }
        if model.isScoringAll { return "Scoring in progress" }
        return "Paused"
    }

    // MARK: - Search

    private var searchSection: some View {
        Section {
            ForEach(sourceRows, id: \.name) { row in
                HStack {
                    Text(row.name)
                    Spacer()
                    Text(row.status)
                        .foregroundStyle(row.tint)
                        .font(.subheadline.monospacedDigit())
                }
            }
            Button("Stop search", role: .destructive) {
                model.cancelSearch()
            }
        } header: {
            if let p = model.fetchProgress {
                Text("\(p.jobsFound) job\(p.jobsFound == 1 ? "" : "s") found so far")
            } else {
                Text("Starting search…")
            }
        } footer: {
            Text("Everything found so far is already saved — stopping keeps it.")
        }
    }

    private struct SourceRow {
        let name: String
        let status: String
        let tint: Color
    }

    private var sourceRows: [SourceRow] {
        guard let p = model.fetchProgress else { return [] }
        let name = { SourceCatalog.displayName(for: $0) }
        var rows: [SourceRow] = []

        for source in p.perSourceFound.keys.sorted(by: { name($0) < name($1) }) {
            let found = p.perSourceFound[source] ?? 0
            let filtered = p.perSourceFiltered[source] ?? 0
            var status = "\(found) found"
            if filtered > 0 { status += " · \(filtered) filtered" }
            rows.append(SourceRow(name: name(source), status: status, tint: .secondary))
        }
        for source in p.blocked.sorted() {
            rows.append(SourceRow(name: name(source), status: "blocked", tint: .secondary))
        }
        for source in p.timedOut.sorted() {
            rows.append(SourceRow(name: name(source), status: "timed out", tint: .secondary))
        }
        for source in p.failed.sorted() {
            rows.append(SourceRow(name: name(source), status: "no response", tint: .red))
        }
        for source in p.authFailed.sorted() {
            rows.append(SourceRow(name: name(source), status: "check the API key", tint: .red))
        }
        for source in p.interrupted.sorted() {
            rows.append(SourceRow(name: name(source), status: "paused, will finish", tint: .secondary))
        }
        let remaining = p.sourcesTotal - p.sourcesDone
        if remaining > 0 {
            rows.append(SourceRow(name: "\(remaining) still searching…", status: "",
                                  tint: .secondary))
        }
        return rows
    }

    // MARK: - Scoring

    private var scoringSection: some View {
        Section("Scoring") {
            VStack(alignment: .leading, spacing: 6) {
                Text("Scored \(model.scoreAllDone) of \(model.scoreAllTotal)")
                    .font(.subheadline.weight(.semibold))
                    .monospacedDigit()
                ProgressView(value: Double(model.scoreAllDone),
                             total: Double(max(model.scoreAllTotal, 1)))
                    .tint(Theme.ember)
            }
            .padding(.vertical, 2)
            Button("Stop scoring", role: .destructive) {
                model.cancelScoreAll()
            }
        }
    }

    // MARK: - Paused

    private var pausedMessage: String {
        switch (model.isSearchPaused, model.isScoringPaused) {
        case (true, true): return "Search and scoring will finish in the background."
        case (true, false): return "Search will finish in the background."
        default: return "Scoring will finish when the AI endpoint is reachable."
        }
    }

    private var pausedSection: some View {
        Section {
            // Deliberately not an error. iOS suspends a backgrounded app after
            // roughly 30 seconds while a LinkedIn search budgets minutes — being
            // cut off is the expected outcome of leaving mid-search. Everything
            // collected so far is already in the Inbox.
            Label {
                Text(pausedMessage)
            } icon: {
                Image(systemName: "pause.circle.fill")
                    .foregroundStyle(.secondary)
            }
            Button("Resume now") {
                Task { @MainActor in
                    await model.resumeInterruptedSearch()
                    model.resumeScoringIfNeeded()
                }
            }
            Button("Stop and keep results", role: .destructive) {
                model.stopActiveRun()
            }
        } footer: {
            Text("Jobs already collected are in your Inbox; scores already written are kept.")
        }
    }
}

/// A transient card shown the first time a user kicks off a search, reassuring
/// them the fetch runs in the background and they'll be notified when it's done.
/// Auto-dismisses; tap to dismiss early.
struct SearchTipToast: View {
    let message: String
    @Binding var isPresented: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: "bell.badge")
                .font(.title3)
                .foregroundStyle(Theme.ember)
            Text(message)
                .font(.callout)
                .foregroundStyle(.primary)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(16)
        .background(RoundedRectangle(cornerRadius: 18).fill(.regularMaterial))
        .overlay(RoundedRectangle(cornerRadius: 18).strokeBorder(.quaternary))
        .shadow(color: .black.opacity(0.18), radius: 16, y: 6)
        .padding(.horizontal, 16)
        .padding(.bottom, 12)
        .contentShape(Rectangle())
        .onTapGesture { withAnimation(.snappy) { isPresented = false } }
        .task {
            // Long enough to read the two-line message, then fade out.
            try? await Task.sleep(for: .seconds(6))
            withAnimation(.snappy) { isPresented = false }
        }
        .transition(.move(edge: .bottom).combined(with: .opacity))
    }
}
