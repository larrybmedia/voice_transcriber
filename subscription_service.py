from datetime import datetime, timezone

from models import (
    Subscription,
)


# ============================================================
# PLAN CONFIGURATION
# ============================================================

PLAN_CONFIG = {
    "free": {
        "monthly": 0,
        "6_months": 0,
        "yearly": 0,
        "record": False,
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
        3 transcriptions per day

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
            "Your Free plan allows 3 transcriptions "
            "per day. Please try again tomorrow or "
            "upgrade your plan."
        )

    return True, "Free transcription available."

