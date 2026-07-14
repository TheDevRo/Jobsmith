"""Tests for save_config() in backend/app_state.py — the atomic config writer.

The symlink case is the Docker one: /app/config.yaml is a symlink into the
bind-mounted ./config directory, so a save that renames onto the link path
would detach the config from the mount and lose the user's settings on the
next container boot.
"""

import os
import stat

import yaml

from backend import app_state as state


def test_save_config_writes_through_a_symlink(tmp_path, monkeypatch):
    target = tmp_path / "config" / "config.yaml"
    target.parent.mkdir()
    target.write_text(yaml.dump({"ai": {"base_url": "http://old:1234/v1"}}))

    link = tmp_path / "config.yaml"
    link.symlink_to(target)
    monkeypatch.setattr(state, "CONFIG_PATH", link)

    state.save_config({"ai": {"base_url": "http://new:1234/v1"}})

    assert yaml.safe_load(target.read_text())["ai"]["base_url"] == "http://new:1234/v1"
    assert link.is_symlink()
    assert os.readlink(link) == str(target)
    assert not (tmp_path / "config.yaml.tmp").exists()


def test_save_config_keeps_the_file_private(tmp_path, monkeypatch):
    p = tmp_path / "config.yaml"
    monkeypatch.setattr(state, "CONFIG_PATH", p)

    state.save_config({"api_keys": {"adzuna_app_key": "secret"}})

    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert yaml.safe_load(p.read_text())["api_keys"]["adzuna_app_key"] == "secret"
