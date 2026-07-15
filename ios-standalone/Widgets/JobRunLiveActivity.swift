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

    static func glyph(for phase: JobRunAttributes.Phase) -> String {
        switch phase {
        case .searching: return "magnifyingglass"
        case .scoring: return "flame.fill"
        case .paused: return "pause.fill"
        case .done: return "checkmark"
        }
    }
}

struct JobRunLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: JobRunAttributes.self) { context in
            LockScreenRunView(state: context.state)
                .activityBackgroundTint(nil)
                .activitySystemActionForegroundColor(RunPalette.ember)
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    RunGlyph(phase: context.state.phase)
                        .padding(.leading, 4)
                }
                DynamicIslandExpandedRegion(.center) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(context.state.title)
                            .font(.subheadline.weight(.semibold))
                            .lineLimit(1)
                        Text(context.state.detail)
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
                Image(systemName: RunPalette.glyph(for: context.state.phase))
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(RunPalette.tint(for: context.state.phase))
            } compactTrailing: {
                CompactFraction(state: context.state)
            } minimal: {
                Image(systemName: RunPalette.glyph(for: context.state.phase))
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(RunPalette.tint(for: context.state.phase))
            }
            .keylineTint(RunPalette.ember)
        }
    }
}

/// "6/9" while searching, "24%" while scoring — the most compact honest read.
private struct CompactFraction: View {
    let state: JobRunAttributes.ContentState

    var body: some View {
        switch state.phase {
        case .searching:
            Text("\(state.completed)/\(state.total)")
                .font(.caption2.weight(.bold).monospacedDigit())
                .foregroundStyle(RunPalette.ember)
        case .scoring:
            Text("\(Int(state.fraction * 100))%")
                .font(.caption2.weight(.bold).monospacedDigit())
                .foregroundStyle(RunPalette.ember)
        case .paused:
            Image(systemName: "pause.fill")
                .font(.caption2)
                .foregroundStyle(RunPalette.amber)
        case .done:
            Image(systemName: "checkmark")
                .font(.caption2.weight(.bold))
                .foregroundStyle(RunPalette.success)
        }
    }
}

private struct RunGlyph: View {
    let phase: JobRunAttributes.Phase

    var body: some View {
        Image(systemName: RunPalette.glyph(for: phase))
            .font(.subheadline.weight(.semibold))
            .foregroundStyle(.white)
            .frame(width: 28, height: 28)
            .background(
                RoundedRectangle(cornerRadius: 7)
                    .fill(LinearGradient(
                        colors: gradient,
                        startPoint: .topLeading, endPoint: .bottomTrailing))
            )
    }

    private var gradient: [Color] {
        switch phase {
        case .paused: return [RunPalette.amber, RunPalette.amber.opacity(0.8)]
        case .done: return [RunPalette.success, RunPalette.success.opacity(0.8)]
        default: return [RunPalette.ember, RunPalette.emberDeep]
        }
    }
}

/// The big tabular numeral: jobs found while searching, done/total while
/// scoring, new-job count when complete.
private struct HeadlineCount: View {
    let state: JobRunAttributes.ContentState

    private var number: String {
        switch state.phase {
        case .scoring: return "\(state.completed)/\(state.total)"
        default: return "\(state.jobsFound)"
        }
    }

    private var label: String {
        switch state.phase {
        case .scoring: return "scored"
        case .done: return "new jobs"
        case .paused: return "found so far"
        case .searching: return "found"
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

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .center, spacing: 10) {
                RunGlyph(phase: state.phase)
                VStack(alignment: .leading, spacing: 1) {
                    Text(state.title)
                        .font(.subheadline.weight(.semibold))
                        .lineLimit(1)
                    Text(state.detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                Spacer(minLength: 8)
                HeadlineCount(state: state)
            }
            RunProgressBar(state: state)
            if state.phase != .done {
                HStack {
                    Spacer()
                    Button(intent: StopRunIntent()) {
                        Text("Stop")
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
