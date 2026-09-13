from datetime import datetime, timezone

from models import (
    db,
    Subscription,
    UserCredit,
    CreditTransaction,
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
        "max_recording_minutes": 10,
        "daily_transcriptions": 1,
    },
    "gold": {
        "monthly": 5000,
        "6_months": 28000,
        "yearly": 55000,
        "record": True,
        "meeting_record": False,
        "upload": False,
    },
    "enterprise": {
        "monthly": 8000,
        "6_months": 46000,
        "yearly": 91000,
        "record": True,
        "meeting_record": True,
        "upload": True,
    },
}



# ============================================================
# PLAN HELPERS
# ============================================================

def get_plan_config(plan):
    """Return normalized configuration for a subscription plan."""
    return PLAN_CONFIG.get((plan or "").lower(), {})


def is_free_plan(user_id):
    subscription = get_active_subscription(user_id)
    return bool(subscription and subscription.plan.lower() == "free")


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
# GET USER CREDIT ACCOUNT
# ============================================================

def get_credit_account(user_id):
    """Return the user's credit account."""

    return UserCredit.query.filter_by(
        user_id=user_id
    ).first()


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
# CHECK CREDIT
# ============================================================

def has_credit(user_id):
    """Return True if the user has at least one available credit."""

    credit_account = get_credit_account(user_id)

    if not credit_account:
        return False

    return credit_account.credits > 0


# ============================================================
# USE ONE CREDIT
# ============================================================

def consume_credit(
    user_id,
    action,
    recording_id=None,
):
    """
    Consume one credit and create a credit transaction.

    Returns:
        (True, message)
        (False, message)
    """

    credit_account = get_credit_account(user_id)

    if not credit_account:
        return False, "Credit account not found."

    if credit_account.credits <= 0:
        return False, "You have no credits remaining."

    success = credit_account.use_credit()

    if not success:
        return False, "You have no credits remaining."

    transaction = CreditTransaction(
        user_id=user_id,
        action=action,
        credits_used=1,
        recording_id=recording_id,
    )

    db.session.add(transaction)
    db.session.commit()

    return True, "Credit used successfully."


# ============================================================
# CHECK ACTION + CREDIT
# ============================================================

def check_action(user_id, action):
    """
    Check whether the user is allowed to perform an action
    without consuming a credit.
    """

    if not has_plan_permission(user_id, action):
        return False, (
            "Your current subscription plan does not allow "
            f"the '{action}' feature."
        )

    if not has_credit(user_id):
        return False, (
            "You have no credits remaining. "
            "Please upgrade or purchase additional credits."
        )

    return True, "Action authorized."


def authorize_action(user_id, action, recording_id=None):
    """
    Check permission and consume one credit.
    """

    success, message = check_action(
        user_id=user_id,
        action=action,
    )

    if not success:
        return False, message

    return consume_credit(
        user_id=user_id,
        action=action,
        recording_id=recording_id,
    )


# ============================================================
# CREATE FREE ACCOUNT
# ============================================================

def create_free_account_records(user_id):
    """
    Create the default Free subscription and 5 credits
    for a newly registered user.

    This function is mainly useful for future migrations
    and existing-user backfills.
    """

    existing_subscription = Subscription.query.filter_by(
        user_id=user_id
    ).first()

    if not existing_subscription:
        subscription = Subscription(
            user_id=user_id,
            plan="free",
            billing_cycle="free",
            amount=0,
            start_date=datetime.now(timezone.utc),
            end_date=None,
            status="active",
        )

        db.session.add(subscription)

    existing_credit = UserCredit.query.filter_by(
        user_id=user_id
    ).first()

    if not existing_credit:
        credit_account = UserCredit(
            user_id=user_id,
            credits=5,
            used_credits=0,
        )

        db.session.add(credit_account)

    db.session.commit()