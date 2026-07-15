import XCTest
@testable import JobsmithKit

/// The app-level glue: Profile <-> dict bridge and device-id persistence.
final class SyncManagerTests: XCTestCase {

    private func sampleProfile() -> Profile {
        Profile(
            fullName: "Alex Kim", middleName: "J", email: "d@example.com",
            phone: "555-1212", location: "Denver, CO", streetAddress2: "Unit 3",
            gender: "Male", raceEthnicity: "Asian", veteranStatus: "Not a veteran",
            disabilityStatus: "No",
            summary: "iOS + backend engineer.",
            skills: ["Swift", "Python", "GRDB"],
            certifications: ["AWS SA"]
        )
    }

    func testProfileDictRoundTrip() {
        let p = sampleProfile()
        let back = SyncManager.dictToProfile(SyncManager.profileToDict(p))
        XCTAssertEqual(back, p)
    }

    func testProfileSurvivesCanonicalMapping() {
        let p = sampleProfile()
        let iosDict = SyncManager.profileToDict(p)
        // iOS -> canonical (what travels) -> iOS (base-overlaid) -> Profile.
        let canonical = SyncEntities.profileIOSToCanonical(iosDict)
        let backDict = iosDict.merging(SyncEntities.profileCanonicalToIOS(canonical)) { _, new in new }
        let back = SyncManager.dictToProfile(backDict)
        XCTAssertEqual(back.fullName, "Alex Kim")
        XCTAssertEqual(back.email, "d@example.com")
        XCTAssertEqual(back.skills, ["Swift", "Python", "GRDB"])
        XCTAssertEqual(back.certifications, ["AWS SA"])
        // The newly-parity'd fields (middle name, address line 2, EEO block)
        // must survive the canonical round-trip too.
        XCTAssertEqual(back.middleName, "J")
        XCTAssertEqual(back.streetAddress2, "Unit 3")
        XCTAssertEqual(back.gender, "Male")
        XCTAssertEqual(back.raceEthnicity, "Asian")
        XCTAssertEqual(back.veteranStatus, "Not a veteran")
        XCTAssertEqual(back.disabilityStatus, "No")
        XCTAssertEqual(back, p)
    }

    func testCanonicalHasSnakeCaseKeys() {
        let canonical = SyncEntities.profileIOSToCanonical(SyncManager.profileToDict(sampleProfile()))
        XCTAssertEqual(canonical["full_name"], .string("Alex Kim"))
        XCTAssertNil(canonical["fullName"])  // mapped, not passed through
    }

    func testDeviceIdIsStableAndPersisted() {
        let defaults = UserDefaults(suiteName: "test-\(UUID().uuidString)")!
        let mgr = SyncManager()
        let id1 = mgr.deviceId(defaults)
        XCTAssertEqual(id1.count, 8)
        XCTAssertEqual(mgr.deviceId(defaults), id1)  // stable
        XCTAssertEqual(defaults.string(forKey: "jobsmith.sync.deviceId"), id1)  // persisted
    }
}
