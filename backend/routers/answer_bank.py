"""
routers/answer_bank.py — CRUD for the answer bank (built-in snippets and
custom answers) plus the match tester.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auto_apply.answer_bank import get_answer_bank

logger = logging.getLogger(__name__)

router = APIRouter()


class AnswerBankSetRequest(BaseModel):
    key: str
    value: str


class CustomAnswerRequest(BaseModel):
    key: str
    label: str
    keywords: list[str]
    value: str


class TestMatchRequest(BaseModel):
    question: str


@router.get("/api/answer-bank")
async def get_answer_bank_snippets():
    """Return all answer bank entries (built-in snippets + custom answers)."""
    bank = get_answer_bank()
    return {
        "snippets": bank.all_snippets(),
        "custom": bank.get_custom_answers(),
    }


@router.post("/api/answer-bank")
async def set_answer_bank_snippet(body: AnswerBankSetRequest):
    """Set a built-in answer bank snippet value."""
    bank = get_answer_bank()
    bank.set(body.key, body.value)
    return {"message": f"Saved snippet for '{body.key}'"}


@router.delete("/api/answer-bank/{key}")
async def delete_answer_bank_snippet(key: str):
    """Delete a built-in answer bank snippet (resets it to seed placeholder)."""
    bank = get_answer_bank()
    deleted = bank.delete(key)
    if not deleted:
        raise HTTPException(404, f"Key '{key}' not found in answer bank")
    return {"message": f"Deleted snippet for '{key}'"}


@router.post("/api/answer-bank/custom")
async def set_custom_answer(body: CustomAnswerRequest):
    """Add or update a custom answer bank entry."""
    bank = get_answer_bank()
    bank.set_custom_answer(body.key, body.label, body.keywords, body.value)
    return {"message": f"Saved custom answer '{body.key}'"}


@router.delete("/api/answer-bank/custom/{key}")
async def delete_custom_answer(key: str):
    """Delete a custom answer bank entry."""
    bank = get_answer_bank()
    deleted = bank.delete_custom_answer(key)
    if not deleted:
        raise HTTPException(404, f"Custom key '{key}' not found")
    return {"message": f"Deleted custom answer '{key}'"}


@router.post("/api/answer-bank/test-match")
async def test_answer_bank_match(body: TestMatchRequest):
    """Test whether a question would match an answer bank entry."""
    bank = get_answer_bank()
    result = bank.score_question(body.question)
    return result
