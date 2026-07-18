"""Settings sync (the config-backed `setting` entity): per-key last-writer-wins
config sync layered on the same folder transport.

Two engines back two SQLite DBs *and* two in-memory config dicts (standing in for
each machine's config.yaml). Category toggles live under `sync.settings.*`. The
tests mirror SETTINGS_SYNC_PLAN.md Phase 1.5:

  * export excludes SECRET + LOCAL rows and any OFF category;
  * per-key LWW across two devices; symmetric category gating;
  * enabled_sources fold round-trips; explicit null preserved;
  * a key absent from the registry is never written on import;
  * prompts.* expands to one record per id (per-prompt LWW);
  * ai.models base-overlay keeps desktop-only sibling keys;
  * unknown entities are skipped on import (backward-compat guard).
"""
import copy
import sqlite3

import pytest

from backend import database as dbmod
from backend.sync import SyncEngine
from backend.sync import settings_registry as sr
from backend.sync.engine import SETTING_ENTITY, iso_ms, _now


async def _init_db(path, monkeypatch):
    monkeypatch.setattr(dbmod, "DB_PATH", path)
    await dbmod.init_db()


class Clock:
    def __init__(self):
        from datetime import datetime, timezone
        self.t = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        from datetime import timedelta
        self.t += timedelta(seconds=1)
        return self.t


class ConfigBox:
    """File-like config store: load() hands out a deep copy, save() replaces the
    backing — so an engine can't rely on mutating the loaded dict in place."""

    def __init__(self, cfg=None):
        self.cfg = cfg or {}

    def load(self):
        return copy.deepcopy(self.cfg)

    def save(self, cfg):
        self.cfg = copy.deepcopy(cfg)


def _all_categories_on():
    return {c.key: True for c in sr.CATEGORIES}


async def _engine(path, device, box, monkeypatch):
    await _init_db(path, monkeypatch)
    return SyncEngine(path, device, now_fn=Clock(),
                      load_settings=box.load, save_settings=box.save)


# ---------------------------------------------------------------------------
# secret unification guard (catches drift between the two historical lists and
# the registry forever)
# ---------------------------------------------------------------------------

def test_profile_secret_keys_are_registry_folder_strips():
    from backend.sync.profile_map import SECRET_KEYS

    canonical = sr.secret_canonical_keys()
    assert SECRET_KEYS  # non-empty
    for key in SECRET_KEYS:
        assert f"profile.{key}" in canonical, f"profile.{key} not in secret_canonical_keys()"


def test_profile_map_round_trips_new_iOS_owned_fields():
    """middle_name, street_address_2 and the EEO block are now iOS-owned scalars.
    They must survive canonical -> iOS -> canonical with zero loss, matching the
    profile-normalize test vectors."""
    import json
    from pathlib import Path

    from backend.sync import profile_map as pm

    vec = Path(__file__).resolve().parent.parent / "backend/sync/test-vectors/profile-normalize"
    canonical = json.loads((vec / "canonical.json").read_text())
    ios = json.loads((vec / "ios-profile.json").read_text())

    # canonical -> iOS reproduces the iOS fixture exactly (all scalars modeled now).
    assert pm.canonical_to_ios(canonical) == ios
    for ik in ("middleName", "streetAddress2", "gender", "raceEthnicity",
               "veteranStatus", "disabilityStatus"):
        assert ik in ios

    # iOS -> canonical (base-overlaid) round-trips back to the canonical fixture.
    assert pm.ios_to_canonical(ios, base=canonical) == canonical
    for ck in ("middle_name", "street_address_2", "gender", "race_ethnicity",
               "veteran_status", "disability_status"):
        assert ck in pm.IOS_OWNED_CANON_KEYS
        assert pm.ios_to_canonical(ios)[ck] == canonical[ck]


def test_http_secret_fields_are_registry_api_masked():
    # routers/settings pulls the (optional) auto_apply stack; skip cleanly when
    # those deps aren't installed rather than failing on an unrelated import.
    pytest.importorskip("aiohttp")
    from backend.routers.settings import _SECRET_FIELDS

    masked = sr.api_masked_keys()
    for section, key in _SECRET_FIELDS:
        assert f"{section}.{key}" in masked, f"{section}.{key} not in api_masked_keys()"


def test_inbox_category_and_settings_registered():
    """The `inbox` category (default ON) plus its two synced prefs are in the
    registry, with the exact enum contract the iOS twin uses."""
    cats = {c.key: c for c in sr.CATEGORIES}
    assert "inbox" in cats
    assert cats["inbox"].default is True
    assert cats["inbox"].label == "Inbox"

    ids = sr.syncable_canonical_ids()
    assert "inbox.require_stated_pay" in ids
    assert "inbox.sort" in ids

    by_canon = {s.canonical: s for s in sr.syncable()}
    rsp = by_canon["inbox.require_stated_pay"]
    assert rsp.kind is sr.Kind.BOOL and rsp.category == "inbox"
    srt = by_canon["inbox.sort"]
    assert srt.kind is sr.Kind.ENUM and srt.category == "inbox"
    assert srt.enum_values == ("best_bets", "best_match", "newest", "salary", "company")

    assert sr.category_for_path("inbox.sort") == "inbox"
    assert sr.category_for_path("inbox.require_stated_pay") == "inbox"


def test_inbox_settings_export_apply_and_gating():
    cfg = {"sync": {"settings": {"inbox": True}},
           "inbox": {"require_stated_pay": True, "sort": "salary"}}
    out = sr.export_settings(cfg)
    assert out["inbox.require_stated_pay"] == {"value": True}
    assert out["inbox.sort"] == {"value": "salary"}

    # Category OFF -> neither pref is exported.
    off = sr.export_settings({"sync": {"settings": {"inbox": False}},
                              "inbox": {"sort": "newest"}})
    assert "inbox.sort" not in off and "inbox.require_stated_pay" not in off

    # apply_setting routes both back into cfg["inbox"] (default path mapping).
    dest = {}
    sr.apply_setting(dest, "inbox.sort", "company")
    sr.apply_setting(dest, "inbox.require_stated_pay", True)
    assert dest["inbox"]["sort"] == "company"
    assert dest["inbox"]["require_stated_pay"] is True


def test_api_key_is_masked_but_not_folder_stripped():
    # The one deliberate asymmetry: the AI api_key syncs through the folder but is
    # still masked over HTTP.
    assert "ai.api_key" in sr.api_masked_keys()
    assert "ai.api_key" not in sr.secret_canonical_keys()


# ---------------------------------------------------------------------------
# export_settings / apply_setting (pure)
# ---------------------------------------------------------------------------

def test_export_excludes_secret_local_and_off_categories():
    cfg = {
        "sync": {"settings": {"documents": True, "ai_connection": True}},
        "application_honesty": {"resume_style": "ledger"},
        "search": {"keywords": ["python"]},          # postings OFF
        "ai": {"base_url": "http://x", "api_key": "k"},
        "api_keys": {"adzuna_app_key": "SECRET"},    # SECRET, never a row
        "server": {"port": 8888},                     # LOCAL
    }
    out = sr.export_settings(cfg)
    assert "application_honesty.resume_style" in out
    assert "ai.base_url" in out
    assert "ai.api_key" in out                        # SYNC (masked over HTTP only)
    assert "search.keywords" not in out               # category OFF
    assert not any(k.startswith("api_keys.") for k in out)
    assert not any(k.startswith("server.") for k in out)


def test_explicit_null_preserved_but_absent_omitted():
    cfg = {"sync": {"settings": {"postings": True}},
           "search": {"min_salary": None, "keywords": ["a"]}}
    out = sr.export_settings(cfg)
    assert out["search.min_salary"] == {"value": None}   # explicit null travels
    assert "search.max_age_days" not in out              # absent => omitted


def test_resume_style_legacy_alias_normalized_on_export_and_apply():
    cfg = {"sync": {"settings": {"documents": True}},
           "application_honesty": {"resume_style": "Modern"}}
    assert sr.export_settings(cfg)["application_honesty.resume_style"] == {"value": "ledger"}

    dest = {}
    sr.apply_setting(dest, "application_honesty.resume_style", "minimal")
    assert dest["application_honesty"]["resume_style"] == "swiss"


def test_ai_models_base_overlay_keeps_sibling_keys():
    cfg = {"ai": {"models": {"strong": {"model": "old", "temperature": 0.1}}}}
    sr.apply_setting(cfg, "ai.models.strong", "new-model")
    assert cfg["ai"]["models"]["strong"]["model"] == "new-model"
    assert cfg["ai"]["models"]["strong"]["temperature"] == 0.1  # sibling survives


def test_enabled_sources_fold_round_trips_sorted():
    cfg = {"sync": {"settings": {"postings": True}},
           "search": {"indeed": {"enabled": True}, "greenhouse": {"enabled": False}}}
    folded = sr.export_settings(cfg)["search.enabled_sources"]["value"]
    assert folded == sorted(folded)          # stable / hash-safe
    assert "indeed" in folded                # explicitly on
    assert "greenhouse" not in folded        # explicitly off
    assert "remoteok" in folded              # defaults on

    dest = {}
    sr.apply_setting(dest, "search.enabled_sources", folded)
    # unfold writes per-source bools; re-fold is identical
    assert sr.fold_enabled_sources(dest) == folded


def test_apply_rejects_paths_not_in_registry():
    cfg = {}
    sr.apply_setting(cfg, "api_keys.adzuna_app_key", "SECRET")   # SECRET row
    sr.apply_setting(cfg, "server.port", 9999)                    # LOCAL row
    sr.apply_setting(cfg, "totally.unknown.key", "x")             # unknown
    assert cfg == {}


# ---------------------------------------------------------------------------
# engine round-trips
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_per_key_lww_across_two_devices(tmp_path, monkeypatch):
    folder = tmp_path / "sync"
    boxA = ConfigBox({"sync": {"settings": {"documents": True}},
                      "application_honesty": {"resume_style": "ledger",
                                              "honesty_level": "honest"}})
    boxB = ConfigBox({"sync": {"settings": {"documents": True}},
                      "application_honesty": {}})

    a = await _engine(tmp_path / "a.db", "A1B2", boxA, monkeypatch)
    b = await _engine(tmp_path / "b.db", "C3D4", boxB, monkeypatch)

    await a.export_changes(folder)
    await b.import_changes(folder)
    assert boxB.cfg["application_honesty"]["resume_style"] == "ledger"
    assert boxB.cfg["application_honesty"]["honesty_level"] == "honest"

    # B changes ONLY resume_style; A changes ONLY honesty_level. Per-key LWW keeps
    # both — no clobber (the whole point of one record per key).
    boxB.cfg["application_honesty"]["resume_style"] = "swiss"
    boxA.cfg["application_honesty"]["honesty_level"] = "tailored"

    await b.export_changes(folder)
    await a.export_changes(folder)
    await a.import_changes(folder)
    await b.import_changes(folder)

    for box in (boxA, boxB):
        assert box.cfg["application_honesty"]["resume_style"] == "swiss"
        assert box.cfg["application_honesty"]["honesty_level"] == "tailored"


@pytest.mark.asyncio
async def test_category_gating_is_symmetric(tmp_path, monkeypatch):
    folder = tmp_path / "sync"
    # A has postings ON and exports keywords; B has postings OFF.
    boxA = ConfigBox({"sync": {"settings": {"postings": True}},
                      "search": {"keywords": ["python"]}})
    boxB = ConfigBox({"sync": {"settings": {"postings": False}},
                      "search": {"keywords": ["existing"]}})

    a = await _engine(tmp_path / "a.db", "A1B2", boxA, monkeypatch)
    b = await _engine(tmp_path / "b.db", "C3D4", boxB, monkeypatch)

    await a.export_changes(folder)
    imp = await b.import_changes(folder)
    assert imp.settings_updated == 0
    assert boxB.cfg["search"]["keywords"] == ["existing"]  # untouched — toggle off

    # Turn B's postings ON; now the same folder converges it.
    boxB.cfg["sync"]["settings"]["postings"] = True
    await b.import_changes(folder)
    assert boxB.cfg["search"]["keywords"] == ["python"]


@pytest.mark.asyncio
async def test_prompts_expand_to_one_record_per_id(tmp_path, monkeypatch):
    folder = tmp_path / "sync"
    boxA = ConfigBox({"sync": {"settings": {"prompts": True}},
                      "prompts": {"score": "A-score", "cover": "A-cover"}})
    boxB = ConfigBox({"sync": {"settings": {"prompts": True}}, "prompts": {}})

    a = await _engine(tmp_path / "a.db", "A1B2", boxA, monkeypatch)
    b = await _engine(tmp_path / "b.db", "C3D4", boxB, monkeypatch)

    exp = await a.export_changes(folder)
    # two prompt records, one per id
    import json
    recs = [json.loads(l) for l in (folder / "changes" / "A1B2.jsonl").read_text().splitlines()]
    ids = {r["id"] for r in recs if r["entity"] == SETTING_ENTITY}
    assert ids == {"prompts.score", "prompts.cover"}

    await b.import_changes(folder)
    assert boxB.cfg["prompts"] == {"score": "A-score", "cover": "A-cover"}

    # Per-prompt LWW: B edits only `cover`, A edits only `score` — both survive.
    boxB.cfg["prompts"]["cover"] = "B-cover"
    boxA.cfg["prompts"]["score"] = "A-score-2"
    await b.export_changes(folder)
    await a.export_changes(folder)
    await a.import_changes(folder)
    await b.import_changes(folder)
    for box in (boxA, boxB):
        assert box.cfg["prompts"] == {"score": "A-score-2", "cover": "B-cover"}


@pytest.mark.asyncio
async def test_deleted_prompt_tombstones_and_removes_on_peer(tmp_path, monkeypatch):
    folder = tmp_path / "sync"
    boxA = ConfigBox({"sync": {"settings": {"prompts": True}},
                      "prompts": {"score": "keep", "junk": "delete-me"}})
    boxB = ConfigBox({"sync": {"settings": {"prompts": True}}, "prompts": {}})
    a = await _engine(tmp_path / "a.db", "A1B2", boxA, monkeypatch)
    b = await _engine(tmp_path / "b.db", "C3D4", boxB, monkeypatch)

    await a.export_changes(folder)
    await b.import_changes(folder)
    assert "junk" in boxB.cfg["prompts"]

    del boxA.cfg["prompts"]["junk"]
    exp = await a.export_changes(folder)
    assert exp.tombstones == 1
    await b.import_changes(folder)
    assert "junk" not in boxB.cfg["prompts"]
    assert boxB.cfg["prompts"]["score"] == "keep"


@pytest.mark.asyncio
async def test_ai_key_syncs_through_folder(tmp_path, monkeypatch):
    folder = tmp_path / "sync"
    boxA = ConfigBox({"sync": {"settings": {"ai_connection": True}},
                      "ai": {"base_url": "http://lan:1234", "api_key": "sk-secret",
                             "models": {"strong": {"model": "big"}}}})
    boxB = ConfigBox({"sync": {"settings": {"ai_connection": True}}, "ai": {}})
    a = await _engine(tmp_path / "a.db", "A1B2", boxA, monkeypatch)
    b = await _engine(tmp_path / "b.db", "C3D4", boxB, monkeypatch)

    await a.export_changes(folder)
    await b.import_changes(folder)
    assert boxB.cfg["ai"]["api_key"] == "sk-secret"          # user chose to sync it
    assert boxB.cfg["ai"]["base_url"] == "http://lan:1234"
    assert boxB.cfg["ai"]["models"]["strong"]["model"] == "big"


@pytest.mark.asyncio
async def test_reexport_after_import_is_noop(tmp_path, monkeypatch):
    folder = tmp_path / "sync"
    boxA = ConfigBox({"sync": {"settings": _all_categories_on()},
                      "application_honesty": {"resume_style": "swiss"},
                      "search": {"keywords": ["go"], "min_salary": None},
                      "ai": {"api_key": "k", "models": {"strong": {"model": "m"}}},
                      "prompts": {"p": "v"}})
    boxB = ConfigBox({"sync": {"settings": _all_categories_on()}})
    a = await _engine(tmp_path / "a.db", "A1B2", boxA, monkeypatch)
    b = await _engine(tmp_path / "b.db", "C3D4", boxB, monkeypatch)

    await a.export_changes(folder)
    await b.import_changes(folder)
    # A settled export right after import must emit nothing new.
    reexp = await b.export_changes(folder / "empty")
    assert reexp.live == 0 and reexp.tombstones == 0


@pytest.mark.asyncio
async def test_unknown_entity_is_skipped_on_import(tmp_path, monkeypatch):
    """Backward-compat: a client that predates an entity must skip records it has
    no adapter for, not error. Regression guard for the format bump."""
    folder = tmp_path / "sync"
    changes = folder / "changes"
    changes.mkdir(parents=True)
    import json
    rec = {"v": 1, "entity": "totally_future_entity", "id": "x",
           "updated_at": "2099-01-01T00:00:00.000Z", "device": "ZZ",
           "deleted": False, "data": {"whatever": 1}}
    (changes / "ZZ.jsonl").write_text(json.dumps(rec) + "\n")

    box = ConfigBox({"sync": {"settings": _all_categories_on()}})
    b = await _engine(tmp_path / "b.db", "C3D4", box, monkeypatch)
    imp = await b.import_changes(folder)  # must not raise
    assert imp.upserts == 0 and imp.settings_updated == 0


@pytest.mark.asyncio
async def test_profile_gate_off_stops_sync_without_tombstoning(tmp_path, monkeypatch):
    folder = tmp_path / "sync"
    # A syncs its profile; B has the profile toggle OFF, so it neither imports the
    # incoming profile nor (critically) tombstones anything.
    boxA = ConfigBox({"sync": {"settings": {"profile": True}},
                      "profile": {"full_name": "Alex"}})
    boxB = ConfigBox({"sync": {"settings": {"profile": False}},
                      "profile": {"full_name": "Local"}})

    await _init_db(tmp_path / "a.db", monkeypatch)
    await _init_db(tmp_path / "b.db", monkeypatch)

    def mk(path, dev, box):
        return SyncEngine(
            path, dev, now_fn=Clock(),
            load_profile=lambda: box.cfg.get("profile"),
            save_profile=lambda p: box.cfg.__setitem__("profile", p),
            load_settings=box.load, save_settings=box.save,
        )

    a = mk(tmp_path / "a.db", "A1B2", boxA)
    b = mk(tmp_path / "b.db", "C3D4", boxB)

    await a.export_changes(folder)
    imp = await b.import_changes(folder)
    assert imp.profile_updated is False
    assert boxB.cfg["profile"]["full_name"] == "Local"  # untouched

    # And B, with profile OFF, exports no profile record and no tombstone for it.
    exp = await b.export_changes(folder)
    import json
    recs = [json.loads(l) for l in (folder / "changes" / "C3D4.jsonl").read_text().splitlines()] \
        if (folder / "changes" / "C3D4.jsonl").exists() else []
    assert not any(r["entity"] == "profile" for r in recs)
