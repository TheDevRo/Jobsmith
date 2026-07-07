import Foundation
import JobsmithKit

/// Canned AI responses for UI tests and demo runs (-UseMockAI). Keys match
/// distinctive phrases in the prompt templates.
extension MockAIEngine {
    static func standardFixtures() -> MockAIEngine {
        let mock = MockAIEngine()

        mock.register("Evaluate how well this candidate's existing experience fits", .text("""
        {"score": 82, "reasoning": "Strong overlap on core backend skills; missing Kubernetes depth.",
         "matched_skills": ["Python", "Docker"], "missing_skills": ["Kubernetes"],
         "matched_soft_skills": ["Communication"], "missing_soft_skills": [],
         "title_alignment": "strong", "keywords": ["python", "microservices"]}
        """))

        mock.register("Tailor the candidate's resume", .text("""
        SUMMARY
        Backend engineer with 8 years building high-throughput services.

        SKILLS
        Python, Docker, AWS, PostgreSQL

        EXPERIENCE
        Title: Senior Software Engineer
        Company: Acme Corp
        Dates: Jan 2022 - Present
        - Built microservices handling 10k requests/sec
        - Led migration to containerized deployments
        - Mentored four junior engineers

        EDUCATION
        Degree: B.S. Computer Science
        School: State University
        Year: 2019
        """))

        mock.register("Write a tailored cover letter", .text("""
        Dear Hiring Team,

        I am excited to apply for this role. My background building backend services maps directly to your needs.

        Sincerely,
        """))

        mock.register("SCOPED EDIT", .text("""
        SUMMARY
        Backend engineer with 8 years building high-throughput services and Kubernetes platforms.

        SKILLS
        Python, Docker, Kubernetes, AWS

        EXPERIENCE
        Title: Senior Software Engineer
        Company: Acme Corp
        Dates: Jan 2022 - Present
        - Built microservices handling 10k requests/sec
        - Ran production Kubernetes clusters
        - Mentored four junior engineers
        """))

        mock.register("exactly two arrays", .text("""
        {"resume_changes": [], "cover_letter_changes": []}
        """))

        mock.register("résumé parser", .text("""
        {"full_name": "Test User", "email": "test@example.com", "phone": "555-0100",
         "location": "Denver, CO", "summary": "Engineer.", "skills": ["Python"],
         "experience": [{"title": "Engineer", "company": "Acme", "start_date": "2020",
                         "end_date": "Present", "bullets": ["Did things"]}],
         "education": [], "certifications": []}
        """))

        mock.register("FORM FIELDS TO MAP", .text("[]"))
        mock.setModels(["mock-model"])
        return mock
    }
}
