"""
routers/prompts.py — Read/edit/reset the internal LLM prompts.

Overrides are stored in config.yaml under the top-level `prompts:` key
(only customized prompts are persisted; everything else falls back to the
defaults in prompt_registry so upstream prompt improvements keep flowing
to non-customized installs).
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import app_state as state
from .. import prompt_registry

logger = logging.getLogger(__name__)

router = APIRouter()


class PromptUpdate(BaseModel):
    template: str


def _prompt_payload(key: str, meta: dict, overrides: dict) -> dict:
    override = overrides.get(key)
    if not (isinstance(override, str) and override.strip()):
        override = None
    return {
        "key": key,
        "label": meta["label"],
        "group": meta["group"],
        "description": meta["description"],
        "variables": meta["variables"],
        "default": meta["default"],
        "override": override,
        "customized": override is not None,
    }


@router.get("/api/prompts")
async def list_prompts():
    """All internal prompts: metadata, default template, and any override."""
    cfg = state.load_config()
    overrides = cfg.get("prompts") or {}
    return {
        "prompts": [
            _prompt_payload(key, meta, overrides)
            for key, meta in prompt_registry.PROMPTS.items()
        ]
    }


@router.put("/api/prompts/{key}")
async def update_prompt(key: str, body: PromptUpdate):
    """Save a custom template for one prompt.

    Saving an empty template (or one identical to the default) removes the
    override instead, so the prompt tracks future default updates again.
    """
    meta = prompt_registry.PROMPTS.get(key)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Unknown prompt: {key}")

    template = body.template.replace("\r\n", "\n")
    cfg = state.load_config()
    prompts = cfg.get("prompts") or {}

    if not template.strip() or template.strip() == meta["default"].strip():
        prompts.pop(key, None)
        customized = False
    else:
        prompts[key] = template
        customized = True

    if prompts:
        cfg["prompts"] = prompts
    else:
        cfg.pop("prompts", None)
    state.save_config(cfg)

    # Placeholders referenced by the template that the code never supplies —
    # they would render as literal {text}. Surfaced as a warning in the GUI.
    known = set(meta["variables"])
    unknown = [
        name for name in prompt_registry.template_placeholders(template)
        if name not in known
    ]
    logger.info("Prompt %r %s", key, "customized" if customized else "reset to default")
    return {"key": key, "customized": customized, "unknown_variables": unknown}


@router.delete("/api/prompts/{key}")
async def reset_prompt(key: str):
    """Remove the override for one prompt, restoring the built-in default."""
    if key not in prompt_registry.PROMPTS:
        raise HTTPException(status_code=404, detail=f"Unknown prompt: {key}")
    cfg = state.load_config()
    prompts = cfg.get("prompts") or {}
    prompts.pop(key, None)
    if prompts:
        cfg["prompts"] = prompts
    else:
        cfg.pop("prompts", None)
    state.save_config(cfg)
    logger.info("Prompt %r reset to default", key)
    return {"key": key, "customized": False}
