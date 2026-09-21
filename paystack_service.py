import os

import httpx
from dotenv import load_dotenv


load_dotenv()


PAYSTACK_BASE_URL = "https://api.paystack.co"

PAYSTACK_SECRET_KEY = os.getenv(
    "PAYSTACK_SECRET_KEY"
)


def initialize_transaction(
    email,
    amount,
    reference,
    callback_url,
):
    """
    Initialize a Paystack transaction.

    Amount must be supplied in NGN naira and is
    converted to kobo before sending to Paystack.
    """

    if not PAYSTACK_SECRET_KEY:
        raise RuntimeError(
            "PAYSTACK_SECRET_KEY is not configured."
        )

    if amount <= 0:
        raise ValueError(
            "Payment amount must be greater than zero."
        )

    payload = {
        "email": email,
        "amount": int(amount) * 100,
        "reference": reference,
        "callback_url": callback_url,
        "currency": "NGN",
    }

    headers = {
        "Authorization": (
            f"Bearer {PAYSTACK_SECRET_KEY}"
        ),
        "Content-Type": "application/json",
    }

    response = httpx.post(
        f"{PAYSTACK_BASE_URL}/transaction/initialize",
        json=payload,
        headers=headers,
        timeout=30.0,
    )

    response.raise_for_status()

    data = response.json()

    if not data.get("status"):
        raise RuntimeError(
            data.get(
                "message",
                "Paystack transaction initialization failed."
            )
        )

    return data["data"]


def verify_transaction(reference):
    """
    Verify a Paystack transaction using its reference.

    Returns the verified Paystack transaction data.
    """

    if not PAYSTACK_SECRET_KEY:
        raise RuntimeError(
            "PAYSTACK_SECRET_KEY is not configured."
        )

    reference = (reference or "").strip()

    if not reference:
        raise ValueError(
            "Transaction reference is required."
        )

    headers = {
        "Authorization": (
            f"Bearer {PAYSTACK_SECRET_KEY}"
        ),
        "Content-Type": "application/json",
    }

    response = httpx.get(
        f"{PAYSTACK_BASE_URL}/transaction/verify/"
        f"{reference}",
        headers=headers,
        timeout=30.0,
    )

    response.raise_for_status()

    data = response.json()

    if not data.get("status"):
        raise RuntimeError(
            data.get(
                "message",
                "Paystack transaction verification failed."
            )
        )

    return data["data"]