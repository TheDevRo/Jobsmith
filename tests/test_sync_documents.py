"""Content-addressed document store: dedup, materialize, missing-blob, atomic."""
from backend.sync.documents import DocumentStore, sha256_file


def _write(path, data=b"%PDF-1.4 hello"):
    path.write_bytes(data)
    return path


def test_put_is_content_addressed_and_dedups(tmp_path):
    store = DocumentStore(tmp_path / "documents")
    a = _write(tmp_path / "resume.pdf")
    ref = store.put(a)
    assert ref["ext"] == "pdf"
    assert ref["hash"] == sha256_file(a)
    assert store.has(ref)
    assert store.blob_path(ref).name == f"{ref['hash']}.pdf"

    # Identical bytes under a different name map to the same blob (no dup).
    b = _write(tmp_path / "copy.pdf")
    ref2 = store.put(b)
    assert ref2 == ref
    assert len(list((tmp_path / "documents").glob("*.pdf"))) == 1


def test_materialize_roundtrips_bytes(tmp_path):
    store = DocumentStore(tmp_path / "documents", local_dir=tmp_path / "local")
    src = _write(tmp_path / "cover.pdf", b"cover bytes")
    ref = store.put(src)

    out = store.materialize(ref, "app123_cover")
    assert out is not None
    assert out.endswith("app123_cover.pdf")
    assert (tmp_path / "local" / "app123_cover.pdf").read_bytes() == b"cover bytes"


def test_materialize_missing_blob_returns_none(tmp_path):
    store = DocumentStore(tmp_path / "documents", local_dir=tmp_path / "local")
    assert store.materialize({"hash": "deadbeef", "ext": "pdf"}, "x") is None


def test_no_temp_files_left_behind(tmp_path):
    store = DocumentStore(tmp_path / "documents", local_dir=tmp_path / "local")
    ref = store.put(_write(tmp_path / "r.pdf"))
    store.materialize(ref, "out")
    leftovers = list((tmp_path / "documents").glob(".tmp-*")) + list((tmp_path / "local").glob(".tmp-*"))
    assert leftovers == []
