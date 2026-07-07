import SwiftUI

/// Jobsmith visual system. One signature idea: fit score is "heat" — the
/// forge metaphor. Scores render on a steel→ember ramp; amber is reserved
/// for heat and the primary action, everything else stays quiet slate.
enum Theme {
    // Base palette (brand-anchored: the desktop app's charcoal-blue).
    static let ink = Color(hex: 0x0C0E14)          // near-black blue
    static let slate = Color(hex: 0x1A1E2A)        // card surface (dark)
    static let steel = Color(hex: 0x5E7CA0)        // cool secondary
    static let ember = Color(hex: 0xF0863A)        // signature accent
    static let emberDeep = Color(hex: 0xD9541E)    // hot end of the ramp
    static let success = Color(hex: 0x4CAF7D)

    /// The heat ramp: cool steel blue (poor fit) → ember (strong fit).
    static func heat(for score: Double) -> Color {
        let t = min(max(score / 100.0, 0), 1)
        // Interpolate steel → amber → deep ember in two segments.
        if t < 0.6 {
            return Color.lerp(Color(hex: 0x6B7A94), Color(hex: 0xE8A13C), t / 0.6)
        }
        return Color.lerp(Color(hex: 0xE8A13C), emberDeep, (t - 0.6) / 0.4)
    }

    static func heatGradient(for score: Double) -> LinearGradient {
        LinearGradient(colors: [heat(for: score).opacity(0.75), heat(for: score)],
                       startPoint: .topLeading, endPoint: .bottomTrailing)
    }
}

extension Color {
    init(hex: UInt32) {
        self.init(.sRGB,
                  red: Double((hex >> 16) & 0xFF) / 255,
                  green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255,
                  opacity: 1)
    }

    static func lerp(_ a: Color, _ b: Color, _ t: Double) -> Color {
        let ua = UIColor(a), ub = UIColor(b)
        var (ra, ga, ba, aa): (CGFloat, CGFloat, CGFloat, CGFloat) = (0, 0, 0, 0)
        var (rb, gb, bb, ab): (CGFloat, CGFloat, CGFloat, CGFloat) = (0, 0, 0, 0)
        ua.getRed(&ra, green: &ga, blue: &ba, alpha: &aa)
        ub.getRed(&rb, green: &gb, blue: &bb, alpha: &ab)
        let clamped = CGFloat(min(max(t, 0), 1))
        return Color(.sRGB,
                     red: ra + (rb - ra) * clamped,
                     green: ga + (gb - ga) * clamped,
                     blue: ba + (bb - ba) * clamped,
                     opacity: aa + (ab - aa) * clamped)
    }
}

/// Small uppercase "eyebrow" label — section voice of the app.
struct Eyebrow: View {
    let text: String
    var body: some View {
        Text(text.uppercased())
            .font(.caption2.weight(.semibold))
            .fontWidth(.expanded)
            .foregroundStyle(.secondary)
            .tracking(1.2)
    }
}

/// The heat chip: score rendered on the steel→ember ramp.
struct HeatChip: View {
    let score: Double?

    var body: some View {
        Group {
            if let score {
                HStack(spacing: 4) {
                    Image(systemName: "flame.fill")
                        .font(.system(size: 11))
                    Text("\(Int(score))")
                        .font(.callout.weight(.bold).monospacedDigit())
                }
                .foregroundStyle(.white)
                .padding(.horizontal, 9)
                .padding(.vertical, 4)
                .background(Capsule().fill(Theme.heatGradient(for: score)))
            } else {
                HStack(spacing: 4) {
                    Image(systemName: "flame")
                        .font(.system(size: 11))
                    Text("—")
                        .font(.callout.weight(.semibold))
                }
                .foregroundStyle(.secondary)
                .padding(.horizontal, 9)
                .padding(.vertical, 4)
                .background(Capsule().strokeBorder(.quaternary))
            }
        }
        .accessibilityLabel(score.map { "Fit score \(Int($0))" } ?? "Not scored")
    }
}

/// Detail-screen heat ring.
struct HeatRing: View {
    let score: Double

    var body: some View {
        ZStack {
            Circle()
                .stroke(Color.primary.opacity(0.08), lineWidth: 8)
            Circle()
                .trim(from: 0, to: min(max(score / 100, 0), 1))
                .stroke(Theme.heat(for: score),
                        style: StrokeStyle(lineWidth: 8, lineCap: .round))
                .rotationEffect(.degrees(-90))
            VStack(spacing: 0) {
                Text("\(Int(score))")
                    .font(.system(.title, design: .rounded).weight(.bold).monospacedDigit())
                Text("FIT")
                    .font(.system(size: 9, weight: .semibold))
                    .fontWidth(.expanded)
                    .foregroundStyle(.secondary)
            }
        }
        .frame(width: 76, height: 76)
        .accessibilityElement()
        .accessibilityLabel("Fit score \(Int(score)) of 100")
    }
}
