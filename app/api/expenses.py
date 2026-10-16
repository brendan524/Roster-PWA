"""Expense claims: anyone submits with receipts; only the approvers (EXPENSE_APPROVERS) decide.
Approved claims are paid through the next pay report."""

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import mailer
from app.api.leave import MAX_ATTACHMENT_BYTES, MAX_ATTACHMENTS, _safe_filename, sniff_type, today
from app.config import get_settings
from app.db import get_db
from app.deps import admin_staff, ready_staff
from app.models import ExpenseAttachment, ExpenseClaim, Staff

router = APIRouter(prefix="/api", tags=["expenses"])

CATEGORIES = {"parking": "Parking / tolls", "travel": "Accommodation & meals (travel)",
              "equipment": "Equipment / supplies", "other": "Other"}
MAX_AGE_DAYS = 90
MAX_AMOUNT = Decimal("10000")


def approvers() -> set[str]:
    return {x.strip().upper() for x in get_settings().expense_approvers.split(",") if x.strip()}


def currency_for(staff: Staff) -> str:
    return "PHP" if staff.location_type == "offshore" else "AUD"


def money(amount, currency: str) -> str:
    return f"{'₱' if currency == 'PHP' else '$'}{Decimal(amount):,.2f}"


def _files(db: Session, ids: list[int]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    if ids:
        for a in db.query(ExpenseAttachment.id, ExpenseAttachment.claim_id, ExpenseAttachment.filename,
                          ExpenseAttachment.content_type, ExpenseAttachment.size).filter(ExpenseAttachment.claim_id.in_(ids)):
            out.setdefault(a.claim_id, []).append({"id": a.id, "filename": a.filename, "content_type": a.content_type,
                                                   "size": a.size})
    return out


def claim_out(c: ExpenseClaim, names: dict[str, str], files: dict[int, list[dict]]) -> dict:
    return {"id": c.id, "staff_id": c.staff_id, "name": names.get(c.staff_id, c.staff_id),
            "expense_date": c.expense_date.isoformat(), "category": c.category,
            "category_label": CATEGORIES.get(c.category, c.category), "amount": float(c.amount),
            "currency": c.currency, "amount_text": money(c.amount, c.currency), "description": c.description,
            "status": c.status, "created_at": c.created_at.isoformat() if c.created_at else None,
            "decided_by": names.get(c.decided_by, c.decided_by) if c.decided_by else None,
            "decided_at": c.decided_at.isoformat() if c.decided_at else None, "decision_note": c.decision_note,
            "paid_in_period": c.paid_in_period.isoformat() if c.paid_in_period else None,
            "attachments": files.get(c.id, [])}


def _names(db: Session) -> dict[str, str]:
    return {s.staff_id: s.name for s in db.query(Staff.staff_id, Staff.name)}


@router.get("/expenses/me")
def my_expenses(staff: Staff = Depends(ready_staff), db: Session = Depends(get_db)):
    claims = (db.query(ExpenseClaim).filter_by(staff_id=staff.staff_id)
              .order_by(ExpenseClaim.expense_date.desc(), ExpenseClaim.id.desc()).limit(50).all())
    names, files = _names(db), _files(db, [x.id for x in claims])
    return {"today": today().isoformat(), "currency": currency_for(staff), "max_age_days": MAX_AGE_DAYS,
            "categories": [{"code": k, "label": v} for k, v in CATEGORIES.items()],
            "can_approve": staff.staff_id in approvers(),
            "claims": [claim_out(c, names, files) for c in claims]}


class ClaimIn(BaseModel):
    expense_date: date
    category: str
    amount: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    description: str = Field("", max_length=500)


@router.post("/expenses")
def submit(body: ClaimIn, staff: Staff = Depends(ready_staff), db: Session = Depends(get_db)):
    if body.category not in CATEGORIES:
        raise HTTPException(400, "Choose a category.")
    if len(body.description.strip()) < 3:
        raise HTTPException(400, "Please say what it was for.")
    if body.amount > MAX_AMOUNT:
        raise HTTPException(400, f"That's more than {money(MAX_AMOUNT, currency_for(staff))} — talk to Brendan.")
    if body.expense_date > today():
        raise HTTPException(400, "The date can't be in the future.")
    if body.expense_date < today() - timedelta(days=MAX_AGE_DAYS):
        raise HTTPException(400, f"Claims must be made within {MAX_AGE_DAYS} days.")
    c = ExpenseClaim(staff_id=staff.staff_id, expense_date=body.expense_date, category=body.category,
                     amount=body.amount, currency=currency_for(staff), description=body.description.strip(),
                     status="pending")
    db.add(c)
    db.flush()
    for a in db.query(Staff).filter(Staff.staff_id.in_(approvers())):
        if a.email and a.staff_id != staff.staff_id:
            mailer.queue(db, a.email, f"Expense claim: {staff.name} — {money(c.amount, c.currency)}",
                         f"{staff.name} ({staff.staff_id}) has claimed {money(c.amount, c.currency)}:\n\n"
                         f"  {CATEGORIES[c.category]}, {c.expense_date:%a %-d %b %Y}\n  {c.description}\n\n"
                         "Check the receipt and approve or decline it in the staff app (Leave → Expenses).")
    db.commit()
    return claim_out(c, _names(db), {})


@router.post("/expenses/{claim_id}/cancel")
def cancel(claim_id: int, staff: Staff = Depends(ready_staff), db: Session = Depends(get_db)):
    c = db.query(ExpenseClaim).filter_by(id=claim_id, staff_id=staff.staff_id).with_for_update().first()
    if c is None:
        raise HTTPException(404, "Claim not found.")
    if c.status != "pending":
        raise HTTPException(409, f"This claim was already {c.status}.")
    c.status = "cancelled"
    db.commit()
    return {"ok": True}


@router.post("/expenses/{claim_id}/attachments")
async def add_receipt(claim_id: int, request: Request, staff: Staff = Depends(ready_staff), db: Session = Depends(get_db)):
    c = db.query(ExpenseClaim).filter_by(id=claim_id, staff_id=staff.staff_id).first()
    if c is None:
        raise HTTPException(404, "Claim not found.")
    if c.status != "pending":
        raise HTTPException(409, f"This claim was {c.status}, so files can't be added.")
    if db.query(ExpenseAttachment).filter_by(claim_id=c.id).count() >= MAX_ATTACHMENTS:
        raise HTTPException(409, f"A claim can have at most {MAX_ATTACHMENTS} files.")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(413, "That file is too big (10 MB maximum). Try a smaller photo or a PDF.")
    data = bytes(body)
    if not data:
        raise HTTPException(400, "The file is empty.")
    ctype = sniff_type(data)
    if ctype is None:
        raise HTTPException(415, "Only PDFs and photos (JPG, PNG, HEIC, WEBP) can be attached.")
    att = ExpenseAttachment(claim_id=c.id, filename=_safe_filename(request.headers.get("x-filename", ""), ctype),
                            content_type=ctype, size=len(data), sha256=hashlib.sha256(data).hexdigest(), data=data)
    db.add(att)
    db.commit()
    return {"id": att.id, "filename": att.filename, "content_type": ctype, "size": att.size}


@router.get("/expenses/attachments/{attachment_id}")
def get_receipt(attachment_id: int, staff: Staff = Depends(ready_staff), db: Session = Depends(get_db)):
    att = db.get(ExpenseAttachment, attachment_id)
    c = db.get(ExpenseClaim, att.claim_id) if att else None
    if att is None or c is None or (c.staff_id != staff.staff_id and not staff.is_admin):
        raise HTTPException(404, "File not found.")
    inline = att.content_type in ("application/pdf", "image/jpeg", "image/png", "image/webp")
    return Response(att.data, media_type=att.content_type, headers={
        "Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{att.filename}"',
        "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox",
        "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


@router.delete("/expenses/attachments/{attachment_id}")
def delete_receipt(attachment_id: int, staff: Staff = Depends(ready_staff), db: Session = Depends(get_db)):
    att = db.get(ExpenseAttachment, attachment_id)
    c = db.get(ExpenseClaim, att.claim_id) if att else None
    if att is None or c is None or c.staff_id != staff.staff_id:
        raise HTTPException(404, "File not found.")
    if c.status != "pending":
        raise HTTPException(409, "Files can only be removed while the claim is waiting for approval.")
    db.delete(att)
    db.commit()
    return {"ok": True}


# ---- admin ----------------------------------------------------------------------

@router.get("/admin/expenses")
def admin_expenses(admin: Staff = Depends(admin_staff), db: Session = Depends(get_db)):
    pending = db.query(ExpenseClaim).filter_by(status="pending").order_by(ExpenseClaim.created_at).all()
    since = datetime.now(UTC) - timedelta(days=60)
    recent = (db.query(ExpenseClaim).filter(ExpenseClaim.status.in_(("approved", "declined")),
                                            ExpenseClaim.decided_at >= since)
              .order_by(ExpenseClaim.decided_at.desc()).limit(100).all())
    names, files = _names(db), _files(db, [c.id for c in pending + recent])
    return {"can_approve": admin.staff_id in approvers(),
            "approver_names": sorted(names.get(a, a) for a in approvers()),
            "pending": [claim_out(c, names, files) for c in pending],
            "recent": [claim_out(c, names, files) for c in recent]}


class DecideIn(BaseModel):
    decision: str = Field(pattern="^(approve|decline)$")
    note: str = Field("", max_length=500)


@router.post("/admin/expenses/{claim_id}/decide")
def decide(claim_id: int, body: DecideIn, admin: Staff = Depends(admin_staff), db: Session = Depends(get_db)):
    if admin.staff_id not in approvers():
        raise HTTPException(403, "Only Brendan can approve expense claims.")
    c = db.query(ExpenseClaim).filter_by(id=claim_id).with_for_update().first()
    if c is None:
        raise HTTPException(404, "Claim not found.")
    if c.status != "pending":
        raise HTTPException(409, f"This claim was already {c.status}.")
    if c.staff_id == admin.staff_id:
        raise HTTPException(403, "You can't approve your own claim.")
    if body.decision == "approve" and not db.query(ExpenseAttachment).filter_by(claim_id=c.id).count():
        raise HTTPException(409, "There's no receipt attached yet.")
    if body.decision == "decline" and len(body.note.strip()) < 3:
        raise HTTPException(400, "Please give a reason for declining.")
    c.status = "approved" if body.decision == "approve" else "declined"
    c.decided_by, c.decided_at, c.decision_note = admin.staff_id, datetime.now(UTC), body.note.strip()
    staff = db.get(Staff, c.staff_id)
    word = "approved" if c.status == "approved" else "declined"
    mailer.queue(db, staff.email, f"Your expense claim was {word}",
                 f"Hi {staff.name.split()[0]},\n\n{admin.name} {word} your claim for {money(c.amount, c.currency)} "
                 f"({CATEGORIES[c.category].lower()}, {c.expense_date:%a %-d %b})."
                 + ("\n\nIt will be paid with your next pay." if c.status == "approved" else "")
                 + (f"\n\nNote: {c.decision_note}" if c.decision_note else ""))
    db.commit()
    return claim_out(c, _names(db), _files(db, [c.id]))
