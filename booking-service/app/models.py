import uuid
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, Float, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from .database import Base


class Event(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    description = Column(String, default="")
    venue = Column(String, default="")
    date = Column(String, default="")
    price = Column(Float, default=0.0)
    capacity = Column(Integer, default=100)  # number of teams (or people for individual events)
    min_team_size = Column(Integer, default=1)
    max_team_size = Column(Integer, default=1)  # 1 = individual event
    category = Column(String, default="General")
    image_url = Column(String, default="")
    seat_rows = Column(Integer, default=0)  # 0 = no seat selection
    seat_cols = Column(Integer, default=0)
    tickets = relationship("Ticket", back_populates="event", cascade="all, delete-orphan")
    seat_assignments = relationship("SeatAssignment", foreign_keys="SeatAssignment.event_id",
                                    cascade="all, delete-orphan")
    waitlist = relationship("WaitlistEntry", back_populates="event", cascade="all, delete-orphan")


class Ticket(Base):
    __tablename__ = "tickets"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, unique=True, index=True, default=lambda: uuid.uuid4().hex[:12].upper())
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    student_name = Column(String, nullable=False)
    student_email = Column(String, nullable=False)
    payment_ref = Column(String)
    checked_in = Column(Integer, default=0)
    checked_in_at = Column(DateTime)
    team_name = Column(String)
    members = Column(Text)  # JSON list of the other team members: [{"name":..., "email":...}]
    user_id = Column(Integer)  # the student account that registered, if logged in
    status = Column(String, default="active")  # active | cancelled
    amount_paid = Column(Float)  # price after any coupon
    coupon_code = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    event = relationship("Event", back_populates="tickets")
    reminders = relationship("Reminder", back_populates="ticket", cascade="all, delete-orphan")
    scores = relationship("Score", back_populates="ticket", cascade="all, delete-orphan")
    seats = relationship("SeatAssignment", back_populates="ticket", foreign_keys="SeatAssignment.ticket_id",
                         cascade="all, delete-orphan")


class Reminder(Base):
    __tablename__ = "reminders"
    id = Column(Integer, primary_key=True, index=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id"), nullable=False)
    status = Column(String, default="sent")
    sent_at = Column(DateTime, default=datetime.utcnow)
    ticket = relationship("Ticket", back_populates="reminders")


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)  # stored lower-case
    password_hash = Column(String, nullable=False)
    role = Column(String, default="student")  # student | judge
    created_at = Column(DateTime, default=datetime.utcnow)


class WaitlistEntry(Base):
    __tablename__ = "waitlist"
    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False)
    status = Column(String, default="waiting")  # waiting | notified | joined
    created_at = Column(DateTime, default=datetime.utcnow)
    event = relationship("Event", back_populates="waitlist")


class Score(Base):
    __tablename__ = "scores"
    __table_args__ = (UniqueConstraint("ticket_id", "judge_id", name="one_score_per_judge"),)
    id = Column(Integer, primary_key=True, index=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id"), nullable=False)
    judge_id = Column(Integer, nullable=False)
    innovation = Column(Integer, default=0)
    technical = Column(Integer, default=0)
    presentation = Column(Integer, default=0)
    impact = Column(Integer, default=0)
    comment = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    ticket = relationship("Ticket", back_populates="scores")


class Coupon(Base):
    __tablename__ = "coupons"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, unique=True, index=True, nullable=False)  # stored upper-case
    percent = Column(Integer, nullable=False)
    max_uses = Column(Integer)  # None = unlimited
    uses = Column(Integer, default=0)
    active = Column(Integer, default=1)
    event_id = Column(Integer)  # None = works for every event


class SeatAssignment(Base):
    __tablename__ = "seat_assignments"
    __table_args__ = (UniqueConstraint("event_id", "seat", name="one_ticket_per_seat"),)
    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    ticket_id = Column(Integer, ForeignKey("tickets.id"), nullable=False)
    seat = Column(String, nullable=False)  # like "A5": row letter, then seat number
    ticket = relationship("Ticket", back_populates="seats", foreign_keys=[ticket_id])


class FoodStall(Base):
    __tablename__ = "food_stalls"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    description = Column(String, default="")
    emoji = Column(String, default="🍽️")
    is_open = Column(Integer, default=1)
    items = relationship("MenuItem", back_populates="stall", cascade="all, delete-orphan",
                         order_by="MenuItem.id")


class MenuItem(Base):
    __tablename__ = "menu_items"
    id = Column(Integer, primary_key=True, index=True)
    stall_id = Column(Integer, ForeignKey("food_stalls.id"), nullable=False)
    name = Column(String, nullable=False)
    price = Column(Float, default=0.0)
    available = Column(Integer, default=1)
    stock = Column(Integer)  # None = unlimited
    stall = relationship("FoodStall", back_populates="items")


class FoodOrder(Base):
    __tablename__ = "food_orders"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, unique=True, index=True, default=lambda: "F" + uuid.uuid4().hex[:5].upper())
    stall_id = Column(Integer, ForeignKey("food_stalls.id"), nullable=False)
    ticket_id = Column(Integer)  # optional: the fest ticket, so the stall knows the seat
    user_id = Column(Integer)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False)
    note = Column(String, default="")
    total = Column(Float, default=0.0)
    status = Column(String, default="placed")  # placed | preparing | ready | collected | cancelled
    payment_ref = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)
    stall = relationship("FoodStall")
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "order_items"
    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("food_orders.id"), nullable=False)
    item_id = Column(Integer)  # kept even if the menu item is later deleted
    name = Column(String, nullable=False)
    price = Column(Float, default=0.0)
    qty = Column(Integer, default=1)
    order = relationship("FoodOrder", back_populates="items")

class Wishlist(Base):
    __tablename__ = "wishlists"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("user_id", "event_id", name="one_wishlist_event_per_user"),)


class Feedback(Base):
    __tablename__ = "feedback"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    event_id = Column(Integer, ForeignKey("events.id"), nullable=False)
    rating = Column(Integer, nullable=False)
    comment = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("user_id", "event_id", name="one_feedback_per_user_event"),)


class Notification(Base):
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True)  # null means announcement for all students
    title = Column(String, nullable=False)
    message = Column(Text, nullable=False)
    read = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
