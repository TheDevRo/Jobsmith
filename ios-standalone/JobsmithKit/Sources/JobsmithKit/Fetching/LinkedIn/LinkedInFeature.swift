import Foundation

/// The one switch for LinkedIn sourcing, in two honest layers.
///
/// `isBuildEnabled` is compiled in: a build made with `JOBSMITH_NO_LINKEDIN`
/// defined has no LinkedIn source at all, and the setting for it doesn't
/// render. That is the lever to pull if the App Store ever declines the
/// feature — one build setting, no code surgery, and sideload/TestFlight
/// builds are unaffected.
///
/// `isEnabled(_:)` adds the user's own Settings toggle (default on) on top.
///
/// Deliberately absent: any remote kill switch and any notion of *who* is
/// running the app. Whatever a reviewer sees is exactly what every user of
/// that same build sees.
public enum LinkedInFeature {
    public static var isBuildEnabled: Bool {
        #if JOBSMITH_NO_LINKEDIN
        return false
        #else
        return true
        #endif
    }

    /// Whether LinkedIn may be fetched at all for this config.
    public static func isEnabled(_ config: AppConfig) -> Bool {
        isBuildEnabled && config.search.linkedInEnabled
    }

    /// The user's own `li_at` session, captured by the in-app sign-in. When it
    /// is present LinkedIn is fetched as the user, which is both the mode
    /// LinkedIn's own terms contemplate and the one that survives the guest
    /// authwall; guest scraping is the fallback for people who don't sign in.
    public static func cookie(_ config: AppConfig) -> String? {
        let cookie = config.apiKeys.linkedInCookie.trimmingCharacters(in: .whitespacesAndNewlines)
        return cookie.isEmpty ? nil : cookie
    }

    public static func isAuthenticated(_ config: AppConfig) -> Bool {
        cookie(config) != nil
    }
}
