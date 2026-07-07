import SwiftUI

/// First-run screen (and the "change server" sheet later): point the app at
/// the self-hosted Jobsmith backend, verify it's really there, save it.
struct ServerSetupView: View {
    let isFirstRun: Bool

    @EnvironmentObject private var config: ServerConfig
    @Environment(\.dismiss) private var dismiss

    @State private var address: String = ""
    @State private var testing = false
    @State private var errorMessage: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("192.168.1.100:8888", text: $address)
                        .keyboardType(.URL)
                        .textContentType(.URL)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                        .onSubmit { connect() }
                } header: {
                    Text("Server address")
                } footer: {
                    Text("The address of the machine running Jobsmith — the desktop app, Docker, or `start_server.sh`. Port 8888 is assumed if you leave it off.")
                }

                if let errorMessage {
                    Section {
                        Label(errorMessage, systemImage: "exclamationmark.triangle.fill")
                            .foregroundStyle(.red)
                            .font(.callout)
                    }
                }

                Section {
                    Button(action: connect) {
                        if testing {
                            HStack(spacing: 10) {
                                ProgressView()
                                Text("Connecting…")
                            }
                        } else {
                            Text("Connect")
                                .fontWeight(.semibold)
                        }
                    }
                    .disabled(testing || address.trimmingCharacters(in: .whitespaces).isEmpty)
                }

                if isFirstRun {
                    Section {
                        VStack(alignment: .leading, spacing: 8) {
                            Label("How it works", systemImage: "info.circle")
                                .font(.headline)
                            Text("Jobsmith runs as a self-hosted server on your Mac or homelab. This app connects to it over your network — fetching, scoring, tailoring, and Apply Assist all run on the server.")
                            Text("For Apply Assist on iPhone, enable the **Jobsmith Assist** extension in Settings → Apps → Safari → Extensions after installing this app.")
                        }
                        .font(.callout)
                        .foregroundStyle(.secondary)
                    }
                }
            }
            .navigationTitle(isFirstRun ? "Welcome to Jobsmith" : "Server")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                if !isFirstRun {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Cancel") { dismiss() }
                    }
                }
            }
            .onAppear {
                if address.isEmpty, let current = config.serverURL {
                    address = current.absoluteString
                }
            }
        }
        .interactiveDismissDisabled(testing)
    }

    private func connect() {
        guard let url = ServerConfig.normalize(address) else {
            errorMessage = "That doesn't look like a valid address. Try something like 192.168.1.100:8888."
            return
        }
        errorMessage = nil
        testing = true
        Task {
            let result = await ServerConfig.testConnection(to: url)
            testing = false
            switch result {
            case .success:
                config.setServer(url)
                if !isFirstRun { dismiss() }
            case .failure(let error):
                errorMessage = error.message
            }
        }
    }
}
