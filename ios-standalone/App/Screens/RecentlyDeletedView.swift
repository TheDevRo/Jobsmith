import SwiftUI
import JobsmithKit

/// The recycle bin: postings you deleted are kept here (soft-deleted, so they
/// don't reappear in searches) with per-row restore and an "empty bin" that
/// permanently erases them — after which they become discoverable again.
struct RecentlyDeletedView: View {
    @Environment(AppModel.self) private var model
    @State private var showEmptyConfirm = false

    var body: some View {
        Group {
            if model.recentlyDeleted.isEmpty {
                ContentUnavailableView(
                    "No deleted postings", systemImage: "trash",
                    description: Text("Postings you delete land here so they don't reappear in searches."))
            } else {
                List {
                    ForEach(model.recentlyDeleted) { job in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(job.title)
                                .font(.body)
                            Text("\(job.company) · \(SourceCatalog.displayName(for: job.source))")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                            Button {
                                model.restoreDeleted(job.id)
                            } label: {
                                Label("Restore", systemImage: "arrow.uturn.backward")
                            }
                            .tint(.blue)
                            .accessibilityLabel("Restore")
                        }
                    }
                }
            }
        }
        .navigationTitle("Recently deleted")
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button(role: .destructive) {
                    showEmptyConfirm = true
                } label: {
                    Text("Empty Recycling Bin")
                }
                .disabled(model.recentlyDeleted.isEmpty)
                .accessibilityLabel("Empty Recycling Bin")
            }
        }
        .confirmationDialog(
            model.recentlyDeleted.count == 1
                ? "Permanently erase 1 posting?"
                : "Permanently erase \(model.recentlyDeleted.count) postings?",
            isPresented: $showEmptyConfirm, titleVisibility: .visible) {
            Button("Erase", role: .destructive) {
                model.emptyRecycleBin()
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("These postings may reappear in future searches. This can't be undone.")
        }
    }
}
