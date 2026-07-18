import XCTest
import GRDB
@testable import JobsmithKit

/// The iOS settings-sync mapper (SettingsSync) and its config-backed engine
/// bridge — the Swift twin of tests/test_sync_settings.py. Pure-dict mapper tests
/// plus a GRDB engine round-trip over two in-memory config stores.
final class SettingsSyncTests: XCTestCase {

    // MARK: mapper (pure)

    func testExportGatedByCategoryFoldsAndExpands() {
        let config: [String: JSONValue] = [
            "honesty": .object(["level": .string("tailored"), "resumeStyle": .string("modern")]),
            "search": .object([
                "keywords": .array([.string("swift")]),
                "enabledSources": .array([.string("greenhouse")]),
                "linkedInEnabled": .bool(true),
            ]),
            "ai": .object(["apiKey": .string("sk"), "strongModel": .string("big"),
                           "fastModel": .string("apple-on-device")]),
            "promptOverrides": .object(["score": .string("P")]),
        ]
        let out = SettingsSync.export(config, enabled: ["documents", "ai_connection", "prompts"])

        // documents + ai_connection + prompts on; postings OFF.
        XCTAssertEqual(out["application_honesty.resume_style"]?["value"], .string("ledger"))  // alias
        XCTAssertEqual(out["application_honesty.honesty_level"]?["value"], .string("tailored"))
        XCTAssertEqual(out["ai.api_key"]?["value"], .string("sk"))
        XCTAssertEqual(out["ai.models.strong"]?["value"], .string("big"))
        XCTAssertNil(out["ai.models.fast"])                     // on-device sentinel skipped
        XCTAssertEqual(out["prompts.score"]?["value"], .string("P"))
        XCTAssertNil(out["search.keywords"])                    // postings OFF
    }

    func testEnabledSourcesFoldIsSortedAndLinkedInFolds() {
        let config: [String: JSONValue] = ["search": .object([
            "enabledSources": .array([.string("remoteok"), .string("greenhouse")]),
            "linkedInEnabled": .bool(true),
        ])]
        let folded = SettingsSync.foldEnabledSources(config)
        XCTAssertEqual(folded, ["greenhouse", "linkedin", "remoteok"])  // sorted, linkedin folded in

        var dest: [String: JSONValue] = [:]
        SettingsSync.apply(&dest, path: "search.enabled_sources",
                           value: .array(folded.map { .string($0) }))
        // linkedin lives on the flag, not in enabledSources.
        XCTAssertEqual(SettingsSync.foldEnabledSources(dest), folded)
        if case .array(let arr)? = (dest["search"]?.objectValue?["enabledSources"]) {
            XCTAssertFalse(arr.contains(.string("linkedin")))
        } else { XCTFail("enabledSources missing") }
        XCTAssertEqual(dest["search"]?.objectValue?["linkedInEnabled"], .bool(true))
    }

    func testApplyRejectsNonRegistryPaths() {
        var cfg: [String: JSONValue] = [:]
        SettingsSync.apply(&cfg, path: "apiKeys.adzunaAppKey", value: .string("SECRET"))
        SettingsSync.apply(&cfg, path: "totally.unknown", value: .string("x"))
        XCTAssertTrue(cfg.isEmpty)
    }

    func testUnmodeledPathIsNotModeled() {
        XCTAssertFalse(SettingsSync.isModeled("pipeline.ghost_after_days"))  // ios == nil
        XCTAssertTrue(SettingsSync.isModeled("application_honesty.resume_style"))
        XCTAssertTrue(SettingsSync.isModeled("prompts.anything"))
    }

    func testCanonicalIDsAreSortedAndUnique() {
        let ids = SettingsSync.canonicalIDs()
        XCTAssertEqual(ids, ids.sorted())
        XCTAssertEqual(Set(ids).count, ids.count)
        XCTAssertTrue(ids.contains("search.enabled_sources"))
        XCTAssertTrue(ids.contains("prompts.*"))
        // The two new inbox settings (parity with settings_registry.py).
        XCTAssertTrue(ids.contains("inbox.require_stated_pay"))
        XCTAssertTrue(ids.contains("inbox.sort"))
    }

    // MARK: inbox category + settings

    func testInboxCategoryIsRegisteredDefaultOn() {
        let cat = SettingsSync.categories.first { $0.key == "inbox" }
        XCTAssertEqual(cat?.label, "Inbox")
        XCTAssertEqual(cat?.defaultOn, true)   // default ON preserves today's behavior
        XCTAssertEqual(SettingsSync.category(for: "inbox.require_stated_pay"), "inbox")
        XCTAssertEqual(SettingsSync.category(for: "inbox.sort"), "inbox")
    }

    func testInboxSettingsExportAndApplyRoundTrip() {
        let config: [String: JSONValue] = ["search": .object([
            "requireStatedPay": .bool(true),
            "inboxSort": .string("salary"),
        ])]
        let out = SettingsSync.export(config, enabled: ["inbox"])
        XCTAssertEqual(out["inbox.require_stated_pay"]?["value"], .bool(true))
        XCTAssertEqual(out["inbox.sort"]?["value"], .string("salary"))

        var dest: [String: JSONValue] = [:]
        SettingsSync.apply(&dest, path: "inbox.require_stated_pay", value: .bool(true))
        SettingsSync.apply(&dest, path: "inbox.sort", value: .string("newest"))
        XCTAssertEqual(dest["search"]?.objectValue?["requireStatedPay"], .bool(true))
        XCTAssertEqual(dest["search"]?.objectValue?["inboxSort"], .string("newest"))
    }

    /// The `inbox` category gates its two paths: with it excluded, neither is
    /// emitted even though the values are present in the config.
    func testInboxCategoryGatesExport() {
        let config: [String: JSONValue] = ["search": .object([
            "requireStatedPay": .bool(true),
            "inboxSort": .string("salary"),
        ])]
        let out = SettingsSync.export(config, enabled: ["documents", "postings"])  // inbox OFF
        XCTAssertNil(out["inbox.require_stated_pay"])
        XCTAssertNil(out["inbox.sort"])
    }

    /// An out-of-vocabulary sort value is ignored on apply — the existing value
    /// stands (desktop ENUM-normalization parity).
    func testInboxSortRejectsUnknownEnumValue() {
        var dest: [String: JSONValue] = ["search": .object(["inboxSort": .string("best_match")])]
        SettingsSync.apply(&dest, path: "inbox.sort", value: .string("bogus"))
        XCTAssertEqual(dest["search"]?.objectValue?["inboxSort"], .string("best_match"))
        // A valid one does write.
        SettingsSync.apply(&dest, path: "inbox.sort", value: .string("company"))
        XCTAssertEqual(dest["search"]?.objectValue?["inboxSort"], .string("company"))
    }

    // MARK: engine round-trip (config-backed)

    final class Clock: @unchecked Sendable {
        var t = Date(timeIntervalSince1970: 1_775_000_000)
        func now() -> Date { t += 1; return t }
    }

    /// A file-like config store: load() hands out the current dict, save()
    /// replaces it — matching how ConfigStore persists between calls.
    final class ConfigBox: @unchecked Sendable {
        var cfg: [String: JSONValue]
        init(_ cfg: [String: JSONValue]) { self.cfg = cfg }
    }

    private func engine(_ db: AppDatabase, _ device: String, _ box: ConfigBox,
                        enabled: Set<String>, clock: Clock) -> SyncEngine {
        SyncEngine(db: db, deviceId: device,
                   loadSettings: { box.cfg },
                   saveSettings: { box.cfg = $0 },
                   settingsEnabled: enabled, now: clock.now)
    }

    func testPerKeyLWWAcrossDevices() throws {
        let clock = Clock()
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent("settingsync-\(UUID().uuidString)")

        let boxA = ConfigBox(["honesty": .object(["resumeStyle": .string("ledger"),
                                                  "level": .string("honest")])])
        let boxB = ConfigBox([:])
        let dbA = try AppDatabase.inMemory(), dbB = try AppDatabase.inMemory()
        let a = engine(dbA, "A1B2", boxA, enabled: ["documents"], clock: clock)
        let b = engine(dbB, "C3D4", boxB, enabled: ["documents"], clock: clock)

        try a.export(to: folder)
        let imp = try b.importChanges(from: folder)
        XCTAssertGreaterThan(imp.settingsUpdated, 0)
        XCTAssertEqual(boxB.cfg["honesty"]?.objectValue?["resumeStyle"], .string("ledger"))
        XCTAssertEqual(boxB.cfg["honesty"]?.objectValue?["level"], .string("honest"))

        // B changes only resumeStyle; A changes only level. Per-key LWW keeps both.
        SettingsSync.apply(&boxB.cfg, path: "application_honesty.resume_style", value: .string("swiss"))
        SettingsSync.apply(&boxA.cfg, path: "application_honesty.honesty_level", value: .string("tailored"))
        try b.export(to: folder)
        try a.export(to: folder)
        try a.importChanges(from: folder)
        try b.importChanges(from: folder)

        for box in [boxA, boxB] {
            XCTAssertEqual(box.cfg["honesty"]?.objectValue?["resumeStyle"], .string("swiss"))
            XCTAssertEqual(box.cfg["honesty"]?.objectValue?["level"], .string("tailored"))
        }
    }

    /// Base-overlay: a canonical key iOS doesn't model (pipeline.ghost_after_days)
    /// survives a round-trip through the phone untouched — the phone applies
    /// nothing and, crucially, never tombstones it.
    func testUnmodeledSettingSurvivesThePhoneUntouched() throws {
        let clock = Clock()
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent("settingsync-\(UUID().uuidString)")
        let changes = folder.appendingPathComponent("changes")
        try FileManager.default.createDirectory(at: changes, withIntermediateDirectories: true)
        // A desktop peer's pipeline setting (a category iOS doesn't model).
        let rec = "{\"v\":1,\"entity\":\"setting\",\"id\":\"pipeline.ghost_after_days\","
            + "\"updated_at\":\"2026-07-08T10:00:00.000Z\",\"device\":\"DESK\",\"deleted\":false,"
            + "\"data\":{\"value\":30}}\n"
        try rec.write(to: changes.appendingPathComponent("DESK.jsonl"),
                      atomically: true, encoding: .utf8)

        let box = ConfigBox([:])
        let db = try AppDatabase.inMemory()
        let phone = engine(db, "C3D4", box, enabled: ["pipeline", "documents"], clock: clock)

        try phone.importChanges(from: folder)   // must not crash; nothing to write
        XCTAssertNil(box.cfg["pipeline"])        // iOS doesn't model it

        // The phone must NOT broadcast a tombstone for the unmodeled key.
        let outFolder = folder.appendingPathComponent("out")
        try phone.export(to: outFolder)
        let log = outFolder.appendingPathComponent("changes/C3D4.jsonl")
        let text = (try? String(contentsOf: log, encoding: .utf8)) ?? ""
        XCTAssertFalse(text.contains("pipeline.ghost_after_days"),
                       "phone tombstoned a setting it doesn't model")
    }

    /// A predates-the-entity client must skip records it has no handler for.
    func testUnknownEntityIsSkipped() throws {
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent("settingsync-\(UUID().uuidString)")
        let changes = folder.appendingPathComponent("changes")
        try FileManager.default.createDirectory(at: changes, withIntermediateDirectories: true)
        let rec = "{\"v\":1,\"entity\":\"future_entity\",\"id\":\"x\","
            + "\"updated_at\":\"2099-01-01T00:00:00.000Z\",\"device\":\"ZZ\",\"deleted\":false,"
            + "\"data\":{\"k\":1}}\n"
        try rec.write(to: changes.appendingPathComponent("ZZ.jsonl"), atomically: true, encoding: .utf8)

        let db = try AppDatabase.inMemory()
        let box = ConfigBox([:])
        let e = engine(db, "C3D4", box, enabled: ["documents"], clock: Clock())
        let imp = try e.importChanges(from: folder)  // must not throw
        XCTAssertEqual(imp.upserts, 0)
        XCTAssertEqual(imp.settingsUpdated, 0)
    }

    func testReexportAfterImportIsNoOp() throws {
        let clock = Clock()
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent("settingsync-\(UUID().uuidString)")
        let boxA = ConfigBox([
            "honesty": .object(["resumeStyle": .string("swiss")]),
            "search": .object(["keywords": .array([.string("go")]),
                               "enabledSources": .array([.string("greenhouse")]),
                               "linkedInEnabled": .bool(false)]),
            "promptOverrides": .object(["p": .string("v")]),
        ])
        let boxB = ConfigBox([:])
        let dbA = try AppDatabase.inMemory(), dbB = try AppDatabase.inMemory()
        let enabled: Set<String> = ["documents", "postings", "prompts"]
        let a = engine(dbA, "A1B2", boxA, enabled: enabled, clock: clock)
        let b = engine(dbB, "C3D4", boxB, enabled: enabled, clock: clock)

        try a.export(to: folder)
        try b.importChanges(from: folder)
        let re = try b.export(to: folder.appendingPathComponent("empty"))
        XCTAssertEqual(re.total, 0, "a settled export right after import must be a no-op")
    }
}
