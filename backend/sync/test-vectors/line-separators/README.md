# Vector: line-separators

Regression guard: change logs MUST be split on `\n` only, never with
`str.splitlines()`.

`json.dumps(rec, ensure_ascii=False)` — how the engine serialises a record —
leaves Unicode line separators (U+2028 LINE SEPARATOR, U+2029 PARAGRAPH
SEPARATOR, and also U+0085/VT/FF) **literal** inside string values. Python's
`str.splitlines()` treats every one of them as a line boundary, so a single
record carrying such a character in a string field (common in scraped job
descriptions) would be torn into pieces and fail to parse ("Unterminated
string"). `\n` is the only real delimiter — `json.dumps` escapes any `\n`
inside a string — so splitting on `\n` keeps the record whole.

One device `A1B2`, one `job` record whose `description` contains a literal
U+2028 and a literal U+2029. Feed the log to a merge engine and assert the
result equals `expected.json` (ignoring `_comment` / `_won_from`):

- the record loads as **one** record, not several;
- its `description` is preserved **verbatim**, separators and all.

The fixture files hold the actual separator bytes (not `\uXXXX` escapes), so an
engine that mistakenly uses `splitlines()` will read a torn, unparseable log and
fail this vector.
