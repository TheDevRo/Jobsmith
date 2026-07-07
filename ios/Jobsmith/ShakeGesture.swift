import SwiftUI
import UIKit

/// Shake the device to open the server settings sheet — documented on the
/// setup screen and in README-IOS.md. (The web dashboard fills the whole
/// screen, so there's no native chrome to hang a settings button on.)
extension UIWindow {
    open override func motionEnded(_ motion: UIEvent.EventSubtype, with event: UIEvent?) {
        if motion == .motionShake {
            NotificationCenter.default.post(name: .deviceDidShake, object: nil)
        }
        super.motionEnded(motion, with: event)
    }
}

extension Notification.Name {
    static let deviceDidShake = Notification.Name("jobsmith.deviceDidShake")
}

private struct ShakeModifier: ViewModifier {
    let action: () -> Void

    func body(content: Content) -> some View {
        content.onReceive(NotificationCenter.default.publisher(for: .deviceDidShake)) { _ in
            action()
        }
    }
}

extension View {
    func onShake(perform action: @escaping () -> Void) -> some View {
        modifier(ShakeModifier(action: action))
    }
}
