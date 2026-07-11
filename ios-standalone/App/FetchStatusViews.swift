import SwiftUI
import JobsmithKit

/// A friendly, honest upper bound on how long a fetch will take, derived from
/// the slowest enabled source's timeout (sources run in parallel).
func fetchEstimateText(for config: AppConfig) -> String {
    let duration = SourceRegistry.estimatedDuration(for: Array(config.search.enabledSources))
    let seconds = Int(duration.components.seconds)
    if seconds <= 90 { return "up to a minute" }
    let minutes = Int((Double(seconds) / 60).rounded(.up))
    if minutes <= 2 { return "a minute or two" }
    return "up to about \(minutes) minutes"
}

/// Live, per-source fetch status shown in place of a bare "Fetching…" spinner.
/// Reads `FetchProgress` events streamed from `FetchPipeline`: each finished
/// board reports how many jobs it found (and how many its filters dropped),
/// blocked/failed boards say so, and the rest show as still searching.
struct FetchProgressBanner: View {
    let progress: FetchProgress?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header
            if let progress {
                ProgressView(value: Double(progress.sourcesDone),
                             total: Double(max(progress.sourcesTotal, 1)))
                    .tint(Theme.ember)
                let lines = statusLines(progress)
                if !lines.isEmpty {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(lines, id: \.self) { line in
                            Text(line)
                                .font(.caption.monospacedDigit())
                                .foregroundStyle(.secondary)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                    // Long runs (many boards) stay compact and scrollable.
                    .frame(maxHeight: 168)
                }
            } else {
                ProgressView().tint(Theme.ember)
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 16).fill(.thickMaterial))
        .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(.quaternary))
        .padding(.horizontal, 16)
        .padding(.top, 8)
    }

    private var header: some View {
        HStack(spacing: 8) {
            ProgressView().controlSize(.small)
            if let progress {
                Text(progress.sourcesDone >= progress.sourcesTotal && progress.sourcesTotal > 0
                     ? progress.detail
                     : "Searching \(progress.sourcesTotal) job board\(progress.sourcesTotal == 1 ? "" : "s")…")
                    .font(.subheadline.weight(.semibold))
            } else {
                Text("Starting search…").font(.subheadline.weight(.semibold))
            }
        }
    }

    /// One line per finished board, plus a rollup of those still running.
    private func statusLines(_ p: FetchProgress) -> [String] {
        var lines: [String] = []
        let name = { SourceCatalog.displayName(for: $0) }

        for source in p.perSourceFound.keys.sorted(by: { name($0) < name($1) }) {
            let found = p.perSourceFound[source] ?? 0
            let filtered = p.perSourceFiltered[source] ?? 0
            var line = "\(name(source)) — \(found) found"
            if filtered > 0 { line += " · \(filtered) filtered" }
            lines.append(line)
        }
        for source in p.blocked.sorted() { lines.append("\(name(source)) — blocked") }
        for source in p.timedOut.sorted() { lines.append("\(name(source)) — timed out") }
        for source in p.failed.sorted() { lines.append("\(name(source)) — no response") }

        let remaining = p.sourcesTotal - p.sourcesDone
        if remaining > 0 { lines.append("…\(remaining) still searching") }
        return lines
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
