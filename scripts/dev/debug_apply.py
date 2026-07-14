#!/usr/bin/env python3
"""
debug_apply.py — CLI diagnostic tool for the new auto-apply orchestrator.

Usage:
    python debug_apply.py <JOB_URL>

    python debug_apply.py "https://boards.greenhouse.io/acme/jobs/123" \
        --mode autofill

    python debug_apply.py "https://jobs.lever.co/acme/apply" \
        --mode submit --no-headless

What it does
------------
1. Loads config.yaml
2. Resolves which adapter would be selected
3. Launches a browser (non-headless by default for visual debugging)
4. Navigates to the URL and takes a DOM snapshot
5. Sends the snapshot to LM Studio and prints the field mapping
6. Fills the form in autofill mode (does NOT submit)
7. Dumps a JSON report of all fields, LLM confidence scores, and actions taken

This is safe to run on any real application URL — it will never submit.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import yaml

# Make sure backend/ is importable
sys.path.insert(0, str(Path(__file__).resolve().parent))


def load_config() -> dict:
    cfg_path = Path(__file__).resolve().parent / "config.yaml"
    if not cfg_path.exists():
        cfg_path = Path(__file__).resolve().parent / "config.example.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


async def run_debug(url: str, mode: str, headless: bool) -> None:
    from backend.auto_apply.adapters import ALL_ADAPTERS
    from backend.auto_apply.answer_bank import get_answer_bank
    from backend.auto_apply.browser_controller import BrowserController
    from backend.auto_apply.llm_client import LLMClient
    from backend.auto_apply.logger import AutoApplyLogger
    from backend.auto_apply.models import (
        ApplyMode,
        JobApplicationRequest,
        UserProfile,
    )

    config = load_config()
    # Force headless to CLI arg value
    config.setdefault("auto_apply", {})["headless"] = headless

    profile = UserProfile.from_config(config)
    job = JobApplicationRequest(
        job_id="debug-001",
        title="[Debug Run]",
        company="[Debug]",
        url=url,
        description="",
    )

    apply_mode = ApplyMode.AUTOFILL  # Always autofill in debug mode

    # ── Adapter selection ──────────────────────────────────────────────────
    chosen = None
    for adapter in ALL_ADAPTERS:
        if adapter.matches(url, ""):
            chosen = adapter
            break

    print(f"\n{'='*60}")
    print(f"  URL:     {url}")
    print(f"  Adapter: {chosen.name if chosen else 'none'}")
    print(f"  Mode:    {apply_mode.value} (forced — debug never submits)")
    print(f"  Headless: {headless}")
    print(f"{'='*60}\n")

    log = AutoApplyLogger(
        job_id="debug-001",
        app_id="debug",
        site=url[:50],
        adapter=chosen.name if chosen else "unknown",
        mode="debug",
    )

    # ── Session state ──────────────────────────────────────────────────────
    # Mirror the orchestrator's session-loading logic so debug runs use the
    # same authenticated browser context as production.
    storage_state_path = None
    profile_dir = None

    if chosen and chosen.name == "linkedin":
        try:
            from backend.auto_apply_legacy import LINKEDIN_SESSION_DIR  # type: ignore[import]
            candidate = LINKEDIN_SESSION_DIR / "storage_state.json"
        except Exception:
            candidate = Path(__file__).resolve().parent / "data" / "linkedin_session" / "storage_state.json"
        if candidate.exists():
            storage_state_path = candidate
            print(f"[+] LinkedIn session loaded from {candidate}")
        else:
            print(f"[!] No LinkedIn session found at {candidate} — you may see a login wall")

    elif chosen and chosen.name == "indeed":
        from backend.auto_apply import INDEED_CHROME_PROFILE_DIR, _INDEED_SENTINEL
        sentinel = INDEED_CHROME_PROFILE_DIR / _INDEED_SENTINEL
        if INDEED_CHROME_PROFILE_DIR.is_dir() and sentinel.exists():
            profile_dir = INDEED_CHROME_PROFILE_DIR
            print(f"[+] Indeed persistent profile loaded from {INDEED_CHROME_PROFILE_DIR}")
        else:
            print(
                f"[!] No Indeed session found at {INDEED_CHROME_PROFILE_DIR} — "
                "run the Indeed login flow first (Settings → Connect Indeed)"
            )

    # ── Browser launch + DOM snapshot ─────────────────────────────────────
    async with BrowserController(
        config,
        storage_state_path=storage_state_path,
        profile_dir=profile_dir,
    ) as ctrl:
        print(f"[*] Navigating to {url} ...")
        try:
            await ctrl.navigate(url)
        except Exception as exc:
            print(f"[!] Navigation failed: {exc}")
            return

        print("[*] Taking DOM snapshot ...")
        fields = await ctrl.get_dom_snapshot()
        print(f"[+] Found {len(fields)} form fields\n")

        for i, f in enumerate(fields):
            print(f"  [{i:02d}] id={f.field_id!r:12s} type={f.field_type!r:10s} "
                  f"label={f.label!r:30s} required={f.required}")

        # ── LLM field mapping ──────────────────────────────────────────────
        print(f"\n[*] Sending {len(fields)} fields to LM Studio for mapping ...")
        llm = LLMClient(config)
        bank = get_answer_bank()
        bank_dict = {k: v for k, v in bank.all_snippets().items()
                     if not (v.startswith("<") and v.endswith(">"))}

        non_file = [f for f in fields if f.field_type != "file"]
        try:
            mappings = await llm.map_fields_to_values(profile, job, non_file, bank_dict)
        except Exception as exc:
            print(f"[!] LLM call failed: {exc}")
            mappings = []

        print(f"\n{'─'*60}")
        print("  LLM FIELD MAPPING RESULT")
        print(f"{'─'*60}")
        for m in mappings:
            conf_bar = "█" * int(m.confidence * 10) + "░" * (10 - int(m.confidence * 10))
            print(
                f"  {m.field_id:10s} {m.action:6s} conf=[{conf_bar}] {m.confidence:.2f} "
                f"src={m.source:15s} val={m.value[:50]!r}"
            )

        # ── Fill fields in autofill mode ───────────────────────────────────
        print(f"\n[*] Filling fields in AUTOFILL mode (no submit) ...")
        if chosen:
            result = await chosen.apply(ctrl, profile, job, llm, apply_mode, log)
        else:
            from backend.auto_apply.adapters.generic import GenericAdapter
            result = await GenericAdapter().apply(ctrl, profile, job, llm, apply_mode, log)

        # ── Report ─────────────────────────────────────────────────────────
        print(f"\n{'='*60}")
        print("  RESULT")
        print(f"{'='*60}")
        print(json.dumps(result.to_legacy_dict(), indent=2))
        print(f"\n  Fields filled:  {result.fields_filled}")
        print(f"  Fields skipped: {result.fields_skipped}")
        print(f"  Adapter used:   {result.adapter_used}")
        print(f"\n  Log entries ({len(log.entries)}):")
        for entry in log.entries:
            level = entry.get("level", "").upper()
            msg   = entry.get("message", "")
            print(f"    [{level:8s}] {msg}")

        print(f"\n[+] Debug run complete. Browser stays open for 10s ...")
        await asyncio.sleep(10)


def main():
    parser = argparse.ArgumentParser(description="Debug auto-apply orchestrator")
    parser.add_argument("url", help="Job application URL to test")
    parser.add_argument(
        "--mode", choices=["autofill", "submit"], default="autofill",
        help="Apply mode (default: autofill — always safe)"
    )
    parser.add_argument(
        "--no-headless", action="store_true",
        help="Show browser window (default when running debug_apply)"
    )
    args = parser.parse_args()

    # Default to visible browser for debugging
    headless = args.no_headless is False if hasattr(args, "no_headless") else False
    # Actually: --no-headless flag means show browser
    headless = False if args.no_headless else False
    # By default debug runs visible
    headless = False

    asyncio.run(run_debug(args.url, args.mode, headless=headless))


if __name__ == "__main__":
    main()
