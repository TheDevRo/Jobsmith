import SwiftUI
import JobsmithKit

/// The API-key sections of "Search & sources": the two credentialed job boards
/// (Adzuna, USAJobs) plus the optional BLS salary key.
///
/// Split out of `SearchSettingsView` so that screen is about *what to search
/// for* and this one is about *credentials*. The fields stay `@Binding`s owned
/// by the parent rather than local `@State`: the parent flushes every text
/// field through a single `model.saveConfig` on disappear, and moving only
/// half of that mirror down here would split the write into two racing saves.
struct SourceKeysSettingsView: View {
    @Environment(AppModel.self) private var model

    @Binding var adzunaAppID: String
    @Binding var adzunaAppKey: String
    @Binding var usajobsEmail: String
    @Binding var usajobsKey: String
    @Binding var blsKey: String

    private var enabled: Set<String> { model.config.search.enabledSources }

    var body: some View {
        // Key fields appear only for the sources the user actually turned on.
        if enabled.contains("adzuna") {
            Section {
                TextField("App ID", text: $adzunaAppID)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .accessibilityLabel("Adzuna app ID")
                SecureField("App key", text: $adzunaAppKey)
                    .accessibilityLabel("Adzuna app key")
            } header: {
                Eyebrow(text: "Adzuna keys")
            } footer: {
                Text("Free at developer.adzuna.com. Also powers salary estimates on job pages.")
            }
        }
        if enabled.contains("usajobs") {
            Section {
                TextField("Registered email", text: $usajobsEmail)
                    .keyboardType(.emailAddress)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .accessibilityLabel("USAJobs registered email")
                SecureField("API key", text: $usajobsKey)
                    .accessibilityLabel("USAJobs API key")
            } header: {
                Eyebrow(text: "USAJobs keys")
            } footer: {
                Text("Free at developer.usajobs.gov.")
            }
        }
        Section {
            SecureField("BLS registration key", text: $blsKey)
                .accessibilityLabel("BLS registration key")
        } header: {
            Eyebrow(text: "Salary estimates (optional)")
        } footer: {
            Text("Fallback wage data for salary estimates when Adzuna has none. Free at data.bls.gov/registrationEngine.")
        }
    }
}
