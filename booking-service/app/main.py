import asyncio
import csv
import io
import json
import re
import time
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from typing import Optional
import qrcode
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models, schemas, payment_client, auth, reminders, mailer, certificate, metrics
from .database import Base, engine, get_db, SessionLocal, migrate

Base.metadata.create_all(bind=engine)
migrate()

CRITERIA = ["innovation", "technical", "presentation", "impact"]  # each scored 0 to 10
ACTIVE = or_(models.Ticket.status.is_(None), models.Ticket.status != "cancelled")
admin_only = [Depends(auth.require_admin)]

SEED_EVENTS = [
    dict(name="Hackathon 24h", description="Build something in a day", category="Technical",
         venue="CS Block", date="2026-12-05", price=100, capacity=60, min_team_size=2, max_team_size=4),
    dict(name="Battle of Bands", description="Live music finals", category="Cultural",
         venue="Open Air Theatre", date="2026-12-06", price=150, capacity=96, seat_rows=8, seat_cols=12),
    dict(name="Tech Quiz", description="Team quiz on tech trivia", category="Technical",
         venue="Seminar Hall", date="2026-12-05", price=0, capacity=20, min_team_size=2, max_team_size=3,
         seat_rows=6, seat_cols=10),
    dict(name="Robo Soccer", description="Robots, a ball and a goal", category="Sports",
         venue="Sports Complex", date="2026-12-07", price=50, capacity=20, min_team_size=2, max_team_size=3),
]

SEED_STALLS = [
    ("Biryani Point", "Hot, fresh biryani", "🍛",
     [("Chicken Biryani", 120, 40), ("Veg Biryani", 100, 40), ("Raita", 15, None)]),
    ("Chai & Snacks", "Tea, samosas and more", "☕",
     [("Masala Chai", 20, None), ("Samosa", 25, None), ("Vada Pav", 30, 60)]),
    ("Fresh Juice Bar", "Cold drinks for hot days", "🥤",
     [("Mango Shake", 60, None), ("Cold Coffee", 70, None), ("Lime Soda", 30, None)]),
]
ORDER_FLOW = {"placed": {"preparing", "cancelled"}, "preparing": {"ready", "cancelled"}, "ready": {"collected"}}
SEAT_RE = re.compile(r"^([A-Z])([0-9]{1,2})$")


@asynccontextmanager
async def lifespan(_app):
    db = SessionLocal()
    try:
        if db.query(models.Event).count() == 0:
            db.add_all([models.Event(**e) for e in SEED_EVENTS])
            db.commit()
        if db.query(models.User).filter_by(role="judge").count() == 0:  # demo judge, delete it from Admin if unwanted
            db.add(models.User(name="Demo Judge", email="judge@fest.com", role="judge",
                               password_hash=auth.hash_password("judge123")))
            db.commit()
        if db.query(models.Coupon).count() == 0:
            db.add(models.Coupon(code="FEST20", percent=20))
            db.commit()
        if db.query(models.FoodStall).count() == 0:
            for name, desc, emoji, items in SEED_STALLS:
                stall = models.FoodStall(name=name, description=desc, emoji=emoji)
                stall.items = [models.MenuItem(name=n, price=price, stock=stock) for n, price, stock in items]
                db.add(stall)
            db.commit()
    finally:
        db.close()
    task = asyncio.create_task(reminders.reminder_loop())
    yield
    task.cancel()


app = FastAPI(title="College Fest Booking Service", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def count_requests(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    if request.url.path != "/metrics":
        route = request.scope.get("route")
        path = getattr(route, "path", None) or "unmatched"
        metrics.record(request.method, path, response.status_code, time.perf_counter() - start)
    return response


# ───────────────────────── helpers ─────────────────────────

def _active(ev: models.Event) -> list:
    return [t for t in ev.tickets if (t.status or "active") != "cancelled"]


def _paid(t: models.Ticket) -> float:
    return t.amount_paid if t.amount_paid is not None else (t.event.price or 0)


def _members(t: models.Ticket) -> list:
    try:
        return json.loads(t.members) if t.members else []
    except ValueError:
        return []


def _iso(dt):
    return dt.isoformat() + "Z" if dt else None  # stored in UTC


def _seated(ev: models.Event) -> bool:
    return (ev.seat_rows or 0) > 0 and (ev.seat_cols or 0) > 0


def _seat_key(label: str):
    m = SEAT_RE.match(label)
    return (m.group(1), int(m.group(2))) if m else (label, 0)


def _taken_seats(db: Session, event_id: int) -> set:
    return {row[0] for row in db.query(models.SeatAssignment.seat).filter_by(event_id=event_id).all()}


def _valid_seat(ev: models.Event, label: str) -> bool:
    m = SEAT_RE.match(label)
    return bool(m) and ord(m.group(1)) - 65 < ev.seat_rows and 1 <= int(m.group(2)) <= ev.seat_cols


def _auto_seats(ev: models.Event, taken: set, n: int) -> list:
    """Best seats: a block of n side by side, in the middle rows and near the centre. Otherwise any free seats."""
    rows, cols = ev.seat_rows, ev.seat_cols
    for r in sorted(range(rows), key=lambda x: abs(x - (rows - 1) / 2)):
        best = None
        for start in range(1, cols - n + 2):
            block = [f"{chr(65 + r)}{c}" for c in range(start, start + n)]
            if any(b in taken for b in block):
                continue
            dist = abs(start + (n - 1) / 2 - (cols + 1) / 2)
            if best is None or dist < best[0]:
                best = (dist, block)
        if best:
            return best[1]
    free = [f"{chr(65 + r)}{c}" for r in range(rows) for c in range(1, cols + 1) if f"{chr(65 + r)}{c}" not in taken]
    return free[:n] if len(free) >= n else []


def _event_out(ev: models.Event, db: Session) -> schemas.EventOut:
    sold = db.query(func.count(models.Ticket.id)).filter(models.Ticket.event_id == ev.id, ACTIVE).scalar()
    left = ev.capacity - sold
    if _seated(ev):  # a seated event can never have more spots than free seats
        used = db.query(func.count(models.SeatAssignment.id)).filter_by(event_id=ev.id).scalar()
        left = min(left, (ev.seat_rows * ev.seat_cols - used) // (ev.min_team_size or 1))
    return schemas.EventOut(
        id=ev.id, name=ev.name, description=ev.description or "", venue=ev.venue, date=ev.date,
        price=ev.price, capacity=ev.capacity, seats_left=max(left, 0),
        min_team_size=ev.min_team_size or 1, max_team_size=ev.max_team_size or 1,
        category=ev.category or "General", image_url=ev.image_url or "",
        seat_rows=ev.seat_rows or 0, seat_cols=ev.seat_cols or 0,
    )


def _ticket_out(t: models.Ticket) -> schemas.TicketOut:
    members = _members(t)
    return schemas.TicketOut(
        code=t.code, event_id=t.event_id, event_name=t.event.name,
        student_name=t.student_name, student_email=t.student_email,
        checked_in=bool(t.checked_in), team_name=t.team_name or "",
        members=[schemas.Member(**m) for m in members], team_size=1 + len(members),
        checked_in_at=_iso(t.checked_in_at), status=t.status or "active",
        amount_paid=t.amount_paid, coupon_code=t.coupon_code or "",
        event_date=t.event.date or "", event_venue=t.event.venue or "",
        seats=sorted((x.seat for x in t.seats), key=_seat_key),
    )


def _find_coupon(db: Session, code: str, ev: models.Event) -> Optional[models.Coupon]:
    code = (code or "").strip().upper()
    if not code:
        return None
    c = db.query(models.Coupon).filter(models.Coupon.code == code, models.Coupon.active == 1).first()
    if not c or (c.event_id and c.event_id != ev.id) or (c.max_uses and c.uses >= c.max_uses):
        raise HTTPException(400, "Invalid or expired coupon code")
    return c


def _final_price(ev: models.Event, coupon: Optional[models.Coupon]) -> float:
    price = float(ev.price or 0)
    return round(price * (100 - coupon.percent) / 100, 2) if coupon else price


def _valid_email(email: str) -> bool:
    return "@" in email and "." in email.split("@")[-1] and " " not in email


def _login_response(u: models.User) -> dict:
    token = auth.create_token(u.email, role=u.role, uid=u.id, name=u.name, email=u.email)
    return {"token": token, "role": u.role, "name": u.name, "email": u.email}


def _new_account(data: schemas.SignupIn, role: str, db: Session) -> models.User:
    email = data.email.strip().lower()
    if not data.name.strip():
        raise HTTPException(422, "Please enter a name")
    if not _valid_email(email):
        raise HTTPException(422, "Enter a valid email address")
    if len(data.password) < 6:
        raise HTTPException(422, "Password must be at least 6 characters")
    if db.query(models.User).filter(func.lower(models.User.email) == email).first():
        raise HTTPException(409, "An account with this email already exists")
    u = models.User(name=data.name.strip(), email=email, role=role,
                    password_hash=auth.hash_password(data.password))
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _safe_cell(value) -> str:
    """Stops spreadsheet formulas in exported CSV files."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def _promote_waitlist(db: Session, ev: models.Event) -> Optional[str]:
    """When a seat frees up, tell the first person waiting. Returns their name."""
    if _event_out(ev, db).seats_left <= 0:
        return None
    w = (db.query(models.WaitlistEntry).filter_by(event_id=ev.id, status="waiting")
         .order_by(models.WaitlistEntry.id).first())
    if not w:
        return None
    w.status = "notified"
    db.commit()
    mailer.send_email(w.email, f"A spot opened up: {ev.name}",
                      f"Hi {w.name},\n\nA spot is now free for {ev.name} on {ev.date} at {ev.venue}.\n"
                      "Open the fest website and register now. It is first come, first served.")
    return w.name


# ───────────────────────── public: events ─────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/events", response_model=list[schemas.EventOut])
def list_events(q: str = "", db: Session = Depends(get_db)):
    query = db.query(models.Event)
    if q:
        query = query.filter(models.Event.name.ilike(f"%{q}%"))
    return [_event_out(e, db) for e in query.all()]


@app.post("/events", response_model=schemas.EventOut, status_code=201, dependencies=admin_only)
def create_event(data: schemas.EventCreate, db: Session = Depends(get_db)):
    if data.min_team_size < 1 or data.max_team_size < data.min_team_size:
        raise HTTPException(400, "Team size is invalid: minimum must be at least 1 and not more than the maximum")
    if data.max_team_size > 20:
        raise HTTPException(400, "Maximum team size is 20")
    if data.capacity < 1:
        raise HTTPException(400, "Capacity must be at least 1")
    if data.seat_rows or data.seat_cols:
        if not (1 <= data.seat_rows <= 26 and 1 <= data.seat_cols <= 40):
            raise HTTPException(400, "Seating needs 1 to 26 rows and 1 to 40 seats per row")
    fields = data.model_dump()
    fields["category"] = (data.category or "General").strip() or "General"
    fields["image_url"] = data.image_url.strip()
    ev = models.Event(**fields)
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return _event_out(ev, db)


@app.delete("/events/{event_id}", status_code=204, dependencies=admin_only)
def delete_event(event_id: int, db: Session = Depends(get_db)):
    ev = db.get(models.Event, event_id)
    if not ev:
        raise HTTPException(404, "Event not found")
    db.delete(ev)
    db.commit()


# ───────────────────────── accounts ─────────────────────────

@app.post("/auth/signup", status_code=201)
def signup(data: schemas.SignupIn, db: Session = Depends(get_db)):
    return _login_response(_new_account(data, "student", db))


@app.post("/auth/login")
def login(data: schemas.LoginIn, db: Session = Depends(get_db)):
    if auth.check_credentials(data.username, data.password):
        return {"token": auth.create_token(data.username), "role": "admin", "name": "Organizer", "email": ""}
    u = db.query(models.User).filter(func.lower(models.User.email) == data.username.strip().lower()).first()
    if not u or not auth.verify_password(data.password, u.password_hash):
        raise HTTPException(401, "Wrong username or password")
    return _login_response(u)


@app.get("/auth/me")
def me(user: dict = Depends(auth.current_user)):
    return {"role": user.get("role", "admin"), "name": user.get("name", ""), "email": user.get("email", "")}


@app.get("/my/tickets", response_model=list[schemas.TicketOut])
def my_tickets(user: dict = Depends(auth.current_user), db: Session = Depends(get_db)):
    conds = []
    if user.get("uid") is not None:
        conds.append(models.Ticket.user_id == user["uid"])
    if user.get("email"):
        conds.append(func.lower(models.Ticket.student_email) == user["email"].lower())
    if not conds:
        return []
    rows = db.query(models.Ticket).filter(or_(*conds)).order_by(models.Ticket.id.desc()).all()
    return [_ticket_out(t) for t in rows]


# ───────────────────────── student profile and engagement features ─────────────────────────

@app.put("/auth/profile")
def update_profile(data: dict, user: dict = Depends(auth.require_student), db: Session = Depends(get_db)):
    account = db.get(models.User, user["uid"])
    if not account:
        raise HTTPException(404, "Account not found")
    name = str(data.get("name", account.name)).strip()
    if not name:
        raise HTTPException(422, "Name cannot be empty")
    account.name = name[:120]
    db.commit()
    return {"name": account.name, "email": account.email, "role": account.role}


@app.get("/my/wishlist")
def get_wishlist(user: dict = Depends(auth.require_student), db: Session = Depends(get_db)):
    rows = db.query(models.Wishlist).filter_by(user_id=user["uid"]).all()
    return [{"event_id": r.event_id, "event": db.get(models.Event, r.event_id).name}
            for r in rows if db.get(models.Event, r.event_id)]


@app.post("/my/wishlist/{event_id}", status_code=201)
def add_wishlist(event_id: int, user: dict = Depends(auth.require_student), db: Session = Depends(get_db)):
    if not db.get(models.Event, event_id):
        raise HTTPException(404, "Event not found")
    if not db.query(models.Wishlist).filter_by(user_id=user["uid"], event_id=event_id).first():
        db.add(models.Wishlist(user_id=user["uid"], event_id=event_id)); db.commit()
    return {"saved": True, "event_id": event_id}


@app.delete("/my/wishlist/{event_id}", status_code=204)
def remove_wishlist(event_id: int, user: dict = Depends(auth.require_student), db: Session = Depends(get_db)):
    db.query(models.Wishlist).filter_by(user_id=user["uid"], event_id=event_id).delete(); db.commit()


@app.post("/my/feedback", status_code=201)
def submit_feedback(data: dict, user: dict = Depends(auth.require_student), db: Session = Depends(get_db)):
    event_id, rating = int(data.get("event_id", 0)), int(data.get("rating", 0))
    if not db.get(models.Event, event_id): raise HTTPException(404, "Event not found")
    if not 1 <= rating <= 5: raise HTTPException(422, "Rating must be from 1 to 5")
    existing = db.query(models.Feedback).filter_by(user_id=user["uid"], event_id=event_id).first()
    if existing:
        existing.rating, existing.comment = rating, str(data.get("comment", ""))[:2000]
    else:
        db.add(models.Feedback(user_id=user["uid"], event_id=event_id, rating=rating,
                               comment=str(data.get("comment", ""))[:2000]))
    db.commit(); return {"saved": True}


@app.get("/my/notifications")
def my_notifications(user: dict = Depends(auth.require_student), db: Session = Depends(get_db)):
    rows = db.query(models.Notification).filter(or_(models.Notification.user_id == user["uid"],
        models.Notification.user_id.is_(None))).order_by(models.Notification.id.desc()).limit(100).all()
    return [{"id": n.id, "title": n.title, "message": n.message, "read": bool(n.read),
             "created_at": _iso(n.created_at)} for n in rows]


@app.post("/my/notifications/{notification_id}/read", status_code=204)
def read_notification(notification_id: int, user: dict = Depends(auth.require_student), db: Session = Depends(get_db)):
    n = db.query(models.Notification).filter(models.Notification.id == notification_id,
        or_(models.Notification.user_id == user["uid"], models.Notification.user_id.is_(None))).first()
    if not n: raise HTTPException(404, "Notification not found")
    n.read = 1; db.commit()


@app.post("/admin/announcements", status_code=201, dependencies=admin_only)
def create_announcement(data: dict, db: Session = Depends(get_db)):
    title, message = str(data.get("title", "")).strip(), str(data.get("message", "")).strip()
    if not title or not message: raise HTTPException(422, "Title and message are required")
    n = models.Notification(title=title[:160], message=message[:4000], user_id=None)
    db.add(n); db.commit(); db.refresh(n)
    return {"id": n.id, "title": n.title}


@app.get("/admin/feedback", dependencies=admin_only)
def admin_feedback(db: Session = Depends(get_db)):
    rows = db.query(models.Feedback).order_by(models.Feedback.id.desc()).all()
    return [{"event": db.get(models.Event, r.event_id).name if db.get(models.Event, r.event_id) else "Deleted event",
             "rating": r.rating, "comment": r.comment, "user_id": r.user_id, "created_at": _iso(r.created_at)} for r in rows]


@app.post("/admin/organizers", status_code=201, dependencies=admin_only)
def create_organizer(data: schemas.SignupIn, db: Session = Depends(get_db)):
    return _login_response(_new_account(data, "organizer", db))


@app.get("/admin/organizers", dependencies=admin_only)
def list_organizers(db: Session = Depends(get_db)):
    return [{"id": u.id, "name": u.name, "email": u.email} for u in db.query(models.User).filter_by(role="organizer").all()]


@app.delete("/admin/organizers/{organizer_id}", status_code=204, dependencies=admin_only)
def delete_organizer(organizer_id: int, db: Session = Depends(get_db)):
    u = db.get(models.User, organizer_id)
    if not u or u.role != "organizer": raise HTTPException(404, "Organizer not found")
    db.delete(u); db.commit()


# ───────────────────────── registration, cancel, waitlist ─────────────────────────

@app.post("/coupons/validate")
def validate_coupon(data: schemas.CouponCheck, db: Session = Depends(get_db)):
    ev = db.get(models.Event, data.event_id)
    if not ev:
        raise HTTPException(404, "Event not found")
    c = _find_coupon(db, data.code, ev)
    if not c:
        raise HTTPException(400, "Enter a coupon code")
    return {"valid": True, "percent": c.percent, "original_price": ev.price, "final_price": _final_price(ev, c)}


@app.post("/payments/order")
def create_payment_order(data: dict, user: dict = Depends(auth.require_student)):
    """Create a Razorpay order for the authenticated student; the purchase endpoint verifies it."""
    try:
        amount = round(float(data.get("amount", 0)), 2)
        email = str(user.get("email", "")).strip()
        if amount <= 0 or not _valid_email(email):
            raise HTTPException(422, "A positive amount and valid email are required")
        return payment_client.create_order(amount, email, str(data.get("receipt", "")))
    except HTTPException:
        raise
    except Exception as exc:
        detail = getattr(getattr(exc, "response", None), "json", lambda: {})()
        message = detail.get("detail") if isinstance(detail, dict) else None
        raise HTTPException(502, message or "Unable to start Razorpay checkout") from exc


@app.post("/register", response_model=schemas.TicketOut, status_code=201)
def register(data: schemas.RegisterIn, user: dict = Depends(auth.require_student),
             db: Session = Depends(get_db)):
    # The authenticated account is the source of truth for the team leader.
    data.student_name = user.get("name", "")
    data.student_email = user.get("email", "")
    ev = db.get(models.Event, data.event_id)
    if not ev:
        raise HTTPException(404, "Event not found")
    if _event_out(ev, db).seats_left <= 0:
        raise HTTPException(409, "Event is sold out")

    max_size, min_size = ev.max_team_size or 1, ev.min_team_size or 1
    members = [{"name": m.name.strip(), "email": m.email.strip()} for m in data.members]
    team_name = data.team_name.strip()
    if max_size > 1:
        if not team_name:
            raise HTTPException(422, "Team name is required for this event")
        if any(not m["name"] for m in members):
            raise HTTPException(422, "Every team member needs a name")
        if not (min_size <= 1 + len(members) <= max_size):
            raise HTTPException(422, f"Team size must be between {min_size} and {max_size} members (including you)")
    elif members:
        raise HTTPException(422, "This event is for individual participants only")

    seats = []
    if _seated(ev):
        need = 1 + len(members)
        taken = _taken_seats(db, ev.id)
        wanted = [x.strip().upper() for x in data.seats if x.strip()]
        if wanted:
            if len(set(wanted)) != len(wanted) or len(wanted) != need:
                raise HTTPException(422, f"Please choose exactly {need} different seat{'' if need == 1 else 's'}")
            for x in wanted:
                if not _valid_seat(ev, x):
                    raise HTTPException(422, f"Seat {x} does not exist")
                if x in taken:
                    raise HTTPException(409, f"Seat {x} was just taken, please choose another")
            seats = wanted
        else:
            seats = _auto_seats(ev, taken, need)
            if len(seats) < need:
                raise HTTPException(409, "Not enough seats are left for your group")
    elif data.seats:
        raise HTTPException(422, "This event has no seat selection")

    coupon = _find_coupon(db, data.coupon_code, ev)
    final_price = _final_price(ev, coupon)
    result = {"status": "success", "reference": None}
    if final_price > 0:
        try:
            if payment_client.PAYMENT_MODE == "mock":
                result = payment_client.charge(final_price, data.student_email)
                if result.get("status") != "success":
                    raise HTTPException(402, "Payment failed")
            else:
                if not (data.razorpay_order_id and data.razorpay_payment_id and data.razorpay_signature):
                    raise HTTPException(402, "Complete Razorpay checkout before registering")
                result = payment_client.verify(data.razorpay_order_id, data.razorpay_payment_id,
                                               data.razorpay_signature, final_price)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(402, "Payment verification failed. No ticket was created.") from exc

    ticket = models.Ticket(
        event_id=ev.id, student_name=data.student_name,
        student_email=data.student_email, payment_ref=result.get("reference"),
        team_name=team_name if max_size > 1 else None,
        members=json.dumps(members) if members else None,
        user_id=user.get("uid") if user else None,
        amount_paid=final_price, coupon_code=coupon.code if coupon else None,
    )
    try:
        db.add(ticket)
        for x in seats:
            ticket.seats.append(models.SeatAssignment(event_id=ev.id, seat=x))
        if coupon:
            coupon.uses = (coupon.uses or 0) + 1
        db.query(models.WaitlistEntry).filter(
            models.WaitlistEntry.event_id == ev.id,
            func.lower(models.WaitlistEntry.email) == data.student_email.strip().lower(),
            models.WaitlistEntry.status != "joined",
        ).update({"status": "joined"}, synchronize_session=False)
        db.commit()
    except IntegrityError:  # someone took a seat at the same moment: give the money back
        db.rollback()
        try:
            if result.get("reference"):
                payment_client.refund(final_price, result.get("reference"))
        except Exception:
            pass
        raise HTTPException(409, "Those seats were just taken, please choose again")
    db.refresh(ticket)
    return _ticket_out(ticket)


@app.post("/events/{event_id}/waitlist", status_code=201)
def join_waitlist(event_id: int, data: schemas.WaitlistIn, user: dict = Depends(auth.require_student),
                  db: Session = Depends(get_db)):
    data.name = user.get("name", "")
    data.email = user.get("email", "")
    ev = db.get(models.Event, event_id)
    if not ev:
        raise HTTPException(404, "Event not found")
    if _event_out(ev, db).seats_left > 0:
        raise HTTPException(409, "Spots are still available, you can register directly")
    email = data.email.strip().lower()
    if not data.name.strip() or not _valid_email(email):
        raise HTTPException(422, "Enter your name and a valid email")
    waiting = db.query(models.WaitlistEntry).filter(
        models.WaitlistEntry.event_id == ev.id, models.WaitlistEntry.status.in_(["waiting", "notified"])).all()
    if any(w.email.lower() == email for w in waiting):
        raise HTTPException(409, "You are already on the waitlist for this event")
    db.add(models.WaitlistEntry(event_id=ev.id, name=data.name.strip(), email=email))
    db.commit()
    return {"position": len(waiting) + 1}


@app.get("/tickets/{code}", response_model=schemas.TicketOut)
def get_ticket(code: str, db: Session = Depends(get_db)):
    t = db.query(models.Ticket).filter_by(code=code).first()
    if not t:
        raise HTTPException(404, "Ticket not found")
    return _ticket_out(t)


@app.get("/tickets/{code}/qr.png")
def ticket_qr(code: str, db: Session = Depends(get_db)):
    t = db.query(models.Ticket).filter_by(code=code).first()
    if not t:
        raise HTTPException(404, "Ticket not found")
    buf = io.BytesIO()
    qrcode.make(t.code).save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@app.post("/tickets/{code}/cancel")
def cancel_ticket(code: str, user: dict = Depends(auth.current_user), db: Session = Depends(get_db)):
    t = db.query(models.Ticket).filter_by(code=code).first()
    if not t:
        raise HTTPException(404, "Ticket not found")
    is_admin = user.get("role", "admin") == "admin"
    is_owner = (t.user_id is not None and t.user_id == user.get("uid")) or \
        bool(user.get("email") and t.student_email.lower() == user["email"].lower())
    if not (is_admin or is_owner):
        raise HTTPException(403, "You can only cancel your own tickets")
    if (t.status or "active") == "cancelled":
        raise HTTPException(409, "This ticket is already cancelled")
    if t.checked_in:
        raise HTTPException(409, "Checked-in tickets cannot be cancelled")
    amount = _paid(t)
    if amount > 0:
        try:
            payment_client.refund(amount, t.payment_ref)
        except Exception:
            raise HTTPException(502, "Refund failed, please try again")
    t.status = "cancelled"
    db.query(models.SeatAssignment).filter_by(ticket_id=t.id).delete()  # the seats become free again
    db.commit()
    return {"status": "cancelled", "refunded": amount, "waitlist_notified": _promote_waitlist(db, t.event)}


@app.get("/tickets/{code}/certificate.pdf")
def ticket_certificate(code: str, name: str = "", db: Session = Depends(get_db)):
    t = db.query(models.Ticket).filter_by(code=code).first()
    if not t or (t.status or "active") == "cancelled":
        raise HTTPException(404, "Ticket not found")
    if not t.checked_in:
        raise HTTPException(409, "The certificate is available after check-in")
    people = [t.student_name] + [m["name"] for m in _members(t)]
    who = t.student_name
    if name:
        match = [p for p in people if p.lower() == name.strip().lower()]
        if not match:
            raise HTTPException(404, "That name is not on this ticket")
        who = match[0]
    pdf = certificate.build_certificate(who, t.event.name, t.event.date or "", t.event.venue or "",
                                        t.team_name or "")
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="certificate-{t.code}.pdf"'})


# ───────────────────────── check-in (admin) ─────────────────────────

@app.post("/tickets/{code}/checkin", response_model=schemas.TicketOut, dependencies=admin_only)
def check_in(code: str, db: Session = Depends(get_db)):
    t = db.query(models.Ticket).filter_by(code=code).first()
    if not t:
        raise HTTPException(404, "Ticket not found")
    if (t.status or "active") == "cancelled":
        raise HTTPException(409, "This ticket was cancelled")
    if t.checked_in:
        who = t.student_name + (f", team {t.team_name}" if t.team_name else "")
        raise HTTPException(409, f"Ticket already used ({who})")
    t.checked_in = 1
    t.checked_in_at = datetime.utcnow()
    db.commit()
    return _ticket_out(t)


@app.get("/admin/checkins", dependencies=admin_only)
def checkins(limit: int = 100, db: Session = Depends(get_db)):
    """Everyone checked in so far, newest first."""
    rows = db.query(models.Ticket).filter(models.Ticket.checked_in == 1).all()
    rows.sort(key=lambda t: t.checked_in_at or datetime.min, reverse=True)
    return {"count": len(rows),
            "items": [{"code": t.code, "name": t.student_name, "email": t.student_email,
                       "team_name": t.team_name or "", "team_size": 1 + len(_members(t)),
                       "event": t.event.name, "event_id": t.event_id,
                       "seats": sorted((x.seat for x in t.seats), key=_seat_key),
                       "checked_in_at": _iso(t.checked_in_at)} for t in rows[:limit]]}


# ───────────────────────── admin: dashboard ─────────────────────────

@app.get("/admin/stats", dependencies=admin_only)
def stats(db: Session = Depends(get_db)):
    rows = []
    food = db.query(models.FoodOrder).filter(models.FoodOrder.status != "cancelled").all()
    for ev in db.query(models.Event).all():
        act = _active(ev)
        o = _event_out(ev, db)
        waiting = db.query(models.WaitlistEntry).filter(
            models.WaitlistEntry.event_id == ev.id, models.WaitlistEntry.status.in_(["waiting", "notified"])).count()
        rows.append({"id": ev.id, "event": ev.name, "registered": len(act), "seats_left": o.seats_left,
                     "seated": _seated(ev), "seat_rows": o.seat_rows, "seat_cols": o.seat_cols,
                     "revenue": sum(_paid(t) for t in act),
                     "team_event": o.max_team_size > 1,
                     "min_team_size": o.min_team_size, "max_team_size": o.max_team_size,
                     "people": sum(1 + len(_members(t)) for t in act),
                     "checked_in": sum(1 for t in act if t.checked_in),
                     "waitlist": waiting, "cancelled": len(ev.tickets) - len(act)})
    return {"events": rows,
            "total_registrations": sum(r["registered"] for r in rows),
            "total_teams": sum(r["registered"] for r in rows if r["team_event"]),
            "total_individuals": sum(r["registered"] for r in rows if not r["team_event"]),
            "total_people": sum(r["people"] for r in rows),
            "total_checked_in": sum(r["checked_in"] for r in rows),
            "total_waitlist": sum(r["waitlist"] for r in rows),
            "total_revenue": sum(r["revenue"] for r in rows),
            "food_orders": len(food), "food_revenue": round(sum(o.total for o in food), 2)}


@app.get("/admin/events/{event_id}/registrations", dependencies=admin_only)
def registrations(event_id: int, db: Session = Depends(get_db)):
    ev = db.get(models.Event, event_id)
    if not ev:
        raise HTTPException(404, "Event not found")
    return [{"name": t.student_name, "email": t.student_email, "code": t.code,
             "team_name": t.team_name or "", "members": _members(t), "team_size": 1 + len(_members(t)),
             "checked_in": bool(t.checked_in), "checked_in_at": _iso(t.checked_in_at),
             "reminders_sent": len(t.reminders), "status": t.status or "active",
             "amount_paid": _paid(t), "coupon_code": t.coupon_code or "",
             "seats": sorted((x.seat for x in t.seats), key=_seat_key)}
            for t in ev.tickets]


@app.post("/admin/events/{event_id}/remind", dependencies=admin_only)
def remind(event_id: int, db: Session = Depends(get_db)):
    ev = db.get(models.Event, event_id)
    if not ev:
        raise HTTPException(404, "Event not found")
    return reminders.send_reminders(db, ev)


@app.get("/admin/analytics", dependencies=admin_only)
def analytics(db: Session = Depends(get_db)):
    today = datetime.utcnow().date()
    days = [today - timedelta(days=i) for i in range(13, -1, -1)]
    per_day = {d: 0 for d in days}
    by_event, cancelled = [], 0
    for ev in db.query(models.Event).all():
        act = _active(ev)
        cancelled += len(ev.tickets) - len(act)
        for t in act:
            if t.created_at and t.created_at.date() in per_day:
                per_day[t.created_at.date()] += 1
        checked = sum(1 for t in act if t.checked_in)
        by_event.append({"event": ev.name, "registered": len(act), "revenue": sum(_paid(t) for t in act),
                         "checked_in": checked, "check_in_rate": round(100 * checked / len(act)) if act else 0})
    coupon_uses = db.query(func.coalesce(func.sum(models.Coupon.uses), 0)).scalar()
    return {"by_day": [{"date": d.isoformat(), "count": per_day[d]} for d in days],
            "by_event": by_event, "cancelled": cancelled, "coupon_uses": int(coupon_uses or 0)}


@app.get("/admin/export.csv", dependencies=admin_only)
def export_csv(db: Session = Depends(get_db)):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Event", "Ticket code", "Status", "Team", "Leader / participant", "Email", "Other members",
                "Paid", "Coupon", "Registered at (UTC)", "Checked in at (UTC)"])
    for t in db.query(models.Ticket).order_by(models.Ticket.id).all():
        others = "; ".join(m["name"] for m in _members(t))
        w.writerow([_safe_cell(x) for x in [
            t.event.name, t.code, t.status or "active", t.team_name or "", t.student_name, t.student_email,
            others, _paid(t), t.coupon_code or "", t.created_at.strftime("%Y-%m-%d %H:%M") if t.created_at else "",
            t.checked_in_at.strftime("%Y-%m-%d %H:%M") if t.checked_in_at else ""]])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="registrations.csv"'})


# ───────────────────────── admin: coupons and judges ─────────────────────────

@app.post("/admin/coupons", status_code=201, dependencies=admin_only)
def create_coupon(data: schemas.CouponIn, db: Session = Depends(get_db)):
    code = data.code.strip().upper()
    if not (3 <= len(code) <= 20) or not code.isalnum():
        raise HTTPException(422, "Coupon code must be 3 to 20 letters or numbers")
    if not (1 <= data.percent <= 100):
        raise HTTPException(422, "Discount must be between 1 and 100 percent")
    if data.max_uses is not None and data.max_uses < 1:
        raise HTTPException(422, "Maximum uses must be at least 1")
    if db.query(models.Coupon).filter_by(code=code).first():
        raise HTTPException(409, "That coupon code already exists")
    c = models.Coupon(code=code, percent=data.percent, max_uses=data.max_uses, event_id=data.event_id)
    db.add(c)
    db.commit()
    db.refresh(c)
    return {"id": c.id, "code": c.code, "percent": c.percent, "max_uses": c.max_uses, "uses": 0}


@app.get("/admin/coupons", dependencies=admin_only)
def list_coupons(db: Session = Depends(get_db)):
    return [{"id": c.id, "code": c.code, "percent": c.percent, "max_uses": c.max_uses, "uses": c.uses or 0}
            for c in db.query(models.Coupon).order_by(models.Coupon.id).all()]


@app.delete("/admin/coupons/{coupon_id}", status_code=204, dependencies=admin_only)
def delete_coupon(coupon_id: int, db: Session = Depends(get_db)):
    c = db.get(models.Coupon, coupon_id)
    if not c:
        raise HTTPException(404, "Coupon not found")
    db.delete(c)
    db.commit()


@app.post("/admin/judges", status_code=201, dependencies=admin_only)
def create_judge(data: schemas.JudgeIn, db: Session = Depends(get_db)):
    u = _new_account(data, "judge", db)
    return {"id": u.id, "name": u.name, "email": u.email}


@app.get("/admin/judges", dependencies=admin_only)
def list_judges(db: Session = Depends(get_db)):
    return [{"id": u.id, "name": u.name, "email": u.email}
            for u in db.query(models.User).filter_by(role="judge").order_by(models.User.id).all()]


@app.delete("/admin/judges/{judge_id}", status_code=204, dependencies=admin_only)
def delete_judge(judge_id: int, db: Session = Depends(get_db)):
    u = db.get(models.User, judge_id)
    if not u or u.role != "judge":
        raise HTTPException(404, "Judge not found")
    db.query(models.Score).filter_by(judge_id=u.id).delete()
    db.delete(u)
    db.commit()


# ───────────────────────── judging and leaderboard ─────────────────────────

def _avg_scores(t: models.Ticket) -> Optional[dict]:
    if not t.scores:
        return None
    return {c: round(sum(getattr(s, c) for s in t.scores) / len(t.scores), 2) for c in CRITERIA}


@app.get("/judge/events/{event_id}/teams")
def judge_teams(event_id: int, user: dict = Depends(auth.require_judge), db: Session = Depends(get_db)):
    ev = db.get(models.Event, event_id)
    if not ev:
        raise HTTPException(404, "Event not found")
    items = []
    for t in _active(ev):
        mine = next((s for s in t.scores if s.judge_id == user.get("uid")), None)
        my_scores = None
        if mine:
            my_scores = {c: getattr(mine, c) for c in CRITERIA}
            my_scores["comment"] = mine.comment or ""
        items.append({"code": t.code, "team": t.team_name or t.student_name, "leader": t.student_name,
                      "members": [m["name"] for m in _members(t)], "my_scores": my_scores})
    return {"event": ev.name, "criteria": CRITERIA, "items": items}


@app.put("/judge/scores")
def save_score(data: schemas.ScoreIn, user: dict = Depends(auth.require_judge), db: Session = Depends(get_db)):
    if user.get("role") != "judge" or user.get("uid") is None:
        raise HTTPException(403, "Only judges can score teams")
    t = db.query(models.Ticket).filter_by(code=data.code).first()
    if not t:
        raise HTTPException(404, "Team not found")
    if (t.status or "active") == "cancelled":
        raise HTTPException(409, "This team's registration was cancelled")
    s = db.query(models.Score).filter_by(ticket_id=t.id, judge_id=user["uid"]).first()
    if not s:
        s = models.Score(ticket_id=t.id, judge_id=user["uid"])
        db.add(s)
    for c in CRITERIA:
        setattr(s, c, getattr(data, c))
    s.comment = data.comment.strip()
    db.commit()
    return {"saved": True, "total": sum(getattr(data, c) for c in CRITERIA)}


@app.get("/events/{event_id}/leaderboard")
def leaderboard(event_id: int, db: Session = Depends(get_db)):
    ev = db.get(models.Event, event_id)
    if not ev:
        raise HTTPException(404, "Event not found")
    rows, pending = [], 0
    for t in _active(ev):
        avg = _avg_scores(t)
        if not avg:
            pending += 1
            continue
        rows.append({"team": t.team_name or t.student_name, "leader": t.student_name,
                     "avg": avg, "total": round(sum(avg.values()), 2), "judges": len(t.scores)})
    rows.sort(key=lambda r: (-r["total"], r["team"].lower()))
    for r in rows:
        r["rank"] = 1 + sum(1 for o in rows if o["total"] > r["total"])
    return {"event": ev.name, "criteria": CRITERIA, "max_total": 10 * len(CRITERIA),
            "items": rows, "pending": pending}


# ───────────────────────── monitoring ─────────────────────────

@app.get("/metrics")
def metrics_endpoint(db: Session = Depends(get_db)):
    tickets = db.query(models.Ticket).filter(ACTIVE).all()
    food = db.query(models.FoodOrder).filter(models.FoodOrder.status != "cancelled").all()
    gauges = {
        "fest_events_total": ("Events created", db.query(models.Event).count()),
        "fest_registrations_total": ("Active registrations (teams or individuals)", len(tickets)),
        "fest_checked_in_total": ("Registrations checked in", sum(1 for t in tickets if t.checked_in)),
        "fest_revenue_total": ("Revenue in rupees", sum(_paid(t) for t in tickets)),
        "fest_cancellations_total": ("Cancelled registrations",
                                     db.query(models.Ticket).filter(models.Ticket.status == "cancelled").count()),
        "fest_waitlist_waiting": ("People waiting for a spot",
                                  db.query(models.WaitlistEntry).filter_by(status="waiting").count()),
        "fest_accounts_total": ("Student and judge accounts", db.query(models.User).count()),
        "fest_seats_booked": ("Seats currently assigned", db.query(models.SeatAssignment).count()),
        "fest_food_orders_total": ("Food orders, not counting cancelled", len(food)),
        "fest_food_revenue_total": ("Food revenue in rupees", sum(o.total for o in food)),
        "fest_food_orders_open": ("Food orders being prepared",
                                  sum(1 for o in food if o.status in ("placed", "preparing", "ready"))),
    }
    return Response(content=metrics.render(gauges), media_type="text/plain; version=0.0.4")


# ───────────────────────── seat allotment ─────────────────────────

def _clean_seats(raw: list) -> list:
    return [x.strip().upper() for x in raw if x.strip()]


@app.get("/events/{event_id}/seats")
def event_seats(event_id: int, db: Session = Depends(get_db)):
    ev = db.get(models.Event, event_id)
    if not ev:
        raise HTTPException(404, "Event not found")
    return {"seated": _seated(ev), "rows": ev.seat_rows or 0, "cols": ev.seat_cols or 0,
            "taken": sorted(_taken_seats(db, ev.id), key=_seat_key)}


@app.get("/admin/events/{event_id}/seatmap", dependencies=admin_only)
def seat_map(event_id: int, db: Session = Depends(get_db)):
    ev = db.get(models.Event, event_id)
    if not ev:
        raise HTTPException(404, "Event not found")
    if not _seated(ev):
        raise HTTPException(409, "This event has no seat selection")
    seats = [{"seat": x.seat, "code": t.code, "name": t.team_name or t.student_name, "holder": t.student_name,
              "checked_in": bool(t.checked_in)} for t in _active(ev) for x in t.seats]
    return {"event": ev.name, "rows": ev.seat_rows, "cols": ev.seat_cols, "seats": seats}


@app.put("/admin/tickets/{code}/seats", response_model=schemas.TicketOut, dependencies=admin_only)
def reassign_seats(code: str, data: schemas.SeatsIn, db: Session = Depends(get_db)):
    t = db.query(models.Ticket).filter_by(code=code).first()
    if not t:
        raise HTTPException(404, "Ticket not found")
    ev = t.event
    if not _seated(ev):
        raise HTTPException(409, "This event has no seat selection")
    if (t.status or "active") == "cancelled":
        raise HTTPException(409, "This ticket was cancelled")
    need = 1 + len(_members(t))
    wanted = _clean_seats(data.seats)
    if len(set(wanted)) != len(wanted) or len(wanted) != need:
        raise HTTPException(422, f"This ticket needs exactly {need} different seat{'' if need == 1 else 's'}")
    taken = _taken_seats(db, ev.id) - {x.seat for x in t.seats}
    for x in wanted:
        if not _valid_seat(ev, x):
            raise HTTPException(422, f"Seat {x} does not exist")
        if x in taken:
            raise HTTPException(409, f"Seat {x} is already taken")
    try:
        for old in list(t.seats):
            t.seats.remove(old)
        db.flush()
        for x in wanted:
            t.seats.append(models.SeatAssignment(event_id=ev.id, seat=x))
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Those seats were just taken")
    db.refresh(t)
    return _ticket_out(t)


# ───────────────────────── food stalls and pre-orders ─────────────────────────

def _stall_out(s: models.FoodStall) -> dict:
    return {"id": s.id, "name": s.name, "description": s.description or "", "emoji": s.emoji or "🍽️",
            "open": bool(s.is_open),
            "items": [{"id": i.id, "name": i.name, "price": i.price, "stock": i.stock,
                       "available": bool(i.available) and (i.stock is None or i.stock > 0)} for i in s.items]}


def _order_out(db: Session, o: models.FoodOrder) -> dict:
    t = db.get(models.Ticket, o.ticket_id) if o.ticket_id else None
    return {"code": o.code, "stall_id": o.stall_id, "stall": o.stall.name, "stall_emoji": o.stall.emoji or "🍽️",
            "name": o.name, "email": o.email, "note": o.note or "", "total": o.total, "status": o.status,
            "items": [{"name": i.name, "price": i.price, "qty": i.qty} for i in o.items],
            "seats": sorted((x.seat for x in t.seats), key=_seat_key) if t else [],
            "created_at": _iso(o.created_at), "updated_at": _iso(o.updated_at)}


def _restore_stock(db: Session, o: models.FoodOrder) -> None:
    for line in o.items:
        item = db.get(models.MenuItem, line.item_id) if line.item_id else None
        if item and item.stock is not None:
            item.stock += line.qty


@app.get("/stalls")
def list_stalls(db: Session = Depends(get_db)):
    return [_stall_out(s) for s in db.query(models.FoodStall).order_by(models.FoodStall.id).all()]


@app.post("/orders", status_code=201)
def place_order(data: schemas.OrderIn, user: dict = Depends(auth.require_student),
                db: Session = Depends(get_db)):
    # Food orders are owned by the signed-in student, not a client-supplied identity.
    data.name = user.get("name", "")
    data.email = user.get("email", "")
    stall = db.get(models.FoodStall, data.stall_id)
    if not stall:
        raise HTTPException(404, "Stall not found")
    if not stall.is_open:
        raise HTTPException(409, "This stall is closed right now")
    email = data.email.strip()
    if not data.name.strip() or not _valid_email(email):
        raise HTTPException(422, "Enter your name and a valid email")
    if not data.items:
        raise HTTPException(422, "Add at least one item to your order")
    wanted = {}
    for line in data.items:
        wanted[line.item_id] = wanted.get(line.item_id, 0) + line.qty
    ticket = None
    if data.ticket_code.strip():
        ticket = db.query(models.Ticket).filter_by(code=data.ticket_code.strip().upper()).first()
        if not ticket or (ticket.status or "active") == "cancelled":
            raise HTTPException(404, "Ticket code not found")

    lines, total = [], 0.0
    for item_id, qty in wanted.items():
        item = db.get(models.MenuItem, item_id)
        if not item or item.stall_id != stall.id:
            raise HTTPException(400, "An item in your order is not on this stall's menu")
        if not item.available or (item.stock is not None and item.stock <= 0):
            raise HTTPException(409, f"{item.name} is sold out")
        if qty > 20:
            raise HTTPException(422, "You can order at most 20 of one item")
        if item.stock is not None and qty > item.stock:
            raise HTTPException(409, f"Only {item.stock} left of {item.name}")
        lines.append((item, qty))
        total += item.price * qty
    total = round(total, 2)

    for item, qty in lines:  # take the stock first, so two people cannot buy the last plate
        if item.stock is not None:
            taken = db.query(models.MenuItem).filter(models.MenuItem.id == item.id, models.MenuItem.stock >= qty) \
                .update({models.MenuItem.stock: models.MenuItem.stock - qty}, synchronize_session=False)
            if taken == 0:
                db.rollback()
                raise HTTPException(409, f"Sorry, {item.name} just ran out")
    if total > 0:
        try:
            if payment_client.PAYMENT_MODE == "mock":
                result = payment_client.charge(total, email)
                if result.get("status") != "success":
                    raise HTTPException(402, "Payment failed")
            else:
                if not (data.razorpay_order_id and data.razorpay_payment_id and data.razorpay_signature):
                    raise HTTPException(402, "Complete Razorpay checkout before placing the order")
                result = payment_client.verify(data.razorpay_order_id, data.razorpay_payment_id,
                                               data.razorpay_signature, total)
        except HTTPException:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            raise HTTPException(402, "Payment verification failed. No food order was created.") from exc
    else:
        result = {"reference": None}

    order = models.FoodOrder(stall_id=stall.id, ticket_id=ticket.id if ticket else None,
                             user_id=user.get("uid") if user else None, name=data.name.strip(), email=email,
                             note=data.note.strip()[:200], total=total, payment_ref=result.get("reference"))
    for item, qty in lines:
        order.items.append(models.OrderItem(item_id=item.id, name=item.name, price=item.price, qty=qty))
    db.add(order)
    db.commit()
    db.refresh(order)
    return _order_out(db, order)


@app.get("/orders/{code}")
def get_order(code: str, db: Session = Depends(get_db)):
    o = db.query(models.FoodOrder).filter_by(code=code.strip().upper()).first()
    if not o:
        raise HTTPException(404, "Order not found")
    return _order_out(db, o)


@app.get("/orders/{code}/qr.png")
def order_qr(code: str, db: Session = Depends(get_db)):
    o = db.query(models.FoodOrder).filter_by(code=code.strip().upper()).first()
    if not o:
        raise HTTPException(404, "Order not found")
    buf = io.BytesIO()
    qrcode.make(o.code).save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@app.get("/my/orders")
def my_orders(user: dict = Depends(auth.current_user), db: Session = Depends(get_db)):
    conds = []
    if user.get("uid") is not None:
        conds.append(models.FoodOrder.user_id == user["uid"])
    if user.get("email"):
        conds.append(func.lower(models.FoodOrder.email) == user["email"].lower())
    if not conds:
        return []
    rows = db.query(models.FoodOrder).filter(or_(*conds)).order_by(models.FoodOrder.id.desc()).all()
    return [_order_out(db, o) for o in rows]


def _cancel_order(db: Session, o: models.FoodOrder) -> None:
    if o.total > 0:
        try:
            payment_client.refund(o.total, o.payment_ref)
        except Exception:
            raise HTTPException(502, "Refund failed, please try again")
    _restore_stock(db, o)
    o.status = "cancelled"
    o.updated_at = datetime.utcnow()
    db.commit()


@app.post("/orders/{code}/cancel")
def cancel_order(code: str, email: str = "", user: Optional[dict] = Depends(auth.optional_user),
                 db: Session = Depends(get_db)):
    o = db.query(models.FoodOrder).filter_by(code=code.strip().upper()).first()
    if not o:
        raise HTTPException(404, "Order not found")
    is_admin = bool(user) and user.get("role", "admin") == "admin"
    mine = bool(user) and ((o.user_id is not None and o.user_id == user.get("uid")) or
                           bool(user.get("email") and o.email.lower() == user["email"].lower()))
    if not (is_admin or mine or (email and o.email.lower() == email.strip().lower())):
        raise HTTPException(403, "Only the person who ordered can cancel this order")
    if o.status != "placed":
        raise HTTPException(409, "The stall has already started on this order, so it cannot be cancelled")
    _cancel_order(db, o)
    return _order_out(db, o)


@app.get("/admin/orders", dependencies=admin_only)
def admin_orders(active: bool = False, db: Session = Depends(get_db)):
    query = db.query(models.FoodOrder).order_by(models.FoodOrder.id.desc())
    if active:
        query = query.filter(models.FoodOrder.status.in_(["placed", "preparing", "ready"]))
    return [_order_out(db, o) for o in query.limit(200).all()]


@app.put("/admin/orders/{code}/status", dependencies=admin_only)
def set_order_status(code: str, data: schemas.StatusIn, db: Session = Depends(get_db)):
    o = db.query(models.FoodOrder).filter_by(code=code.strip().upper()).first()
    if not o:
        raise HTTPException(404, "Order not found")
    new = data.status.strip().lower()
    if new not in ORDER_FLOW.get(o.status, set()):
        raise HTTPException(409, f"An order that is {o.status} cannot become {new}")
    if new == "cancelled":
        _cancel_order(db, o)
    else:
        o.status = new
        o.updated_at = datetime.utcnow()
        db.commit()
        if new == "ready":
            mailer.send_email(o.email, f"Your food order {o.code} is ready",
                              f"Hi {o.name},\n\nYour order from {o.stall.name} is ready. "
                              f"Show pickup code {o.code} at the stall.")
    return _order_out(db, o)


@app.post("/admin/stalls", status_code=201, dependencies=admin_only)
def create_stall(data: schemas.StallIn, db: Session = Depends(get_db)):
    if not data.name.strip():
        raise HTTPException(422, "Stall name is required")
    s = models.FoodStall(name=data.name.strip(), description=data.description.strip(),
                         emoji=data.emoji.strip() or "🍽️")
    db.add(s)
    db.commit()
    db.refresh(s)
    return _stall_out(s)


@app.put("/admin/stalls/{stall_id}", dependencies=admin_only)
def update_stall(stall_id: int, data: schemas.StallUpdate, db: Session = Depends(get_db)):
    s = db.get(models.FoodStall, stall_id)
    if not s:
        raise HTTPException(404, "Stall not found")
    if data.open is not None:
        s.is_open = 1 if data.open else 0
    db.commit()
    return _stall_out(s)


@app.delete("/admin/stalls/{stall_id}", status_code=204, dependencies=admin_only)
def delete_stall(stall_id: int, db: Session = Depends(get_db)):
    s = db.get(models.FoodStall, stall_id)
    if not s:
        raise HTTPException(404, "Stall not found")
    if db.query(models.FoodOrder).filter_by(stall_id=s.id).count():
        raise HTTPException(409, "This stall has orders. Close it instead of deleting it.")
    db.delete(s)
    db.commit()


@app.post("/admin/stalls/{stall_id}/items", status_code=201, dependencies=admin_only)
def add_item(stall_id: int, data: schemas.ItemIn, db: Session = Depends(get_db)):
    s = db.get(models.FoodStall, stall_id)
    if not s:
        raise HTTPException(404, "Stall not found")
    if not data.name.strip():
        raise HTTPException(422, "Item name is required")
    if data.price < 0 or (data.stock is not None and data.stock < 0):
        raise HTTPException(422, "Price and stock cannot be negative")
    s.items.append(models.MenuItem(name=data.name.strip(), price=data.price, stock=data.stock))
    db.commit()
    return _stall_out(s)


@app.put("/admin/items/{item_id}", dependencies=admin_only)
def update_item(item_id: int, data: schemas.ItemUpdate, db: Session = Depends(get_db)):
    item = db.get(models.MenuItem, item_id)
    if not item:
        raise HTTPException(404, "Item not found")
    if data.price is not None:
        if data.price < 0:
            raise HTTPException(422, "Price cannot be negative")
        item.price = data.price
    if data.available is not None:
        item.available = 1 if data.available else 0
    if data.unlimited:
        item.stock = None
    elif data.stock is not None:
        if data.stock < 0:
            raise HTTPException(422, "Stock cannot be negative")
        item.stock = data.stock
    db.commit()
    return _stall_out(item.stall)


@app.delete("/admin/items/{item_id}", status_code=204, dependencies=admin_only)
def delete_item(item_id: int, db: Session = Depends(get_db)):
    item = db.get(models.MenuItem, item_id)
    if not item:
        raise HTTPException(404, "Item not found")
    db.delete(item)
    db.commit()
