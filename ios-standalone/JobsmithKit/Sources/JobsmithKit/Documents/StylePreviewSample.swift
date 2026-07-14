import Foundation

/// The sample resume behind the style picker.
///
/// Renders through the real generator — `build` then `DocxPDFRenderer` — so the
/// preview cannot drift from the file the user actually gets. Mirrors
/// `PREVIEW_PROFILE` / `PREVIEW_CONTENT` in backend/resume_generator.py; keep
/// the two in step so a style looks the same on both platforms.
public enum StylePreviewSample {

    /// Invented person, invented history. Never the user's own data: the
    /// preview is about the *look*, and real content reads as a document
    /// rather than as a specimen. Exercises every element a style can touch —
    /// an accent company name, right-aligned dates, bullets, each section type.
    public static let profile = Profile(
        fullName: "Morgan Reyes",
        email: "morgan.reyes@example.com",
        phone: "(503) 555-0142",
        location: "Portland, OR",
        linkedin: "linkedin.com/in/morganreyes"
    )

    public static let content = DocResumeContent(
        summary: "Data analyst who turns messy operational data into decisions leaders act on.",
        skillsText: "SQL, Python, dbt, Snowflake, Tableau",
        experiences: [
            DocExperience(
                title: "Senior Data Analyst",
                company: "Northwind Logistics",
                dates: "2021 - Present",
                bullets: [
                    "Cut freight spend 12% ($2.1M annually) across 14 distribution centers.",
                    "Built the demand model that now sets staffing for every West Coast hub.",
                ]
            ),
            DocExperience(
                title: "Data Analyst",
                company: "Cascade Retail Group",
                dates: "2018 - 2021",
                bullets: [
                    "Replaced a 40-hour manual close with a dbt pipeline that runs in nine minutes.",
                ]
            ),
        ],
        education: [
            DocEducation(degree: "B.S. Statistics", school: "Oregon State University", year: "2016"),
        ],
        certifications: ["Tableau Desktop Certified Professional"]
    )

    /// PDF bytes for one style/accent. Synchronous and local — no disk, no
    /// network — so the picker can redraw on every tap.
    public static func pdf(style: HonestyConfig.Style,
                           accent: HonestyConfig.ResumeAccent = .default) -> Data {
        let doc = ResumeDocxGenerator.build(
            content: content, profile: profile, style: style, accent: accent
        )
        return DocxPDFRenderer.render(doc)
    }
}
