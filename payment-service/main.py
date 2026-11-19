"""Razorpay payment adapter. Uses TEST keys by default; never handles card data."""
import hashlib
import hmac
import os
import uuid
from typing import Optional
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Fest Payment Service", version="2.0.0")
MODE = os.getenv("PAYMENT_MODE", "razorpay").lower()
KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
BASE = "https://api.razorpay.com/v1"


def configured():
    if not KEY_ID or not KEY_SECRET:
        raise HTTPException(503, "Razorpay is not configured. Add RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET to your .env file.")


class OrderIn(BaseModel):
    amount: float = Field(gt=0, le=1000000)
    email: str
    receipt: Optional[str] = None


class VerifyIn(BaseModel):
    order_id: str
    payment_id: str
    signature: str
    expected_amount: float = Field(gt=0)


class RefundIn(BaseModel):
    amount: float = Field(gt=0)
    reference: str


@app.get("/health")
def health():
    return {"status": "ok", "provider": "razorpay", "mode": MODE, "configured": bool(KEY_ID and KEY_SECRET)}


@app.get("/config")
def config():
    configured()
    return {"key_id": KEY_ID, "currency": "INR", "provider": "razorpay"}


@app.post("/orders")
def create_order(data: OrderIn):
    configured()
    amount_paise = int(round(data.amount * 100))
    payload = {"amount": amount_paise, "currency": "INR", "receipt": (data.receipt or "fest-" + uuid.uuid4().hex[:18])[:40],
               "notes": {"customer_email": data.email[:200], "platform": "fest-ticketing"}}
    try:
        with httpx.Client(timeout=15, auth=(KEY_ID, KEY_SECRET)) as client:
            r = client.post(f"{BASE}/orders", json=payload)
        r.raise_for_status()
        order = r.json()
        return {"id": order["id"], "amount": order["amount"], "currency": order["currency"],
                "key_id": KEY_ID, "provider": "razorpay"}
    except httpx.HTTPStatusError as exc:
        raise HTTPException(502, "Razorpay could not create the payment order. Check your test keys and account settings.") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Could not reach Razorpay. Please try again.") from exc


@app.post("/verify")
def verify_payment(data: VerifyIn):
    configured()
    expected = hmac.new(KEY_SECRET.encode(), f"{data.order_id}|{data.payment_id}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, data.signature):
        raise HTTPException(400, "Payment signature verification failed")
    try:
        with httpx.Client(timeout=15, auth=(KEY_ID, KEY_SECRET)) as client:
            order_r = client.get(f"{BASE}/orders/{data.order_id}")
            payment_r = client.get(f"{BASE}/payments/{data.payment_id}")
        order_r.raise_for_status(); payment_r.raise_for_status()
        order, payment = order_r.json(), payment_r.json()
        expected_paise = int(round(data.expected_amount * 100))
        if order.get("amount") != expected_paise or payment.get("amount") != expected_paise:
            raise HTTPException(400, "Payment amount does not match this purchase")
        if order.get("currency") != "INR" or payment.get("currency") != "INR":
            raise HTTPException(400, "Unexpected payment currency")
        if payment.get("order_id") != data.order_id or payment.get("status") not in ("captured", "authorized"):
            raise HTTPException(400, "Payment has not been completed")
        if payment.get("status") == "authorized":
            with httpx.Client(timeout=15, auth=(KEY_ID, KEY_SECRET)) as client:
                cap = client.post(f"{BASE}/payments/{data.payment_id}/capture", json={"amount": expected_paise, "currency": "INR"})
            cap.raise_for_status()
        return {"status": "success", "reference": data.payment_id, "order_id": data.order_id}
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Could not verify payment with Razorpay") from exc


@app.post("/refund")
def refund(data: RefundIn):
    configured()
    try:
        with httpx.Client(timeout=15, auth=(KEY_ID, KEY_SECRET)) as client:
            r = client.post(f"{BASE}/payments/{data.reference}/refund", json={"amount": int(round(data.amount * 100))})
        r.raise_for_status()
        result = r.json()
        return {"status": "refunded", "reference": result.get("id")}
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Razorpay refund could not be processed") from exc
