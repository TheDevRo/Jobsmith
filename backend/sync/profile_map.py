#!/usr/bin/env python3
# VENDORED from jobsmith-sync@19a5068 (reference/profile_map.py). Do not edit
# here — see backend/sync/VENDOR.md.
"""Reference profile normalization (see spec/PROFILE.md).

Maps the canonical profile payload to/from each side's native shape and
implements the base-overlay rule that keeps a typed client from dropping fields
it doesn't model. Like merge.py, this is the oracle the real Swift/Python
mappers must match. Dependency-free.

The desktop's native shape is already canonical snake_case (it's the config.yaml
`profile:` dict), so the only desktop transform is excluding the ATS-login
credentials. iOS is camelCase and models a subset, so it needs a real rename map
plus base-overlay to preserve the fields it doesn't carry.
"""
from __future__ import annotations

# Scalar canonical(snake) -> iOS(camel). Domain = the canonical keys iOS models.
CANON_TO_IOS_SCALAR = {
    "full_name": "fullName",
    "middle_name": "middleName",
    "email": "email",
    "phone": "phone",
    "location": "location",
    "street_address": "streetAddress",
    "street_address_2": "streetAddress2",
    "city": "city",
    "state": "state",
    "zip_code": "zipCode",
    "linkedin": "linkedin",
    "github": "github",
    "portfolio": "portfolio",
    "desired_salary": "desiredSalary",
    "work_authorization": "workAuthorization",
    "sponsorship_required": "sponsorshipRequired",
    "gender": "gender",
    "race_ethnicity": "raceEthnicity",
    "veteran_status": "veteranStatus",
    "disability_status": "disabilityStatus",
    "available_start": "availableStart",
    "notice_period": "noticePeriod",
    "summary": "summary",
}
CANON_TO_IOS_LIST = {"skills": "skills", "certifications": "certifications"}

EXP_CANON_TO_IOS = {
    "id": "id", "title": "title", "company": "company",
    "start_date": "startDate", "end_date": "endDate",
    "bullets": "bullets", "pinned": "pinned",
}
EDU_CANON_TO_IOS = {"id": "id", "degree": "degree", "school": "school", "year": "year"}
REF_CANON_TO_IOS = {
    "id": "id", "name": "name", "position": "position",
    "email": "email", "phone": "phone",
}

# Canonical keys iOS owns (emits on export). middle_name, street_address_2 and
# the EEO block are now iOS-owned scalars; only forward-compat keys iOS doesn't
# yet model are preserved via base.
IOS_OWNED_CANON_KEYS = (
    set(CANON_TO_IOS_SCALAR)
    | set(CANON_TO_IOS_LIST)
    | {"experience", "education", "references"}
)

# Desktop-only keys that must never enter a change record. Derived from the
# canonical settings registry (the single source of truth that replaced the two
# disagreeing secret lists): the profile-scoped folder-strip secrets, with the
# `profile.` prefix removed since this filter runs over the profile sub-dict. A
# guard test pins that every member is in settings_registry.secret_canonical_keys.
from . import settings_registry as _sr  # noqa: E402

SECRET_KEYS = frozenset(
    k[len("profile."):]
    for k in _sr.secret_canonical_keys()
    if k.startswith("profile.")
)


def _map_items(items, mapping):
    return [{mapping[k]: v for k, v in item.items() if k in mapping} for item in items]


def _invert(mapping):
    return {v: k for k, v in mapping.items()}


# ---- iOS side ---------------------------------------------------------------

def canonical_to_ios(canon: dict) -> dict:
    """Canonical -> iOS Profile JSON (only the fields iOS models)."""
    out: dict = {}
    for ck, ik in CANON_TO_IOS_SCALAR.items():
        if ck in canon:
            out[ik] = canon[ck]
    for ck, ik in CANON_TO_IOS_LIST.items():
        if ck in canon:
            out[ik] = list(canon[ck])
    if "experience" in canon:
        out["experience"] = _map_items(canon["experience"], EXP_CANON_TO_IOS)
    if "education" in canon:
        out["education"] = _map_items(canon["education"], EDU_CANON_TO_IOS)
    if "references" in canon:
        out["references"] = _map_items(canon["references"], REF_CANON_TO_IOS)
    return out


def ios_to_canonical(ios: dict, base: dict | None = None) -> dict:
    """iOS Profile JSON -> canonical, overlaid on `base` to preserve unmodeled fields."""
    result = dict(base or {})
    for ck, ik in CANON_TO_IOS_SCALAR.items():
        if ik in ios:
            result[ck] = ios[ik]
    for ck, ik in CANON_TO_IOS_LIST.items():
        if ik in ios:
            result[ck] = list(ios[ik])
    if "experience" in ios:
        result["experience"] = _map_items(ios["experience"], _invert(EXP_CANON_TO_IOS))
    if "education" in ios:
        result["education"] = _map_items(ios["education"], _invert(EDU_CANON_TO_IOS))
    if "references" in ios:
        result["references"] = _map_items(ios["references"], _invert(REF_CANON_TO_IOS))
    return result


# ---- Desktop side -----------------------------------------------------------

def desktop_to_canonical(cfg: dict) -> dict:
    """config.yaml profile dict -> canonical: drop the ATS-login credentials."""
    return {k: v for k, v in cfg.items() if k not in SECRET_KEYS}


def canonical_to_desktop(canon: dict, base: dict | None = None) -> dict:
    """Canonical -> config.yaml profile dict, overlaid on `base` to keep local secrets."""
    result = dict(base or {})
    result.update(canon)  # canonical never carries secrets, so overlay never clears them
    return result
