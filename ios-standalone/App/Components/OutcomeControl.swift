import SwiftUI
import JobsmithKit

/// Post-apply outcome affordances.
///
/// The phone is where you find out: the rejection email and the "can you do
/// Tuesday?" land here, not at the desk. So recording an outcome has to cost one
/// tap — anything heavier and the funnel just stays empty, which is exactly what
/// happened when this only existed on the desktop.

extension ApplicationOutcome {
    var tint: Color {
        switch self {
        case .awaiting:              return .secondary
        case .noResponse, .rejected: return Theme.steel
        case .withdrawn:             return .secondary
        case .screening:             return Color(hex: 0xE8A13C)
        case .interview:             return Theme.ember
        case .offer:                 return Theme.success
        }
    }

    var systemImage: String {
        switch self {
        case .awaiting:    return "clock"
        case .noResponse:  return "wind"
        case .screening:   return "phone"
        case .interview:   return "person.2"
        case .offer:       return "checkmark.seal"
        case .rejected:    return "xmark.circle"
        case .withdrawn:   return "arrow.uturn.backward"
        }
    }
}

/// A compact, tappable chip showing where an application stands. Tap for the
/// full list; the three most common answers are hoisted to the top of the menu.
struct OutcomeChip: View {
    let outcome: ApplicationOutcome
    let onSelect: (ApplicationOutcome) -> Void

    var body: some View {
        Menu {
            OutcomeMenuItems(current: outcome, onSelect: onSelect)
        } label: {
            HStack(spacing: 4) {
                Image(systemName: outcome.systemImage)
                Text(outcome.label)
            }
            .font(.caption2.weight(.semibold))
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .background(outcome.tint.opacity(0.15), in: Capsule())
            .foregroundStyle(outcome.tint)
        }
        .accessibilityLabel("Outcome: \(outcome.label)")
        .accessibilityHint("Change what the employer did")
    }
}

/// The menu body, shared by the chip and the detail screen. `screening`,
/// `rejected`, and `noResponse` come first because they are what actually
/// happens most; the rest sit below a divider.
struct OutcomeMenuItems: View {
    let current: ApplicationOutcome
    let onSelect: (ApplicationOutcome) -> Void

    private static let common: [ApplicationOutcome] = [.screening, .rejected, .noResponse]
    private static let rest: [ApplicationOutcome] = [.interview, .offer, .withdrawn, .awaiting]

    var body: some View {
        ForEach(Self.common, id: \.self) { item(for: $0) }
        Divider()
        ForEach(Self.rest, id: \.self) { item(for: $0) }
    }

    private func item(for outcome: ApplicationOutcome) -> some View {
        Button {
            onSelect(outcome)
        } label: {
            Label(outcome.label, systemImage: current == outcome
                  ? "checkmark" : outcome.systemImage)
        }
    }
}

/// The funnel, read from event history — so an application that interviewed and
/// was then rejected still counts toward the stages it actually reached.
struct OutcomeFunnelView: View {
    let applied: Int
    let stages: [(ApplicationOutcome, Int)]

    var body: some View {
        HStack(spacing: 0) {
            stage(label: "Applied", count: applied, tint: Theme.steel)
            ForEach(stages, id: \.0) { outcome, count in
                Image(systemName: "chevron.compact.right")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
                stage(label: outcome.label, count: count, tint: outcome.tint)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(
            ([("Applied", applied)] + stages.map { ($0.0.label, $0.1) })
                .map { "\($0.0): \($0.1)" }
                .joined(separator: ", "))
    }

    private func stage(label: String, count: Int, tint: Color) -> some View {
        VStack(spacing: 2) {
            Text("\(count)")
                .font(.title3.weight(.semibold).monospacedDigit())
                .foregroundStyle(count > 0 ? tint : .secondary)
            Text(label)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
        }
        .frame(maxWidth: .infinity)
    }
}
