"""Chatbot endpoint grounded in DB / KPI data."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.chat.engine import answer_question
from app.db import get_db

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    message: str | None = Field(
        default=None,
        description="Alias for question (UI convenience)",
    )


@router.post("")
def chat(body: ChatRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    question = (body.question or body.message or "").strip()
    return answer_question(db, question)


@router.post("/ask")
def chat_ask(body: ChatRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    return chat(body, db)
