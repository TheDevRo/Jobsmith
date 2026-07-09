import XCTest
@testable import JobsmithKit

/// The app-level glue: Profile <-> dict bridge and device-id persistence.
final class SyncManagerTests: XCTestCase {

    private func sampleProfile() -> Profile {
        Profile(
            fullName: "Deven Rouse", email: "d@example.com", phone: "555-1212",
            location: "Denver, CO", summary: "iOS + backend engineer.",
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
        XCTAssertEqual(back.fullName, "Deven Rouse")
        XCTAssertEqual(back.email, "d@example.com")
        XCTAssertEqual(back.skills, ["Swift", "Python", "GRDB"])
        XCTAssertEqual(back.certifications, ["AWS SA"])
        XCTAssertEqual(back, p)
    }

    func testCanonicalHasSnakeCaseKeys() {
        let canonical = SyncEntities.profileIOSToCanonical(SyncManager.profileToDict(sampleProfile()))
        XCTAssertEqual(canonical["full_name"], .string("Deven Rouse"))
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
