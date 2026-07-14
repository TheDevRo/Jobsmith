import SwiftUI
import UIKit
import UniformTypeIdentifiers
import JobsmithKit

/// Settings → Sync: enable two-way folder sync, pick the shared folder, and
/// trigger a sync. The engine lives in JobsmithKit (SyncManager); this is just
/// the control surface.
struct SyncSettingsView: View {
    @Environment(AppModel.self) private var model

    @State private var enabled = SyncManager.shared.isEnabled()
    @State private var folderName = SyncManager.shared.folderName()
    @State private var interval = SyncManager.shared.syncIntervalSeconds()
    @State private var showPicker = false
    @State private var syncing = false
    @State private var status = ""
    // One flag per sync category (jobsmith.sync.settings.<key>), seeded from the
    // registry defaults. Kept in @State so toggling re-renders; persisted to
    // UserDefaults via SyncManager.
    @State private var categoryOn: [String: Bool] = SyncSettingsView.readCategories()

    private static func readCategories() -> [String: Bool] {
        var out: [String: Bool] = [:]
        for c in SettingsSync.categories { out[c.key] = SyncManager.shared.settingsCategoryEnabled(c.key) }
        return out
    }

    var body: some View {
        Form {
            Section {
                Toggle("Enable sync", isOn: $enabled)
                    .onChange(of: enabled) { _, on in
                        SyncManager.shared.setEnabled(on)
                        if on { model.startAutoSync() } else { model.stopAutoSync() }
                    }
            } footer: {
                Text("Two-way sync with your other devices through a shared folder "
                     + "(iCloud Drive, Dropbox, a synced folder). No account, no server. "
                     + "Your ATS-login passwords are never written to the folder.")
            }

            Section("Shared folder") {
                Button {
                    showPicker = true
                } label: {
                    HStack {
                        Text("Choose folder…")
                        Spacer()
                        Text(folderName ?? "None").foregroundStyle(.secondary)
                    }
                }
                Text("Tip: pick the same folder in Files here and on your Mac (e.g. an "
                     + "iCloud Drive subfolder) so both devices read and write it.")
                    .font(.footnote).foregroundStyle(.secondary)
            }

            Section {
                Picker("Sync every", selection: $interval) {
                    Text("30 seconds").tag(30)
                    Text("1 minute").tag(60)
                    Text("5 minutes").tag(300)
                    Text("15 minutes").tag(900)
                    Text("Manual only").tag(0)
                }
                .onChange(of: interval) { _, secs in
                    SyncManager.shared.setSyncIntervalSeconds(secs)
                    model.startAutoSync()   // apply the new cadence immediately
                }
            } header: {
                Text("Automatic sync")
            } footer: {
                Text("How often to sync while the app is open. Sync also runs the "
                     + "moment you open the app. \"Manual only\" syncs just when you tap Sync now.")
            }

            Section {
                ForEach(SettingsSync.categories, id: \.key) { category in
                    categoryToggle(category)
                }
            } header: {
                Text("Sync these across devices")
            } footer: {
                Text("Pick which groups of settings ride the shared folder. A group only "
                     + "syncs when it's on here AND on your other device.\n\n"
                     + "AI Connection INCLUDES your AI API key — it travels in your sync folder "
                     + "with the endpoint and model choices, so this device works without "
                     + "re-entering it. Never synced: your LinkedIn cookie, ATS/Workday passwords, "
                     + "Adzuna/USAJobs/BLS keys, and the sync folder itself.")
            }

            Section {
                Button {
                    Task { await runSync() }
                } label: {
                    HStack {
                        Text("Sync now")
                        Spacer()
                        if syncing { ProgressView() }
                    }
                }
                .disabled(syncing || folderName == nil)

                if !status.isEmpty {
                    Text(status).font(.footnote).foregroundStyle(.secondary)
                }
            } footer: {
                Text("This device: \(SyncManager.shared.deviceId())")
            }
        }
        .navigationTitle("Sync")
        .onAppear {
            // A NavigationLink destination's @State is initialized eagerly at
            // list-render time, so re-read the live values when the screen shows.
            enabled = SyncManager.shared.isEnabled()
            folderName = SyncManager.shared.folderName()
            interval = SyncManager.shared.syncIntervalSeconds()
            categoryOn = SyncSettingsView.readCategories()
        }
        .fileImporter(isPresented: $showPicker, allowedContentTypes: [.folder]) { result in
            if case .success(let url) = result {
                do {
                    try SyncManager.shared.storeFolder(url)
                    folderName = SyncManager.shared.folderName()
                    status = ""
                } catch {
                    status = "Couldn't save folder: \(error.localizedDescription)"
                }
            }
        }
    }

    @ViewBuilder
    private func categoryToggle(_ category: SettingsSync.Category) -> some View {
        let binding = Binding<Bool>(
            get: { categoryOn[category.key] ?? category.defaultOn },
            set: { on in
                categoryOn[category.key] = on
                SyncManager.shared.setSettingsCategoryEnabled(category.key, on)
            }
        )
        Toggle(category.label, isOn: binding)
            .disabled(!enabled)   // a group can't sync while master sync is off
    }

    @MainActor
    private func runSync() async {
        syncing = true
        status = "Syncing…"
        do {
            let r = try await SyncManager.shared.syncNow(
                db: model.database,
                configStore: model.configStore,
                deviceLabel: UIDevice.current.name)
            status = "Received \(r.imported.upserts) update(s), \(r.imported.deletes) deletion(s); "
                + "sent \(r.exported.live + r.exported.tombstones) change(s)."
        } catch {
            status = "Sync failed: \(error.localizedDescription)"
        }
        syncing = false
    }
}
