import XCTest
@testable import JobsmithKit

@MainActor
final class UndoStackTests: XCTestCase {

    func testTopIsTheMostRecentAction() {
        let stack = UndoStack()
        stack.register("first") {}
        stack.register("second") {}
        XCTAssertEqual(stack.top?.label, "second")
        XCTAssertTrue(stack.canUndo)
    }

    func testEmptyStackHasNothingToOffer() {
        let stack = UndoStack()
        XCTAssertNil(stack.top)
        XCTAssertFalse(stack.canUndo)
        XCTAssertNil(stack.undo(UUID()))
    }

    func testUndoRunsTheRevertAndDropsTheAction() {
        let stack = UndoStack()
        var reverted: [String] = []
        stack.register("first") { reverted.append("first") }
        stack.register("second") { reverted.append("second") }

        guard let top = stack.top else { return XCTFail("expected an action") }
        stack.undo(top.id)

        XCTAssertEqual(reverted, ["second"])
        XCTAssertEqual(stack.actions.map(\.label), ["first"])
    }

    /// The prompt names one action, and something else — a notification's outcome
    /// button, a background task finishing — can land on the stack while it's on
    /// screen. Confirming must take back what was named, not whatever is on top.
    func testUndoTakesBackTheNamedActionEvenWhenItIsNoLongerOnTop() {
        let stack = UndoStack()
        var reverted: [String] = []
        stack.register("swipe") { reverted.append("swipe") }
        guard let promptedAction = stack.top else { return XCTFail("expected an action") }
        stack.register("outcome from a notification") { reverted.append("outcome") }

        stack.undo(promptedAction.id)

        XCTAssertEqual(reverted, ["swipe"])
        XCTAssertEqual(stack.actions.map(\.label), ["outcome from a notification"])
    }

    func testAnActionIsOnlyEverUndoneOnce() {
        let stack = UndoStack()
        var reverts = 0
        stack.register("once") { reverts += 1 }
        guard let action = stack.top else { return XCTFail("expected an action") }

        stack.undo(action.id)
        XCTAssertNil(stack.undo(action.id))

        XCTAssertEqual(reverts, 1)
        XCTAssertFalse(stack.canUndo)
    }

    func testOldestActionsFallOffAtCapacity() {
        let stack = UndoStack()
        for index in 0..<(UndoStack.capacity + 3) {
            stack.register("action \(index)") {}
        }
        XCTAssertEqual(stack.actions.count, UndoStack.capacity)
        XCTAssertEqual(stack.actions.first?.label, "action 3")
        XCTAssertEqual(stack.top?.label, "action \(UndoStack.capacity + 2)")
    }

    func testClearDropsTheHistoryWithoutRevertingIt() {
        let stack = UndoStack()
        var reverts = 0
        stack.register("wiped") { reverts += 1 }

        stack.clear()

        XCTAssertEqual(reverts, 0, "a wipe deletes the rows; the revert would write back to nothing")
        XCTAssertFalse(stack.canUndo)
    }
}
