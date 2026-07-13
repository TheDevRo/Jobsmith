import SwiftUI
import JobsmithKit

struct ActivityView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        NavigationStack {
            List {
                Section {
                    statGrid
                        .listRowInsets(EdgeInsets())
                        .listRowBackground(Color.clear)
                }
                Section {
                    if model.activity.isEmpty {
                        Text("Activity will appear here as you fetch, scout, and apply.")
                            .foregroundStyle(.secondary)
                    } else {
                        ForEach(model.activity) { entry in
                            VStack(alignment: .leading, spacing: 2) {
                                Text(entry.details)
                                    .font(.callout)
                                HStack {
                                    Eyebrow(text: entry.action)
                                    Spacer()
                                    Text(formatted(entry.timestamp))
                                        .font(.caption2)
                                        .foregroundStyle(.tertiary)
                                }
                            }
                            .padding(.vertical, 2)
                        }
                    }
                } header: {
                    Eyebrow(text: "Recent activity")
                }
            }
            .listStyle(.insetGrouped)
            .navigationTitle("Activity")
            .refreshable { model.refresh() }
        }
    }

    private var statGrid: some View {
        LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 10) {
            statTile("Jobs tracked", value: "\(model.stats.totalJobs)")
            statTile("In inbox", value: "\(model.stats.newInInbox)")
            statTile("Pending review", value: "\(model.stats.pendingReview)")
            statTile("Applied", value: "\(model.stats.appliedTotal)")
        }
        .padding(.vertical, 4)
    }

    private func statTile(_ label: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(value)
                .font(.system(.title, design: .rounded).weight(.bold).monospacedDigit())
            Eyebrow(text: label)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(RoundedRectangle(cornerRadius: 14).fill(Color.primary.opacity(0.04)))
        // Read as "Jobs tracked, 12" instead of the bare number followed by an
        // uppercased, letter-spaced eyebrow.
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(label), \(value)")
    }

    private func formatted(_ iso: String) -> String {
        guard let date = ISO8601DateFormatter().date(from: iso) else { return "" }
        return date.formatted(.relative(presentation: .named))
    }
}
