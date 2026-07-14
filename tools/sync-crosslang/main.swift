import Foundation

// Cross-language verification tool. Compiled on the host together with the real
// JobsmithKit sync sources (JSONValue, SyncMerge, SyncEntities) so it exercises
// the actual iOS mapping + merge code — not a reimplementation.
//
//   sync-crosslang emit  <folder>   write an iOS-origin changes/IOSDEV.jsonl
//   sync-crosslang merge <folder>   print the merged state as canonical JSON
//
// The Python cross-language test (tests/test_sync_crosslang.py) drives both:
// it imports the emitted log with the desktop engine, and compares Swift's
// merge of a Python-produced folder against the Python oracle.

func die(_ msg: String) -> Never { FileHandle.standardError.write(Data((msg + "\n").utf8)); exit(2) }

guard CommandLine.arguments.count == 3 else { die("usage: sync-crosslang <emit|merge> <folder>") }
let command = CommandLine.arguments[1]
let folder = URL(fileURLWithPath: CommandLine.arguments[2])

func record(_ entity: String, _ id: String, _ ts: String, _ data: [String: JSONValue]) -> ChangeRecord {
    ChangeRecord(entity: entity, id: id, updatedAt: ts, device: "IOSDEV", deleted: false, data: data)
}

switch command {
case "emit":
    // iOS-native (camelCase) rows, mapped to canonical via the real mappers.
    let jobIOS: [String: JSONValue] = [
        "source": .string("greenhouse"), "externalId": .string("777"),
        "title": .string("iOS Engineer"), "company": .string("Acme"),
        "status": .string("discovered"), "dateDiscovered": .string("2026-07-08T09:00:00Z"),
        "fitScore": .double(91.0), "isRemote": .bool(true),
        "tags": .array([.string("swift")]), "triage": .string("shortlisted"),
    ]
    let appIOS: [String: JSONValue] = [
        "resumeContent": .string("R"), "coverLetterContent": .string("C"),
        "status": .string("approved"), "createdAt": .string("2026-07-08T09:30:00Z"),
        "honestyLevel": .string("honest"), "stylePreset": .string("modern"),
        "customAnswers": .object(["why": .string("mission")]),
    ]
    // A secret must be stripped by the mapper even though the iOS dict carries it.
    let profileIOS: [String: JSONValue] = [
        "fullName": .string("Alex Kim"), "email": .string("d@example.com"),
        "summary": .string("iOS developer"), "skills": .array([.string("Swift")]),
        "workday_password": .string("SECRET-should-never-sync"),
    ]

    var appCanon = SyncEntities.appIOSToCanonical(appIOS)
    appCanon["job_ref"] = .string("greenhouse:777")

    // The lifecycle decision travels as its own `triage` entity now (folded from
    // the iOS triage+status pair), separate from the job facts record.
    let records = [
        record("job", "greenhouse:777", "2026-07-08T10:00:00.000Z",
               SyncEntities.jobIOSToCanonical(jobIOS)),
        record("triage", "greenhouse:777", "2026-07-08T10:00:00.500Z",
               SyncEntities.triageIOSToCanonical(triage: "shortlisted", status: "discovered")),
        record("application", "app-ios-1", "2026-07-08T10:00:01.000Z", appCanon),
        record("profile", "me", "2026-07-08T10:00:02.000Z",
               SyncEntities.profileIOSToCanonical(profileIOS)),
    ]

    let changes = folder.appendingPathComponent("changes")
    try! FileManager.default.createDirectory(at: changes, withIntermediateDirectories: true)
    let encoder = JSONEncoder()
    var text = ""
    for rec in records {
        let data = try! encoder.encode(rec)
        text += String(data: data, encoding: .utf8)! + "\n"
    }
    try! text.write(to: changes.appendingPathComponent("IOSDEV.jsonl"), atomically: true, encoding: .utf8)

case "merge":
    let merged = SyncMerge.merge(SyncMerge.loadLogs(folder))
    print(merged.canonicalString())

case "registry":
    // Canonical-id parity: the Python test asserts this equals
    // settings_registry.syncable_canonical_ids(). `folder` is ignored.
    let data = try! JSONEncoder().encode(SettingsSync.canonicalIDs())
    print(String(data: data, encoding: .utf8)!)

case "emit-settings":
    // Emit `setting` records produced by the REAL iOS mapper from an iOS-native
    // config dict — exercising enabledSources fold, prompt expansion, the
    // on-device-sentinel skip, and secret-strip-by-omission (a folder-strip
    // secret like adzuna_app_key is not a registry row, so it never appears).
    let iosConfig: [String: JSONValue] = [
        "honesty": .object([
            "level": .string("tailored"),
            "resumeStyle": .string("modern"),          // legacy alias -> ledger
        ]),
        "search": .object([
            "keywords": .array([.string("swift"), .string("ios")]),
            "minSalary": .null,                          // explicit null travels
            "enabledSources": .array([.string("greenhouse"), .string("remoteok")]),
            "linkedInEnabled": .bool(true),              // folds in as "linkedin"
        ]),
        "ai": .object([
            "baseURL": .string("http://lan:1234/v1"),
            "apiKey": .string("sk-shared"),              // synced by decision
            "strongModel": .string("big-model"),
            "fastModel": .string("apple-on-device"),     // sentinel -> skipped
        ]),
        "promptOverrides": .object(["score": .string("my score prompt")]),
        "apiKeys": .object(["adzunaAppKey": .string("SECRET-must-not-sync")]),
    ]
    let enabled: Set<String> = ["documents", "postings", "ai_connection", "prompts"]
    let exported = SettingsSync.export(iosConfig, enabled: enabled)
    var recs: [ChangeRecord] = []
    var i = 0
    for (path, data) in exported.sorted(by: { $0.key < $1.key }) {
        i += 1
        let ts = String(format: "2026-07-08T11:00:%02d.000Z", i)
        recs.append(record("setting", path, ts, data))
    }
    let changes = folder.appendingPathComponent("changes")
    try! FileManager.default.createDirectory(at: changes, withIntermediateDirectories: true)
    let encoder = JSONEncoder()
    var text = ""
    for rec in recs { text += String(data: try! encoder.encode(rec), encoding: .utf8)! + "\n" }
    try! text.write(to: changes.appendingPathComponent("IOSDEV.jsonl"), atomically: true, encoding: .utf8)

default:
    die("unknown command: \(command)")
}
