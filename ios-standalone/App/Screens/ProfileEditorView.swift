import SwiftUI
import JobsmithKit

/// Edit the candidate profile — the single source of truth the AI draws
/// from. Resume-file import lands with the onboarding flow.
struct ProfileEditorView: View {
    @Environment(AppModel.self) private var model
    @State private var profile = Profile()
    @State private var skillsText = ""

    var body: some View {
        Form {
            Section {
                TextField("Full name", text: $profile.fullName)
                    .textContentType(.name)
                TextField("Email", text: $profile.email)
                    .textContentType(.emailAddress)
                    .keyboardType(.emailAddress)
                    .textInputAutocapitalization(.never)
                TextField("Phone", text: $profile.phone)
                    .textContentType(.telephoneNumber)
                    .keyboardType(.phonePad)
                TextField("Location (City, ST)", text: $profile.location)
                TextField("LinkedIn URL", text: $profile.linkedin)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
            } header: {
                Eyebrow(text: "Identity")
            }

            Section {
                TextField("Street address", text: $profile.streetAddress)
                    .textContentType(.streetAddressLine1)
                TextField("City", text: $profile.city)
                    .textContentType(.addressCity)
                TextField("State", text: $profile.state)
                    .textContentType(.addressState)
                TextField("ZIP code", text: $profile.zipCode)
                    .textContentType(.postalCode)
                    .keyboardType(.numbersAndPunctuation)
            } header: {
                Eyebrow(text: "Address")
            } footer: {
                Text("Some applications ask for a full mailing address — Apply Assist fills these automatically.")
            }

            Section {
                TextField("Professional summary", text: $profile.summary, axis: .vertical)
                    .lineLimit(4...10)
            } header: {
                Eyebrow(text: "Summary")
            }

            Section {
                TextField("Python, Docker, AWS", text: $skillsText, axis: .vertical)
                    .lineLimit(2...6)
                    .textInputAutocapitalization(.never)
            } header: {
                Eyebrow(text: "Skills (comma-separated)")
            }

            Section {
                ForEach($profile.experience) { $exp in
                    NavigationLink {
                        ExperienceEditorView(experience: $exp)
                    } label: {
                        VStack(alignment: .leading, spacing: 2) {
                            HStack {
                                Text(exp.title.isEmpty ? "New role" : exp.title)
                                    .font(.callout.weight(.medium))
                                if exp.pinned {
                                    Image(systemName: "pin.fill")
                                        .font(.caption2)
                                        .foregroundStyle(Theme.ember)
                                }
                            }
                            Text("\(exp.company) · \(exp.startDate) – \(exp.endDate)")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
                .onDelete { profile.experience.remove(atOffsets: $0) }
                Button {
                    profile.experience.append(WorkExperience())
                } label: {
                    Label("Add role", systemImage: "plus")
                }
            } header: {
                Eyebrow(text: "Experience")
            } footer: {
                Text("Pin a role to force-include it on every tailored resume.")
            }

            Section {
                ForEach($profile.education) { $edu in
                    VStack(spacing: 6) {
                        TextField("Degree", text: $edu.degree)
                        TextField("School", text: $edu.school)
                        TextField("Year", text: $edu.year)
                            .keyboardType(.numberPad)
                    }
                }
                .onDelete { profile.education.remove(atOffsets: $0) }
                Button {
                    profile.education.append(Education())
                } label: {
                    Label("Add education", systemImage: "plus")
                }
            } header: {
                Eyebrow(text: "Education")
            }

            Section {
                TextField("Desired salary", text: $profile.desiredSalary)
                TextField("Work authorization (Yes/No)", text: $profile.workAuthorization)
                TextField("Sponsorship required (Yes/No)", text: $profile.sponsorshipRequired)
            } header: {
                Eyebrow(text: "Application answers")
            } footer: {
                Text("Used to autofill standard ATS questions. The answer bank in Apply Assist learns the rest.")
            }
        }
        .navigationTitle("Profile")
        .onAppear {
            // Only hydrate from config on the first appearance. Returning
            // from the per-role editor (a NavigationLink push/pop) fires
            // onAppear again — re-reading config here would clobber live
            // in-memory edits made in the child, like a freshly toggled pin,
            // which aren't persisted until our onDisappear below.
            if profile.isEmpty {
                profile = model.config.profile
                skillsText = profile.skills.joined(separator: ", ")
            }
        }
        .onChange(of: model.config.profile) { _, newValue in
            // The config can land after we appeared (async load at launch,
            // an import finishing). Adopt it only while our snapshot is
            // still empty — never stomp live edits, and never let the
            // save-on-disappear below write an empty snapshot over a
            // freshly imported profile.
            if profile.isEmpty {
                profile = newValue
                skillsText = newValue.skills.joined(separator: ", ")
            }
        }
        .onDisappear {
            var updated = profile
            updated.skills = skillsText.split(separator: ",")
                .map { $0.trimmingCharacters(in: .whitespaces) }
                .filter { !$0.isEmpty }
            let final = updated
            model.saveConfig { $0.profile = final }
        }
    }
}

struct ExperienceEditorView: View {
    @Binding var experience: WorkExperience
    @State private var bulletsText = ""

    var body: some View {
        Form {
            Section {
                TextField("Title", text: $experience.title)
                TextField("Company", text: $experience.company)
                TextField("Start (e.g. Jan 2022)", text: $experience.startDate)
                TextField("End (or Present)", text: $experience.endDate)
                Toggle(isOn: $experience.pinned) {
                    Label("Pin to every resume", systemImage: "pin")
                }
                .tint(Theme.ember)
            } header: {
                Eyebrow(text: "Role")
            }
            Section {
                TextEditor(text: $bulletsText)
                    .frame(minHeight: 140)
                    .font(.callout)
            } header: {
                Eyebrow(text: "Bullets (one per line)")
            }
        }
        .navigationTitle(experience.title.isEmpty ? "Role" : experience.title)
        .onAppear { bulletsText = experience.bullets.joined(separator: "\n") }
        .onDisappear {
            experience.bullets = bulletsText.split(separator: "\n")
                .map { $0.trimmingCharacters(in: .whitespaces) }
                .filter { !$0.isEmpty }
        }
    }
}
