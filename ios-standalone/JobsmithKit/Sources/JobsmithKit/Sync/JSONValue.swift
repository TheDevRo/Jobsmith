import Foundation

/// A JSON value for sync `data` payloads — heterogeneous, schema-less, and
/// carried verbatim so keys one client doesn't model survive a round-trip.
///
/// Deterministic `canonicalString()` (sorted keys, compact) backs the snapshot
/// diff, mirroring the Python engine's `_canon`. Cross-implementation agreement
/// is proven by structural equality of merged results, not by matching this
/// string byte-for-byte.
public indirect enum JSONValue: Codable, Equatable, Sendable {
    case string(String)
    case int(Int)
    case double(Double)
    case bool(Bool)
    case null
    case array([JSONValue])
    case object([String: JSONValue])

    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() {
            self = .null
        } else if let b = try? c.decode(Bool.self) {
            self = .bool(b)
        } else if let i = try? c.decode(Int.self) {
            self = .int(i)
        } else if let d = try? c.decode(Double.self) {
            self = .double(d)
        } else if let s = try? c.decode(String.self) {
            self = .string(s)
        } else if let a = try? c.decode([JSONValue].self) {
            self = .array(a)
        } else if let o = try? c.decode([String: JSONValue].self) {
            self = .object(o)
        } else {
            throw DecodingError.dataCorruptedError(in: c, debugDescription: "unsupported JSON value")
        }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let s): try c.encode(s)
        case .int(let i): try c.encode(i)
        case .double(let d): try c.encode(d)
        case .bool(let b): try c.encode(b)
        case .null: try c.encodeNil()
        case .array(let a): try c.encode(a)
        case .object(let o): try c.encode(o)
        }
    }

    // MARK: Bridging to/from Foundation JSON

    /// Build from a `JSONSerialization` value (NSNumber/NSString/NSNull/...).
    public static func from(_ any: Any) -> JSONValue {
        switch any {
        case is NSNull: return .null
        case let n as NSNumber:
            // NSNumber can't reliably distinguish Bool from 0/1 by type, so
            // check the ObjC type encoding.
            if CFGetTypeID(n) == CFBooleanGetTypeID() { return .bool(n.boolValue) }
            if n.stringValue.contains(".") || n.stringValue.contains("e") {
                return .double(n.doubleValue)
            }
            return .int(n.intValue)
        case let s as String: return .string(s)
        case let a as [Any]: return .array(a.map(JSONValue.from))
        case let o as [String: Any]:
            return .object(o.mapValues(JSONValue.from))
        default: return .null
        }
    }

    /// Convert to a `JSONSerialization`-compatible value.
    public func toAny() -> Any {
        switch self {
        case .string(let s): return s
        case .int(let i): return i
        case .double(let d): return d
        case .bool(let b): return b
        case .null: return NSNull()
        case .array(let a): return a.map { $0.toAny() }
        case .object(let o): return o.mapValues { $0.toAny() }
        }
    }

    public var objectValue: [String: JSONValue]? {
        if case .object(let o) = self { return o }
        return nil
    }

    public var stringValue: String? {
        if case .string(let s) = self { return s }
        return nil
    }

    /// Sorted-key compact serialization for snapshot change-detection.
    public func canonicalString() -> String {
        let data = (try? JSONSerialization.data(withJSONObject: toAny(), options: [.sortedKeys, .fragmentsAllowed])) ?? Data()
        return String(data: data, encoding: .utf8) ?? ""
    }

    /// Drop `_`-prefixed annotation keys recursively (documentation-only fields
    /// in the test vectors' expected.json).
    public func strippingAnnotations() -> JSONValue {
        switch self {
        case .array(let a): return .array(a.map { $0.strippingAnnotations() })
        case .object(let o):
            var out: [String: JSONValue] = [:]
            for (k, v) in o where !k.hasPrefix("_") {
                out[k] = v.strippingAnnotations()
            }
            return .object(out)
        default: return self
        }
    }
}

public extension Dictionary where Key == String, Value == JSONValue {
    func canonicalString() -> String { JSONValue.object(self).canonicalString() }
}
