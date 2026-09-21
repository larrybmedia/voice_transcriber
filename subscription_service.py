from datetime import datetime, timedelta, timezone

from flask import current_app

from models import (
    Payment,
    Subscription,
    db,
)

# ============================================================
# PLAN CONFIGURATION
# ============================================================

PLAN_CONFIG = {
    "free": {
        "monthly": 0,
        "6_months": 0,
        "yearly": 0,
        "record": True,
        "meeting_record": False,
        "upload": False,
        "max_recording_minutes": 30,
        "daily_transcriptions": 2,
    },

    "gold": {
        "monthly": 5000,
        "6_months": 28000,
        "yearly": 57000,
        "record": True,
        "meeting_record": False,
        "upload": False,
        "max_recording_minutes": None,
        "daily_transcriptions": None,
    },

    "enterprise": {
        "monthly": 8500,
        "6_months": 48000,
        "yearly": 98000,
        "record": True,
        "meeting_record": True,
        "upload": True,
        "max_recording_minutes": None,
        "daily_transcriptions": None,
    },
}


# ============================================================
# PLAN HELPERS
# ============================================================

def get_plan_config(plan):
    """Return normalized configuration for a subscription plan."""

    return PLAN_CONFIG.get(
        (plan or "").lower(),
        {},
    )


def is_free_plan(user_id):
    """Return True if the user has an active Free subscription."""

    subscription = get_active_subscription(user_id)

    return bool(
        subscription
        and subscription.plan.lower() == "free"
    )


# ============================================================
# GET ACTIVE SUBSCRIPTION
# ============================================================

def get_active_subscription(user_id):
    """Return the user's currently active subscription."""

    subscriptions = (
        Subscription.query
        .filter_by(
            user_id=user_id,
            status="active",
        )
        .order_by(Subscription.id.desc())
        .all()
    )

    for subscription in subscriptions:
        if subscription.is_active():
            return subscription

    return None


# ============================================================
# CHECK PLAN PERMISSION
# ============================================================

def has_plan_permission(user_id, action):
    """
    Check whether the user's active subscription allows an action.

    Supported actions:
        record
        meeting_record
        upload
    """

    subscription = get_active_subscription(user_id)

    if not subscription:
        return False

    plan = subscription.plan.lower()

    config = PLAN_CONFIG.get(plan)

    if not config:
        return False

    return config.get(action, False)


# ============================================================
# CHECK ACTION
# ============================================================

def check_action(user_id, action):
    """
    Check whether the user's active subscription allows
    the requested action.

    Credits are no longer used.

    Supported actions:
        record
        meeting_record
        upload
    """

    if not has_plan_permission(
        user_id=user_id,
        action=action,
    ):
        return False, (
            "Your current subscription plan does not allow "
            f"the '{action}' feature."
        )

    return True, "Action authorized."


# ============================================================
# TRANSCRIPTION LIMIT
# ============================================================

def get_daily_transcription_limit(user_id):
    """
    Return the user's daily transcription limit.

    Free:
        2 transcriptions per day

    Gold:
        Unlimited

    Enterprise:
        Unlimited
    """

    subscription = get_active_subscription(user_id)

    if not subscription:
        return 0

    config = get_plan_config(
        subscription.plan
    )

    return config.get(
        "daily_transcriptions"
    )


# ============================================================
# FREE PLAN CHECK
# ============================================================

def check_free_transcription_limit(user_id, transcriptions_today):
    """
    Check whether a Free user can perform another
    transcription today.

    Gold and Enterprise users are unlimited.

    Returns:
        (True, message)
        (False, message)
    """

    subscription = get_active_subscription(user_id)

    if not subscription:
        return False, (
            "No active subscription was found."
        )

    plan = subscription.plan.lower()

    if plan != "free":
        return True, "Unlimited transcription available."

    daily_limit = get_daily_transcription_limit(
        user_id
    )

    if (
        daily_limit is not None
        and transcriptions_today >= daily_limit
    ):
        return False, (
            "Your Free plan allows 2 transcriptions "
            "per day. Please try again tomorrow or "
            "upgrade your plan."
        )

    return True, "Free transcription available."


# ============================================================
# PAYMENT SUCCESS → ACTIVATE SUBSCRIPTION
# ============================================================

def activate_subscription_from_payment(
    user_id,
    plan,
    billing_cycle,
    amount,
    payment_gateway,
    transaction_reference,
):
    """
    Record a successful payment and activate the
    corresponding subscription.

    This function should only be called after the
    payment has been verified as successful.
    """

    plan = (plan or "").strip().lower()
    billing_cycle = (
        (billing_cycle or "").strip().lower()
    )

    valid_plans = {
        "gold",
        "enterprise",
    }

    if plan not in valid_plans:
        raise ValueError(
            "Only Gold and Enterprise plans "
            "can be activated through payment."
        )

    valid_billing_cycles = {
        "monthly",
        "6_months",
        "yearly",
    }

    if billing_cycle not in valid_billing_cycles:
        raise ValueError(
            "Invalid billing cycle."
        )

    config = get_plan_config(plan)

    expected_amount = config.get(
        billing_cycle
    )

    if expected_amount is None:
        raise ValueError(
            "Invalid plan or billing cycle."
        )

    if int(amount) != int(expected_amount):
        raise ValueError(
            "Payment amount does not match "
            "the selected subscription plan."
        )

    existing_payment = Payment.query.filter_by(
        transaction_reference=transaction_reference
    ).first()

    if existing_payment:
        if existing_payment.user_id != user_id:
            raise ValueError(
                "This transaction reference belongs to another user."
            )

        if existing_payment.status == "successful":
            return (
                existing_payment,
                get_active_subscription(user_id),
            )

        if existing_payment.status != "pending":
            raise ValueError(
                "This transaction reference cannot be activated."
            )

        payment = existing_payment
    else:
        payment = None

    now = datetime.now(timezone.utc)

    if billing_cycle == "monthly":
        end_date = now + timedelta(days=30)

    elif billing_cycle == "6_months":
        end_date = now + timedelta(days=182)

    else:
        end_date = now + timedelta(days=365)

    subscription = get_active_subscription(
        user_id
    )

    if subscription:
        subscription.plan = plan
        subscription.billing_cycle = billing_cycle
        subscription.amount = expected_amount
        subscription.start_date = now
        subscription.end_date = end_date
        subscription.status = "active"
        subscription.payment_reference = (
            transaction_reference
        )

    else:
        subscription = Subscription(
            user_id=user_id,
            plan=plan,
            billing_cycle=billing_cycle,
            amount=expected_amount,
            start_date=now,
            end_date=end_date,
            status="active",
            payment_reference=(
                transaction_reference
            ),
        )

        db.session.add(subscription)
        db.session.flush()

    if payment is None:
        payment = Payment(
            user_id=user_id,
            subscription_id=subscription.id,
            plan=plan,
            billing_cycle=billing_cycle,
            amount=expected_amount,
            currency="NGN",
            payment_gateway=payment_gateway,
            transaction_reference=transaction_reference,
            status="successful",
            paid_at=now,
        )

        db.session.add(payment)

    else:
        payment.subscription_id = subscription.id
        payment.plan = plan
        payment.billing_cycle = billing_cycle
        payment.amount = expected_amount
        payment.currency = "NGN"
        payment.payment_gateway = payment_gateway
        payment.status = "successful"
        payment.paid_at = now

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    return payment, subscription