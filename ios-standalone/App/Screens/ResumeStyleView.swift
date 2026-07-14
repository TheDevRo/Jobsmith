import SwiftUI
import PDFKit
import JobsmithKit

/// The resume style picker, with the sample resume it produces.
///
/// Replaces two silent `Picker` rows in Settings. The page is rendered by the
/// real generator on-device, so tapping a style shows exactly what a recruiter
/// would open — no round trip, no approximation.
struct ResumeStyleView: View {
    @Environment(AppModel.self) private var model

    @State private var style: HonestyConfig.Style = .default
    @State private var accent: HonestyConfig.ResumeAccent = .default
    @State private var pdf: Data?

    var body: some View {
        ScrollView {
            VStack(spacing: 22) {
                preview
                styleList
                accentRow
            }
            .padding(.vertical, 16)
        }
        .background(Color(.systemGroupedBackground))
        .navigationTitle("Resume Style")
        .navigationBarTitleDisplayMode(.inline)
        .onAppear {
            // A NavigationLink builds its destination eagerly, so @State
            // captured at list-render time is stale. Re-sync on appear.
            style = model.config.honesty.resumeStyle
            accent = model.config.honesty.resumeAccent
            render()
        }
    }

    // MARK: - Sample page

    private var preview: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("SAMPLE RESUME")
                    .font(.caption2.weight(.semibold))
                    .kerning(1.1)
                    .foregroundStyle(.secondary)
                Spacer()
                Text(subtitle)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Group {
                if let pdf {
                    PDFPageView(data: pdf)
                } else {
                    Color(.secondarySystemGroupedBackground)
                }
            }
            .aspectRatio(8.5 / 11, contentMode: .fit)   // US Letter
            .frame(maxWidth: .infinity)
            .clipShape(RoundedRectangle(cornerRadius: 6))
            .overlay(
                RoundedRectangle(cornerRadius: 6)
                    .strokeBorder(Color.black.opacity(0.12))
            )
            .shadow(color: .black.opacity(0.16), radius: 10, y: 5)

            Text("Sample content, not your resume. Rendered by the same code that writes your real files.")
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 20)
    }

    private var subtitle: String {
        style.isMonochrome ? style.label : "\(style.label) · \(accent.label) accent"
    }

    // MARK: - Style

    private var styleList: some View {
        VStack(spacing: 0) {
            ForEach(HonestyConfig.Style.allCases, id: \.self) { s in
                Button {
                    guard s != style else { return }
                    style = s
                    model.saveConfig { $0.honesty.resumeStyle = s }
                    render()
                } label: {
                    HStack(alignment: .top, spacing: 12) {
                        VStack(alignment: .leading, spacing: 2) {
                            HStack(spacing: 6) {
                                Text(s.label).font(.body.weight(.semibold))
                                if s.isMonochrome {
                                    Text("MONO")
                                        .font(.system(size: 9, weight: .semibold))
                                        .kerning(0.6)
                                        .foregroundStyle(.secondary)
                                        .padding(.horizontal, 4)
                                        .padding(.vertical, 1)
                                        .overlay(
                                            RoundedRectangle(cornerRadius: 3)
                                                .strokeBorder(Color.secondary.opacity(0.4))
                                        )
                                }
                            }
                            Text(s.blurb)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .multilineTextAlignment(.leading)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        Spacer(minLength: 0)
                        Image(systemName: s == style ? "checkmark.circle.fill" : "circle")
                            .foregroundStyle(s == style ? Color.accentColor : Color.secondary.opacity(0.4))
                            .font(.title3)
                    }
                    .contentShape(Rectangle())
                    .padding(.horizontal, 16)
                    .padding(.vertical, 11)
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(s == style ? [.isSelected] : [])

                if s != HonestyConfig.Style.allCases.last {
                    Divider().padding(.leading, 16)
                }
            }
        }
        .background(Color(.secondarySystemGroupedBackground))
        .clipShape(RoundedRectangle(cornerRadius: 10))
        .padding(.horizontal, 20)
    }

    // MARK: - Accent

    @ViewBuilder
    private var accentRow: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("ACCENT COLOR")
                .font(.caption2.weight(.semibold))
                .kerning(1.1)
                .foregroundStyle(.secondary)

            HStack(spacing: 12) {
                ForEach(HonestyConfig.ResumeAccent.allCases, id: \.self) { a in
                    Button {
                        guard a != accent else { return }
                        accent = a
                        model.saveConfig { $0.honesty.resumeAccent = a }
                        render()
                    } label: {
                        Circle()
                            .fill(swatch(a))
                            .frame(width: 30, height: 30)
                            .overlay(Circle().strokeBorder(Color.black.opacity(0.15)))
                            .overlay(
                                Circle()
                                    .strokeBorder(Color.accentColor, lineWidth: 2.5)
                                    .padding(-3)
                                    .opacity(a == accent ? 1 : 0)
                            )
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("\(a.label) accent")
                    .accessibilityAddTraits(a == accent ? [.isSelected] : [])
                }
                Spacer(minLength: 0)
            }
            .disabled(style.isMonochrome)
            .opacity(style.isMonochrome ? 0.35 : 1)

            // Say why the control is dead rather than letting it no-op.
            if style.isMonochrome {
                Text("\(style.label) is monochrome by design — it ignores the accent color.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 20)
    }

    /// `.default` means "whatever the chosen style ships with", so it gets a
    /// neutral split swatch rather than implying one specific color.
    private func swatch(_ a: HonestyConfig.ResumeAccent) -> AnyShapeStyle {
        guard let hex = a.hex else {
            return AnyShapeStyle(LinearGradient(
                colors: [Color(hex6: "1F3A5F"), Color(hex6: "6D1F2C")],
                startPoint: .topLeading, endPoint: .bottomTrailing
            ))
        }
        return AnyShapeStyle(Color(hex6: hex))
    }

    // MARK: - Render

    private func render() {
        let s = style
        let a = accent
        Task.detached(priority: .userInitiated) {
            let data = StylePreviewSample.pdf(style: s, accent: a)
            await MainActor.run { pdf = data }
        }
    }
}

/// A single non-interactive PDF page, scaled to fit. `PDFView` rather than a
/// rasterized image so the type stays crisp at any size.
private struct PDFPageView: UIViewRepresentable {
    let data: Data

    func makeUIView(context: Context) -> PDFView {
        let view = FittedPDFView()
        view.displayMode = .singlePage
        view.displaysPageBreaks = false
        view.isUserInteractionEnabled = false
        view.backgroundColor = .white
        view.subviews.first?.backgroundColor = .white   // the inner scroll view
        return view
    }

    func updateUIView(_ view: PDFView, context: Context) {
        view.document = PDFDocument(data: data)
        view.setNeedsLayout()
    }
}

/// `scaleFactorForSizeToFit` is only meaningful once the view has its real
/// bounds. Setting it from `updateUIView` runs too early and leaves the page
/// zoomed in with its right edge cut off, so re-fit on every layout pass
/// instead. `autoScales` alone doesn't do this — it keeps the user's zoom.
private final class FittedPDFView: PDFView {
    override func layoutSubviews() {
        super.layoutSubviews()
        guard document != nil, scaleFactorForSizeToFit > 0 else { return }
        scaleFactor = scaleFactorForSizeToFit
    }
}
