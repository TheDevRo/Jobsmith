import SwiftUI
import QuickLook
import JobsmithKit

/// Review and edit the tailored resume and cover letter; AI Edit revises
/// with natural-language instructions; approve locks it for applying.
struct DocumentReviewView: View {
    @Environment(AppModel.self) private var model
    let jobId: String

    enum Doc: String, CaseIterable { case resume = "Resume", coverLetter = "Cover letter" }

    @State private var application: Application?
    @State private var job: Job?
    @State private var selected: Doc = .resume
    @State private var resumeText = ""
    @State private var coverText = ""
    @State private var showAIEdit = false
    @State private var revising = false
    @State private var previewURL: URL?

    var body: some View {
        Group {
            if application != nil {
                editor
            } else {
                ContentUnavailableView("Nothing to review yet", systemImage: "doc.text",
                                       description: Text("Tailor this job first to generate documents."))
            }
        }
        .navigationTitle("Review")
        .navigationBarTitleDisplayMode(.inline)
        .onAppear(perform: load)
    }

    private var editor: some View {
        VStack(spacing: 0) {
            Picker("Document", selection: $selected) {
                ForEach(Doc.allCases, id: \.self) { Text($0.rawValue).tag($0) }
            }
            .pickerStyle(.segmented)
            .padding(.horizontal, 16)
            .padding(.vertical, 10)

            TextEditor(text: selected == .resume ? $resumeText : $coverText)
                .font(.system(.footnote, design: .monospaced))
                .padding(.horizontal, 12)
                .scrollContentBackground(.hidden)
                .background(Color.primary.opacity(0.03))

            HStack(spacing: 10) {
                Button {
                    showAIEdit = true
                } label: {
                    Label("AI Edit", systemImage: "wand.and.stars")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
                .disabled(revising)

                Button {
                    preview()
                } label: {
                    Label("Preview", systemImage: "doc.richtext")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)

                Button {
                    approve()
                } label: {
                    Label("Approve", systemImage: "checkmark")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .tint(Theme.ember)
            }
            .padding(16)
        }
        .overlay {
            if revising {
                ProgressView("Revising…")
                    .padding(24)
                    .background(RoundedRectangle(cornerRadius: 14).fill(.thickMaterial))
            }
        }
        .sheet(isPresented: $showAIEdit) {
            AIEditSheet { instructions, honesty, tier in
                await revise(instructions: instructions, honesty: honesty, tier: tier)
            }
        }
        .quickLookPreview($previewURL)
        .onDisappear(perform: saveEdits)
    }

    private func load() {
        job = try? model.jobStore.job(id: jobId)
        application = try? model.applicationStore.application(jobId: jobId)
        resumeText = application?.resumeContent ?? ""
        coverText = application?.coverLetterContent ?? ""
    }

    private func saveEdits() {
        guard let application else { return }
        try? model.applicationStore.updateContent(id: application.id,
                                                  resume: resumeText, coverLetter: coverText)
    }

    private func preview() {
        guard let application, let job else { return }
        saveEdits()
        var updated = application
        updated.resumeContent = resumeText
        updated.coverLetterContent = coverText
        try? model.regenerateDocuments(for: updated, job: job)
        let kind: FileVault.Kind = selected == .resume ? .resume : .coverLetter
        previewURL = FileVault.url(jobId: job.id, kind: kind,
                                   format: model.config.honesty.documentFormat)
    }

    private func approve() {
        guard let application, let job else { return }
        saveEdits()
        var updated = application
        updated.resumeContent = resumeText
        updated.coverLetterContent = coverText
        try? model.regenerateDocuments(for: updated, job: job)
        try? model.applicationStore.updateStatus(id: application.id, status: "approved")
        model.activityStore.log("approved", "Documents approved for \(job.title)", jobId: job.id)
        model.refresh()
    }

    private func revise(instructions: String, honesty: HonestyConfig.Level, tier: ModelTier) async {
        guard let job else { return }
        revising = true
        defer { revising = false }
        do {
            if selected == .resume {
                resumeText = try await TailoringService.reviseResume(
                    currentResume: resumeText, instructions: instructions, job: job,
                    profile: model.config.profile, config: model.config,
                    engine: model.aiEngine, tier: tier, honestyLevel: honesty)
            } else {
                coverText = try await TailoringService.reviseCoverLetter(
                    currentLetter: coverText, instructions: instructions, job: job,
                    profile: model.config.profile, config: model.config,
                    engine: model.aiEngine, tier: tier, honestyLevel: honesty)
            }
            saveEdits()
        } catch {
            model.lastError = "AI Edit failed: \(error.localizedDescription)"
        }
    }
}

struct AIEditSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    let onSubmit: (String, HonestyConfig.Level, ModelTier) async -> Void

    @State private var instructions = ""
    @State private var honesty: HonestyConfig.Level = .honest
    @State private var tier: ModelTier = .strong

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("e.g. Emphasize my Kubernetes experience and shorten the summary",
                              text: $instructions, axis: .vertical)
                        .lineLimit(3...8)
                } header: {
                    Eyebrow(text: "What should change?")
                }
                Section {
                    Picker("Honesty", selection: $honesty) {
                        ForEach(HonestyConfig.Level.allCases, id: \.self) {
                            Text($0.rawValue.capitalized).tag($0)
                        }
                    }
                    Picker("Model", selection: $tier) {
                        Text("Strong").tag(ModelTier.strong)
                        Text("Fast").tag(ModelTier.fast)
                    }
                } header: {
                    Eyebrow(text: "This edit only")
                }
            }
            .navigationTitle("AI Edit")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Revise") {
                        let text = instructions
                        let level = honesty
                        let modelTier = tier
                        dismiss()
                        Task { await onSubmit(text, level, modelTier) }
                    }
                    .disabled(instructions.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
        }
        .onAppear { honesty = model.config.honesty.level; tier = model.config.honesty.aiEditTier }
        .presentationDetents([.medium])
    }
}
