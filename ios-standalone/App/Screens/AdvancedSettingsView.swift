import SwiftUI
import JobsmithKit

/// Advanced settings — the power-user surface split out of the main Settings
/// list: Workday one-tap credentials (moved here from Setup) and the AI Prompts
/// editor. Both are things most users never need to touch.
struct AdvancedSettingsView: View {
    @Environment(AppModel.self) private var model

    /// Prompts with an override stored — matches the per-row "Customized"
    /// badge in `PromptsSettingsView` (presence of a key).
    private var customizedPrompts: Int {
        model.config.promptOverrides.count
    }

    var body: some View {
        List {
            Section {
                NavigationLink {
                    WorkdaySettingsView()
                } label: {
                    row("Workday", system: "building.2",
                        detail: model.config.apiKeys.workdayEmail.isEmpty ? "Not set up" : "Configured")
                }
                NavigationLink {
                    PromptsSettingsView()
                } label: {
                    row("AI Prompts", system: "text.book.closed",
                        detail: customizedPrompts > 0 ? "\(customizedPrompts) customized" : nil)
                }
            } header: {
                Eyebrow(text: "Advanced")
            } footer: {
                Text("Workday one-tap sign-in credentials, and editable templates for every internal AI prompt. The built-in prompts are tuned to work well — customize them only if you know what you're changing.")
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle("Advanced")
    }

    private func row(_ title: String, system: String, detail: String?) -> some View {
        HStack {
            Label(title, systemImage: system)
            if let detail {
                Spacer()
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(detail.map { "\(title), \($0)" } ?? title)
    }
}
