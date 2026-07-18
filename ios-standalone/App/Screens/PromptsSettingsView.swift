import SwiftUI
import JobsmithKit

/// AI Prompts editor — parity with the desktop Settings → Prompts tab.
///
/// Lists every internal LLM prompt grouped by purpose; each row opens a
/// `PromptEditorView`. An override lives in `AppConfig.promptOverrides` keyed by
/// the prompt id and wins over the built-in default (see `PromptRegistry`); the
/// "Customized" badge tracks whether such an override exists.
struct PromptsSettingsView: View {
    @Environment(AppModel.self) private var model

    /// Prompts bucketed by group, groups in first-appearance order.
    private var groups: [(name: String, infos: [PromptRegistry.PromptInfo])] {
        var order: [String] = []
        var byGroup: [String: [PromptRegistry.PromptInfo]] = [:]
        for info in PromptRegistry.orderedInfos {
            if byGroup[info.group] == nil { order.append(info.group) }
            byGroup[info.group, default: []].append(info)
        }
        return order.map { ($0, byGroup[$0] ?? []) }
    }

    var body: some View {
        List {
            ForEach(groups, id: \.name) { group in
                Section {
                    ForEach(group.infos) { info in
                        NavigationLink {
                            PromptEditorView(info: info)
                        } label: {
                            row(info)
                        }
                    }
                } header: {
                    Eyebrow(text: group.name)
                }
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle("AI Prompts")
    }

    @ViewBuilder
    private func row(_ info: PromptRegistry.PromptInfo) -> some View {
        let customized = model.config.promptOverrides[info.id] != nil
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text(info.label)
                if customized {
                    Text("Customized")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(.tint)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 2)
                        .background(Capsule().fill(Color.accentColor.opacity(0.15)))
                }
            }
            Text(info.description)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(customized ? "\(info.label), customized" : info.label)
    }
}

/// Editor for one prompt's template: a monospaced editor seeded with the
/// current effective template (override-or-default), tappable placeholder chips
/// that append `{name}`, and Save / Reset that write through to
/// `AppConfig.promptOverrides`.
struct PromptEditorView: View {
    @Environment(AppModel.self) private var model
    let info: PromptRegistry.PromptInfo

    @State private var text = ""
    @State private var loaded = false
    @State private var showResetConfirm = false

    private var defaultTemplate: String {
        PromptRegistry.defaultTemplate(info.id) ?? ""
    }

    /// The template in effect right now (override when set and non-blank, else
    /// the default) — what Save diffs against so it only enables on a real edit.
    private var effectiveTemplate: String {
        PromptRegistry.template(info.id, config: model.config)
    }

    private var isCustomized: Bool {
        model.config.promptOverrides[info.id] != nil
    }

    /// Editor text with Windows line endings folded to `\n`, as stored.
    private var normalizedText: String {
        text.replacingOccurrences(of: "\r\n", with: "\n")
    }

    private var canSave: Bool {
        normalizedText != effectiveTemplate
    }

    /// `{token}` matches in the editor that aren't declared placeholders — a
    /// non-blocking heads-up, since unknown tokens render as literal text.
    private var unknownTokens: [String] {
        let known = Set(info.variables.map { $0.name })
        guard let re = try? NSRegularExpression(pattern: "\\{([a-z][a-z0-9_]*)\\}") else { return [] }
        let ns = text as NSString
        var seen: [String] = []
        for m in re.matches(in: text, range: NSRange(location: 0, length: ns.length)) {
            let name = ns.substring(with: m.range(at: 1))
            if !known.contains(name) && !seen.contains(name) { seen.append(name) }
        }
        return seen
    }

    var body: some View {
        List {
            Section {
                Text(info.description)
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } header: {
                Eyebrow(text: "What this does")
            }

            Section {
                if info.variables.isEmpty {
                    Text("This prompt takes no variables.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    ChipFlowLayout(spacing: 6) {
                        ForEach(info.variables, id: \.name) { variable in
                            chip(variable)
                        }
                    }
                }
            } header: {
                Eyebrow(text: "Placeholders")
            } footer: {
                if info.variables.isEmpty {
                    Text("Tap in the template to edit it. Any {token} you add that isn't a known placeholder is left as literal text.")
                } else {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Tap a placeholder to append it to the template.")
                        ForEach(info.variables, id: \.name) { variable in
                            Text("{\(variable.name)}").monospaced()
                                + Text(" — \(variable.doc)")
                        }
                    }
                }
            }

            Section {
                TextEditor(text: $text)
                    .font(.system(.footnote, design: .monospaced))
                    .frame(minHeight: 320)
                    .autocorrectionDisabled()
                    .textInputAutocapitalization(.never)
                    .accessibilityLabel("Prompt template")
            } header: {
                Eyebrow(text: "Template")
            }

            if !unknownTokens.isEmpty {
                Section {
                    Label {
                        Text("Unrecognized placeholders will appear literally in the prompt: \(unknownTokens.map { "{\($0)}" }.joined(separator: ", "))")
                    } icon: {
                        Image(systemName: "exclamationmark.triangle")
                    }
                    .font(.caption)
                    .foregroundStyle(.orange)
                }
            }

            Section {
                Button {
                    save()
                } label: {
                    Label(isCustomized ? "Save changes" : "Save customization",
                          systemImage: "checkmark.circle")
                }
                .disabled(!canSave)

                if isCustomized {
                    Button(role: .destructive) {
                        showResetConfirm = true
                    } label: {
                        Label("Reset to default", systemImage: "arrow.uturn.backward")
                    }
                }
            } footer: {
                Text("Saving text identical to the default (or empty) removes your customization. Resetting restores the built-in template.")
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle(info.label)
        .navigationBarTitleDisplayMode(.inline)
        .onAppear {
            if !loaded {
                text = effectiveTemplate
                loaded = true
            }
        }
        .confirmationDialog("Reset to default?",
                            isPresented: $showResetConfirm, titleVisibility: .visible) {
            Button("Reset to default", role: .destructive) { reset() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Discards your customization and restores the built-in \(info.label) template.")
        }
    }

    private func chip(_ variable: PromptRegistry.PromptVariable) -> some View {
        Button {
            insert(variable.name)
        } label: {
            Text("{\(variable.name)}")
                .font(.caption.monospaced())
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(Capsule().fill(Color.secondary.opacity(0.15)))
        }
        .buttonStyle(.borderless)
        .accessibilityLabel("Insert placeholder \(variable.name)")
    }

    /// Append `{name}` to the template, keeping a separating space so tokens
    /// don't fuse onto the preceding word (insert-at-end per spec).
    private func insert(_ name: String) {
        let token = "{\(name)}"
        if text.isEmpty || text.hasSuffix("\n") || text.hasSuffix(" ") {
            text += token
        } else {
            text += " " + token
        }
    }

    /// Empty or default-identical text removes the override; anything else sets
    /// it (line endings normalized to `\n`).
    private func save() {
        let id = info.id
        let value = normalizedText
        if value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || value == defaultTemplate {
            model.saveConfig { $0.promptOverrides.removeValue(forKey: id) }
            text = defaultTemplate
        } else {
            model.saveConfig { $0.promptOverrides[id] = value }
            text = value
        }
    }

    private func reset() {
        let id = info.id
        model.saveConfig { $0.promptOverrides.removeValue(forKey: id) }
        text = defaultTemplate
    }
}

/// Minimal wrapping-row layout for the placeholder chips: lays subviews left to
/// right, wrapping to a new line when the next one would overflow the width.
private struct ChipFlowLayout: Layout {
    var spacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        arrange(subviews: subviews, maxWidth: proposal.width ?? .infinity).size
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize,
                       subviews: Subviews, cache: inout ()) {
        let frames = arrange(subviews: subviews, maxWidth: bounds.width).frames
        for (index, frame) in frames.enumerated() {
            subviews[index].place(
                at: CGPoint(x: bounds.minX + frame.minX, y: bounds.minY + frame.minY),
                proposal: ProposedViewSize(frame.size))
        }
    }

    private func arrange(subviews: Subviews, maxWidth: CGFloat) -> (frames: [CGRect], size: CGSize) {
        var frames: [CGRect] = []
        var x: CGFloat = 0, y: CGFloat = 0, rowHeight: CGFloat = 0, maxRowWidth: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x > 0 && x + size.width > maxWidth {
                x = 0
                y += rowHeight + spacing
                rowHeight = 0
            }
            frames.append(CGRect(x: x, y: y, width: size.width, height: size.height))
            x += size.width + spacing
            rowHeight = max(rowHeight, size.height)
            maxRowWidth = max(maxRowWidth, x - spacing)
        }
        return (frames, CGSize(width: maxRowWidth, height: y + rowHeight))
    }
}
