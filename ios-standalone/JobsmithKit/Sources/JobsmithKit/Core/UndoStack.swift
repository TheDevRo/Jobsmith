import Foundation

/// One action the user can take back, and the closure that takes it back.
///
/// `label` is written as a past-tense sentence ("Passed on Senior Engineer at
/// Acme.") because it is shown as the body of the undo prompt and, once undone,
/// as the activity-log entry — the same words have to read correctly in both.
@MainActor
public struct UndoableAction: Identifiable {
    public let id = UUID()
    public let label: String
    let revert: () -> Void

    public init(label: String, revert: @escaping () -> Void) {
        self.label = label
        self.revert = revert
    }
}

/// The undo history behind the shake gesture.
///
/// In memory only, and deliberately so: an undo offered after a relaunch would
/// be reverting something the user stopped thinking about hours ago — and on a
/// synced install, something the desktop may have already acted on.
@MainActor
public final class UndoStack {
    /// Deep enough to walk back a bad run of swipes, shallow enough that the
    /// oldest entry still refers to something the user remembers doing.
    public static let capacity = 10

    public private(set) var actions: [UndoableAction] = []

    public init() {}

    public var canUndo: Bool { !actions.isEmpty }

    /// The action a shake offers to take back.
    public var top: UndoableAction? { actions.last }

    public func register(_ label: String, revert: @escaping () -> Void) {
        actions.append(UndoableAction(label: label, revert: revert))
        if actions.count > Self.capacity {
            actions.removeFirst(actions.count - Self.capacity)
        }
    }

    /// Take back a specific action and drop it from the history.
    ///
    /// By id rather than "the top one" because the two are not always the same:
    /// the prompt names an action, and something else — a notification's outcome
    /// button, a finishing background task — can land on the stack while that
    /// prompt is on screen. Undoing whatever happens to be on top at *confirm*
    /// time would then revert an action the user never agreed to.
    @discardableResult
    public func undo(_ id: UUID) -> UndoableAction? {
        guard let index = actions.firstIndex(where: { $0.id == id }) else { return nil }
        let action = actions.remove(at: index)
        action.revert()
        return action
    }

    /// Drop the history without reverting anything — for when the rows the
    /// closures would write back no longer exist (a wipe, a factory reset).
    public func clear() {
        actions.removeAll()
    }
}
