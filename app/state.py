"""Tiny key/value store for background-job bookkeeping."""

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import AppState


def get(db: Session, key: str) -> str:
    row = db.get(AppState, key)
    return row.value if row else ""


def put(db: Session, key: str, value: str) -> None:
    db.execute(insert(AppState).values(key=key, value=value)
               .on_conflict_do_update(index_elements=["key"], set_={"value": value}))
