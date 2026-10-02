import os
import time
import tempfile
import secrets
import hashlib
import subprocess
import shutil
import threading
import uuid
import hmac
import hashlib

from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
from groq import Groq
from flask_migrate import Migrate
from models import (
    db,
    User,
    Subscription,
    UserCredit,
    CreditTransaction,
    Payment,
    TranscriptionHistory,
)

from subscription_service import (
    check_action,
    get_plan_config,
    get_active_subscription,
    has_plan_permission,
    get_daily_transcription_limit,
    activate_subscription_from_payment,
)


import resend

import jwt
from datetime import datetime, timedelta, timezone
from paystack_service import (
    PAYSTACK_SECRET_KEY,
    initialize_transaction,
    verify_transaction,
)


# ============================================================
# LOAD ENVIRONMENT VARIABLES
# ============================================================

load_dotenv()

resend.api_key = os.getenv("RESEND_API_KEY")

RESEND_FROM_EMAIL = os.getenv(
    "RESEND_FROM_EMAIL",
    "noreply@bestvisionenterprises.ng"
)


# ============================================================
# CREATE FLASK APPLICATION
# ============================================================

app = Flask(__name__)


# ============================================================
# DATABASE CONFIGURATION
# ============================================================

database_url = os.getenv(
    "DATABASE_URL",
    "sqlite:///voice_transcriber.db"
)

# Render/PostgreSQL sometimes provides postgres://
# SQLAlchemy expects postgresql://
if database_url.startswith("postgres://"):
    database_url = database_url.replace(
        "postgres://",
        "postgresql://",
        1
    )

app.config["SQLALCHEMY_DATABASE_URI"] = database_url

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False


# ============================================================
# INITIALIZE DATABASE
# ============================================================

db.init_app(app)

migrate = Migrate(
    app,
    db
)


# ============================================================
# CORS
# ============================================================

CORS(
    app,
    resources={
        r"/api/*": {
            "origins": [
                "https://voice-transcribe-11.web.app",
                "https://voice-transcribe-11.firebaseapp.com",
                "https://nabtranscriber-admin.web.app",
                r"^http://localhost:\d+$",
                r"^http://127\.0\.0\.1:\d+$",
            ],
            "methods": [
                "GET",
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
                "OPTIONS",
            ],
            "allow_headers": [
                "Content-Type",
                "Authorization",
            ],
            "supports_credentials": True,
        }
    }
)

@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        return "", 204


# ============================================================
# GROQ CONFIGURATION
# ============================================================

groq_api_key = os.getenv("GROQ_API_KEY")

if not groq_api_key:
    raise RuntimeError(
        "GROQ_API_KEY is not set. "
        "Please add GROQ_API_KEY to your .env file."
    )


client = Groq(
    api_key=groq_api_key
)


# ============================================================
# JWT CONFIGURATION
# ============================================================

jwt_secret_key = os.getenv("JWT_SECRET_KEY")

if not jwt_secret_key:
    raise RuntimeError(
        "JWT_SECRET_KEY is not set. "
        "Please add JWT_SECRET_KEY to your .env file."
    )

JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24


# ----------------------------------------------------
# TRANSCRIPTION JOBS
# ----------------------------------------------------

transcription_jobs = {}


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

    return jsonify({
        "status": "ok",
        "message": "Voice Transcriber API is running",
        "provider": "Groq"
    })


# ============================================================
# AUTHENTICATION - REGISTER
# ============================================================

@app.route(
    "/api/auth/register",
    methods=["POST"]
)
def register():

    data = request.get_json(silent=True)

    if not data:
        return jsonify({
            "success": False,
            "error": "Request body must be JSON."
        }), 400

    email = data.get("email")
    password = data.get("password")

    # Validate email
    if not email:
        return jsonify({
            "success": False,
            "error": "Email is required."
        }), 400

    # Validate password
    if not password:
        return jsonify({
            "success": False,
            "error": "Password is required."
        }), 400

    email = email.strip().lower()

    # Basic email validation
    if "@" not in email or "." not in email.split("@")[-1]:
        return jsonify({
            "success": False,
            "error": "Please provide a valid email address."
        }), 400

    # Password length
    if len(password) < 8:
        return jsonify({
            "success": False,
            "error": "Password must be at least 8 characters."
        }), 400

    # Check whether user already exists
    existing_user = User.query.filter_by(
        email=email
    ).first()

    if existing_user:
        return jsonify({
            "success": False,
            "error": "An account with this email already exists."
        }), 409

    # ============================================================
    # CREATE USER
    # ============================================================

    user = User(
        email=email
    )

    # Hash password
    user.set_password(password)

    # Add user first so we get the user ID.
    db.session.add(user)
    db.session.flush()


    # ============================================================
    # CREATE FREE SUBSCRIPTION
    # ============================================================

    free_subscription = Subscription(
        user_id=user.id,
        plan="free",
        billing_cycle="free",
        amount=0,
        start_date=datetime.now(timezone.utc),
        end_date=None,
        status="active",
    )

    db.session.add(free_subscription)


    # ============================================================
    # CREATE 5 FREE CREDITS
    # ============================================================

    credit_account = UserCredit(
        user_id=user.id,
        credits=5,
        used_credits=0,
    )

    db.session.add(credit_account)


    # ============================================================
    # RECORD INITIAL CREDIT ALLOCATION
    # ============================================================

    credit_transaction = CreditTransaction(
        user_id=user.id,
        action="registration",
        credits_used=0,
        recording_id=None,
    )

    db.session.add(credit_transaction)


    # ============================================================
    # SAVE EVERYTHING
    # ============================================================

    db.session.commit()

    return jsonify({
        "success": True,
        "message": "Account created successfully.",
        "user": user.to_dict()
    }), 201


# ============================================================
# AUTHENTICATION - LOGIN
# ============================================================

@app.route(
    "/api/auth/login",
    methods=["POST"]
)
def login():

    data = request.get_json(silent=True)

    if not data:
        return jsonify({
            "success": False,
            "error": "Request body must be JSON."
        }), 400

    email = data.get("email")
    password = data.get("password")

    if not email:
        return jsonify({
            "success": False,
            "error": "Email is required."
        }), 400

    if not password:
        return jsonify({
            "success": False,
            "error": "Password is required."
        }), 400

    email = email.strip().lower()

    # Find user
    user = User.query.filter_by(
        email=email
    ).first()

    # Do not reveal whether the email exists
    if not user or not user.check_password(password):
        return jsonify({
            "success": False,
            "error": "Invalid email or password."
        }), 401

    # Create JWT payload
    now = datetime.now(timezone.utc)

    payload = {
        "sub": str(user.id),
        "email": user.email,
        "iat": now,
        "exp": now + timedelta(
            hours=JWT_EXPIRATION_HOURS
        )
    }

    # Generate token
    token = jwt.encode(
        payload,
        jwt_secret_key,
        algorithm=JWT_ALGORITHM
    )

    return jsonify({
        "success": True,
        "message": "Login successful.",
        "token": token,
        "token_type": "Bearer",
        "expires_in": JWT_EXPIRATION_HOURS * 60 * 60,
        "user": user.to_dict()
    }), 200


def get_authenticated_user():

    auth_header = request.headers.get("Authorization")

    if not auth_header:
        return None, (
            jsonify({
                "success": False,
                "error": "Authorization header is required."
            }),
            401
        )

    if not auth_header.startswith("Bearer "):
        return None, (
            jsonify({
                "success": False,
                "error": "Invalid authorization format."
            }),
            401
        )

    token = auth_header.split(" ", 1)[1].strip()

    if not token:
        return None, (
            jsonify({
                "success": False,
                "error": "Token is required."
            }),
            401
        )

    try:

        payload = jwt.decode(
            token,
            jwt_secret_key,
            algorithms=[JWT_ALGORITHM]
        )

        user_id = payload.get("sub")

        if not user_id:
            print("JWT PAYLOAD HAS NO SUB:", payload)

            return None, (
                jsonify({
                    "success": False,
                    "error": "Invalid token."
                }),
                401
            )

        try:
            user_id = int(user_id)
        except (ValueError, TypeError):
            return None, (
                jsonify({
                    "success": False,
                    "error": "Invalid user ID in token."
                }),
                401
            )

        user = db.session.get(
            User,
            user_id
        )

        if not user:
            return None, (
                jsonify({
                    "success": False,
                    "error": "User not found."
                }),
                404
            )

        return user, None

    except jwt.ExpiredSignatureError:

        return None, (
            jsonify({
                "success": False,
                "error": "Token has expired."
            }),
            401
        )

    except jwt.InvalidTokenError as e:
        print("JWT DECODE ERROR:", type(e).__name__, str(e))

        return None, (
            jsonify({
                "success": False,
                "error": "Invalid token."
            }),
            401
        )


# ============================================================
# ADMIN AUTHORIZATION
# ============================================================

def require_admin():

    user, auth_error = get_authenticated_user()

    if auth_error:
        return None, auth_error

    if user.role != "admin":
        return None, (
            jsonify({
                "success": False,
                "error": "Admin access required."
            }),
            403
        )

    return user, None


# ============================================================
# ADMIN - DASHBOARD SUMMARY
# ============================================================

@app.route(
    "/api/admin/dashboard",
    methods=["GET"]
)
def admin_dashboard():

    admin, auth_error = require_admin()

    if auth_error:
        return auth_error

    total_users = User.query.count()

    total_admins = User.query.filter_by(
        role="admin"
    ).count()

    total_free_users = Subscription.query.filter_by(
        plan="free",
        status="active"
    ).count()

    total_gold_users = Subscription.query.filter_by(
        plan="gold",
        status="active"
    ).count()

    total_enterprise_users = Subscription.query.filter_by(
        plan="enterprise",
        status="active"
    ).count()

    total_active_subscriptions = Subscription.query.filter_by(
        status="active"
    ).count()

    return jsonify({
        "success": True,
        "dashboard": {
            "total_users": total_users,
            "total_admins": total_admins,
            "total_free_users": total_free_users,
            "total_gold_users": total_gold_users,
            "total_enterprise_users": total_enterprise_users,
            "total_active_subscriptions": total_active_subscriptions,
        }
    }), 200


# ============================================================
# ADMIN - USERS
# ============================================================

@app.route(
    "/api/admin/users",
    methods=["GET"]
)
def admin_users():

    admin, auth_error = require_admin()

    if auth_error:
        return auth_error

    # --------------------------------------------------------
    # Pagination
    # --------------------------------------------------------

    try:
        page = int(
            request.args.get(
                "page",
                1
            )
        )

        per_page = int(
            request.args.get(
                "per_page",
                20
            )
        )

    except (ValueError, TypeError):

        return jsonify({
            "success": False,
            "error": "Invalid pagination values."
        }), 400

    if page < 1:
        page = 1

    if per_page < 1:
        per_page = 20

    if per_page > 100:
        per_page = 100

    # --------------------------------------------------------
    # Search
    # --------------------------------------------------------

    search = request.args.get(
        "search",
        ""
    ).strip().lower()

    query = User.query

    if search:
        query = query.filter(
            User.email.ilike(
                f"%{search}%"
            )
        )

    # --------------------------------------------------------
    # Get paginated users
    # --------------------------------------------------------

    pagination = query.order_by(
        User.created_at.desc()
    ).paginate(
        page=page,
        per_page=per_page,
        error_out=False
    )

    users = []

    for user in pagination.items:

        subscription = Subscription.query.filter_by(
            user_id=user.id,
            status="active"
        ).order_by(
            Subscription.created_at.desc()
        ).first()

        users.append({
            "id": user.id,
            "email": user.email,
            "role": user.role,
            "created_at": (
                user.created_at.isoformat()
                if user.created_at
                else None
            ),
            "subscription": (
                subscription.to_dict()
                if subscription
                else None
            )
        })

    return jsonify({
        "success": True,
        "users": users,
        "pagination": {
            "page": pagination.page,
            "per_page": pagination.per_page,
            "total": pagination.total,
            "pages": pagination.pages,
            "has_next": pagination.has_next,
            "has_prev": pagination.has_prev,
        }
    }), 200


# ============================================================
# ADMIN - USER DETAILS
# ============================================================

@app.route(
    "/api/admin/users/<int:user_id>",
    methods=["GET"]
)
def admin_user_details(user_id):

    admin, auth_error = require_admin()

    if auth_error:
        return auth_error

    user = db.session.get(
        User,
        user_id
    )

    if not user:
        return jsonify({
            "success": False,
            "error": "User not found."
        }), 404

    # --------------------------------------------------------
    # Active subscription
    # --------------------------------------------------------

    subscription = Subscription.query.filter_by(
        user_id=user.id,
        status="active"
    ).order_by(
        Subscription.created_at.desc()
    ).first()

    # --------------------------------------------------------
    # Credit account
    # --------------------------------------------------------

    credit_account = UserCredit.query.filter_by(
        user_id=user.id
    ).first()

    # --------------------------------------------------------
    # Build response
    # --------------------------------------------------------

    return jsonify({
        "success": True,
        "user": {
            "id": user.id,
            "email": user.email,
            "role": user.role,
            "created_at": (
                user.created_at.isoformat()
                if user.created_at
                else None
            ),
            "updated_at": (
                user.updated_at.isoformat()
                if user.updated_at
                else None
            ),
            "subscription": (
                subscription.to_dict()
                if subscription
                else None
            ),
            "credits": (
                credit_account.to_dict()
                if credit_account
                else None
            )
        }
    }), 200


# ============================================================
# ADMIN - UPDATE USER SUBSCRIPTION
# ============================================================

@app.route(
    "/api/admin/users/<int:user_id>/subscription",
    methods=["PATCH"]
)
def admin_update_user_subscription(user_id):

    admin, auth_error = require_admin()

    if auth_error:
        return auth_error

    user = db.session.get(
        User,
        user_id
    )

    if not user:
        return jsonify({
            "success": False,
            "error": "User not found."
        }), 404

    data = request.get_json(silent=True)

    if not data:
        return jsonify({
            "success": False,
            "error": "Request body must be JSON."
        }), 400

    plan = str(
        data.get("plan", "")
    ).strip().lower()

    billing_cycle = str(
        data.get("billing_cycle", "")
    ).strip().lower()

    valid_plans = {
        "free",
        "gold",
        "enterprise",
    }

    if plan not in valid_plans:
        return jsonify({
            "success": False,
            "error": "Invalid subscription plan."
        }), 400

    # --------------------------------------------------------
    # Validate billing cycle
    # --------------------------------------------------------

    if plan == "free":

        billing_cycle = "free"

    else:

        valid_billing_cycles = {
            "monthly",
            "6_months",
            "yearly",
        }

        if billing_cycle not in valid_billing_cycles:
            return jsonify({
                "success": False,
                "error": (
                    "Billing cycle must be monthly, "
                    "6_months, or yearly."
                )
            }), 400

    # --------------------------------------------------------
    # Get plan configuration
    # --------------------------------------------------------

    config = get_plan_config(plan)

    if not config:
        return jsonify({
            "success": False,
            "error": "Subscription plan configuration not found."
        }), 400

    amount = config.get(
        billing_cycle,
        0
    )

    now = datetime.now(timezone.utc)

    # --------------------------------------------------------
    # Calculate subscription end date
    # --------------------------------------------------------

    end_date = None

    if plan != "free":

        if billing_cycle == "monthly":
            end_date = now + timedelta(
                days=30
            )

        elif billing_cycle == "6_months":
            end_date = now + timedelta(
                days=182
            )

        elif billing_cycle == "yearly":
            end_date = now + timedelta(
                days=365
            )

    # --------------------------------------------------------
    # Find existing active subscription
    # --------------------------------------------------------

    subscription = get_active_subscription(
        user.id
    )

    if subscription:

        subscription.plan = plan
        subscription.billing_cycle = billing_cycle
        subscription.amount = amount
        subscription.start_date = now
        subscription.end_date = end_date
        subscription.status = "active"
        subscription.payment_reference = (
            "admin-manual-assignment"
        )

    else:

        subscription = Subscription(
            user_id=user.id,
            plan=plan,
            billing_cycle=billing_cycle,
            amount=amount,
            start_date=now,
            end_date=end_date,
            status="active",
            payment_reference=(
                "admin-manual-assignment"
            ),
        )

        db.session.add(
            subscription
        )

    db.session.commit()

    return jsonify({
        "success": True,
        "message": "User subscription updated successfully.",
        "subscription": subscription.to_dict(),
    }), 200


# ============================================================
# PAYSTACK PAYMENT INITIALIZATION
# ============================================================

@app.route(
    "/api/payment/initialize",
    methods=["POST"]
)
def payment_initialize():

    user, auth_error = get_authenticated_user()

    if auth_error:
        return auth_error

    data = request.get_json(silent=True)

    if not data:
        return jsonify({
            "success": False,
            "error": "Request body must be JSON."
        }), 400

    plan = str(
        data.get("plan", "")
    ).strip().lower()

    billing_cycle = str(
        data.get("billing_cycle", "")
    ).strip().lower()

    valid_plans = {
        "gold",
        "enterprise",
    }

    if plan not in valid_plans:
        return jsonify({
            "success": False,
            "error": (
                "Only Gold and Enterprise plans "
                "can be purchased."
            )
        }), 400

    valid_billing_cycles = {
        "monthly",
        "6_months",
        "yearly",
    }

    if billing_cycle not in valid_billing_cycles:
        return jsonify({
            "success": False,
            "error": (
                "Billing cycle must be monthly, "
                "6_months, or yearly."
            )
        }), 400

    config = get_plan_config(plan)

    if not config:
        return jsonify({
            "success": False,
            "error": "Subscription plan not found."
        }), 400

    amount = config.get(
        billing_cycle
    )

    if amount is None:
        return jsonify({
            "success": False,
            "error": "Invalid subscription pricing."
        }), 400

    reference = (
        f"NAB-{plan.upper()}-"
        f"{billing_cycle.upper()}-"
        f"{uuid.uuid4().hex[:12].upper()}"
    )

    callback_url = (
        "https://voice-transcribe-11.web.app/"
        "payment/callback"
    )

    try:

        paystack_data = initialize_transaction(
            email=user.email,
            amount=amount,
            reference=reference,
            callback_url=callback_url,
        )

    except ValueError as error:

        return jsonify({
            "success": False,
            "error": str(error),
        }), 400

    except Exception:

        return jsonify({
            "success": False,
            "error": (
                "Unable to initialize "
                "Paystack payment."
            ),
        }), 502

    payment = Payment(
        user_id=user.id,
        plan=plan,
        billing_cycle=billing_cycle,
        amount=amount,
        currency="NGN",
        payment_gateway="paystack",
        transaction_reference=reference,
        status="pending",
    )

    db.session.add(payment)

    try:
        db.session.commit()

    except Exception:

        db.session.rollback()

        return jsonify({
            "success": False,
            "error": (
                "Payment initialization could "
                "not be recorded."
            ),
        }), 500

    return jsonify({
        "success": True,
        "message": (
            "Paystack payment initialized "
            "successfully."
        ),
        "payment": {
            "id": payment.id,
            "plan": plan,
            "billing_cycle": billing_cycle,
            "amount": amount,
            "currency": "NGN",
            "payment_gateway": "paystack",
            "transaction_reference": reference,
            "status": payment.status,
        },
        "paystack": {
            "authorization_url": paystack_data.get(
                "authorization_url"
            ),
            "access_code": paystack_data.get(
                "access_code"
            ),
            "reference": paystack_data.get(
                "reference"
            ),
        },
    }), 200


# ============================================================
# PAYSTACK PAYMENT VERIFICATION
# ============================================================

@app.route(
    "/api/payment/verify",
    methods=["POST"]
)
def payment_verify():

    user, auth_error = get_authenticated_user()

    if auth_error:
        return auth_error

    data = request.get_json(silent=True)

    if not data:
        return jsonify({
            "success": False,
            "error": "Request body must be JSON."
        }), 400

    reference = str(
        data.get("reference", "")
    ).strip()

    if not reference:
        return jsonify({
            "success": False,
            "error": "Transaction reference is required."
        }), 400

    payment = Payment.query.filter_by(
        transaction_reference=reference,
        user_id=user.id,
    ).first()

    if not payment:
        return jsonify({
            "success": False,
            "error": "Payment record not found."
        }), 404

    if payment.payment_gateway != "paystack":
        return jsonify({
            "success": False,
            "error": "Invalid payment gateway."
        }), 400

    if payment.status == "successful":
        subscription = get_active_subscription(
            user.id
        )

        return jsonify({
            "success": True,
            "message": "Payment has already been verified.",
            "payment": payment.to_dict(),
            "subscription": (
                subscription.to_dict()
                if subscription
                else None
            ),
        }), 200

    if payment.status != "pending":
        return jsonify({
            "success": False,
            "error": (
                "This payment cannot be verified "
                f"because its status is {payment.status}."
            )
        }), 400

    try:

        paystack_data = verify_transaction(
            reference
        )

    except Exception:

        return jsonify({
            "success": False,
            "error": (
                "Unable to verify payment "
                "with Paystack."
            ),
        }), 502

    paystack_status = str(
        paystack_data.get("status", "")
    ).strip().lower()

    paystack_reference = str(
        paystack_data.get("reference", "")
    ).strip()

    try:
        paid_amount_kobo = int(
            paystack_data.get("amount", 0)
        )
    except (ValueError, TypeError):

        return jsonify({
            "success": False,
            "error": "Invalid payment amount from Paystack."
        }), 400

    expected_amount_kobo = (
        int(payment.amount) * 100
    )

    if paystack_reference != reference:
        return jsonify({
            "success": False,
            "error": "Payment reference does not match."
        }), 400

    if paid_amount_kobo != expected_amount_kobo:
        return jsonify({
            "success": False,
            "error": "Payment amount does not match."
        }), 400

    if paystack_status != "success":

        # Keep payments that are still pending as pending so
        # they can be verified again after Paystack completes them.
        if paystack_status in {
            "pending",
            "ongoing",
            "processing",
        }:
            payment.status = "pending"
        else:
            # Paystack has returned a terminal non-success status.
            payment.status = paystack_status or "failed"

        db.session.commit()

        return jsonify({
            "success": False,
            "message": (
                "Paystack payment has not been "
                "successfully completed."
            ),
            "payment": payment.to_dict(),
        }), 400

    try:

        activated_payment, subscription = (
            activate_subscription_from_payment(
                user_id=user.id,
                plan=payment.plan,
                billing_cycle=payment.billing_cycle,
                amount=payment.amount,
                payment_gateway="paystack",
                transaction_reference=reference,
            )
        )

    except ValueError as error:

        db.session.rollback()

        return jsonify({
            "success": False,
            "error": str(error),
        }), 400

    except Exception:

        db.session.rollback()

        return jsonify({
            "success": False,
            "error": (
                "Payment was verified, but the "
                "subscription could not be activated."
            ),
        }), 500

    return jsonify({
        "success": True,
        "message": (
            "Payment verified and subscription "
            "activated successfully."
        ),
        "payment": activated_payment.to_dict(),
        "subscription": subscription.to_dict(),
    }), 200


@app.route("/api/payment/webhook", methods=["POST"])
def paystack_webhook():
    signature = request.headers.get("x-paystack-signature", "")

    if not signature:
        return jsonify({
            "success": False,
            "error": "Missing Paystack signature."
        }), 401

    payload = request.get_data()

    expected_signature = hmac.new(
        PAYSTACK_SECRET_KEY.encode("utf-8"),
        payload,
        hashlib.sha512
    ).hexdigest()

    if not hmac.compare_digest(
        signature,
        expected_signature
    ):
        return jsonify({
            "success": False,
            "error": "Invalid Paystack signature."
        }), 401

    data = request.get_json(silent=True)

    if not data:
        return jsonify({
            "success": False,
            "error": "Invalid webhook payload."
        }), 400

    event = data.get("event")
    event_data = data.get("data") or {}

    if event != "charge.success":
        return jsonify({
            "success": True,
            "message": "Webhook event received."
        }), 200

    reference = str(
        event_data.get("reference", "")
    ).strip()

    if not reference:
        return jsonify({
            "success": False,
            "error": "Transaction reference is missing."
        }), 400

    payment = Payment.query.filter_by(
        transaction_reference=reference
    ).first()

    if not payment:
        return jsonify({
            "success": False,
            "error": "Payment record not found."
        }), 404

    if payment.status == "successful":
        return jsonify({
            "success": True,
            "message": "Payment already processed."
        }), 200

    try:
        paid_amount_kobo = int(
            event_data.get("amount", 0)
        )
    except (ValueError, TypeError):
        return jsonify({
            "success": False,
            "error": "Invalid payment amount."
        }), 400

    expected_amount_kobo = int(payment.amount) * 100

    if paid_amount_kobo != expected_amount_kobo:
        return jsonify({
            "success": False,
            "error": "Payment amount does not match."
        }), 400

    try:
        activated_payment, subscription = (
            activate_subscription_from_payment(
                user_id=payment.user_id,
                plan=payment.plan,
                billing_cycle=payment.billing_cycle,
                amount=payment.amount,
                payment_gateway="paystack",
                transaction_reference=reference,
            )
        )

    except ValueError as error:
        db.session.rollback()

        return jsonify({
            "success": False,
            "error": str(error)
        }), 400

    except Exception:
        db.session.rollback()

        return jsonify({
            "success": False,
            "error": (
                "Payment was received, but the "
                "subscription could not be activated."
            )
        }), 500

    return jsonify({
        "success": True,
        "message": "Paystack payment processed successfully.",
        "payment": activated_payment.to_dict(),
        "subscription": subscription.to_dict(),
    }), 200

# ============================================================
# ADMIN PAYMENT HISTORY
# ============================================================

@app.route(
    "/api/admin/payments",
    methods=["GET"]
)
def admin_payments():

    admin, auth_error = require_admin()

    if auth_error:
        return auth_error

    try:
        page = int(
            request.args.get("page", 1)
        )
        per_page = int(
            request.args.get("per_page", 20)
        )
    except (ValueError, TypeError):
        return jsonify({
            "success": False,
            "error": "Invalid pagination values."
        }), 400

    if page < 1:
        page = 1

    if per_page < 1:
        per_page = 20

    if per_page > 100:
        per_page = 100

    search = request.args.get(
        "search",
        ""
    ).strip().lower()

    status = request.args.get(
        "status",
        ""
    ).strip().lower()

    plan = request.args.get(
        "plan",
        ""
    ).strip().lower()

    query = Payment.query

    if search:
        query = query.filter(
            db.or_(
                Payment.transaction_reference.ilike(
                    f"%{search}%"
                ),
                Payment.payment_gateway.ilike(
                    f"%{search}%"
                )
            )
        )

    if status:
        query = query.filter(
            Payment.status == status
        )

    if plan:
        query = query.filter(
            Payment.plan == plan
        )

    pagination = query.order_by(
        Payment.id.desc()
    ).paginate(
        page=page,
        per_page=per_page,
        error_out=False
    )

    payments = []

    for payment in pagination.items:
        payment_data = payment.to_dict()

        subscription = None

        if payment.subscription_id:
            subscription = Subscription.query.get(
                payment.subscription_id
            )

        payment_data["payment_date"] = (
            payment.paid_at.isoformat()
            if payment.paid_at
            else None
        )

        payment_data["subscription_start_date"] = (
            subscription.start_date.isoformat()
            if subscription and subscription.start_date
            else None
        )

        payment_data["subscription_end_date"] = (
            subscription.end_date.isoformat()
            if subscription and subscription.end_date
            else None
        )

        payment_data["subscription_status"] = (
            subscription.status
            if subscription
            else None
        )

        payments.append(payment_data)

    return jsonify({
        "success": True,
        "payments": payments,
        "pagination": {
            "page": pagination.page,
            "per_page": pagination.per_page,
            "total": pagination.total,
            "pages": pagination.pages,
            "has_next": pagination.has_next,
            "has_prev": pagination.has_prev,
        }
    }), 200
    
# ============================================================
# AUTHENTICATION - CURRENT USER
# ============================================================

@app.route(
    "/api/auth/me",
    methods=["GET"]
)
def current_user():

    auth_header = request.headers.get("Authorization")

    if not auth_header:
        return jsonify({
            "success": False,
            "error": "Authorization header is required."
        }), 401

    if not auth_header.startswith("Bearer "):
        return jsonify({
            "success": False,
            "error": "Invalid authorization format."
        }), 401

    token = auth_header.split(" ", 1)[1].strip()

    if not token:
        return jsonify({
            "success": False,
            "error": "Token is required."
        }), 401

    try:
        payload = jwt.decode(
            token,
            jwt_secret_key,
            algorithms=[JWT_ALGORITHM]
        )

        user_id = payload.get("sub")

        if not user_id:
            return jsonify({
                "success": False,
                "error": "Invalid token."
            }), 401

        user = db.session.get(
            User,
            int(user_id)
        )

        if not user:
            return jsonify({
                "success": False,
                "error": "User not found."
            }), 404

        return jsonify({
            "success": True,
            "user": user.to_dict()
        }), 200

    except jwt.ExpiredSignatureError:
        return jsonify({
            "success": False,
            "error": "Token has expired."
        }), 401

    except jwt.InvalidTokenError:
        return jsonify({
            "success": False,
            "error": "Invalid token."
        }), 401

    except (ValueError, TypeError):
        return jsonify({
            "success": False,
            "error": "Invalid user ID in token."
        }), 401


# ============================================================
# AUTHENTICATION - LOGOUT
# ============================================================

@app.route(
    "/api/auth/logout",
    methods=["POST"]
)
def logout():

    # JWT is currently stateless.
    # The client should delete its stored token.
    return jsonify({
        "success": True,
        "message": "Logout successful. Please remove the access token from the client."
    }), 200


# ============================================================
# AUTHENTICATION - FORGOT PASSWORD
# ============================================================

@app.route(
    "/api/auth/forgot-password",
    methods=["POST"]
)
def forgot_password():

    data = request.get_json(silent=True)

    if not data:
        return jsonify({
            "success": False,
            "error": "Request body must be JSON."
        }), 400

    email = data.get("email")

    if not email:
        return jsonify({
            "success": False,
            "error": "Email is required."
        }), 400

    email = email.strip().lower()

    # Always return the same general response.
    # This prevents revealing whether an email exists.
    generic_response = {
        "success": True,
        "message": (
            "If an account with that email exists, "
            "a password reset link has been sent."
        )
    }

    user = User.query.filter_by(
        email=email
    ).first()

    if not user:
        return jsonify(generic_response), 200

    # Generate a cryptographically secure random token.
    reset_token = secrets.token_urlsafe(32)

    # Store only the SHA-256 hash of the token.
    token_hash = hashlib.sha256(
        reset_token.encode("utf-8")
    ).hexdigest()

    # Token expires after 30 minutes.
    expires_at = (
        datetime.now(timezone.utc)
        + timedelta(minutes=30)
    )

    user.password_reset_token_hash = token_hash
    user.password_reset_expires_at = expires_at
    user.password_reset_used = False

    db.session.commit()

    # Frontend page that will handle the password reset.
    reset_link = (
        "https://voice-transcribe-11.web.app/#/reset-password"
        f"?token={reset_token}"
    )

    try:
        resend.Emails.send({
            "from": RESEND_FROM_EMAIL,
            "to": [email],
            "subject": "Reset your NabTranscriber password",
            "html": f"""
                <div style="
                    font-family: Arial, sans-serif;
                    max-width: 600px;
                    margin: 0 auto;
                    padding: 30px;
                    color: #222;
                ">
                    <h2 style="margin-bottom: 10px;">
                        Reset Your Password
                    </h2>

                    <p>
                        We received a request to reset your
                        NabTranscriber password.
                    </p>

                    <p>
                        Click the button below to create a new password.
                    </p>

                    <p style="margin: 30px 0;">
                        <a href="{reset_link}"
                           style="
                               background: #1B5E20;
                               color: white;
                               padding: 14px 24px;
                               text-decoration: none;
                               border-radius: 6px;
                               display: inline-block;
                               font-weight: bold;
                           ">
                            RESET PASSWORD
                        </a>
                    </p>

                    <p>
                        This link will expire in
                        <strong>30 minutes</strong>.
                    </p>

                    <p>
                        If you did not request a password reset,
                        you can safely ignore this email.
                    </p>

                    <hr style="
                        margin: 30px 0;
                        border: none;
                        border-top: 1px solid #ddd;
                    ">

                    <p style="
                        font-size: 12px;
                        color: #777;
                    ">
                        NabTranscriber<br>
                        Automated security notification
                    </p>
                </div>
            """
        })

    except Exception as email_error:
        print(
            "Password reset email failed:",
            email_error
        )

        return jsonify({
            "success": False,
            "error": (
                "We could not send the password reset email. "
                "Please try again later."
            )
        }), 500

    return jsonify(generic_response), 200

# ============================================================
# ACCOUNT - DELETE
# ============================================================

@app.route(
    "/api/account",
    methods=["DELETE"]
)
def delete_account():

    # --------------------------------------------------------
    # AUTHENTICATE USER
    # --------------------------------------------------------

    user, auth_error = get_authenticated_user()

    if auth_error:
        return auth_error

    try:

        user_id = user.id

        # ----------------------------------------------------
        # DELETE USER SUBSCRIPTIONS
        # ----------------------------------------------------

        Subscription.query.filter_by(
            user_id=user_id
        ).delete(
            synchronize_session=False
        )

        # ----------------------------------------------------
        # DELETE USER CREDIT ACCOUNT
        # ----------------------------------------------------

        UserCredit.query.filter_by(
            user_id=user_id
        ).delete(
            synchronize_session=False
        )

        # ----------------------------------------------------
        # DELETE USER CREDIT TRANSACTIONS
        # ----------------------------------------------------

        CreditTransaction.query.filter_by(
            user_id=user_id
        ).delete(
            synchronize_session=False
        )

        # ----------------------------------------------------
        # DELETE USER ACCOUNT
        # ----------------------------------------------------

        db.session.delete(user)

        db.session.commit()

        return jsonify({
            "success": True,
            "message": (
                "Your NabTranscriber account and "
                "associated account data have been deleted."
            )
        }), 200

    except Exception as e:

        db.session.rollback()

        print(
            "Account deletion failed:",
            e
        )

        return jsonify({
            "success": False,
            "error": (
                "We could not delete your account. "
                "Please try again later."
            )
        }), 500


# ============================================================
# AUTHENTICATION - RESET PASSWORD
# ============================================================

@app.route(
    "/api/auth/reset-password",
    methods=["POST"]
)
def reset_password():

    data = request.get_json(silent=True)

    if not data:
        return jsonify({
            "success": False,
            "error": "Request body must be JSON."
        }), 400

    token = data.get("token")
    new_password = data.get("password")

    # --------------------------------------------------------
    # VALIDATE TOKEN
    # --------------------------------------------------------

    if not token:
        return jsonify({
            "success": False,
            "error": "Reset token is required."
        }), 400

    # --------------------------------------------------------
    # VALIDATE NEW PASSWORD
    # --------------------------------------------------------

    if not new_password:
        return jsonify({
            "success": False,
            "error": "New password is required."
        }), 400

    if len(new_password) < 8:
        return jsonify({
            "success": False,
            "error": "Password must be at least 8 characters."
        }), 400

    # --------------------------------------------------------
    # HASH THE TOKEN
    # --------------------------------------------------------

    token_hash = hashlib.sha256(
        token.encode("utf-8")
    ).hexdigest()

    # --------------------------------------------------------
    # FIND USER
    # --------------------------------------------------------

    user = User.query.filter_by(
        password_reset_token_hash=token_hash
    ).first()

    if not user:
        print(
        )

        return jsonify({
            "success": False,
            "error": "Invalid or expired reset token."
        }), 400

    # --------------------------------------------------------
    # CHECK WHETHER TOKEN HAS ALREADY BEEN USED
    # --------------------------------------------------------

    if user.password_reset_used:
        return jsonify({
            "success": False,
            "error": "This reset link has already been used."
        }), 400

    # --------------------------------------------------------
    # CHECK TOKEN EXPIRATION
    # --------------------------------------------------------

    if not user.password_reset_expires_at:
        return jsonify({
            "success": False,
            "error": "Invalid or expired reset token."
        }), 400

    expires_at = user.password_reset_expires_at

    # Handle databases that return a naive datetime.
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(
            tzinfo=timezone.utc
        )

    if datetime.now(timezone.utc) > expires_at:
        return jsonify({
            "success": False,
            "error": "This reset link has expired. Please request a new one."
        }), 400

    # --------------------------------------------------------
    # UPDATE PASSWORD
    # --------------------------------------------------------

    user.set_password(new_password)

    # --------------------------------------------------------
    # INVALIDATE RESET TOKEN
    # --------------------------------------------------------

    user.password_reset_used = True
    user.password_reset_token_hash = None
    user.password_reset_expires_at = None

    db.session.commit()

    return jsonify({
        "success": True,
        "message": "Password reset successfully. You can now log in."
    }), 200


# ============================================================
# TEMPORARY GOOGLE PLAY REVIEWER ACCESS
# REMOVE AFTER PLAY STORE REVIEW ACCOUNT IS CONFIGURED
# ============================================================

@app.route(
    "/api/reviewer/enterprise",
    methods=["POST"]
)
def temporary_reviewer_enterprise():

    user, auth_error = get_authenticated_user()

    if auth_error:
        return auth_error

    reviewer_email = "nabtranscriber.review@gmail.com"

    if user.email.lower() != reviewer_email.lower():
        return jsonify({
            "success": False,
            "error": "Not authorized."
        }), 403

    # Check for an existing active subscription
    subscription = get_active_subscription(user.id)

    if subscription:
        subscription.plan = "enterprise"
        subscription.billing_cycle = "reviewer"
        subscription.amount = 0
        subscription.status = "active"
        subscription.start_date = datetime.now(timezone.utc)
        subscription.end_date = None
        subscription.payment_reference = "google-play-reviewer"

    else:
        subscription = Subscription(
            user_id=user.id,
            plan="enterprise",
            billing_cycle="reviewer",
            amount=0,
            start_date=datetime.now(timezone.utc),
            end_date=None,
            status="active",
            payment_reference="google-play-reviewer",
        )

        db.session.add(subscription)

    db.session.commit()

    return jsonify({
        "success": True,
        "message": "Reviewer account upgraded to Enterprise.",
        "plan": "enterprise",
        "status": "active",
    }), 200


# ============================================================
# SUBSCRIPTION / CREDIT CHECK
# ============================================================

@app.route(
    "/api/subscription/check",
    methods=["POST"]
)
def check_subscription_action():

    user, auth_error = get_authenticated_user()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True) or {}
    action = data.get("action")
    action_aliases = {"upload_audio": "upload", "transcription": "transcribe"}
    action = action_aliases.get(action, action)

    allowed_actions = {"record", "meeting_record", "upload", "transcribe"}
    if action not in allowed_actions:
        return jsonify({"success": False, "error": "Invalid action."}), 400

    subscription = get_active_subscription(user.id)

    # Users without a paid/active subscription are treated
    # as Free users and receive the Free plan limits.
    if subscription:
        plan = subscription.plan.lower()
    else:
        plan = "free"

    config = get_plan_config(plan)

    # Transcription is available to all plans that can record; Free has a daily limit.
    if action == "transcribe":
        if not config.get("record", False):
            return jsonify({"success": False, "error": "Your current subscription plan does not allow transcription."}), 403

        remaining_daily = None
        if plan == "free":
            remaining_daily = free_transcription_remaining_today(user.id)
            if remaining_daily <= 0:
                return jsonify({
                    "success": False,
                    "error": "Your Free plan allows 2 transcriptions per day. Please try again tomorrow or upgrade your plan.",
                    "code": "daily_transcription_limit_reached",
                    "remaining_daily_transcriptions": 0,
                }), 403

        return jsonify({
            "success": True,
            "message": "Action authorized.",
            "action": action,
            "plan": plan,
            "status": subscription.status if subscription else "active",
            "subscription_start_date": (
                subscription.start_date.isoformat()
                if subscription and subscription.start_date
                else None
            ),
            "subscription_end_date": (
                subscription.end_date.isoformat()
                if subscription and subscription.end_date
                else None
            ),
            "billing_cycle": (
                subscription.billing_cycle
                if subscription
                else None
            ),
            "remaining_daily_transcriptions": remaining_daily,
        }), 200

    if not config.get(action, False):
        messages = {
            "record": "Your current subscription plan does not allow voice recording.",
            "meeting_record": "Meeting recording is available on the Enterprise plan only.",
            "upload": "Audio upload is available on the Enterprise plan only.",
        }
        return jsonify({"success": False, "error": messages.get(action, "Feature not available on your plan.")}), 403

    return jsonify({
        "success": True,
        "message": "Action authorized.",
        "action": action,
        "plan": plan,
        "status": subscription.status if subscription else "active",
        "subscription_start_date": (
            subscription.start_date.isoformat()
            if subscription and subscription.start_date
            else None
        ),
        "subscription_end_date": (
            subscription.end_date.isoformat()
            if subscription and subscription.end_date
            else None
        ),
        "billing_cycle": (
            subscription.billing_cycle
            if subscription
            else None
        ),
    }), 200


# ============================================================
# SUBSCRIPTION / CREDIT AUTHORIZATION
# ============================================================

@app.route(
    "/api/subscription/authorize",
    methods=["POST"]
)
def authorize_subscription_action():

    user, auth_error = get_authenticated_user()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True) or {}
    action = data.get("action")
    action_aliases = {"upload_audio": "upload", "transcription": "transcribe"}
    action = action_aliases.get(action, action)

    allowed_actions = {"record", "meeting_record", "upload", "transcribe"}
    if action not in allowed_actions:
        return jsonify({"success": False, "error": "Invalid action."}), 400

    # Subscription permissions, not the legacy credit balance, control plan access.
    subscription = get_active_subscription(user.id)

    # Users without an active paid subscription are treated as Free.
    if subscription:
        plan = subscription.plan.lower()
    else:
        plan = "free"

    config = get_plan_config(plan)
    if not config.get("record", False) and action == "transcribe":
        return jsonify({"success": False, "error": "Your current subscription plan does not allow transcription."}), 403

    if action != "transcribe" and not config.get(action, False):
        return jsonify({"success": False, "error": f"Your current subscription plan does not allow the '{action}' feature."}), 403

    return jsonify({
        "success": True,
        "message": "Action authorized.",
        "action": action,
        "plan": plan,
    }), 200


# ============================================================
# BACKGROUND TRANSCRIPTION WORKER
# ============================================================

def process_transcription_job(
    job_id,
    temp_path,
    file_extension,
    user_id=None,
):

    chunk_directory = None

    try:

        # ----------------------------------------------------
        # CREATE CHUNK DIRECTORY
        # ----------------------------------------------------

        chunk_directory = tempfile.mkdtemp(
            prefix="voice_transcriber_chunks_"
        )

        # ----------------------------------------------------
        # SPLIT AUDIO INTO 5-MINUTE CHUNKS
        # ----------------------------------------------------

        chunk_pattern = os.path.join(
            chunk_directory,
            "chunk_%04d.mp3"
        )

        ffmpeg_command = [
            "ffmpeg",
            "-y",
            "-i",
            temp_path,
            "-map",
            "0:a:0",
            "-vn",
            "-f",
            "segment",
            "-segment_time",
            "300",
            "-reset_timestamps",
            "1",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "64k",
            chunk_pattern,
        ]

        print(
            f"[{job_id}] "
            "Splitting audio into 5-minute chunks..."
        )

        result = subprocess.run(
            ffmpeg_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        if result.returncode != 0:

            print(
                f"[{job_id}] FFmpeg error:"
            )

            print(result.stderr)

            transcription_jobs[job_id][
                "status"
            ] = "failed"

            transcription_jobs[job_id][
                "error"
            ] = (
                "Unable to process the audio file. "
                "Please try again."
            )

            return

        # ----------------------------------------------------
        # GET CHUNKS
        # ----------------------------------------------------

        chunk_files = sorted(
            [
                os.path.join(
                    chunk_directory,
                    filename
                )
                for filename in os.listdir(
                    chunk_directory
                )
                if filename.startswith("chunk_")
            ]
        )

        if not chunk_files:

            transcription_jobs[job_id][
                "status"
            ] = "failed"

            transcription_jobs[job_id][
                "error"
            ] = "No audio chunks were created."

            return

        total_chunks = len(
            chunk_files
        )

        transcription_jobs[job_id][
            "total_chunks"
        ] = total_chunks

        transcription_jobs[job_id][
            "completed_chunks"
        ] = 0

        transcription_jobs[job_id][
            "progress"
        ] = 0

        print(
            f"[{job_id}] "
            f"Created {total_chunks} audio chunks."
        )

        # ----------------------------------------------------
        # TRANSCRIBE EACH CHUNK
        # ----------------------------------------------------

        transcripts = []

        detected_languages = []

        max_retries = 3

        for index, chunk_path in enumerate(
            chunk_files
        ):

            chunk_number = index + 1

            print(
                f"[{job_id}] "
                f"Transcribing chunk "
                f"{chunk_number}/{total_chunks}..."
            )

            chunk_completed = False

            for attempt in range(
                1,
                max_retries + 1
            ):

                try:

                    with open(
                        chunk_path,
                        "rb"
                    ) as chunk_audio:

                        transcription = (
                            client.audio.transcriptions.create(
                                file=chunk_audio,
                                model="whisper-large-v3-turbo",
                                response_format="json"
                            )
                        )

                    chunk_language = getattr(
                        transcription,
                        "language",
                        None
                    )

                    if chunk_language:
                        detected_languages.append(
                            str(chunk_language).strip().lower()
                        )

                        print(
                            f"[{job_id}] "
                            f"Detected language for chunk "
                            f"{chunk_number}: "
                            f"{chunk_language}"
                        )

                    chunk_text = (
                        transcription.text
                        or ""
                    ).strip()

                    if chunk_text:

                        transcripts.append(
                            chunk_text
                        )

                    # ------------------------------------------------
                    # UPDATE PROGRESS
                    # ------------------------------------------------

                    progress = int(
                        (
                            chunk_number
                            / total_chunks
                        ) * 100
                    )

                    transcription_jobs[job_id][
                        "completed_chunks"
                    ] = chunk_number

                    transcription_jobs[job_id][
                        "progress"
                    ] = progress

                    print(
                        f"[{job_id}] "
                        f"Chunk "
                        f"{chunk_number}/{total_chunks} "
                        f"completed "
                        f"({progress}%)."
                    )

                    chunk_completed = True

                    break

                except Exception as chunk_error:

                    error_text = str(
                        chunk_error
                    )

                    print(
                        f"[{job_id}] "
                        f"Chunk "
                        f"{chunk_number}/{total_chunks} "
                        f"attempt "
                        f"{attempt}/{max_retries} "
                        "failed:"
                    )

                    print(error_text)

                    # ------------------------------------------------
                    # TEMPORARY RATE LIMIT / RETRY
                    # ------------------------------------------------

                    is_rate_limit = (
                        "rate_limit_exceeded"
                        in error_text.lower()
                        or "rate limit"
                        in error_text.lower()
                        or "too many requests"
                        in error_text.lower()
                        or "429"
                        in error_text
                        or "retry-after"
                        in error_text.lower()
                    )

                    is_hourly_limit = (
                        "seconds of audio per hour"
                        in error_text.lower()
                        or "audio per hour"
                        in error_text.lower()
                    )

                    is_retryable = (
                        is_rate_limit
                        or is_hourly_limit
                        or "timeout"
                        in error_text.lower()
                        or "temporarily unavailable"
                        in error_text.lower()
                        or "service unavailable"
                        in error_text.lower()
                        or "connection"
                        in error_text.lower()
                    )

                    if is_retryable and attempt < max_retries:

                        # Progressive backoff:
                        # 30s, 60s, 120s
                        retry_seconds = (
                            30 * (2 ** (attempt - 1))
                        )

                        print(
                            f"[{job_id}] "
                            f"Waiting "
                            f"{retry_seconds} seconds "
                            "before retry..."
                        )

                        time.sleep(
                            retry_seconds
                        )

                        continue

                    # ------------------------------------------------
                    # HOURLY LIMIT AFTER RETRIES
                    # ------------------------------------------------

                    if is_hourly_limit:

                        error_message = (
                            "The transcription service "
                            "has temporarily reached its "
                            "audio processing allowance. "
                            "Your recording has not been "
                            "lost. Please try again later."
                        )

                        transcription_jobs[job_id][
                            "status"
                        ] = "failed"

                        transcription_jobs[job_id][
                            "error"
                        ] = error_message

                        return

                    # ------------------------------------------------
                    # OTHER TRANSCRIPTION ERROR
                    # ------------------------------------------------

                    transcription_jobs[job_id][
                        "status"
                    ] = "failed"

                    transcription_jobs[job_id][
                        "error"
                    ] = error_text

                    return

            if not chunk_completed:

                transcription_jobs[job_id][
                    "status"
                ] = "failed"

                transcription_jobs[job_id][
                    "error"
                ] = (
                    f"Chunk {chunk_number} "
                    "could not be transcribed."
                )

                return

        # ----------------------------------------------------
        # COMBINE TRANSCRIPTS
        # ----------------------------------------------------

        final_text = "\n\n".join(
            transcripts
        )

        detected_language = None

        if detected_languages:
            from collections import Counter

            detected_language = Counter(
                detected_languages
            ).most_common(1)[0][0]

        # ----------------------------------------------------
        # MARK JOB AS COMPLETED
        # ----------------------------------------------------

        transcription_jobs[job_id][
            "status"
        ] = "completed"

        transcription_jobs[job_id][
            "completed_chunks"
        ] = total_chunks

        transcription_jobs[job_id][
            "progress"
        ] = 100

        transcription_jobs[job_id][
            "text"
        ] = final_text

        transcription_jobs[job_id][
            "detected_language"
        ] = detected_language

        print(
            f"[{job_id}] "
            "Transcription successful."
        )

        print(
            f"[{job_id}] "
            f"Total chunks: {total_chunks}"
        )

        print(
            f"[{job_id}] "
            f"Transcript length: "
            f"{len(final_text)} characters"
        )

        # Record one successful Free-plan transcription
        # for the daily limit.
        if user_id is not None:

            with app.app_context():

                subscription = (
                    get_active_subscription(
                        user_id
                    )
                )

                if (
                    subscription
                    and subscription.plan.lower()
                    == "free"
                ):

                    db.session.add(
                        CreditTransaction(
                            user_id=user_id,
                            action="transcription",
                            credits_used=0,
                            recording_id=job_id,
                        )
                    )

                    db.session.commit()

    except Exception as e:

        print(
            f"[{job_id}] "
            "TRANSCRIPTION ERROR:"
        )

        print(e)

        transcription_jobs[job_id][
            "status"
        ] = "failed"

        transcription_jobs[job_id][
            "error"
        ] = str(e)

    finally:

        # ----------------------------------------------------
        # DELETE ORIGINAL FILE
        # ----------------------------------------------------

        if (
            temp_path
            and os.path.exists(temp_path)
        ):

            try:

                os.remove(
                    temp_path
                )

            except Exception as cleanup_error:

                print(
                    "Could not delete "
                    "temporary file:",
                    cleanup_error
                )

        # ----------------------------------------------------
        # DELETE CHUNKS
        # ----------------------------------------------------

        if (
            chunk_directory
            and os.path.exists(
                chunk_directory
            )
        ):

            try:

                shutil.rmtree(
                    chunk_directory
                )

            except Exception as cleanup_error:

                print(
                    "Could not delete "
                    "chunk directory:",
                    cleanup_error
                )


# ============================================================
# SUBSCRIPTION TRANSCRIPTION LIMIT HELPERS
# ============================================================

def free_transcription_remaining_today(user_id):
    """Return remaining Free-plan transcriptions for the current UTC day."""
    from datetime import datetime, timezone

    start_of_day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    used = CreditTransaction.query.filter(
        CreditTransaction.user_id == user_id,
        CreditTransaction.action == "transcription",
        CreditTransaction.created_at >= start_of_day,
    ).count()
    daily_limit = get_daily_transcription_limit(user_id)
    if daily_limit is None:
        return None
    return max(0, daily_limit - used)


def get_audio_duration_seconds(file_path):
    """Return media duration using ffprobe."""
    command = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path,
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    return float(result.stdout.strip())


# ============================================================
# VIDEO TRANSCRIPTION
# ============================================================

@app.route(
    "/api/transcribe-video",
    methods=["POST"]
)
def transcribe_video():

    # --------------------------------------------------------
    # AUTHENTICATE USER
    # --------------------------------------------------------

    user, auth_error = get_authenticated_user()

    if auth_error:
        return auth_error

    # --------------------------------------------------------
    # CHECK ENTERPRISE SUBSCRIPTION
    # --------------------------------------------------------

    subscription = get_active_subscription(user.id)

    if not subscription:
        return jsonify({
            "success": False,
            "error": (
                "Video transcription is available "
                "on the Enterprise plan only."
            ),
            "code": "video_upload_not_allowed",
        }), 403

    plan = subscription.plan.lower()

    if plan != "enterprise":
        return jsonify({
            "success": False,
            "error": (
                "Video transcription is available "
                "on the Enterprise plan only."
            ),
            "code": "video_upload_not_allowed",
        }), 403

    # --------------------------------------------------------
    # CHECK VIDEO FILE
    # --------------------------------------------------------

    if "video" not in request.files:
        return jsonify({
            "success": False,
            "error": "No video file provided."
        }), 400

    video_file = request.files["video"]

    if not video_file.filename:
        return jsonify({
            "success": False,
            "error": "No video filename provided."
        }), 400

    # --------------------------------------------------------
    # ALLOWED VIDEO EXTENSIONS
    # --------------------------------------------------------

    allowed_extensions = {
        ".mp4",
        ".mov",
        ".webm",
        ".mkv",
        ".avi",
        ".m4v",
    }

    original_filename = (
        video_file.filename
        or "video.mp4"
    )

    _, file_extension = os.path.splitext(
        original_filename
    )

    file_extension = file_extension.lower()

    if file_extension not in allowed_extensions:
        return jsonify({
            "success": False,
            "error": (
                "Unsupported video format. "
                "Please upload MP4, MOV, WebM, MKV, AVI, or M4V."
            ),
            "code": "unsupported_video_format",
        }), 400

    temp_path = None

    # --------------------------------------------------------
    # CREATE TRANSCRIPTION JOB
    # --------------------------------------------------------

    job_id = str(uuid.uuid4())

    transcription_jobs[job_id] = {
        "status": "processing",
        "completed_chunks": 0,
        "total_chunks": 0,
        "progress": 0,
        "text": None,
        "detected_language": None,
        "error": None,
    }

    try:

        # ----------------------------------------------------
        # SAVE VIDEO TEMPORARILY
        # ----------------------------------------------------

        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix=file_extension,
        ) as temp:

            video_file.save(temp.name)

            temp_path = temp.name

        file_size = os.path.getsize(
            temp_path
        )

        print(
            f"[{job_id}] "
            f"Video received: "
            f"{file_size} bytes"
        )

        # ----------------------------------------------------
        # CHECK VIDEO DURATION
        # ----------------------------------------------------

        try:

            duration_seconds = (
                get_audio_duration_seconds(
                    temp_path
                )
            )

        except Exception as duration_error:

            print(
                f"[{job_id}] "
                "Could not determine video duration:"
            )

            print(duration_error)

            if (
                temp_path
                and os.path.exists(temp_path)
            ):
                os.remove(temp_path)

            transcription_jobs.pop(
                job_id,
                None
            )

            return jsonify({
                "success": False,
                "error": (
                    "Could not read the duration "
                    "of the uploaded video."
                ),
                "code": "video_duration_error",
            }), 400

        print(
            f"[{job_id}] "
            f"Video duration: "
            f"{duration_seconds:.2f} seconds"
        )

        # ----------------------------------------------------
        # CHECK THAT VIDEO HAS AUDIO
        # ----------------------------------------------------

        audio_check_command = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            temp_path,
        ]

        audio_check = subprocess.run(
            audio_check_command,
            capture_output=True,
            text=True,
        )

        if (
            audio_check.returncode != 0
            or not audio_check.stdout.strip()
        ):

            if (
                temp_path
                and os.path.exists(temp_path)
            ):
                os.remove(temp_path)

            transcription_jobs.pop(
                job_id,
                None
            )

            return jsonify({
                "success": False,
                "error": (
                    "The uploaded video does not "
                    "contain an audio track."
                ),
                "code": "video_has_no_audio",
            }), 400

        # ----------------------------------------------------
        # START EXISTING TRANSCRIPTION WORKER
        # ----------------------------------------------------

        transcription_thread = threading.Thread(
            target=process_transcription_job,
            args=(
                job_id,
                temp_path,
                file_extension,
                user.id,
            ),
            daemon=True,
        )

        transcription_thread.start()

        print(
            f"[{job_id}] "
            "Video transcription started."
        )

        # ----------------------------------------------------
        # RETURN JOB INFORMATION
        # ----------------------------------------------------

        return jsonify({
            "success": True,
            "job_id": job_id,
            "status": "processing",
            "completed_chunks": 0,
            "total_chunks": 0,
            "progress": 0,
            "duration_seconds": duration_seconds,
            "message": (
                "Video transcription started successfully."
            ),
        }), 202

    except Exception as error:

        print(
            f"[{job_id}] "
            "VIDEO TRANSCRIPTION START ERROR:"
        )

        print(error)

        transcription_jobs[job_id][
            "status"
        ] = "failed"

        transcription_jobs[job_id][
            "error"
        ] = str(error)

        if (
            temp_path
            and os.path.exists(temp_path)
        ):

            try:

                os.remove(temp_path)

            except Exception as cleanup_error:

                print(
                    "Could not delete "
                    "temporary video file:",
                    cleanup_error
                )

        return jsonify({
            "success": False,
            "error": (
                "Unable to start video transcription."
            ),
            "code": "video_transcription_start_error",
        }), 500
    

# ============================================================
# TRANSCRIPTION
# ============================================================

@app.route(
    "/api/transcribe",
    methods=["POST"]
)
def transcribe():

    # --------------------------------------------------------
    # AUTHENTICATE USER
    # --------------------------------------------------------

    user, auth_error = get_authenticated_user()
    if auth_error:
        return auth_error

    subscription = get_active_subscription(user.id)

    # Users without an active paid subscription are treated as Free.
    if subscription:
        plan = subscription.plan.lower()
    else:
        plan = "free"

    config = get_plan_config(plan)
    source = (request.form.get("source") or "recording").strip().lower()
    if source in {"upload_audio", "file", "uploaded"}:
        source = "upload"

    if source == "upload" and not config.get("upload", False):
        return jsonify({
            "success": False,
            "error": "Audio upload and file transcription are available on the Enterprise plan only.",
            "code": "upload_not_allowed",
        }), 403

    if source == "meeting" and not config.get("meeting_record", False):
        return jsonify({
            "success": False,
            "error": "Meeting recording is available on the Enterprise plan only.",
            "code": "meeting_record_not_allowed",
        }), 403

    if not config.get("record", False):
        return jsonify({"success": False, "error": "Your current subscription plan does not allow transcription."}), 403

    if plan == "free" and free_transcription_remaining_today(user.id) <= 0:
        return jsonify({
            "success": False,
            "error": "Your Free plan allows 2 transcriptions per day. Please try again tomorrow or upgrade your plan.",
            "code": "daily_transcription_limit_reached",
        }), 403

    # --------------------------------------------------------
    # CHECK THAT AN AUDIO FILE WAS UPLOADED
    # --------------------------------------------------------

    if "audio" not in request.files:

        return jsonify({
            "success": False,
            "error": "No audio file provided"
        }), 400

    audio_file = request.files["audio"]

    # --------------------------------------------------------
    # CHECK FILENAME
    # --------------------------------------------------------

    if audio_file.filename == "":

        return jsonify({
            "success": False,
            "error": "No audio filename provided"
        }), 400

    temp_path = None

    # --------------------------------------------------------
    # CREATE UNIQUE TRANSCRIPTION JOB
    # --------------------------------------------------------

    job_id = str(
        uuid.uuid4()
    )

    transcription_jobs[job_id] = {
        "status": "processing",
        "completed_chunks": 0,
        "total_chunks": 0,
        "progress": 0,
        "text": None,
        "error": None
    }

    try:

        # ----------------------------------------------------
        # PRESERVE FILE EXTENSION
        # ----------------------------------------------------

        original_filename = (
            audio_file.filename
            or "recording.webm"
        )

        _, file_extension = os.path.splitext(
            original_filename
        )

        if not file_extension:

            file_extension = ".webm"

        # ----------------------------------------------------
        # SAVE ORIGINAL AUDIO
        # ----------------------------------------------------

        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix=file_extension
        ) as temp:

            audio_file.save(
                temp.name
            )

            temp_path = temp.name

        file_size = os.path.getsize(
            temp_path
        )

        print(
            f"[{job_id}] "
            f"Audio received: "
            f"{file_size} bytes"
        )


        # ----------------------------------------------------
        # FREE PLAN 30-MINUTE RECORDING LIMIT
        # ----------------------------------------------------

        max_recording_minutes = config.get(
            "max_recording_minutes"
        )

        if (
            plan == "free"
            and max_recording_minutes is not None
        ):
            duration_seconds = get_audio_duration_seconds(
                temp_path
            )

            max_duration_seconds = (
                max_recording_minutes * 60
            )

            if duration_seconds > max_duration_seconds:
                os.remove(temp_path)

                transcription_jobs.pop(
                    job_id,
                    None
                )

                return jsonify({
                    "success": False,
                    "error": (
                        "Free plan recordings are limited "
                        f"to {max_recording_minutes} minutes."
                    ),
                    "code": "recording_duration_limit_reached",
                }), 403

        # ----------------------------------------------------
        # START BACKGROUND WORKER
        # ----------------------------------------------------

        transcription_thread = threading.Thread(
            target=process_transcription_job,
            args=(
                job_id,
                temp_path,
                file_extension,
                user.id,
            ),
            daemon=True
        )

        transcription_thread.start()

        print(
            f"[{job_id}] "
            "Background transcription started."
        )

        # ----------------------------------------------------
        # RETURN IMMEDIATELY
        # ----------------------------------------------------

        return jsonify({
            "success": True,
            "job_id": job_id,
            "status": "processing",
            "completed_chunks": 0,
            "total_chunks": 0,
            "progress": 0,
            "message": (
                "Transcription started successfully."
            )
        }), 202

    except Exception as e:

        print(
            f"[{job_id}] "
            "TRANSCRIPTION START ERROR:"
        )

        print(e)

        transcription_jobs[job_id][
            "status"
        ] = "failed"

        transcription_jobs[job_id][
            "error"
        ] = str(e)

        # ----------------------------------------------------
        # CLEANUP IF WORKER DID NOT START
        # ----------------------------------------------------

        if (
            temp_path
            and os.path.exists(temp_path)
        ):

            try:

                os.remove(
                    temp_path
                )

            except Exception as cleanup_error:

                print(
                    "Could not delete "
                    "temporary file:",
                    cleanup_error
                )

        return jsonify({
            "success": False,
            "job_id": job_id,
            "error": str(e)
        }), 500


# --------------------------------------------------------
# TRANSCRIPTION STATUS
# --------------------------------------------------------

@app.route(
    "/api/transcription-status/<job_id>",
    methods=["GET"]
)
def transcription_status(job_id):

    user, auth_error = get_authenticated_user()

    if auth_error:
        return auth_error

    job = transcription_jobs.get(job_id)

    if not job:
        return jsonify({
            "success": False,
            "error": "Transcription job not found.",
            "code": "job_not_found",
        }), 404

    return jsonify({
        "success": True,
        "job_id": job_id,
        "status": job.get("status"),
        "completed_chunks": job.get("completed_chunks", 0),
        "total_chunks": job.get("total_chunks", 0),
        "progress": job.get("progress", 0),
        "text": job.get("text"),
        "detected_language": job.get("detected_language"),
        "error": job.get("error"),
    }), 200


# ============================================================
# TRANSCRIPTION HISTORY
# ============================================================


def cleanup_expired_transcriptions(user_id=None):
    """
    Remove transcription history records that have passed
    their 30-day retention period.

    If user_id is provided, only that user's records are
    cleaned up.
    """

    now = datetime.now(timezone.utc)

    query = TranscriptionHistory.query.filter(
        TranscriptionHistory.expires_at <= now
    )

    if user_id is not None:
        query = query.filter(
            TranscriptionHistory.user_id == user_id
        )

    expired_records = query.all()

    for record in expired_records:
        db.session.delete(record)

    if expired_records:
        db.session.commit()

    return len(expired_records)


@app.route(
    "/api/transcriptions",
    methods=["GET"]
)
def get_transcriptions():
    """
    Return the authenticated user's transcription history.

    Records older than 30 days are removed automatically.
    The Flutter app can request the latest 10 records or
    retrieve more using the limit parameter.
    """

    user, auth_error = get_authenticated_user()

    if auth_error:
        return auth_error

    try:
        cleanup_expired_transcriptions(user.id)

        limit = request.args.get(
            "limit",
            10,
            type=int
        )

        if limit < 1:
            limit = 10

        # Prevent unnecessarily large requests.
        limit = min(limit, 100)

        records = (
            TranscriptionHistory.query
            .filter_by(user_id=user.id)
            .order_by(
                TranscriptionHistory.created_at.desc()
            )
            .limit(limit)
            .all()
        )

        return jsonify({
            "success": True,
            "transcriptions": [
                record.to_dict()
                for record in records
            ],
            "count": len(records)
        }), 200

    except Exception as e:

        db.session.rollback()

        print(
            "Transcription history GET error:",
            str(e)
        )

        return jsonify({
            "success": False,
            "error": "Unable to load transcription history."
        }), 500


@app.route(
    "/api/transcriptions",
    methods=["POST"]
)
def create_transcription():
    """
    Save a raw transcription to the authenticated user's
    30-day transcription history.
    """

    user, auth_error = get_authenticated_user()

    if auth_error:
        return auth_error

    try:
        data = request.get_json(
            silent=True
        ) or {}

        transcript = (
            data.get("transcript") or ""
        ).strip()

        if not transcript:
            return jsonify({
                "success": False,
                "error": "Transcript is required."
            }), 400

        title = (
            data.get("title")
            or "Untitled Transcription"
        ).strip()

        if not title:
            title = "Untitled Transcription"

        duration_seconds = data.get(
            "duration_seconds"
        )

        if duration_seconds is not None:
            try:
                duration_seconds = int(
                    duration_seconds
                )
            except (
                ValueError,
                TypeError
            ):
                duration_seconds = None

        now = datetime.now(timezone.utc)

        expires_at = now + timedelta(
            days=30
        )

        transcription = TranscriptionHistory(
            user_id=user.id,
            title=title[:255],
            transcript=transcript,
            duration_seconds=duration_seconds,
            created_at=now,
            expires_at=expires_at,
        )

        db.session.add(transcription)
        db.session.commit()

        return jsonify({
            "success": True,
            "message": "Transcription saved.",
            "transcription": (
                transcription.to_dict()
            )
        }), 201

    except Exception as e:

        db.session.rollback()

        print(
            "Transcription history POST error:",
            str(e)
        )

        return jsonify({
            "success": False,
            "error": "Unable to save transcription."
        }), 500


@app.route(
    "/api/transcriptions/<int:transcription_id>",
    methods=["PATCH"]
)
def update_transcription(
    transcription_id
):
    """
    Edit a user's transcription history item.
    """

    user, auth_error = get_authenticated_user()

    if auth_error:
        return auth_error

    try:
        cleanup_expired_transcriptions(
            user.id
        )

        transcription = (
            TranscriptionHistory.query
            .filter_by(
                id=transcription_id,
                user_id=user.id
            )
            .first()
        )

        if not transcription:
            return jsonify({
                "success": False,
                "error": "Transcription not found."
            }), 404

        data = request.get_json(
            silent=True
        ) or {}

        if "title" in data:

            title = (
                data.get("title") or ""
            ).strip()

            if title:
                transcription.title = (
                    title[:255]
                )

        if "transcript" in data:

            transcript = (
                data.get("transcript") or ""
            ).strip()

            if not transcript:
                return jsonify({
                    "success": False,
                    "error": "Transcript cannot be empty."
                }), 400

            transcription.transcript = transcript

        if "structured_report" in data:

            structured_report = (
                data.get("structured_report") or ""
            ).strip()

            if structured_report:
                transcription.structured_report = (
                    structured_report
                )
            else:
                transcription.structured_report = None

        if "structured_report_type" in data:

            structured_report_type = (
                data.get("structured_report_type") or ""
            ).strip()

            if structured_report_type:
                transcription.structured_report_type = (
                    structured_report_type[:100]
                )
            else:
                transcription.structured_report_type = None

        if "translated_transcript" in data:

            translated_transcript = (
                data.get("translated_transcript") or ""
            ).strip()

            if translated_transcript:
                transcription.translated_transcript = (
                    translated_transcript
                )
            else:
                transcription.translated_transcript = None

        if "translation_language" in data:

            translation_language = (
                data.get("translation_language") or ""
            ).strip()

            if translation_language:
                transcription.translation_language = (
                    translation_language[:100]
                )
            else:
                transcription.translation_language = None

        db.session.commit()

        return jsonify({
            "success": True,
            "message": "Transcription updated.",
            "transcription": (
                transcription.to_dict()
            )
        }), 200


    except Exception as e:

        db.session.rollback()

        print(
            "Transcription history PATCH error:",
            str(e)
        )

        return jsonify({
            "success": False,
            "error": "Unable to update transcription."
        }), 500


@app.route(
    "/api/transcriptions/<int:transcription_id>",
    methods=["DELETE"]
)
def delete_transcription(
    transcription_id
):
    """
    Delete a user's transcription history item.
    """

    user, auth_error = get_authenticated_user()

    if auth_error:
        return auth_error

    try:
        transcription = (
            TranscriptionHistory.query
            .filter_by(
                id=transcription_id,
                user_id=user.id
            )
            .first()
        )

        if not transcription:
            return jsonify({
                "success": False,
                "error": "Transcription not found."
            }), 404

        db.session.delete(
            transcription
        )

        db.session.commit()

        return jsonify({
            "success": True,
            "message": "Transcription deleted."
        }), 200

    except Exception as e:

        db.session.rollback()

        print(
            "Transcription history DELETE error:",
            str(e)
        )

        return jsonify({
            "success": False,
            "error": "Unable to delete transcription."
        }), 500


# ============================================================
# STRUCTURED REPORT GENERATION
# ============================================================

def split_transcript_for_report(transcript, max_chars=5000):
    """
    Split a long transcript into manageable sections.

    Splitting prefers paragraph, line, sentence and word
    boundaries so that content is not unnecessarily broken.
    """
    transcript = (transcript or "").strip()

    if not transcript:
        return []

    if len(transcript) <= max_chars:
        return [transcript]

    chunks = []
    remaining = transcript

    while len(remaining) > max_chars:

        split_at = remaining.rfind("\n\n", 0, max_chars)

        if split_at < max_chars * 0.50:
            split_at = remaining.rfind("\n", 0, max_chars)

        if split_at < max_chars * 0.50:
            split_at = remaining.rfind(". ", 0, max_chars)

            if split_at != -1:
                split_at += 1

        if split_at < max_chars * 0.50:
            split_at = remaining.rfind(" ", 0, max_chars)

        if split_at <= 0:
            split_at = max_chars

        chunk = remaining[:split_at].strip()

        if chunk:
            chunks.append(chunk)

        remaining = remaining[split_at:].strip()

    if remaining:
        chunks.append(remaining)

    return chunks


def generate_structured_report_section(
    system_prompt,
    report_type,
    report_instructions,
    transcript_section,
):
    """
    Generate a structured report from one transcript section.

    Automatically retries when Groq returns a 429 TPM rate-limit
    response. This prevents a temporary rate limit from causing
    the entire structured report request to fail.
    """

    import time
    import re

    user_prompt = f"""
Report type:
{report_type}

Instructions:
{report_instructions}

Transcript section:
--------------------
{transcript_section}
--------------------
"""

    max_retries = 8

    for attempt in range(max_retries):
        try:
            completion = client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=[
                    {
                        "role": "system",
                        "content": system_prompt,
                    },
                    {
                        "role": "user",
                        "content": user_prompt,
                    },
                ],
                temperature=0.2,
            )

            report = (
                completion.choices[0].message.content
                or ""
            ).strip()

            if not report:
                raise ValueError(
                    "The model returned an empty report section."
                )

            return report

        except Exception as error:
            error_text = str(error)

            # Groq TPM rate limit
            if "429" in error_text or "rate_limit_exceeded" in error_text:
                wait_seconds = 20.0

                # Try to extract Groq's suggested wait time.
                match = re.search(
                    r"Please try again in ([0-9.]+)s",
                    error_text,
                    re.IGNORECASE,
                )

                if match:
                    try:
                        wait_seconds = float(match.group(1)) + 2.0
                    except ValueError:
                        pass

                # Never wait less than 2 seconds.
                wait_seconds = max(wait_seconds, 2.0)

                # Add a small increase on later retries.
                wait_seconds += attempt * 2

                print(
                    "STRUCTURED REPORT RATE LIMIT:",
                    f"attempt {attempt + 1}/{max_retries}",
                    f"waiting {wait_seconds:.1f}s before retry",
                )

                if attempt == max_retries - 1:
                    raise

                time.sleep(wait_seconds)
                continue

            # Any non-rate-limit error should behave as before.
            raise

    raise RuntimeError(
        "Unable to generate structured report section after retries."
    )


@app.route(
    "/api/structured-report",
    methods=["POST"]
)
def structured_report():

    user, auth_error = get_authenticated_user()

    if auth_error:
        return auth_error

    try:
        data = request.get_json(silent=True) or {}

        transcript = (
            data.get("transcript") or ""
        ).strip()

        report_type = (
            data.get("report_type") or ""
        ).strip().lower()

        if not transcript:
            return jsonify({
                "success": False,
                "error": "Transcript is required.",
                "code": "transcript_required",
            }), 400

        allowed_report_types = {
            "meeting_minutes",
            "business_report",
            "lecture_notes",
            "interview_report",
            "general_report",
            "summary_key_points",
            "action_items",
        }

        if report_type not in allowed_report_types:
            return jsonify({
                "success": False,
                "error": "Invalid report type.",
                "code": "invalid_report_type",
                "allowed_report_types": sorted(
                    allowed_report_types
                ),
            }), 400

        report_instructions = {
            "meeting_minutes": """
Create professional meeting minutes.

Use these sections where supported by the transcript:
1. Meeting Title
2. Date/Time
3. Participants
4. Agenda
5. Discussion Points
6. Decisions
7. Action Items
8. Next Steps
9. Conclusion

Do not invent participants, dates, decisions,
deadlines or other information that is not supported
by the transcript.
""",

            "business_report": """
Create a professional business report based strictly on the transcript.

Use these sections where supported:
1. Title
2. Executive Summary
3. Background
4. Key Issues
5. Findings
6. Discussion
7. Recommendations
8. Action Points
9. Conclusion

STRICT SOURCE-BASED RULES:
- Use ONLY information explicitly stated in the transcript.
- Do not invent, infer, assume, or add facts that are not stated.
- Do not create new recommendations.
- Under "Recommendations", include only recommendations explicitly made or stated in the transcript.
- Do not turn your own analysis into a recommendation.
- Do not create statistics, targets, percentages, deadlines, dates, names, participants, decisions, causes, risks, strategies, or business outcomes that are not stated.
- Do not create action items that were not stated in the transcript.
- Do not create owners or responsible persons unless explicitly identified.
- Do not create deadlines unless explicitly stated.
- If a section is not supported by the transcript, write "Not specified" rather than filling the gap with assumptions.
- You may reorganize, summarize, and improve the wording of information that is already in the transcript, but you must preserve its original meaning.
""",

            "lecture_notes": """
Convert the transcript into organized lecture/study notes.

Use:
1. Topic
2. Main Concepts
3. Key Points
4. Explanations
5. Examples
6. Important Terms
7. Summary
8. Study Points

Preserve important technical terminology.
Do not invent information that was not discussed.
""",

            "interview_report": """
Create an organized interview report.

Use:
1. Interview Subject
2. Main Topics
3. Key Questions
4. Responses
5. Important Statements
6. Key Findings
7. Summary

Do not invent answers, names or facts.
""",

            "general_report": """
Create a clear professional general report based strictly on the transcript.

Use:
1. Title
2. Overview
3. Key Points
4. Detailed Discussion
5. Findings
6. Conclusion
7. Recommendations

STRICT SOURCE-BASED RULES:
- Use ONLY information explicitly stated in the transcript.
- Do not invent, infer, assume, or add facts that are not stated.
- Do not create new recommendations.
- Under "Recommendations", include only recommendations explicitly stated in the transcript.
- Do not turn your own analysis or interpretation into a recommendation.
- Do not create new conclusions that go beyond the transcript.
- Do not create statistics, targets, deadlines, dates, names, participants, decisions, causes, risks, strategies, or outcomes that are not stated.
- Do not create action items that were not stated.
- If a section is not supported by the transcript, write "Not specified".
- You may reorganize and summarize information from the transcript, but preserve its original meaning.
""",

            "summary_key_points": """
Create a concise summary based strictly on the transcript.

Use:
1. Summary
2. Key Points
3. Important Information
4. Conclusions

STRICT SOURCE-BASED RULES:
- Use ONLY information explicitly stated in the transcript.
- Do not invent, infer, assume, or add facts that are not stated.
- Do not create new conclusions or opinions.
- Under "Conclusions", include only conclusions explicitly stated in the transcript.
- Do not turn your own interpretation into a conclusion.
- Do not create new recommendations, action items, decisions, causes, outcomes, targets, deadlines, dates, names, or statistics.
- Preserve names, organisations, locations, numbers, dates, and terminology exactly where provided.
- You may shorten and reorganize the transcript, but preserve its original meaning.
- If a section is not supported by the transcript, write "Not specified".
- Avoid unnecessary repetition.
""",

            "action_items": """
Extract actionable tasks from the transcript.

Use a table with:
- Action Item
- Responsible Person
- Deadline
- Priority
- Notes

STRICT SOURCE-BASED RULES:
- Extract ONLY tasks or actions explicitly stated in the transcript.
- Do not invent, infer, assume, or create new tasks.
- Do not turn recommendations, observations, discussion points, or general statements into tasks unless the transcript explicitly presents them as actions to be taken.
- Include a Responsible Person only when explicitly stated or clearly assigned in the transcript.
- Include a Deadline only when explicitly stated.
- Include a Priority only when explicitly stated.
- Do not infer priority from the importance or urgency of an action.
- Do not create dates, deadlines, names, owners, priorities, or responsibilities.
- If a responsible person, deadline, or priority is not provided, write "Not specified".
- Preserve the original meaning of each action.
- If the transcript contains no actionable tasks, state "No actionable items specified."
""",
        }

        system_prompt = """
You are NabTranscriber Report Assistant.

Your job is to transform an existing speech transcript
into a professional structured report.

Important rules:

1. Do not fabricate facts.
2. Do not invent names, dates, participants,
   decisions, deadlines or quotations.
3. Preserve the meaning of the original transcript.
4. Correct obvious transcription formatting problems
   where the intended meaning is clear.
5. Preserve Nigerian names, locations, organizations,
   government agencies, companies and local terminology.
6. Recognize Nigerian English usage and do not
   automatically replace Nigerian expressions with
   American or British alternatives.
7. If Nigerian English is mixed with Yoruba, Igbo,
   Hausa, Pidgin or another language, preserve the
   original meaning and important local terms.
8. Use clear professional English for the report while
   preserving important original terminology.
9. If information is unavailable, use "Not specified"
   rather than inventing it.
10. Return only the requested report.
"""

        chunks = split_transcript_for_report(
            transcript,
            max_chars=5000,
        )

        print(
            "STRUCTURED REPORT:",
            f"{len(transcript):,} characters,",
            f"{len(chunks)} section(s)"
        )

        # Normal/small transcript
        if len(chunks) == 1:

            report = generate_structured_report_section(
                system_prompt=system_prompt,
                report_type=report_type,
                report_instructions=report_instructions[
                    report_type
                ],
                transcript_section=chunks[0],
            )

        # Long transcript
        else:

            section_reports = []

            for index, chunk in enumerate(
                chunks,
                start=1
            ):

                print(
                    "STRUCTURED REPORT SECTION:",
                    f"{index}/{len(chunks)}"
                )

                section_report = (
                    generate_structured_report_section(
                        system_prompt=system_prompt,
                        report_type=report_type,
                        report_instructions=(
                            report_instructions[
                                report_type
                            ]
                        ),
                        transcript_section=chunk,
                    )
                )

                section_reports.append(
                    f"""
SOURCE SECTION {index}
====================
{section_report}
"""
                )

            # The individual section reports have already been
            # generated by the model. Do not send all of them back
            # to the model for a second consolidation request because
            # Groq's GPT-OSS-120B on-demand tier has an 8,000-token
            # request/TPM limit.
            #
            # Combining the generated sections locally avoids the
            # oversized second model request while preserving the
            # source-based report content.

            report = "\n\n".join(
                section.strip()
                for section in section_reports
                if section.strip()
            )

        if not report:
            return jsonify({
                "success": False,
                "error": "The report could not be generated.",
                "code": "empty_report",
            }), 500

        return jsonify({
            "success": True,
            "report_type": report_type,
            "report": report,
        }), 200

    except Exception as e:

        print("=" * 60)
        print("STRUCTURED REPORT ERROR")
        print("=" * 60)
        print(e)

        return jsonify({
            "success": False,
            "error": (
                "Unable to generate the structured report "
                "at this time."
            ),
            "code": "structured_report_error",
        }), 500

# ============================================================
# TRANSCRIPT TRANSLATION
# ============================================================

@app.route(
    "/api/translate",
    methods=["POST"]
)
def translate_transcript():

    user, auth_error = get_authenticated_user()

    if auth_error:
        return auth_error

    try:
        data = request.get_json(
            silent=True
        ) or {}

        transcript = (
            data.get("transcript") or ""
        ).strip()

        target_language = (
            data.get("target_language") or ""
        ).strip()

        if not transcript:
            return jsonify({
                "success": False,
                "error": "Transcript is required."
            }), 400

        if not target_language:
            return jsonify({
                "success": False,
                "error": "Target language is required."
            }), 400

        allowed_languages = {
            "English",
            "Yoruba",
            "Igbo",
            "Hausa",
            "Nigerian Pidgin",
            "French",
            "Spanish",
            "Portuguese",
            "Arabic",
            "German",
            "Chinese",
            "Japanese",
            "Korean",
        }

        if target_language not in allowed_languages:
            return jsonify({
                "success": False,
                "error": "Unsupported target language."
            }), 400

        system_prompt = """
You are the translation engine for NabTranscriber.

Translate the supplied transcript into the requested target language.

STRICT RULES:

1. Translate the meaning accurately.
2. Do not add information that is not in the transcript.
3. Do not remove important information.
4. Preserve the original structure and meaning where practical.
5. Preserve names of people exactly.
6. Preserve company names exactly.
7. Preserve government agencies and acronyms exactly.
8. Preserve locations such as Abuja, Lagos, Kano, Ibadan,
   Port Harcourt, etc. unless the target language has a standard
   translated form that is clearly appropriate.
9. Preserve numbers, dates, times, amounts and percentages accurately.
10. Preserve technical terms where translating them would change their meaning.
11. Preserve Nigerian context and terminology.
12. For Nigerian Pidgin, use natural Nigerian Pidgin.
13. For Yoruba, use natural standard Yoruba.
14. For Igbo, use natural standard Igbo.
15. For Hausa, use natural standard Hausa.
16. Do not explain the translation.
17. Return only the translated transcript.
"""

        user_prompt = f"""
Target language: {target_language}

Transcript:

{transcript}
"""

        completion = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": user_prompt,
                },
            ],
            temperature=0.2,
        )

        translation = (
            completion.choices[0]
            .message
            .content
            .strip()
        )

        if not translation:
            return jsonify({
                "success": False,
                "error": "Translation returned an empty result."
            }), 500

        return jsonify({
            "success": True,
            "target_language": target_language,
            "translation": translation,
        }), 200

    except Exception as e:

        print(
            "Translation error:",
            str(e)
        )

        return jsonify({
            "success": False,
            "error": "Unable to translate transcript."
        }), 500

# ============================================================
# APPLICATION START
# ============================================================

if __name__ == "__main__":

    print("")
    print("=" * 50)
    print("VOICE TRANSCRIBER BACKEND")
    print("=" * 50)
    print("Provider: Groq")
    print("Model: whisper-large-v3-turbo")
    print(
        "Database:",
        database_url
    )
    print(
        "Server: "
        "https://voice-transcriber-3982.onrender.com"
    )
    print("=" * 50)
    print("")


    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True
    )


















