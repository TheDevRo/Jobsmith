# Vector: document-ref

Proves content-addressed document references ride through the merge as ordinary
record fields, and update with the winning version.

- `uuid-app-9` is drafted on A1B2 (`resume_doc: 9f2c4a.pdf`, no cover), then
  regenerated on C3D4 (`resume_doc: d4e5f6.pdf`, `cover_doc: 7a8b9c.docx`).
- Newer version wins → the live record points at C3D4's hashes.
- The `documents/*.pdf|docx` blobs here are placeholders. The merge engine never
  reads them; they are synced as opaque files and referenced by name (sha256).
  A record may arrive before its blob — that is expected and non-fatal.
