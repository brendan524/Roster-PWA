"""Key register: numbered keys and remotes issued to Australian staff, accepted with a typed
signature, and returned when they leave."""

import re
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import mailer
from app.db import get_db
from app.deps import admin_staff, ready_staff
from app.models import KeyIssue, KeyItem, Staff

router = APIRouter(prefix="/api", tags=["keys"])

KINDS = {"key": "Key", "remote": "Remote"}
OPEN = ("pending", "held")
TERMS = (
    "I acknowledge that I have received the item(s) listed above, which remain the property of AB Group. "
    "I will keep them secure, will not copy, lend or give them to anyone, will report a loss or theft to "
    "AB Group straight away, and will return them when my employment ends or whenever AB Group asks."
)


def item_label(item: KeyItem) -> str:
    return f"{KINDS.get(item.kind, item.kind)} {item.number}" + (f" ({item.description})" if item.description else "")


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def issue_out(i: KeyIssue, item: KeyItem, names: dict[str, str]) -> dict:
    return {"id": i.id, "item_id": item.id, "item": item_label(item), "kind": item.kind, "number": item.number,
            "staff_id": i.staff_id, "name": names.get(i.staff_id, i.staff_id), "status": i.status,
            "issued_by": names.get(i.issued_by, i.issued_by), "issued_at": i.issued_at.isoformat() if i.issued_at else None,
            "accepted_at": i.accepted_at.isoformat() if i.accepted_at else None, "signed_name": i.signed_name,
            "closed_at": i.closed_at.isoformat() if i.closed_at else None,
            "closed_by": names.get(i.closed_by, i.closed_by) if i.closed_by else None, "note": i.note}


def outstanding_for(db: Session, staff_id: str) -> list[str]:
    """Labels of keys/remotes this person still has (or hasn't accepted yet)."""
    rows = (db.query(KeyIssue, KeyItem).join(KeyItem, KeyItem.id == KeyIssue.item_id)
            .filter(KeyIssue.staff_id == staff_id, KeyIssue.status.in_(OPEN)).all())
    return [item_label(item) for _i, item in rows]


# ---- staff ----------------------------------------------------------------------

@router.get("/keys/me")
def my_keys(staff: Staff = Depends(ready_staff), db: Session = Depends(get_db)):
    rows = (db.query(KeyIssue, KeyItem).join(KeyItem, KeyItem.id == KeyIssue.item_id)
            .filter(KeyIssue.staff_id == staff.staff_id).order_by(KeyIssue.issued_at.desc()).all())
    names = {s.staff_id: s.name for s in db.query(Staff.staff_id, Staff.name)}
    return {"terms": TERMS, "name": staff.name,
            "pending": [issue_out(i, it, names) for i, it in rows if i.status == "pending"],
            "held": [issue_out(i, it, names) for i, it in rows if i.status == "held"],
            "history": [issue_out(i, it, names) for i, it in rows if i.status not in OPEN]}


class AcceptIn(BaseModel):
    issue_ids: list[int] = Field(min_length=1, max_length=20)
    signed_name: str = Field(max_length=120)
    agree: bool


@router.post("/keys/me/accept")
def accept(body: AcceptIn, request: Request, staff: Staff = Depends(ready_staff), db: Session = Depends(get_db)):
    if not body.agree:
        raise HTTPException(400, "Tick the box to agree to the terms.")
    if _norm(body.signed_name) != _norm(staff.name):
        raise HTTPException(400, f"Type your full name exactly as it appears in the app: {staff.name}")
    issues = (db.query(KeyIssue).filter(KeyIssue.id.in_(body.issue_ids), KeyIssue.staff_id == staff.staff_id,
                                        KeyIssue.status == "pending").with_for_update().all())
    if len(issues) != len(set(body.issue_ids)):
        raise HTTPException(409, "Something changed — refresh and try again.")
    now = datetime.now(UTC)
    ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else ""))[:64]
    for i in issues:
        i.status, i.accepted_at, i.signed_name, i.accept_ip, i.terms = "held", now, body.signed_name.strip(), ip, TERMS
    db.commit()
    return {"ok": True}


# ---- admin ---------------------------------------------------------------------

@router.get("/admin/keys")
def register(_: Staff = Depends(admin_staff), db: Session = Depends(get_db)):
    staff = {s.staff_id: s for s in db.query(Staff)}
    names = {k: s.name for k, s in staff.items()}
    items = db.query(KeyItem).order_by(KeyItem.kind, KeyItem.number).all()
    issues = db.query(KeyIssue).order_by(KeyIssue.issued_at.desc()).all()
    by_item: dict[int, list[KeyIssue]] = {}
    for i in issues:
        by_item.setdefault(i.item_id, []).append(i)
    out_items = []
    for it in items:
        hist = by_item.get(it.id, [])
        current = next((i for i in hist if i.status in OPEN), None)
        out_items.append({"id": it.id, "kind": it.kind, "number": it.number, "description": it.description,
                          "label": item_label(it), "retired": it.retired,
                          "current": issue_out(current, it, names) if current else None,
                          "history": [issue_out(i, it, names) for i in hist if i.status not in OPEN][:10]})
    leavers = sorted({i.staff_id for i in issues if i.status in OPEN and i.staff_id in staff and not staff[i.staff_id].active})
    return {"items": out_items,
            "people": [{"staff_id": s.staff_id, "name": s.name} for s in sorted(staff.values(), key=lambda s: s.name.lower())
                       if s.active and s.location_type == "onshore"],
            "leavers": [{"staff_id": sid, "name": names[sid], "items": outstanding_for(db, sid)} for sid in leavers]}


class ItemIn(BaseModel):
    kind: str = Field(pattern="^(key|remote)$")
    number: str = Field(min_length=1, max_length=20)
    description: str = Field("", max_length=200)


@router.post("/admin/keys")
def add_item(body: ItemIn, _: Staff = Depends(admin_staff), db: Session = Depends(get_db)):
    it = KeyItem(kind=body.kind, number=body.number.strip(), description=body.description.strip())
    db.add(it)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"{KINDS[body.kind]} {body.number.strip()} is already in the register.") from None
    return {"id": it.id}


class IssueIn(BaseModel):
    item_ids: list[int] = Field(min_length=1, max_length=10)
    staff_id: str
    note: str = Field("", max_length=300)


@router.post("/admin/keys/issue")
def issue(body: IssueIn, admin: Staff = Depends(admin_staff), db: Session = Depends(get_db)):
    st = db.get(Staff, body.staff_id)
    if st is None or not st.active:
        raise HTTPException(404, "No such staff member.")
    if st.location_type != "onshore":
        raise HTTPException(400, "Keys are only issued to Australian staff.")
    items = db.query(KeyItem).filter(KeyItem.id.in_(body.item_ids)).with_for_update().all()
    if len(items) != len(set(body.item_ids)):
        raise HTTPException(404, "Key or remote not found.")
    for it in items:
        if it.retired:
            raise HTTPException(409, f"{item_label(it)} is retired.")
        if db.query(KeyIssue).filter(KeyIssue.item_id == it.id, KeyIssue.status.in_(OPEN)).first():
            raise HTTPException(409, f"{item_label(it)} is already out — mark it returned first.")
        db.add(KeyIssue(item_id=it.id, staff_id=st.staff_id, status="pending", issued_by=admin.staff_id, note=body.note.strip()))
    labels = ", ".join(item_label(it) for it in items)
    mailer.queue(db, st.email, f"Please accept: {labels}",
                 f"Hi {st.name.split()[0]},\n\nYou've been given {labels}. Please open the staff app and accept "
                 "it on the My shifts page (read the terms and sign with your name).")
    db.commit()
    return {"ok": True}


class CloseIn(BaseModel):
    outcome: str = Field(pattern="^(returned|lost|cancelled)$")
    note: str = Field("", max_length=300)


@router.post("/admin/keys/issues/{issue_id}/close")
def close(issue_id: int, body: CloseIn, admin: Staff = Depends(admin_staff), db: Session = Depends(get_db)):
    i = db.query(KeyIssue).filter_by(id=issue_id).with_for_update().first()
    if i is None:
        raise HTTPException(404, "Not found.")
    if i.status not in OPEN:
        raise HTTPException(409, f"Already {i.status}.")
    if body.outcome == "cancelled" and i.status != "pending":
        raise HTTPException(409, "It's been accepted — mark it returned instead.")
    i.status, i.closed_at, i.closed_by = body.outcome, datetime.now(UTC), admin.staff_id
    if body.note.strip():
        i.note = (i.note + " | " if i.note else "") + body.note.strip()[:200]
    if body.outcome == "lost":
        db.get(KeyItem, i.item_id).retired = True
    db.commit()
    return {"ok": True}


@router.post("/admin/keys/{item_id}/retire")
def retire(item_id: int, _: Staff = Depends(admin_staff), db: Session = Depends(get_db)):
    it = db.get(KeyItem, item_id)
    if it is None:
        raise HTTPException(404, "Not found.")
    if db.query(KeyIssue).filter(KeyIssue.item_id == it.id, KeyIssue.status.in_(OPEN)).first():
        raise HTTPException(409, "It's out with someone — mark it returned first.")
    it.retired = not it.retired
    db.commit()
    return {"ok": True, "retired": it.retired}
