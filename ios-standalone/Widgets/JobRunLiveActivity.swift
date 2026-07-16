import WidgetKit
import SwiftUI

@main
struct JobsmithWidgetsBundle: WidgetBundle {
    var body: some Widget {
        JobRunLiveActivity()
    }
}

/// Brand colors, duplicated from the app's Theme (the widget extension does
/// not link JobsmithKit or the app target). The ramp's meaning is absolute,
/// so these stay constant in both appearances — same as the app.
private enum RunPalette {
    static let ember = Color(red: 0xF0 / 255, green: 0x86 / 255, blue: 0x3A / 255)
    static let emberDeep = Color(red: 0xD9 / 255, green: 0x54 / 255, blue: 0x1E / 255)
    static let steel = Color(red: 0x5E / 255, green: 0x7C / 255, blue: 0xA0 / 255)
    static let success = Color(red: 0x4C / 255, green: 0xAF / 255, blue: 0x7D / 255)
    static let amber = Color(red: 0xF5 / 255, green: 0xA6 / 255, blue: 0x23 / 255)

    static func tint(for phase: JobRunAttributes.Phase) -> Color {
        switch phase {
        case .searching, .scoring: return ember
        case .paused: return amber
        case .done: return success
        }
    }
}

/// The actual app logo (bundled into the widget's own asset catalog) — the
/// face of the activity everywhere, instead of a generic SF-symbol glyph.
private struct AppLogo: View {
    var size: CGFloat = 28

    var body: some View {
        Image("Logo")
            .resizable()
            .scaledToFit()
            .frame(width: size, height: size)
            .clipShape(RoundedRectangle(cornerRadius: size * 0.25))
    }
}

struct JobRunLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: JobRunAttributes.self) { context in
            LockScreenRunView(state: context.state, isStale: context.isStale)
                .activityBackgroundTint(nil)
                .activitySystemActionForegroundColor(RunPalette.ember)
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    AppLogo(size: 28)
                        .padding(.leading, 4)
                }
                DynamicIslandExpandedRegion(.center) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(context.isStale && context.state.phase != .done
                             ? "No longer running" : context.state.title)
                            .font(.subheadline.weight(.semibold))
                            .lineLimit(1)
                        Text(context.isStale && context.state.phase != .done
                             ? "Open Jobsmith to pick it back up" : context.state.detail)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    HeadlineCount(state: context.state)
                        .padding(.trailing, 4)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    RunProgressBar(state: context.state)
                        .padding(.top, 4)
                }
            } compactLeading: {
                AppLogo(size: 20)
            } compactTrailing: {
                CompactCount(state: context.state)
            } minimal: {
                AppLogo(size: 18)
            }
            .keylineTint(RunPalette.ember)
        }
    }
}

/// The live count next to the logo: jobs found so far while searching,
/// scored/total while scoring.
private struct CompactCount: View {
    let state: JobRunAttributes.ContentState

    var body: some View {
        switch (state.kind, state.phase) {
        case (.search, .done):
            Image(systemName: "checkmark")
                .font(.caption2.weight(.bold))
                .foregroundStyle(RunPalette.success)
        case (.scoring, .done):
            Image(systemName: "checkmark")
                .font(.caption2.weight(.bold))
                .foregroundStyle(RunPalette.success)
        case (.search, _):
            Text("\(state.jobsFound)")
                .font(.caption2.weight(.bold).monospacedDigit())
                .foregroundStyle(RunPalette.tint(for: state.phase))
        case (.scoring, _):
            Text("\(state.completed)/\(state.total)")
                .font(.caption2.weight(.bold).monospacedDigit())
                .foregroundStyle(RunPalette.tint(for: state.phase))
        }
    }
}

/// The big tabular numeral. Which count it is — and what the word under it
/// says — follows the run's kind, so a finished scoring batch reads
/// "3 scored", never the search wording "3 new jobs".
private struct HeadlineCount: View {
    let state: JobRunAttributes.ContentState

    private var number: String {
        switch (state.kind, state.phase) {
        case (.scoring, .done): return "\(state.completed)"
        case (.scoring, _): return "\(state.completed)/\(state.total)"
        default: return "\(state.jobsFound)"
        }
    }

    private var label: String {
        switch (state.kind, state.phase) {
        case (.scoring, _): return "scored"
        case (.search, .done): return "new jobs"
        case (.search, .paused): return "found so far"
        default: return "found"
        }
    }

    var body: some View {
        VStack(alignment: .trailing, spacing: 0) {
            Text(number)
                .font(.system(.title3, design: .rounded).weight(.bold).monospacedDigit())
                .lineLimit(1)
                .minimumScaleFactor(0.6)
            Text(label.uppercased())
                .font(.system(size: 9, weight: .semibold))
                .foregroundStyle(.secondary)
        }
    }
}

private struct RunProgressBar: View {
    let state: JobRunAttributes.ContentState

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(.quaternary)
                Capsule()
                    .fill(fill)
                    .frame(width: max(8, geo.size.width * state.fraction))
            }
        }
        .frame(height: 5)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(state.completed) of \(state.total)")
    }

    private var fill: LinearGradient {
        switch state.phase {
        case .paused:
            return LinearGradient(colors: [RunPalette.amber, RunPalette.amber],
                                  startPoint: .leading, endPoint: .trailing)
        case .done:
            return LinearGradient(colors: [RunPalette.success, RunPalette.success],
                                  startPoint: .leading, endPoint: .trailing)
        default:
            return LinearGradient(colors: [RunPalette.steel, RunPalette.ember],
                                  startPoint: .leading, endPoint: .trailing)
        }
    }
}

struct LockScreenRunView: View {
    let state: JobRunAttributes.ContentState
    var isStale: Bool = false

    /// The process that owned this run is gone (force-quit, crash) and never
    /// resumed it. A card that keeps showing "Searching… 1 of 2" would be
    /// lying — say so instead, and offer the same Stop intent as a cleanup
    /// (it relaunches the app process, which retires the run and ends this
    /// activity).
    private var showsAsStale: Bool { isStale && state.phase != .done }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .center, spacing: 10) {
                AppLogo(size: 28)
                VStack(alignment: .leading, spacing: 1) {
                    Text(showsAsStale ? "No longer running" : state.title)
                        .font(.subheadline.weight(.semibold))
                        .lineLimit(1)
                    Text(showsAsStale
                         ? "Open Jobsmith to pick it back up" : state.detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                Spacer(minLength: 8)
                HeadlineCount(state: state)
                    .opacity(showsAsStale ? 0.5 : 1)
            }
            RunProgressBar(state: state)
                .opacity(showsAsStale ? 0.5 : 1)
            if state.phase != .done {
                HStack {
                    Spacer()
                    Button(intent: StopRunIntent()) {
                        Text(showsAsStale ? "Dismiss" : "Stop")
                            .font(.caption.weight(.semibold))
                            .padding(.horizontal, 14)
                            .padding(.vertical, 5)
                    }
                    .buttonStyle(.bordered)
                    .tint(.secondary)
                }
            }
        }
        .padding(14)
    }
}
