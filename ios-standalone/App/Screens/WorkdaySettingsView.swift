import SwiftUI
import JobsmithKit

/// Workday ATS credentials + the remembered-tenants registry.
///
/// Workday requires a separate account per company tenant
/// (`{company}.wd{N}.myworkdayjobs.com`). One email/password pair drives one-tap
/// sign-in / create-account in the in-app Apply browser, and the registry below
/// remembers which tenants already have an account (synced across devices).
///
/// The email lives in the local config (never synced); the password is
/// round-tripped through the Keychain by `ConfigStore`. Following the
/// SearchSettingsView pattern, the text fields are local `@State` flushed on
/// disappear behind a `hasAppeared` guard — so an eagerly-created, never-shown
/// destination instance can't persist its pristine defaults over real values.
struct WorkdaySettingsView: View {
    @Environment(AppModel.self) private var model

    @State private var email = ""
    @State private var password = ""
    @State private var hasAppeared = false
    @State private var accounts: [AtsAccount] = []

    var body: some View {
        List {
            Section {
                TextField("Workday email", text: $email)
                    .keyboardType(.emailAddress)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .accessibilityLabel("Workday email")
                SecureField("Workday password", text: $password)
                    .accessibilityLabel("Workday password")
            } header: {
                Eyebrow(text: "Workday account")
            } footer: {
                Text("Used for one-tap sign-in and account creation on Workday job sites (…myworkdayjobs.com) in the in-app Apply browser. Stored in this device's Keychain and never synced.")
            }

            if !accounts.isEmpty {
                Section {
                    ForEach(accounts, id: \.tenantHost) { acct in
                        accountRow(acct)
                    }
                    .onDelete(perform: deleteAccounts)
                } header: {
                    Eyebrow(text: "Remembered tenants (\(accounts.count))")
                } footer: {
                    Text("Tenants where you already have a Workday account. Kept in sync across your devices. Swipe to forget one.")
                }
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle("Workday")
        .onAppear {
            if !hasAppeared {
                email = model.config.apiKeys.workdayEmail
                password = model.config.apiKeys.workdayPassword
                hasAppeared = true
            }
            reloadAccounts()
        }
        .onDisappear(perform: save)
    }

    @ViewBuilder
    private func accountRow(_ acct: AtsAccount) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(acct.tenantHost)
                    .font(.subheadline)
                    .lineLimit(1)
                if let email = acct.email, !email.isEmpty {
                    Text(email)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
            Spacer()
            if acct.isPending {
                Text("Verify email")
                    .font(.caption2)
                    .foregroundStyle(.orange)
            } else {
                Text("Active")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(acct.tenantHost), \(acct.isPending ? "awaiting email verification" : "active")")
    }

    private func reloadAccounts() {
        accounts = (try? AtsAccountStore(model.database).all()) ?? []
    }

    private func deleteAccounts(at offsets: IndexSet) {
        let store = AtsAccountStore(model.database)
        for i in offsets {
            try? store.delete(accounts[i].tenantHost)
        }
        reloadAccounts()
    }

    private func save() {
        guard hasAppeared else { return }
        let e = email, p = password
        model.saveConfig {
            $0.apiKeys.workdayEmail = e
            $0.apiKeys.workdayPassword = p
        }
    }
}
