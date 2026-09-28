import Foundation

/// The uncased BERT WordPiece tokenizer of the Quick match model (bge-small), read from its HF
/// `tokenizer.json`: BertNormalizer (clean text, space out CJK, strip accents, lowercase),
/// BertPreTokenizer (split on whitespace and punctuation), WordPiece (`##` continuations,
/// words over 100 characters -> [UNK]), `[CLS] text [SEP]` truncated to `maxLength`. Ported from
/// HF `tokenizers` so ids match the desktop exactly (golden-tested). Not handled: special tokens
/// written literally in the input ("[SEP]"), which HF splits out first.
struct BertTokenizer: Sendable {
    private let vocab: [String: Int32]
    let cls: Int32, sep: Int32, unk: Int32
    static let maxWordChars = 100

    enum LoadError: Error { case malformed(String) }

    init(vocab: [String: Int32]) throws {
        guard let cls = vocab["[CLS]"], let sep = vocab["[SEP]"], let unk = vocab["[UNK]"] else {
            throw LoadError.malformed("vocab lacks [CLS]/[SEP]/[UNK]")
        }
        self.vocab = vocab
        self.cls = cls; self.sep = sep; self.unk = unk
    }

    init(contentsOf url: URL) throws {
        let json = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any]
        guard let model = json?["model"] as? [String: Any], model["type"] as? String == "WordPiece",
              let vocab = model["vocab"] as? [String: Int] else {
            throw LoadError.malformed("not a WordPiece tokenizer.json")
        }
        try self.init(vocab: vocab.mapValues { Int32($0) })
    }

    /// `[CLS] tokens [SEP]`, at most `maxLength` ids (no padding).
    func encode(_ text: String, maxLength: Int) -> [Int32] {
        var ids: [Int32] = [cls]
        for word in Self.words(Self.normalize(text)) {
            ids += wordPiece(word)
            if ids.count >= maxLength - 1 { break }
        }
        return Array(ids.prefix(maxLength - 1)) + [sep]
    }

    private func wordPiece(_ word: [Unicode.Scalar]) -> [Int32] {
        guard word.count <= Self.maxWordChars else { return [unk] }
        var out: [Int32] = []
        var start = 0
        while start < word.count {
            var end = word.count
            var found: Int32?
            while start < end {
                var piece = String(String.UnicodeScalarView(word[start..<end]))
                if start > 0 { piece = "##" + piece }
                if let id = vocab[piece] { found = id; break }
                end -= 1
            }
            guard let id = found else { return [unk] }  // no piece fits: the whole word is unknown
            out.append(id)
            start = end
        }
        return out
    }

    /// BertNormalizer(clean_text, handle_chinese_chars, strip_accents (follows lowercase), lowercase).
    static func normalize(_ text: String) -> String {
        var out = String.UnicodeScalarView()
        for s in text.unicodeScalars {
            if s.value == 0 || s.value == 0xFFFD || isControl(s) { continue }
            if isWhitespace(s) { out.append(" "); continue }
            if isCJK(s) { out.append(" "); out.append(s); out.append(" "); continue }
            out.append(s)
        }
        // NFD, drop nonspacing marks (accents), then lowercase
        let stripped = String(out).decomposedStringWithCanonicalMapping.unicodeScalars
            .filter { $0.properties.generalCategory != .nonspacingMark }
        return String(String.UnicodeScalarView(stripped)).lowercased()
    }

    /// BertPreTokenizer: whitespace separates words, each punctuation character is its own word.
    static func words(_ text: String) -> [[Unicode.Scalar]] {
        var out: [[Unicode.Scalar]] = []
        var cur: [Unicode.Scalar] = []
        for s in text.unicodeScalars {
            if isWhitespace(s) {
                if !cur.isEmpty { out.append(cur); cur = [] }
            } else if isPunctuation(s) {
                if !cur.isEmpty { out.append(cur); cur = [] }
                out.append([s])
            } else {
                cur.append(s)
            }
        }
        if !cur.isEmpty { out.append(cur) }
        return out
    }

    static func isWhitespace(_ s: Unicode.Scalar) -> Bool {
        s == "\t" || s == "\n" || s == "\r" || s.properties.isWhitespace
    }

    static func isControl(_ s: Unicode.Scalar) -> Bool {  // Rust char::is_other(): Cc Cf Cs Co Cn
        if s == "\t" || s == "\n" || s == "\r" { return false }
        switch s.properties.generalCategory {
        case .control, .format, .surrogate, .privateUse, .unassigned: return true
        default: return false
        }
    }

    static func isPunctuation(_ s: Unicode.Scalar) -> Bool {
        if s.isASCII, let c = Character(s).asciiValue {
            if (33...47).contains(c) || (58...64).contains(c) || (91...96).contains(c) || (123...126).contains(c) { return true }
        }
        switch s.properties.generalCategory {
        case .connectorPunctuation, .dashPunctuation, .openPunctuation, .closePunctuation,
             .initialPunctuation, .finalPunctuation, .otherPunctuation: return true
        default: return false
        }
    }

    static func isCJK(_ s: Unicode.Scalar) -> Bool {
        switch s.value {
        case 0x4E00...0x9FFF, 0x3400...0x4DBF, 0x20000...0x2A6DF, 0x2A700...0x2B73F,
             0x2B740...0x2B81F, 0x2B820...0x2CEAF, 0xF900...0xFAFF, 0x2F800...0x2FA1F: return true
        default: return false
        }
    }
}
