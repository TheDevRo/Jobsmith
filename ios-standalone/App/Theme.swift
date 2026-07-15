import SwiftUI

/// Jobsmith visual system. One signature idea: fit score is "heat" — the
/// forge metaphor. Scores render on a steel→ember ramp; amber is reserved
/// for heat and the primary action, everything else stays quiet slate.
enum Theme {
    // Base palette (brand-anchored: neutral charcoal, no blue cast — matches the
    // desktop app's chrome). Blue lives only in `steel`, the cool end of the ramp.
    //
    // `ink` and `slate` are *surfaces*, so they must follow the system
    // appearance — a hardcoded near-black card would render as a dark hole in
    // Light Mode. They resolve per-trait; the brand hues (`ember`, `steel`,
    // heat ramp) stay constant in both modes, as the ramp's meaning is absolute.
    static let ink = dynamic(light: 0xF7F7F8, dark: 0x0C0C0E)     // app background
    static let slate = dynamic(light: 0xFFFFFF, dark: 0x1C1C1F)   // card surface
    static let steel = Color(hex: 0x5E7CA0)        // cool secondary
    static let ember = Color(hex: 0xF0863A)        // signature accent
    static let emberDeep = Color(hex: 0xD9541E)    // hot end of the ramp
    static let success = Color(hex: 0x4CAF7D)

    /// A color that resolves to a different hex per interface style.
    static func dynamic(light: UInt32, dark: UInt32) -> Color {
        Color(UIColor { traits in
            UIColor(Color(hex: traits.userInterfaceStyle == .dark ? dark : light))
        })
    }

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

    /// Foreground guaranteed to contrast with the heat ramp underneath it.
    /// The mid-ramp amber (#E8A13C) is far too light for white text (~2.2:1),
    /// while the steel and deep-ember ends are too dark for black — so pick by
    /// the actual luminance of the swatch rather than assuming one or the other.
    static func onHeat(for score: Double) -> Color {
        heat(for: score).relativeLuminance > 0.38 ? Color(hex: 0x231005) : .white
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

    /// 6-char hex, the storage format of the resume accent palette
    /// (`HonestyConfig.ResumeAccent.hex`). Unparseable input falls back to
    /// black rather than trapping — a bad swatch beats a crash.
    init(hex6: String) {
        self.init(hex: UInt32(hex6, radix: 16) ?? 0)
    }

    /// WCAG relative luminance (0 = black, 1 = white), used to choose a
    /// foreground that stays legible across the heat ramp.
    var relativeLuminance: Double {
        var (r, g, b, a): (CGFloat, CGFloat, CGFloat, CGFloat) = (0, 0, 0, 0)
        UIColor(self).getRed(&r, green: &g, blue: &b, alpha: &a)
        func channel(_ c: CGFloat) -> Double {
            let v = Double(c)
            return v <= 0.03928 ? v / 12.92 : pow((v + 0.055) / 1.055, 2.4)
        }
        return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
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
///
/// The number is always drawn as text, so the ramp color is redundant
/// reinforcement rather than the sole carrier of meaning (color-blind safe).
struct HeatChip: View {
    let score: Double?

    var body: some View {
        Group {
            if let score {
                HStack(spacing: 4) {
                    Image(systemName: "flame.fill")
                        .font(.caption2)
                    Text("\(Int(score))")
                        .font(.callout.weight(.bold).monospacedDigit())
                }
                .foregroundStyle(Theme.onHeat(for: score))
                .padding(.horizontal, 9)
                .padding(.vertical, 4)
                .background(Capsule().fill(Theme.heatGradient(for: score)))
            } else {
                HStack(spacing: 4) {
                    Image(systemName: "flame")
                        .font(.caption2)
                    Text("—")
                        .font(.callout.weight(.semibold))
                }
                .foregroundStyle(.secondary)
                .padding(.horizontal, 9)
                .padding(.vertical, 4)
                .background(Capsule().strokeBorder(.quaternary))
            }
        }
        // `.ignore` (not `.combine`) so the em-dash placeholder and the flame
        // glyph don't get spoken alongside the label we actually want.
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(score.map { "Fit score \(Int($0)) of 100" } ?? "Not scored yet")
    }
}

/// Detail-screen heat ring.
struct HeatRing: View {
    let score: Double
    /// The ring grows with the user's text size so the score never clips at AX
    /// sizes; the stroke scales with it to keep the proportions.
    @ScaledMetric(relativeTo: .title) private var diameter: CGFloat = 76
    @ScaledMetric(relativeTo: .title) private var stroke: CGFloat = 8

    var body: some View {
        ZStack {
            Circle()
                .stroke(Color.primary.opacity(0.08), lineWidth: stroke)
            Circle()
                .trim(from: 0, to: min(max(score / 100, 0), 1))
                .stroke(Theme.heat(for: score),
                        style: StrokeStyle(lineWidth: stroke, lineCap: .round))
                .rotationEffect(.degrees(-90))
            VStack(spacing: 0) {
                Text("\(Int(score))")
                    .font(.system(.title, design: .rounded).weight(.bold).monospacedDigit())
                Text("FIT")
                    .font(.caption2.weight(.semibold))
                    .fontWidth(.expanded)
                    .foregroundStyle(.secondary)
            }
            .minimumScaleFactor(0.6)
            .padding(stroke * 1.5)
        }
        .frame(width: diameter, height: diameter)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Fit score \(Int(score)) of 100")
    }
}
