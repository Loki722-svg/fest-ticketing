from typing import Optional
from pydantic import BaseModel, Field


class EventCreate(BaseModel):
    name: str
    description: str = ""
    venue: str = ""
    date: str = ""
    price: float = 0.0
    capacity: int = 100
    min_team_size: int = 1
    max_team_size: int = 1
    category: str = "General"
    image_url: str = ""
    seat_rows: int = 0   # 0 = no seat selection
    seat_cols: int = 0


class EventOut(EventCreate):
    id: int
    seats_left: int


class Member(BaseModel):
    name: str
    email: str = ""


class RegisterIn(BaseModel):
    event_id: int
    student_name: str          # the person registering (team leader for team events)
    student_email: str
    team_name: str = ""
    members: list[Member] = []  # the other team members (not the leader)
    coupon_code: str = ""
    seats: list[str] = []  # chosen seats, like ["A5"]; leave empty to get the best seats automatically
    razorpay_order_id: str = ""
    razorpay_payment_id: str = ""
    razorpay_signature: str = ""


class TicketOut(BaseModel):
    code: str
    event_id: int
    event_name: str
    student_name: str
    student_email: str
    checked_in: bool
    team_name: str = ""
    members: list[Member] = []
    team_size: int = 1
    checked_in_at: Optional[str] = None
    status: str = "active"
    amount_paid: Optional[float] = None
    coupon_code: str = ""
    event_date: str = ""
    event_venue: str = ""
    seats: list[str] = []


class LoginIn(BaseModel):
    username: str
    password: str


class SignupIn(BaseModel):
    name: str
    email: str
    password: str


class JudgeIn(SignupIn):
    pass


class WaitlistIn(BaseModel):
    name: str
    email: str


class ScoreIn(BaseModel):
    code: str
    innovation: int = Field(ge=0, le=10)
    technical: int = Field(ge=0, le=10)
    presentation: int = Field(ge=0, le=10)
    impact: int = Field(ge=0, le=10)
    comment: str = ""


class CouponIn(BaseModel):
    code: str
    percent: int
    max_uses: Optional[int] = None
    event_id: Optional[int] = None


class CouponCheck(BaseModel):
    code: str
    event_id: int


class SeatsIn(BaseModel):
    seats: list[str]


class StallIn(BaseModel):
    name: str
    description: str = ""
    emoji: str = "🍽️"


class StallUpdate(BaseModel):
    open: Optional[bool] = None


class ItemIn(BaseModel):
    name: str
    price: float
    stock: Optional[int] = None


class ItemUpdate(BaseModel):
    available: Optional[bool] = None
    price: Optional[float] = None
    stock: Optional[int] = None
    unlimited: bool = False


class OrderLine(BaseModel):
    item_id: int
    qty: int = Field(ge=1, le=10)


class OrderIn(BaseModel):
    name: str
    email: str
    stall_id: int
    items: list[OrderLine]
    ticket_code: str = ""
    note: str = ""
    razorpay_order_id: str = ""
    razorpay_payment_id: str = ""
    razorpay_signature: str = ""


class StatusIn(BaseModel):
    status: str
