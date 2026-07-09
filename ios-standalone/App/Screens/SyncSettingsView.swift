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
