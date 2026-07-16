import Foundation

/// The one switch for LinkedIn sourcing, in two honest layers.
///
/// `isBuildEnabled` is compiled in: a build made with `JOBSMITH_NO_LINKEDIN`
/// defined has no LinkedIn source at all, and the setting for it doesn't
/// render. That is the lever to pull if the App Store ever declines the
/// feature — one build setting, no code surgery, and sideload/TestFlight
/// builds are unaffected.
///
/// `isEnabled(_:)` adds the user's own Settings toggle (default off — the user
/// opts in, so a fresh install never contacts LinkedIn) on top.
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

    /// The user's own `li_at` session, captured by the in-app sign-in. Used
    /// only to read their own profile during onboarding — never for job
    /// fetching. Sending it to the guest job endpoints was tried and LinkedIn
    /// answers a cookie-bearing request with redirect loops, 429s, or the
    /// logged-in SPA shell none of which the parsers can read (2026-07-15:
    /// 0/102 descriptions signed in, 102/102 as guest). It also puts the
    /// user's own account in front of LinkedIn's automation detection.
    public static func cookie(_ config: AppConfig) -> String? {
        let cookie = config.apiKeys.linkedInCookie.trimmingCharacters(in: .whitespacesAndNewlines)
        return cookie.isEmpty ? nil : cookie
    }

    public static func isAuthenticated(_ config: AppConfig) -> Bool {
        cookie(config) != nil
    }
}
