import SwiftUI
import JobsmithKit

/// An inline search bar that focuses itself on appear and offers clear +
/// cancel. Revealed by the toolbar magnifying-glass button in Inbox/Pipeline.
struct JobSearchField: View {
    @Binding var text: String
    /// When false, the trailing "Cancel" button is omitted — used where the
    /// caller already exposes a dedicated dismiss affordance (e.g. Inbox's
    /// collapsed search button).
    var showsCancel: Bool = true
    var onCancel: () -> Void
    @FocusState private var focused: Bool

    var body: some View {
        HStack(spacing: 8) {
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass")
                    .foregroundStyle(.secondary)
                TextField("Search title, company, board…", text: $text)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .submitLabel(.search)
                    .focused($focused)
                if !text.isEmpty {
                    Button {
                        text = ""
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 8)
            .background(Capsule().fill(Color.primary.opacity(0.06)))

            if showsCancel {
                Button("Cancel") {
                    text = ""
                    onCancel()
                }
                .font(.callout)
            }
        }
        .padding(.horizontal, 16)
        .padding(.top, 8)
        .padding(.bottom, 4)
        .onAppear { focused = true }
    }
}

/// The "Job board" multi-select filter, rendered as a submenu of an overflow
/// menu. Each board is a checkable toggle; an empty selection means all boards.
/// Shared by Inbox and Pipeline so the two behave identically.
struct BoardFilterMenu: View {
    let boards: [String]
    @Binding var selected: Set<String>

    var body: some View {
        Menu {
            ForEach(boards, id: \.self) { board in
                Toggle(SourceCatalog.displayName(for: board), isOn: binding(board))
            }
            if !selected.isEmpty {
                Divider()
                Button("Show all boards") { selected = [] }
            }
        } label: {
            Label(selected.isEmpty ? "Job board" : "Job board (\(selected.count))",
                  systemImage: "rectangle.stack")
        }
    }

    private func binding(_ board: String) -> Binding<Bool> {
        Binding(
            get: { selected.contains(board) },
            set: { isOn in
                if isOn { selected.insert(board) } else { selected.remove(board) }
            }
        )
    }
}
