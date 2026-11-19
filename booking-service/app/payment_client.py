import os
import httpx

PAYMENT_URL = os.getenv("PAYMENT_URL", "http://payment-service:8001")
PAYMENT_MODE = os.getenv("PAYMENT_MODE", "mock").lower()


def charge(amount: float, email: str) -> dict:
    """Legacy mock adapter, retained for isolated automated tests/demo mode."""
    r = httpx.post(f"{PAYMENT_URL}/pay", json={"amount": amount, "email": email}, timeout=5)
    r.raise_for_status()
    return r.json()


def create_order(amount: float, email: str, receipt: str = "") -> dict:
    r = httpx.post(f"{PAYMENT_URL}/orders", json={"amount": amount, "email": email, "receipt": receipt or None}, timeout=20)
    r.raise_for_status()
    return r.json()


def verify(order_id: str, payment_id: str, signature: str, expected_amount: float) -> dict:
    r = httpx.post(f"{PAYMENT_URL}/verify", json={"order_id": order_id, "payment_id": payment_id,
                  "signature": signature, "expected_amount": expected_amount}, timeout=20)
    r.raise_for_status()
    return r.json()


def refund(amount: float, reference: str) -> dict:
    r = httpx.post(f"{PAYMENT_URL}/refund", json={"amount": amount, "reference": reference}, timeout=20)
    r.raise_for_status()
    return r.json()
