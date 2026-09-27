import Foundation

/// The DeBERTa-v3 tokenizer, read from the model's HF `tokenizer.json`: Strip,
/// SentencePiece "Precompiled" and Replace normalizers, Metaspace pre-tokenizer, Unigram
/// (Viterbi) model, `[CLS] A [SEP] B [SEP]` pairs truncated only-first. Ported
/// from HF `tokenizers` so ids match the desktop's Python tokenizer exactly
/// (golden-tested on the gold set's pairs). Not handled: added special tokens
/// written literally in the input text ("[SEP]"), which HF splits out first.
struct DebertaTokenizer: Sendable {
    static let cls: Int32 = 1, sep: Int32 = 2
    private let pieces: [[UInt8]: (id: Int32, score: Double)]
    private let maxPieceBytes: Int
    private let unkID: Int32
    private let unkScore: Double
    private let normalizers: [Normalizer]

    enum Normalizer: Sendable {
        case strip, precompiled(Precompiled), replace(NSRegularExpression, String)
    }

    enum LoadError: Error { case malformed(String) }

    init(contentsOf url: URL) throws {
        let json = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any]
        guard let model = json?["model"] as? [String: Any], model["type"] as? String == "Unigram",
              let vocab = model["vocab"] as? [[Any]], let unk = model["unk_id"] as? Int
        else { throw LoadError.malformed("not a Unigram tokenizer.json") }
        let steps = (json?["normalizer"] as? [String: Any])?["normalizers"] as? [[String: Any]] ?? []
        normalizers = try steps.map { step in
            switch step["type"] as? String {
            case "Strip":
                return .strip
            case "Precompiled":
                guard let map = (step["precompiled_charsmap"] as? String).flatMap({ Data(base64Encoded: $0) })
                    .flatMap(Precompiled.init) else { throw LoadError.malformed("bad precompiled_charsmap") }
                return .precompiled(map)
            case "Replace":
                let pattern = step["pattern"] as? [String: String] ?? [:]
                let re = try pattern["Regex"].map { try NSRegularExpression(pattern: $0) }
                    ?? NSRegularExpression(pattern: NSRegularExpression.escapedPattern(for: pattern["String"] ?? ""))
                return .replace(re, NSRegularExpression.escapedTemplate(for: step["content"] as? String ?? ""))
            default:
                throw LoadError.malformed("unsupported normalizer \(step["type"] ?? "?")")
            }
        }
        var pieces: [[UInt8]: (Int32, Double)] = [:]
        pieces.reserveCapacity(vocab.count)
        var minScore = Double.infinity, maxBytes = 0
        for (i, entry) in vocab.enumerated() {
            guard let piece = entry.first as? String, let score = (entry.last as? NSNumber)?.doubleValue else { continue }
            let bytes = Array(piece.utf8)
            if pieces[bytes] == nil { pieces[bytes] = (Int32(i), score) }
            minScore = min(minScore, score)
            maxBytes = max(maxBytes, bytes.count)
        }
        self.pieces = pieces
        self.maxPieceBytes = maxBytes
        self.unkID = Int32(unk)
        self.unkScore = minScore - 10  // kUnkPenalty
    }

    /// Token ids of one text, without special tokens.
    func encode(_ text: String) -> [Int32] {
        let normalized = normalizers.reduce(text) { s, step in
            switch step {
            case .strip: return s.trimmingCharacters(in: .whitespacesAndNewlines)
            case .precompiled(let map): return map.normalize(s)
            case .replace(let re, let with):
                return re.stringByReplacingMatches(in: s, range: NSRange(s.startIndex..., in: s), withTemplate: with)
            }
        }
        // Metaspace: spaces become ▁, one is prepended, and each word starts at a ▁.
        var s = Array(normalized.replacingOccurrences(of: " ", with: "▁").utf8)
        let mark = Array("▁".utf8)
        if !s.starts(with: mark) { s = mark + s }
        var ids: [Int32] = []
        var start = 0, i = 0
        while i < s.count {
            let atMark = s[i...].starts(with: mark)
            if atMark && i > start {
                ids += viterbi(Array(s[start..<i]))
                start = i
            }
            i += atMark ? mark.count : 1
        }
        return ids + viterbi(Array(s[start...]))
    }

    /// `[CLS] premise [SEP] hypothesis [SEP]`, the premise truncated first to `maxLength`.
    func encodePair(_ premise: String, _ hypothesis: String, maxLength: Int = 512) -> [Int32] {
        var a = encode(premise), b = encode(hypothesis)
        let excess = a.count + b.count + 3 - maxLength
        if excess > 0 {
            let cut = min(excess, a.count)
            a.removeLast(cut)
            b.removeLast(min(excess - cut, b.count))  // HF refuses this case; keep the pair scoreable
        }
        return [Self.cls] + a + [Self.sep] + b + [Self.sep]
    }

    func countTokens(_ premise: String, _ hypothesis: String) -> Int {
        encode(premise).count + encode(hypothesis).count + 3
    }

    /// Unigram best segmentation of one pre-token (HF `encode_optimized`), unknown runs fused.
    private func viterbi(_ s: [UInt8]) -> [Int32] {
        struct Node { var id: Int32 = 0; var score = 0.0; var start: Int? }
        var best = [Node](repeating: Node(), count: s.count + 1)
        var pos = 0
        while pos < s.count {
            let here = best[pos].score
            let charLen = Self.utf8Length(s[pos])
            var single = false
            for len in 1...min(maxPieceBytes, s.count - pos) {
                guard let p = pieces[Array(s[pos..<pos + len])] else { continue }
                let cand = p.score + here
                if best[pos + len].start == nil || cand > best[pos + len].score {
                    best[pos + len] = Node(id: p.id, score: cand, start: pos)
                }
                if len == charLen { single = true }
            }
            if !single {
                let end = min(pos + charLen, s.count), cand = unkScore + here
                if best[end].start == nil || cand > best[end].score {
                    best[end] = Node(id: unkID, score: cand, start: pos)
                }
            }
            pos += charLen
        }
        var out: [Int32] = []
        var end = s.count
        while end > 0, let start = best[end].start {
            let id = best[end].id
            if !(id == unkID && out.last == unkID) { out.append(id) }  // fuse consecutive unknowns
            end = start
        }
        return out.reversed()
    }

    private static func utf8Length(_ lead: UInt8) -> Int {
        lead < 0x80 ? 1 : lead >> 5 == 0b110 ? 2 : lead >> 4 == 0b1110 ? 3 : lead >> 3 == 0b11110 ? 4 : 1
    }

    /// SentencePiece's precompiled normalization map (nmt_nfkc): a double-array
    /// trie over UTF-8 bytes pointing into a pool of NUL-terminated replacements.
    struct Precompiled: Sendable {
        let trie: [UInt32]
        let pool: [UInt8]

        init?(_ data: Data) {
            let bytes = [UInt8](data)
            guard bytes.count >= 4 else { return nil }
            let size = Int(UInt32(bytes[0]) | UInt32(bytes[1]) << 8 | UInt32(bytes[2]) << 16 | UInt32(bytes[3]) << 24)
            guard size % 4 == 0, 4 + size <= bytes.count else { return nil }
            trie = stride(from: 4, to: 4 + size, by: 4).map {
                UInt32(bytes[$0]) | UInt32(bytes[$0 + 1]) << 8 | UInt32(bytes[$0 + 2]) << 16 | UInt32(bytes[$0 + 3]) << 24
            }
            pool = Array(bytes[(4 + size)...])
        }

        /// The replacement for the shortest trie key that prefixes `chunk` (HF `transform`).
        func transform(_ chunk: some Collection<UInt8>) -> [UInt8]? {
            var node = Self.offset(trie[0])
            for c in chunk {
                if c == 0 { break }
                node ^= Int(c)
                guard node < trie.count else { return nil }
                let unit = trie[node]
                guard unit & (1 << 31 | 0xFF) == UInt32(c) else { return nil }
                node ^= Self.offset(unit)
                if (unit >> 8) & 1 == 1 {
                    let start = Int(trie[node] & ((1 << 31) - 1))
                    let end = pool[start...].firstIndex(of: 0) ?? pool.count
                    return Array(pool[start..<end])
                }
            }
            return nil
        }

        /// Per grapheme: the whole cluster when it maps, else scalar by scalar (HF `normalize_string`).
        func normalize(_ s: String) -> String {
            var out: [UInt8] = []
            out.reserveCapacity(s.utf8.count)
            for ch in s {
                let g = Array(ch.utf8)
                if g.count < 6, let r = transform(g) {
                    out += r
                    continue
                }
                for scalar in ch.unicodeScalars {
                    let b = Array(String(scalar).utf8)
                    out += transform(b) ?? b
                }
            }
            return String(decoding: out, as: UTF8.self)
        }

        private static func offset(_ unit: UInt32) -> Int { Int((unit >> 10) << ((unit & (1 << 9)) >> 6)) }
    }
}
