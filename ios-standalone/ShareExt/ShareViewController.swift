import UIKit
import SwiftUI
import UniformTypeIdentifiers
import JobsmithKit

/// Share-sheet entry point: receives a job URL (or text containing one) from
/// Safari/LinkedIn/anywhere, parses the posting, and saves it to the shared
/// job store as source "manual" — the mobile-native Add Job by URL.
final class ShareViewController: UIViewController {
    override func viewDidLoad() {
        super.viewDidLoad()
        let host = UIHostingController(rootView: ShareJobView(
            extractURL: { [weak self] in await self?.extractSharedURL() },
            finish: { [weak self] in
                self?.extensionContext?.completeRequest(returningItems: nil)
            }
        ))
        addChild(host)
        view.addSubview(host.view)
        host.view.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            host.view.topAnchor.constraint(equalTo: view.topAnchor),
            host.view.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            host.view.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            host.view.trailingAnchor.constraint(equalTo: view.trailingAnchor),
        ])
        host.didMove(toParent: self)
    }

    private func extractSharedURL() async -> URL? {
        let providers = (extensionContext?.inputItems as? [NSExtensionItem])?
            .compactMap(\.attachments).flatMap { $0 } ?? []

        for provider in providers where provider.hasItemConformingToTypeIdentifier(UTType.url.identifier) {
            if let url = try? await provider.loadURL() { return url }
        }
        // LinkedIn's app shares plain text with the URL embedded.
        for provider in providers where provider.hasItemConformingToTypeIdentifier(UTType.plainText.identifier) {
            if let text = try? await provider.loadText(),
               let match = text.range(of: #"https?://\S+"#, options: .regularExpression) {
                return URL(string: String(text[match]))
            }
        }
        return nil
    }
}

private extension NSItemProvider {
    func loadURL() async throws -> URL? {
        try await withCheckedThrowingContinuation { continuation in
            loadItem(forTypeIdentifier: UTType.url.identifier) { item, error in
                if let error { continuation.resume(throwing: error) }
                else { continuation.resume(returning: item as? URL) }
            }
        }
    }

    func loadText() async throws -> String? {
        try await withCheckedThrowingContinuation { continuation in
            loadItem(forTypeIdentifier: UTType.plainText.identifier) { item, error in
                if let error { continuation.resume(throwing: error) }
                else { continuation.resume(returning: item as? String) }
            }
        }
    }
}

struct ShareJobView: View {
    let extractURL: () async -> URL?
    let finish: () -> Void

    enum Phase {
        case loading
        case preview(NormalizedJob)
        case saved(String)
        case failed(String)
    }

    @State private var phase: Phase = .loading
    @State private var saving = false

    var body: some View {
        NavigationStack {
            Group {
                switch phase {
                case .loading:
                    ProgressView("Reading job posting…")
                case .preview(let job):
                    preview(job)
                case .saved(let title):
                    ContentUnavailableView {
                        Label("Saved to Jobsmith", systemImage: "checkmark.circle.fill")
                    } description: {
                        Text(title)
                    }
                case .failed(let message):
                    ContentUnavailableView {
                        Label("Couldn't read that page", systemImage: "exclamationmark.triangle")
                    } description: {
                        Text(message)
                    }
                }
            }
            .navigationTitle("Save to Jobsmith")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Done") { finish() }
                }
            }
        }
        .task { await load() }
    }

    private func preview(_ job: NormalizedJob) -> some View {
        VStack(spacing: 18) {
            VStack(alignment: .leading, spacing: 8) {
                Text(job.title)
                    .font(.headline)
                Text(job.company.isEmpty ? job.url : job.company)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                if !job.description.isEmpty {
                    Text(job.description)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .lineLimit(5)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(16)
            .background(RoundedRectangle(cornerRadius: 14).fill(Color.primary.opacity(0.05)))

            Button {
                save(job)
            } label: {
                if saving {
                    ProgressView()
                        .frame(maxWidth: .infinity)
                } else {
                    Text("Save job")
                        .fontWeight(.semibold)
                        .frame(maxWidth: .infinity)
                }
            }
            .buttonStyle(.borderedProminent)
            .disabled(saving)

            Spacer()
        }
        .padding(20)
    }

    private func load() async {
        guard let url = await extractURL() else {
            phase = .failed("No job URL found in what was shared.")
            return
        }
        do {
            let job = try await ManualURLFetcher().fetchJob(from: url.absoluteString)
            phase = .preview(job)
        } catch {
            phase = .failed(error.localizedDescription)
        }
    }

    private func save(_ job: NormalizedJob) {
        saving = true
        do {
            let db = try AppDatabase.shared()
            try JobStore(db).upsert([job])
            ActivityStore(db).log("saved", "Shared in: \(job.title) at \(job.company)")
            phase = .saved("\(job.title) — \(job.company)")
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { finish() }
        } catch {
            saving = false
            phase = .failed("Could not save: \(error.localizedDescription)")
        }
    }
}
