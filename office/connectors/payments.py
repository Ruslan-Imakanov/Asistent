"""Stripe Payment Links: agents create products with a real checkout link; the webhook records sales."""
from __future__ import annotations

import hashlib
import hmac
import os
import time

import httpx

API = "https://api.stripe.com/v1"


class StripeConnector:
    def __init__(self) -> None:
        self.key = os.environ.get("STRIPE_API_KEY", "")
        self.webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    @property
    def configured(self) -> bool:
        return bool(self.key)

    def _post(self, path: str, data: dict) -> dict:
        r = httpx.post(f"{API}/{path}", data=data, auth=(self.key, ""), timeout=30)
        r.raise_for_status()
        return r.json()

    def create_payment_link(self, name: str, description: str, price: float, currency: str,
                            product_id: int) -> str:
        product = self._post("products", {"name": name[:250], "description": description[:500],
                                          "metadata[office_product_id]": str(product_id)})
        price_obj = self._post("prices", {"product": product["id"], "currency": currency.lower(),
                                          "unit_amount": str(int(round(price * 100)))})
        link = self._post("payment_links", {"line_items[0][price]": price_obj["id"],
                                            "line_items[0][quantity]": "1",
                                            "metadata[office_product_id]": str(product_id)})
        return link["url"]

    def verify_webhook(self, payload: bytes, sig_header: str, tolerance: int = 300) -> bool:
        """Stripe-Signature: t=...,v1=... ; HMAC-SHA256 of 't.payload'."""
        if not self.webhook_secret or not sig_header:
            return False
        parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
        ts, sig = parts.get("t"), parts.get("v1")
        if not ts or not sig or abs(time.time() - int(ts)) > tolerance:
            return False
        expected = hmac.new(self.webhook_secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)
