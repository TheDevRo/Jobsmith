import Foundation
import GRDB

/// A remembered ATS account for one company tenant. Workday requires a separate
/// account per tenant (`{company}.wd{N}.myworkdayjobs.com`); this registry lets
/// every surface — the extension Apply Assist, the in-app Apply browser, and the
/// desktop automated adapter — skip the "sign in vs create account" DOM heuristic
/// once a tenant is known.
///
/// Syncs as the `ats_account` entity (see SyncEngine), keyed by
/// `{provider}:{tenantHost}`, plain last-writer-wins. NEVER carries a password.
public struct AtsAccount: Equatable, Sendable {
    public static let providerWorkday = "workday"
    public static let statusActive = "active"
    public static let statusPending = "pending_verification"

    public var tenantHost: String
    public var provider: String
    public var email: String?
    public var status: String
    public var createdAt: String?
    public var lastSignInAt: String?
    public var updatedAt: String?

    public var isPending: Bool { status == Self.statusPending }

    public init(tenantHost: String, provider: String = providerWorkday,
                email: String? = nil, status: String = statusActive,
                createdAt: String? = nil, lastSignInAt: String? = nil,
                updatedAt: String? = nil) {
        self.tenantHost = tenantHost
        self.provider = provider
        self.email = email
        self.status = status
        self.createdAt = createdAt
        self.lastSignInAt = lastSignInAt
        self.updatedAt = updatedAt
    }
}

public final class AtsAccountStore {
    private let db: AppDatabase

    public init(_ db: AppDatabase) {
        self.db = db
    }

    static let isoFmt: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss.SSS'Z'"
        return f
    }()

    public func all() throws -> [AtsAccount] {
        try db.writer.read { dbc in
            try Row.fetchAll(dbc, sql: "SELECT * FROM ats_accounts ORDER BY tenantHost")
                .map(Self.account(from:))
        }
    }

    public func get(_ tenantHost: String) throws -> AtsAccount? {
        let host = tenantHost.lowercased()
        return try db.writer.read { dbc in
            try Row.fetchOne(dbc, sql: "SELECT * FROM ats_accounts WHERE tenantHost = ?",
                             arguments: [host]).map(Self.account(from:))
        }
    }

    /// Record (or update) an account. Preserves the original `createdAt`; always
    /// refreshes `updatedAt`. Returns the resulting row.
    @discardableResult
    public func upsert(tenantHost: String, email: String,
                       status: String = AtsAccount.statusActive,
                       provider: String = AtsAccount.providerWorkday,
                       now: Date = Date()) throws -> AtsAccount {
        let host = tenantHost.lowercased()
        let nowStr = Self.isoFmt.string(from: now)
        return try db.writer.write { dbc in
            let existing = try Row.fetchOne(
                dbc, sql: "SELECT createdAt FROM ats_accounts WHERE tenantHost = ?",
                arguments: [host])
            let createdAt = (existing?["createdAt"] as String?) ?? nowStr
            try dbc.execute(sql: """
                INSERT INTO ats_accounts
                    (tenantHost, provider, email, status, createdAt, lastSignInAt, updatedAt)
                VALUES (?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(tenantHost) DO UPDATE SET
                    provider = excluded.provider, email = excluded.email,
                    status = excluded.status, createdAt = excluded.createdAt,
                    updatedAt = excluded.updatedAt
                """, arguments: [host, provider, email, status, createdAt, nowStr])
            return try Row.fetchOne(dbc, sql: "SELECT * FROM ats_accounts WHERE tenantHost = ?",
                                    arguments: [host]).map(Self.account(from:))!
        }
    }

    /// Stamp a successful sign-in; promotes `pending_verification` → `active`.
    @discardableResult
    public func markSignedIn(_ tenantHost: String, now: Date = Date()) throws -> AtsAccount? {
        let host = tenantHost.lowercased()
        let nowStr = Self.isoFmt.string(from: now)
        return try db.writer.write { dbc in
            try dbc.execute(sql: """
                UPDATE ats_accounts
                SET lastSignInAt = ?, updatedAt = ?,
                    status = CASE WHEN status = 'pending_verification'
                                  THEN 'active' ELSE status END
                WHERE tenantHost = ?
                """, arguments: [nowStr, nowStr, host])
            return try Row.fetchOne(dbc, sql: "SELECT * FROM ats_accounts WHERE tenantHost = ?",
                                    arguments: [host]).map(Self.account(from:))
        }
    }

    /// Delete an account. Removing the row becomes a tombstone on the next export.
    public func delete(_ tenantHost: String) throws {
        let host = tenantHost.lowercased()
        _ = try db.writer.write { dbc in
            try dbc.execute(sql: "DELETE FROM ats_accounts WHERE tenantHost = ?", arguments: [host])
        }
    }

    static func account(from row: Row) -> AtsAccount {
        AtsAccount(tenantHost: row["tenantHost"], provider: row["provider"],
                   email: row["email"], status: row["status"],
                   createdAt: row["createdAt"], lastSignInAt: row["lastSignInAt"],
                   updatedAt: row["updatedAt"])
    }
}
