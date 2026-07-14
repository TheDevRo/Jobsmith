import SwiftUI
import UIKit

/// Shake-to-undo, wired by hand.
///
/// UIKit hands a motion event to the *first responder* and lets it walk up the
/// chain from there — and if there is no first responder at all, the event is
/// dropped on the floor. A SwiftUI hierarchy leaves the slot empty except while
/// a text field is being edited, which is precisely when the user is looking at
/// the deck and most likely to shake. So something of ours has to hold the slot:
/// an empty view that takes it when nobody wants it, and never when somebody
/// does.
///
/// That last part is the whole trick. Taking the slot unconditionally — on every
/// re-attach, every keyboard dismissal — yanks focus out from under a text field
/// the user is typing in, and closes a menu mid-tap. Hence `claimIfIdle`: we ask
/// who holds it first, and if the answer is anyone at all, we leave it alone. A
/// text field that owns the slot also owns the shake, and offers to undo the
/// typing — which is the right thing to happen there anyway.
private final class ShakeResponderView: UIView {
    var onShake: () -> Void = {}

    override var canBecomeFirstResponder: Bool { true }

    override init(frame: CGRect) {
        super.init(frame: frame)
        // A text field takes the slot to edit and then gives it up to *nobody*,
        // so without reclaiming it the first shake after typing anywhere in the
        // app would go nowhere. Waiting for the keyboard to finish hiding — as
        // opposed to reclaiming the moment a field ends editing — keeps us out of
        // the way while the user is tabbing from one field to the next.
        NotificationCenter.default.addObserver(
            self, selector: #selector(claimIfIdle),
            name: UIResponder.keyboardDidHideNotification, object: nil)
        NotificationCenter.default.addObserver(
            self, selector: #selector(claimIfIdle),
            name: UIApplication.didBecomeActiveNotification, object: nil)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("not used from a nib") }

    override func didMoveToWindow() {
        super.didMoveToWindow()
        claimIfIdle()
    }

    /// Take the first-responder slot, but only when nothing else is going on.
    ///
    /// Holding the slot is harmless; *taking* it at the wrong moment is not.
    /// Grabbing it while a menu or sheet is on screen tears that presentation
    /// down — a tap on an outcome menu lands on nothing, because the menu closed
    /// underneath it. So the claim is fenced three ways: the slot must be empty
    /// (a text field that holds it also handles the shake, offering to undo the
    /// typing, which is right there anyway), nothing may be presented, and the
    /// whole thing waits a turn of the run loop for SwiftUI to finish the update
    /// that brought us here — mid-update the slot reads empty a beat before a
    /// text field claims it.
    @objc private func claimIfIdle() {
        DispatchQueue.main.async { [weak self] in
            guard let self, self.window != nil, !self.isFirstResponder,
                  self.window?.rootViewController?.presentedViewController == nil,
                  UIResponder.currentFirstResponder == nil else { return }
            self.becomeFirstResponder()
        }
    }

    override func motionEnded(_ motion: UIEvent.EventSubtype, with event: UIEvent?) {
        guard motion == .motionShake else {
            super.motionEnded(motion, with: event)
            return
        }
        // Switched off in Settings › Accessibility › Touch means switched off
        // here too: a user turns the gesture off because their hands set it off
        // by accident, and our prompt is no less unwelcome than the system's.
        guard UIAccessibility.isShakeToUndoEnabled else { return }
        onShake()
    }
}

/// Holds the answer to "who is the first responder?" between asking and reading.
/// A box rather than a static on the extension below, because an extension can't
/// carry stored properties.
private enum FirstResponderProbe {
    weak static var captured: UIResponder?
}

private extension UIResponder {
    /// Whoever currently holds the first-responder slot, or nil if it is empty.
    /// UIKit routes `sendAction(to: nil)` to the first responder, so whoever
    /// answers is the one we're asking about — no private API involved.
    static var currentFirstResponder: UIResponder? {
        FirstResponderProbe.captured = nil
        UIApplication.shared.sendAction(#selector(captureAsFirstResponder), to: nil, from: nil, for: nil)
        return FirstResponderProbe.captured
    }

    @objc func captureAsFirstResponder() {
        FirstResponderProbe.captured = self
    }
}

private struct ShakeDetector: UIViewRepresentable {
    let onShake: () -> Void

    func makeUIView(context: Context) -> UIView {
        let view = ShakeResponderView(frame: .zero)
        view.onShake = onShake
        return view
    }

    func updateUIView(_ view: UIView, context: Context) {
        (view as? ShakeResponderView)?.onShake = onShake
    }
}

extension View {
    /// Run `action` when the user shakes the device. Attach once, at the root.
    func onShake(perform action: @escaping () -> Void) -> some View {
        background {
            ShakeDetector(onShake: action)
                .frame(width: 0, height: 0)
                .accessibilityHidden(true)
        }
    }
}
