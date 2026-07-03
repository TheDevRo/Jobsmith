"""
routers/ — One APIRouter module per functional area, assembled by main.py.

extension    /api/extension/*   token, packaged-zip downloads, active-job hint
jobs         /api/jobs/* (CRUD, fetch, refetch), /api/sources, /api/screenshots
pipeline     score / tailor / detect-apply-types / estimate-salaries batches
applications /api/applications/*
settings     /api/config, /api/settings/*, /api/onboarding/*, stats/activity
sessions     /api/linkedin/*, /api/indeed/*, /api/sessions/*
system       /api/health, /api/ai/*, /api/debug/*, /api/resumes/*, notifications
assist       /api/assist/*, /assist/launch/*, /assist-sidebar
answer_bank  /api/answer-bank/*, /api/webhooks/*
"""

from . import (  # noqa: F401
    answer_bank,
    applications,
    assist,
    extension,
    jobs,
    pipeline,
    sessions,
    settings,
    system,
)

ALL_ROUTERS = [
    extension.router,
    jobs.router,
    pipeline.router,
    applications.router,
    settings.router,
    sessions.router,
    system.router,
    assist.router,
    answer_bank.router,
]
