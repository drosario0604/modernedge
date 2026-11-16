"""Human review queue for low-confidence / held payroll items."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ReviewItem

router = APIRouter(prefix="/review", tags=["review"])


class ResolveBody(BaseModel):
    status: str = Field(default="resolved", pattern="^(resolved|rejected|pending)$")
    resolved_by: str = Field(default="analyst")
    resolution: dict[str, Any] = Field(default_factory=dict)
    note: Optional[str] = None


def _item_dict(item: ReviewItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "kind": item.kind,
        "status": item.status,
        "title": item.title,
        "reason": item.reason,
        "confidence": float(item.confidence) if item.confidence is not None else None,
        "source": item.source,
        "source_ref": item.source_ref,
        "payload": item.payload,
        "resolution": item.resolution,
        "resolved_at": item.resolved_at.isoformat() if item.resolved_at else None,
        "resolved_by": item.resolved_by,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


@router.get("")
def list_review_items(
    db: Session = Depends(get_db),
    status: Optional[str] = Query(default="pending"),
    kind: Optional[str] = Query(default=None),
) -> dict[str, Any]:
    stmt = select(ReviewItem).order_by(ReviewItem.id.desc())
    if status:
        stmt = stmt.where(ReviewItem.status == status)
    if kind:
        stmt = stmt.where(ReviewItem.kind == kind)
    items = list(db.scalars(stmt).all())
    return {"items": [_item_dict(i) for i in items], "count": len(items)}


@router.get("/{item_id}")
def get_review_item(item_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    item = db.get(ReviewItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Review item not found")
    return _item_dict(item)


@router.post("/{item_id}/resolve")
def resolve_review_item(
    item_id: int,
    body: ResolveBody,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item = db.get(ReviewItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Review item not found")
    item.status = body.status
    item.resolved_by = body.resolved_by
    item.resolved_at = datetime.now(timezone.utc)
    resolution = dict(body.resolution)
    if body.note:
        resolution["note"] = body.note
    item.resolution = resolution
    db.commit()
    db.refresh(item)
    return _item_dict(item)
