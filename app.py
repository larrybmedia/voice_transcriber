import os
import httpx
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

from flask import Flask, request, jsonify, Response
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
                "https://nabtranscriber.com.ng",
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
    Generate one structured report section from a transcript.

    Retries Groq rate-limit errors and performs a targeted
    accuracy check for General Reports.
    """
    import time
    import re

    user_prompt = f"""
Report type:
{report_type}

Instructions:
{report_instructions}

Additional source-accuracy requirements:
- Treat the transcript as the only evidence for factual claims.
- A request or instruction is not proof of agreement or commitment.
- Do not invent recommendations, deadlines, mechanisms,
  decisions, or follow-up meetings.
- Do not describe an inferred objective as an official objective.
- If a detail is not supported by the transcript, say so.
- Follow the required report headings exactly.

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
                completion.choices[0].message.content or ""
            ).strip()

            if not report:
                raise ValueError(
                    "The model returned an empty report section."
                )

            # Targeted check for unsupported claims in General Reports.
            if report_type == "general_report":
                unsupported_patterns = [
                    r"implicit commitment",
                    r"implicit agreement",
                    r"key recommendation derived from the discussion",
                    r"establish a clear deadline",
                    r"establishing a clear deadline",
                ]

                if any(
                    re.search(pattern, report, re.IGNORECASE)
                    for pattern in unsupported_patterns
                ):
                    correction_prompt = f"""
Review the report below against the source transcript.

Remove unsupported recommendations, invented deadlines,
unconfirmed agreements, and implied commitments.

A request from a chair must not be described as an agreement
or commitment unless the source explicitly confirms acceptance.

Do not add new facts. Keep the existing report structure and
all required headings. Preserve every source-supported fact.

SOURCE TRANSCRIPT:
{transcript_section}

REPORT TO CORRECT:
{report}

Return only the corrected report.
"""

                    correction = client.chat.completions.create(
                        model="openai/gpt-oss-120b",
                        messages=[
                            {
                                "role": "system",
                                "content": (
                                    "You are a strict factual editor. "
                                    "Use only the supplied source. "
                                    "Do not invent or infer facts."
                                ),
                            },
                            {
                                "role": "user",
                                "content": correction_prompt,
                            },
                        ],
                        temperature=0.0,
                    )

                    corrected_report = (
                        correction.choices[0].message.content or ""
                    ).strip()

                    if corrected_report:
                        report = corrected_report

            return report

        except Exception as error:
            error_text = str(error)

            if (
                "429" in error_text
                or "rate_limit_exceeded" in error_text
            ):
                wait_seconds = 20.0

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

                wait_seconds = max(wait_seconds, 2.0)
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

ADDITIONAL PROFESSIONAL MEETING MINUTES REQUIREMENTS

PURPOSE AND STANDARD

Prepare formal, comprehensive and factual Minutes of Meeting (MoM) suitable for official government, corporate, institutional or organisational records.

Minutes must accurately document the meeting's proceedings, material contributions, confirmed decisions, assigned responsibilities and follow-up actions. They must be concise enough to avoid reproducing the entire transcript, but detailed enough to preserve all material discussions and resolutions.

Use the following 13 numbered headings in the specified order. Reproduce each heading exactly and include each section once.

1. MEETING DETAILS

Include the organisation, meeting title or purpose, day and date, start and end times, venue or online platform, chairperson, secretary or minute-taker, and other supplied meeting details.

Do not assume that the recording date is the meeting date. Mark unavailable details as "Not specified in the source material."

2. ATTENDANCE

Provide a table with these columns:

- S/N
- Name
- Designation/Department
- Organisation
- Status

Record attendees, apologies, invitees and observers only where the source identifies them. Do not invent names, designations, departments, organisations or attendance status. Do not assume that a speaker list represents the complete attendance register.

3. AGENDA OF THE MEETING

List agenda items in the order they were considered, where the source establishes that order. Distinguish a formally stated agenda from topics that merely arose during the discussion.

Do not invent routine agenda items such as opening prayer, adoption of previous minutes, AOB or closing remarks simply because they commonly appear in meetings.

4. OPENING REMARKS

Identify the person who opened the meeting and summarise the recorded remarks accurately. Do not invent welcoming statements, instructions, themes or expectations.

If opening remarks were not captured, state that they were not specified in the source material.

5. CONFIRMATION OF PREVIOUS MINUTES

Record whether previous minutes were presented, reviewed, corrected, proposed for adoption, seconded or formally adopted, but only where the source supports each detail.

Do not claim that minutes were adopted as a true record without explicit evidence. If this item was not discussed or the relevant information is unavailable, state that accurately.

6. MATTERS ARISING FROM THE PREVIOUS MEETING

Document previous decisions or action points and their recorded progress, responsible persons or units, and status.

Do not classify an item as completed, ongoing, delayed or pending unless the source supports that status. Do not invent previous decisions or action items when previous minutes are unavailable.

7. PROCEEDINGS AND DISCUSSIONS

Organise the substantive proceedings under separate agenda-item or topic subheadings.

For each item, document where available:

- Issue or subject considered.
- Material contributions, including names and designations where supplied.
- Important clarifications, questions and responses.
- Evidence, figures, dates and technical details.
- Proposals and recommendations.
- Confirmed decisions or resolutions.
- Actions expressly assigned by the meeting.

Attribute contributions accurately. Do not attribute a collective position to all members when only one person expressed it. Preserve disagreements and unresolved issues where material.

Distinguish discussion from agreement. Do not reproduce every word of the transcript or add generic commentary.

8. DECISIONS AND RESOLUTIONS

Provide a separate numbered list of formal decisions and resolutions explicitly supported by the source.

For each decision, record the decision or resolution, the responsible person or unit if assigned, and any stated conditions or deadlines.

Separate approved decisions from proposals, recommendations, matters deferred for consideration and issues requiring further consultation.

If no formal decisions were recorded, state: "No formal decisions or resolutions were specified in the source material."

9. ACTION POINTS

Provide a table with these columns:

- S/N
- Action Point
- Responsible Officer/Unit
- Deadline
- Status

Include only actions that were expressly assigned, directed or agreed upon in the source. Distinguish a formal assignment from a proposal that remains unapproved.

Do not invent action owners, deadlines, deliverables, targets or status updates. If an action is recorded but its owner or deadline is missing, use "Not specified in the source material."

Do not turn every discussion topic, suggestion or recommendation into an action point.

If no actionable assignments were recorded, state that no specific action points were identified in the source material.

10. ANY OTHER BUSINESS (AOB)

Record additional matters raised outside the principal agenda only when the source identifies them as such or clearly establishes that they were additional business.

Capture any related decisions or actions accurately. Do not invent an AOB discussion merely to complete the template.

11. DATE OF THE NEXT MEETING

Record the next meeting's date, time, venue or platform only where stated or confirmed.

Distinguish a confirmed meeting arrangement from a suggestion to meet again. If the source does not establish a date, state: "Not specified in the source material."

12. CLOSING REMARKS AND ADJOURNMENT

Record the person who delivered closing remarks and the actual adjournment time only when the source supports those details.

Do not infer that the meeting formally closed or that closing remarks were delivered merely because the recording ended.

13. APPROVAL AND AUTHENTICATION

Provide clearly labelled fields for completion where appropriate:

Prepared by:
Name:
Designation:
Signature:
Date:

Reviewed/Approved by:
Name:
Designation:
Signature:
Date:

Use supplied details where available. Otherwise, leave the fields clearly marked "To be completed" rather than inventing names, signatures, approval dates or confirmation of approval.

STRICT MINUTES ACCURACY RULES

- Use the transcript as the primary source of evidence and any additional meeting details explicitly supplied as input.
- Never invent names, designations, attendance, agenda items, dates, times, venues, questions, responses, decisions, resolutions, commitments, action owners or deadlines.
- A request or instruction does not establish acceptance, agreement or commitment by the people receiving it.
- Do not describe a proposal as approved, a discussion as a resolution, or a recommendation as an assigned action.
- Do not claim that a previous meeting's minutes were adopted unless the source explicitly records adoption.
- Do not assume that silence, lack of objection or the presence of participants establishes consensus.
- Preserve differing views and unresolved matters without inventing a conclusion.
- Correct obvious transcription errors only where the intended meaning is clear.
- Preserve Nigerian names, government institutions, designations and local terminology accurately.
- If a section was not discussed, state that it was not recorded or was not discussed, as appropriate.
- If information is missing, use "Not specified in the source material" rather than guessing.
- Do not use "To be determined" to imply that the meeting agreed to determine something later unless the source supports that intention.
- Use neutral, professional English suitable for official records.
- Avoid repetition and generic filler.
- Ensure the decisions and action tracker agree with the proceedings.
- Return one complete set of minutes with all 13 headings exactly once and in the correct order.
- Before returning the minutes, verify all material statements against the source and remove unsupported claims or contradictions.
- Return only the completed minutes, without drafting notes or explanations of these instructions.
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

ADDITIONAL PROFESSIONAL BUSINESS REPORT REQUIREMENTS

REPORTING STANDARD

- Prepare a formal, well-organised business report suitable for Management, executives, departmental heads and other authorised organisational readers.
- Retain the existing nine-section structure and use the section headings in the prescribed order.
- Use clear, professional business English, concise paragraphs, informative subheadings and tables where they improve readability.
- The level of detail must reflect the actual information available. Do not pad a short transcript or omit important source-supported details from a substantive transcript.
- Do not convert a transcript of a meeting, interview, presentation or informal discussion into a formal business decision-making record unless the source supports that interpretation.

1. TITLE

- Use an official report title only when it is supplied or clearly stated in the transcript.
- Include the organisation, department, reporting period, report date and author only where those details are provided.
- Do not invent a report title, company name, reporting officer or reporting period.
- Where the title is unavailable, use "Business Report" without implying that an official title was supplied.

2. EXECUTIVE SUMMARY

- Summarise the principal business issues, documented findings, explicit recommendations and confirmed outcomes contained in the report.
- Ensure every statement in the executive summary is supported by the detailed sections.
- Do not introduce new findings, decisions, recommendations, financial claims or outcomes in the summary.
- Distinguish reported results from proposals and anticipated outcomes.

3. BACKGROUND

- Present the business context, purpose, scope and relevant history only where the transcript provides that information.
- Do not invent the organisation's strategy, market position, business model, institutional mandate, industry conditions or reasons for a business problem.
- Do not use external knowledge to complete missing background information.

4. KEY ISSUES

- Identify the material business issues, concerns, questions or topics explicitly raised in the transcript.
- Distinguish confirmed problems from concerns, opinions, questions and unverified allegations.
- Do not infer causes, severity, financial exposure, urgency or organisational impact unless the source supports those claims.
- Where multiple issues are recorded, organise them clearly without changing their meaning or importance.

5. FINDINGS

- Record findings, observations, results and evidence explicitly reported in the source.
- Preserve stated figures, financial amounts, percentages, dates, targets and units accurately.
- Do not calculate or extrapolate additional business results unless the calculation is directly supported by the supplied data and is clearly labelled.
- Do not describe an opinion, prediction or proposal as an established finding.
- Where the transcript reports no measurable results, do not manufacture performance indicators or conclusions.

6. DISCUSSION

- Present the material arguments, explanations, viewpoints and clarifications recorded in the transcript.
- Attribute important statements to the correct speaker or organisation where identified.
- Preserve material differences of opinion and unresolved questions.
- Do not introduce external business theories, market analysis, management frameworks or explanations that were not supplied in the source.
- Do not imply consensus where the transcript records only individual views or an ongoing discussion.

7. RECOMMENDATIONS

- Include only recommendations explicitly made or stated in the transcript.
- Preserve the original meaning and intended scope of each recommendation.
- Identify the person or organisation making a recommendation only where the source establishes that identity.
- Distinguish recommendations from proposals, observations, questions, instructions and confirmed decisions.
- Do not generate additional recommendations based on the model's own analysis, professional knowledge or assumptions.
- Do not invent priorities, cost estimates, projected benefits, implementation methods, targets or timelines.
- If no recommendations were explicitly recorded, write "Not specified."

8. ACTION POINTS

- Include only actions explicitly assigned, directed or agreed upon in the transcript.
- Where supported, use a table with these columns:
  S/N; Action Point; Responsible Person/Unit; Deadline; Status.
- Do not treat a recommendation or suggested next step as an assigned action unless the source establishes that status.
- Do not invent action owners, departments, deadlines, budgets, performance targets or completion status.
- Preserve distinctions between proposed actions, formally agreed actions and instructions issued by an identified authority.
- Where an action is stated but its owner or deadline is absent, use "Not specified" for the missing field.
- If no action points were explicitly recorded, write "Not specified."

9. CONCLUSION

- Summarise the main source-supported issues, findings, recommendations and outcomes already documented in the report.
- Do not introduce new conclusions, causal explanations, decisions, commitments or recommendations.
- Do not describe the business activity as successful, unsuccessful, profitable, effective or completed unless the transcript explicitly supports that assessment.
- If the source contains unresolved matters, preserve their unresolved status.

SOURCE CONSISTENCY AND QUALITY CONTROL

- Use the transcript as the primary source and any additional information explicitly supplied as input.
- Preserve the original meaning while correcting grammar and obvious transcription errors where the intended meaning is clear.
- Do not invent company names, staff details, customer information, financial amounts, contracts, market data, business risks, strategies, causes, decisions or outcomes.
- Do not treat an expectation, forecast or proposed result as an actual achievement.
- Do not turn a request into an agreement or a discussion into a decision.
- If source statements conflict, retain the distinction and identify the inconsistency where material rather than choosing a version without evidence.
- If the transcript is incomplete, report only the available information and do not imply that the record is comprehensive.
- Use "Not specified" for unsupported sections or missing information, consistently with the existing instructions.
- Keep the report internally consistent. The executive summary, findings, recommendations, action points and conclusion must not contradict one another.
- Use each of the nine sections once, in the prescribed order. Avoid duplicated sections, repeated paragraphs and generic filler.
- Before returning the report, check that every material factual statement is traceable to the source and remove unsupported claims.
- Return only the completed business report, without drafting notes or explanations of these instructions.
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

ADDITIONAL PROFESSIONAL LECTURE NOTES REQUIREMENTS

PURPOSE AND STRUCTURE

- Produce clear, organised and useful lecture or study notes based strictly on the supplied transcript.
- Retain the existing eight-section structure and present the sections in the prescribed order.
- Use informative subheadings, numbered lists, bullet points and tables where they improve understanding.
- Organise the material logically without changing the lecturer's intended meaning.
- Provide enough detail to support revision and learning, but do not repeat the same information unnecessarily.
- Do not turn a short transcript into a lengthy textbook chapter or add material that was not covered.

1. TOPIC

- Use the lecture title, subject or topic only when stated or clearly identified in the transcript.
- If no topic is identified, write "Not specified."
- Do not invent a course title, lecturer's name, institution, date, department or academic level.

2. MAIN CONCEPTS

- Identify the principal concepts and themes explicitly discussed.
- Arrange related concepts into meaningful groups.
- Explain relationships between concepts only where supported by the transcript.
- Do not introduce additional theories, frameworks or subject areas from general knowledge.

3. KEY POINTS

- Capture the important arguments, definitions, principles, observations, conclusions and facts presented.
- Preserve important technical details, figures, dates, formulas and stated relationships accurately.
- Distinguish the lecturer's main points from questions, opinions, speculation and unresolved issues.
- Do not omit material information simply to make the notes shorter.

4. EXPLANATIONS

- Clarify the concepts using the explanations provided in the transcript.
- Reorganise spoken explanations into clear written English without changing their meaning.
- Correct obvious transcription and grammatical errors only where the intended meaning is clear.
- Do not add external explanations, unsupported interpretations or assumed steps to an incomplete explanation.
- If a concept is mentioned but not explained, do not manufacture an explanation.

5. EXAMPLES

- Include examples, case studies, demonstrations and illustrations explicitly mentioned in the transcript.
- Preserve the relationship between each example and the concept it illustrates.
- Do not invent examples or present hypothetical scenarios as examples used by the lecturer.
- If no examples were provided, write "Not specified."

6. IMPORTANT TERMS

- Extract important technical terms, abbreviations, specialist vocabulary and definitions used in the transcript.
- Preserve the original terminology and explain a term only when the source provides its meaning.
- Expand abbreviations only when their full forms are stated or unambiguously established by the supplied material.
- Do not invent definitions or substitute technical terms with similar but potentially different concepts.

7. SUMMARY

- Provide a concise recap of the main concepts and conclusions covered in the transcript.
- Ensure the summary reflects the notes and does not introduce new information.
- Do not claim that the lecture established a conclusion or achieved a learning objective unless the source supports that claim.

8. STUDY POINTS

- Present revision points drawn directly from the material covered.
- Include important definitions, principles, formulas, distinctions and conclusions where available.
- Do not create new factual claims or introduce material absent from the transcript.
- You may organise source-supported information as revision questions or prompts, but do not introduce new subject matter or imply that the lecturer supplied questions that were not recorded.
- Distinguish explicit lecturer instructions or examination guidance from general revision prompts created by organising the source material.

TECHNICAL ACCURACY AND QUALITY CONTROL

- Treat the transcript as the primary source of evidence.
- Never invent facts, definitions, formulas, statistics, examples, references, quotations or technical explanations.
- Preserve names, terminology, equations, units and numerical values accurately.
- Do not silently guess the intended meaning of unclear technical expressions.
- Where an important statement is unclear, indicate that it is unclear rather than fabricating a correction.
- Do not claim that a topic was covered comprehensively when the transcript contains only a brief mention.
- Use "Not specified." when required information is absent.
- If the transcript contains incomplete sentences, fragmented explanations or missing context, organise only the understandable material.
- Avoid repetitive content, generic filler and unsupported conclusions.
- Use clear, professional language appropriate to the likely learning context without assuming the learner's academic level.
- Before returning the notes, verify that the eight sections appear once, in order, and that the summary agrees with the material presented.
- Return only the completed lecture notes, without drafting commentary or explanations of these instructions.
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

ADDITIONAL PROFESSIONAL INTERVIEW REPORT REQUIREMENTS

PURPOSE AND REPORTING STANDARD

- Produce a clear, factual and professionally organised interview report based strictly on the supplied transcript.
- Retain the existing seven-section structure and present the sections in the prescribed order.
- Preserve the substance of the interview without reproducing every word spoken.
- Use neutral, professional language appropriate to the interview's context.
- Distinguish the interviewer's questions from the interviewee's answers, comments and opinions.
- Do not assume that the interview is for recruitment, research, journalism or stakeholder engagement unless the source establishes its purpose.

1. INTERVIEW SUBJECT

- Identify the interview subject, interviewer, interview date, location or platform, purpose and organisational affiliations only where supplied.
- Do not invent names, job titles, organisations, contact details, dates or interview arrangements.
- If the interviewee's identity is not established, use "Not specified."
- If multiple interviewees are present, distinguish them only where the transcript provides sufficient evidence.

2. MAIN TOPICS

- Identify the principal subjects actually discussed during the interview.
- Arrange topics logically while preserving the sequence where it is important to understanding the discussion.
- Distinguish the interview's stated purpose from topics that arose incidentally.
- Do not introduce external context, assumed motivations or additional subject areas.

3. KEY QUESTIONS

- Record the substantive questions asked by the interviewer or other identified participants.
- Preserve the meaning of each question while correcting obvious transcription errors.
- Group closely related questions only where their meanings remain clear.
- Do not invent questions to make the interview appear more comprehensive.
- If a question is unclear or incomplete, do not reconstruct missing wording based on assumptions.

4. RESPONSES

- Summarise the interviewee's actual responses accurately and attribute them to the correct speaker where identifiable.
- Preserve important explanations, examples, figures, dates, qualifications and conditions.
- Distinguish a direct answer from an indirect response, an opinion, a refusal to answer or an unresolved question where the transcript supports that distinction.
- Do not make an incomplete answer appear definitive.
- Do not treat an interviewee's allegation, belief, prediction or opinion as an independently verified fact.
- Do not infer agreement, acceptance, responsibility or commitment from a response unless explicitly supported.
- Where a question receives no recorded answer, state "No response recorded in the source material."

5. IMPORTANT STATEMENTS

- Capture material statements, declarations, concerns, explanations, promises and qualifications that are explicitly present in the transcript.
- Use direct quotations only when the wording is sufficiently clear and faithful to the recording.
- Otherwise, paraphrase accurately without quotation marks.
- Attribute statements to the correct speaker where possible.
- Preserve important disagreements, reservations and conditions.
- Do not exaggerate a statement, remove a qualification that changes its meaning, or attribute a statement to the wrong person.
- Distinguish stated intentions and promises from actions that have actually been completed.

6. KEY FINDINGS

- Present findings that can reasonably be established from the interview content.
- Distinguish information explicitly stated by the interviewee from observations about the interview itself.
- Do not present unverified claims as established facts.
- Do not infer motives, personality traits, credibility, competence, guilt, intent or psychological characteristics from the transcript.
- Do not make hiring recommendations, research conclusions or media judgements unless the source explicitly supports that purpose and conclusion.
- If the interview does not support a substantive finding, state "Not specified in the source material."

7. SUMMARY

- Provide a concise overview of the interview's main topics, responses and important statements.
- Ensure the summary is consistent with the detailed sections and introduces no new information.
- Preserve unresolved questions, important qualifications and material disagreements.
- Do not describe an issue as resolved or an objective as achieved unless the transcript supports that conclusion.

SOURCE ACCURACY AND QUALITY CONTROL

- Treat the transcript as the primary source of evidence and use additional details only when explicitly supplied as input.
- Never invent answers, names, designations, affiliations, quotations, dates, statistics, events, commitments or conclusions.
- Preserve Nigerian names, local terminology, institutional names and technical expressions accurately.
- Correct obvious transcription and grammatical errors only when the intended meaning is clear.
- Do not confuse speakers when attribution is uncertain.
- Do not fill gaps in the interview using assumptions, general knowledge or external information.
- If the transcript contains conflicting statements, preserve the distinction and identify the inconsistency where material.
- If the transcript is incomplete, report only the available material.
- Use "Not specified in the source material" when relevant information is unavailable.
- Avoid repetition, generic filler and unsupported analysis.
- Before returning the report, verify that all seven sections appear once, in order, and that the summary agrees with the responses and findings.
- Return only the completed interview report, without drafting notes or explanations of these instructions.
""",

            
            "general_report": """
Prepare a comprehensive, formal, factual report suitable
for submission to Management following an official event,
conference, workshop, training, seminar, or meeting.

The report must document what happened, who participated,
what was discussed, what was achieved, the significance
of the event, and the actions that may follow. Do not
produce merely a short summary.

REPORT STRUCTURE

Use the following numbered headings in this order.
Populate each section using the available source material.
If information is unavailable, state "Not specified in
the source material." Do not invent content to fill gaps.

1. COVER PAGE

Include, where available:
- Name of the organisation or institution submitting
  the report.
- Full official title of the event.
- Official theme.
- Lead event organiser.
- Collaborators, partners and sponsors.
- Venue and location.
- Event date, duration, start date and end date.
- Department or unit responsible for the report.
- Report submission date.
- Name and designation of the reporting officer, only
  where supplied.

Do not assume that the transcript's recording date is
the event date or report submission date. Do not invent
an organisation, theme, title, venue or reporting officer.

2. EXECUTIVE SUMMARY

Provide a concise but substantive overview covering:
- Purpose and context of the event.
- Organiser and participant categories.
- Major presentations, discussions and proceedings.
- Significant findings and confirmed outcomes.
- Important challenges or unresolved issues.
- Recommendations supported by the source material.

The executive summary must accurately reflect the body
of the report and must not introduce new facts.

3. INTRODUCTION AND BACKGROUND

Explain the background, context and purpose of the event
using information available in the source. Include the
stated institutional, policy or sector context and the
event's importance to the organisation where supported.

Do not invent policy context, institutional mandates or
background information absent from the source.

4. EVENT DETAILS

Present a clear table with the following fields:
- Event title
- Theme
- Lead organiser
- Collaborators and sponsors
- Date and duration
- Venue and location
- Participants and represented organisations
- Representatives, designations and organisations

Use "Not specified in the source material" for missing
details. Do not infer attendance from speakers mentioned
in the discussion.

5. OBJECTIVES OF THE EVENT

Record the objectives stated by the organiser or
participants. Explain the event's intended purpose where
supported. Do not present assumed objectives as official
objectives. If none are recorded, state that clearly.

6. OPENING CEREMONY AND KEY ADDRESSES

Document the opening proceedings in their recorded order.
For each identified speaker, include where available:
- Full name
- Official designation
- Organisation
- Purpose or subject of the address
- Main messages and issues raised
- Proposals, recommendations or commitments
- Relevance to the event

Explain the substance of each address rather than merely
stating that the speaker delivered an address.

Do not invent quotations or attribute statements,
opinions or commitments to a speaker without evidence.
If no opening ceremony or address is recorded, state that.

7. PROCEEDINGS AND DISCUSSIONS BY SESSION OR DAY

Organise the proceedings chronologically by day, session,
presentation or discussion, as supported by the source.

For each identifiable session, capture:
- Session title or theme
- Moderator, presenter or speaker
- Designation and organisation, where provided
- Main issues and arguments discussed
- Evidence, statistics and examples presented
- Questions raised and responses given
- Clarifications, conclusions and proposed actions

Preserve important figures, dates, examples and technical
or policy details accurately. Do not invent session titles,
speakers, questions, answers or statistics.

For lengthy or multi-day events, provide sufficient detail
to distinguish the sessions and explain their substance.
Do not merge unrelated sessions into a generic summary.

8. CONTRIBUTIONS OF REPRESENTATIVES AND PARTICIPATING ORGANISATIONS

Identify representatives and participating organisations
where they are explicitly mentioned. Explain each person's
recorded presentation, intervention, question, response,
recommendation or commitment and its relevance to the
discussion.

If a person is mentioned only as an attendee, do not
attribute an intervention to that person. Do not invent
names, designations, affiliations or contributions.

9. KEY FINDINGS, OBSERVATIONS AND OUTCOMES

Separate and clearly identify:
- Key findings and lessons learned
- Important observations
- Challenges identified
- Opportunities for collaboration
- Confirmed decisions and resolutions
- Commitments actually made
- Proposals that remain unapproved
- Matters requiring further investigation

Do not present a suggestion as an adopted decision.
Do not claim that an objective was achieved unless the
source provides evidence supporting that conclusion.

10. RELEVANCE TO THE ORGANISATION

Explain the relevance of the event to the organisation
only to the extent supported by the transcript and any
additional context supplied by the user.

Where supported, discuss:
- Potential institutional benefits
- Connections to stated programmes or policies
- Opportunities for collaboration
- Operational or regulatory implications
- Risks, gaps or issues requiring attention
- Possible improvements to the organisation's work

Clearly distinguish documented facts from analysis.
Label implications as potential where they have not been
formally established. Do not invent the organisation's
mandate, programmes, policies or strategic objectives.

11. RECOMMENDATIONS AND ACTION POINTS

Provide recommendations that are directly supported by
the event's findings and discussions.

Use an action-oriented table with these columns:
- S/N
- Recommendation
- Proposed action
- Responsible unit or person
- Expected outcome
- Timeline, where specified

Include exactly three prioritised recommendations only
when three meaningful recommendations are supported by
the source. If fewer are supported, provide fewer.
Do not create additional recommendations just to reach
three.

Distinguish explicitly between:
- Recommendations actually made during the event
- Recommendations derived from documented findings
- Actions formally agreed upon
- Proposed actions requiring Management's approval

Do not invent owners, budgets, deadlines, targets or
expected results. Label unassigned responsibilities,
timelines and proposed actions as "Proposed" or
"Not specified", as appropriate.

12. CHALLENGES AND ISSUES REQUIRING FOLLOW-UP

Document recorded technical, operational, financial,
policy, regulatory or coordination challenges, unresolved
questions and outstanding commitments.

Explain the issue and any follow-up explicitly identified.
Do not manufacture challenges simply to fill this section.
If none are recorded, say so.

13. CONCLUSION

Summarise the event's significance, principal documented
outcomes and relevance to the organisation. Reinforce the
importance of supported follow-up actions without
introducing new facts or recommendations.

14. ATTENDANCE AND REPRESENTATIVES' DETAILS

Where names and details are available, provide a table:
- S/N
- Full name
- Designation
- Organisation
- Role at the event

Distinguish speakers, presenters, moderators and other
participants only where the source supports the role.
Do not claim that the list is a complete attendance
register unless the source establishes that it is.

If attendance information is unavailable, state that an
attendance register was not provided in the source.

15. APPENDICES AND SUPPORTING DOCUMENTS

List supporting materials only when they are supplied
or explicitly identified in the source, such as:
- Event programme or agenda
- Presentation slides
- Attendance register
- Photographs and captions
- Communiqués or resolutions
- Statistics, charts or tables
- Materials distributed during the event

Do not claim that an appendix is attached when it is not.
If no supporting documents are provided, state this.

REPORT QUALITY, EVIDENCE AND CONSISTENCY CONTROLS

REPORTING APPROACH

- Treat this as an official institutional report, not a generic essay, an event template or an expanded transcript.
- Organise the source material into a coherent, readable report while preserving the meaning, sequence and context of the recorded proceedings.
- Give appropriate detail to substantive presentations, discussions, findings and decisions. Do not give every section equal length when the source material does not justify it.
- Where the source describes a meeting, planning session or internal discussion rather than a formal event, report the activity accurately without implying that a ceremony, conference or formal programme took place.
- Do not claim that the report covers the entire event if the transcript covers only a particular session or part of the proceedings.

EVIDENCE AND INTERPRETATION

- Before writing a factual statement, ensure that it is supported by the transcript or by additional event information explicitly supplied as input.
- Do not use general knowledge, internet knowledge, common institutional practices or assumptions to fill gaps in the source.
- Do not describe an event as successful, impactful, productive or effective unless the source contains evidence supporting that assessment.
- Do not claim that a programme, policy, project or initiative achieved its objectives merely because it was discussed or presented.
- Where an analytical observation is appropriate under Section 10, clearly distinguish it from a recorded fact and qualify it as a potential implication where necessary.
- Do not use phrases such as "it was agreed", "participants resolved", "the meeting concluded", "the event achieved" or "Management was advised" unless the source supports the specific statement.
- Do not infer consensus from the absence of objections or infer approval from a proposal being discussed.
- Do not attribute a collective position to all participants when only one speaker expressed it.
- Where the source contains conflicting statements, preserve the distinction and identify the discrepancy where relevant instead of choosing one version without evidence.
- If a statement is unclear or a transcription error cannot be resolved confidently, retain only the understandable information and indicate that the detail is unclear where material.

DECISIONS, RECOMMENDATIONS AND ACTIONS

- Classify each recorded item accurately as a fact, observation, question, proposal, recommendation, instruction, decision, resolution, commitment or action.
- Record a decision as confirmed only where the source explicitly supports that status.
- A request to submit information must not automatically become an approved organisational action plan.
- A recommendation may be included as a report recommendation derived from documented findings only when the reasoning is directly supported by the source. Clearly distinguish it from a recommendation actually made during the event.
- Do not convert a general discussion point into an action item without evidence of a required action.
- If the source records an action but does not identify its owner or deadline, report the action and state that the owner or deadline was not specified.
- Do not assign responsibility to a department, individual or organisation based on assumptions about their usual duties.
- Do not invent measurable targets, expected benefits, financial implications or implementation arrangements.
- If no evidence supports a recommendation, state that no specific recommendation can be established from the available source material.

SECTION MANAGEMENT

- Use each of the 15 numbered headings once and in the prescribed order.
- Reproduce the heading wording exactly as supplied in REPORT STRUCTURE.
- Do not add a second cover page, duplicate executive summary or repeat the full report under different headings.
- Where information is relevant to multiple sections, cross-reference it briefly or present it in the most appropriate section rather than repeating lengthy passages.
- If a section is not applicable to the recorded activity, explain briefly that it was not recorded or is not applicable, as appropriate. Do not invent content to make the section appear complete.
- Use tables for event details, attendance information and action points where the source supports the required fields. Do not create artificial table entries to fill missing fields.
- Use consistent date, number, currency and designation formats, while preserving the original values and meaning.
- Use direct quotations only when the wording is available and sufficiently clear in the source. Otherwise, paraphrase accurately without quotation marks.
- Do not manufacture an appendix, attendance register, programme, resolution or supporting document.

FINAL QUALITY CONTROL BEFORE RETURNING THE REPORT

Before presenting the final report, silently verify that:

1. All 15 required sections appear once, in the correct order, with the exact prescribed headings.
2. Every material factual statement is supported by the supplied source material.
3. Names, titles, designations, organisations, dates, figures, statistics and technical terms have not been invented or altered.
4. Suggestions, instructions, recommendations, decisions and commitments are correctly distinguished.
5. The executive summary agrees with the detailed report and introduces no additional claims.
6. Recommendations and action points are supported by the source or are clearly identified as proposed report recommendations based on documented findings.
7. Missing information is explicitly identified rather than guessed.
8. No generic filler, repeated paragraphs, unsupported institutional background or duplicated report sections remain.
9. The report does not claim that documents are attached, decisions were approved or outcomes were achieved without supporting evidence.
10. The length and level of detail are proportionate to the actual source material.

Return only the completed report. Do not include the internal quality-control checklist, drafting notes, explanations of the prompt or unsupported commentary.

ADDITIONAL RULE FOR OBJECTIVES, AGREEMENTS AND RECOMMENDATIONS

- Record objectives only when explicitly stated or clearly
  documented in the source material.
- Do not describe inferred objectives as official objectives.
- A request or instruction does not establish acceptance,
  agreement or commitment by the people receiving it.
- Do not report an agreement, commitment, decision or
  resolution unless the transcript explicitly records it.
- Do not create recommendations merely because they seem
  like reasonable next steps.
- If no recommendations are supported by the source,
  state that clearly.

STRICT SOURCE-BASED AND ACCURACY RULES

- Use the transcript as the primary source of evidence.
- Use additional event details only when explicitly
  supplied as input to the report.
- A request or instruction does not establish acceptance,
  agreement or commitment by the people receiving it.
- Do not report an agreement, commitment, decision or
  resolution unless the transcript explicitly records it.
- Do not create recommendations merely because they seem
  like reasonable next steps. If no recommendations are
  supported by the source, state that clearly.
- Never invent facts, names, titles, designations,
  organisations, dates, venues, themes, statistics,
  quotations, attendance, questions, responses, decisions,
  commitments, achievements or outcomes.
- Preserve the original meaning of the source.
- Preserve Nigerian names, local terminology, government
  agencies and organisational names accurately.
- Correct grammar and obvious transcription errors only
  when the intended meaning is clear.
- Do not treat a person's presence as evidence that the
  person spoke or made a commitment.
- Do not turn a proposal into a confirmed decision.
- Do not turn discussion into an agreed action.
- Do not invent institutional context or external facts.
- Use "Not specified in the source material" for missing
  information rather than guessing.
- If the transcript is incomplete or covers only part
  of an event, report only the available material and
  make no claim that the record is comprehensive.
- If information from separate transcript sections is
  incomplete, do not invent connecting details.
- Avoid repetition, generic filler and unsupported claims.
- Use formal, clear professional English suitable for
  senior Management and official institutional records.
- Use numbered headings, informative paragraphs and
  tables where appropriate.
- Let the report's length reflect the source material.
  Aim for approximately 5–7 pages when sufficient
  substantive information is available, but do not pad
  a short transcript to reach a target length.

FINAL OVERRIDE — MANDATORY ACCURACY RULES

These rules take precedence over any earlier instruction in this prompt that could be interpreted differently.

1. Do not create a recommendation, action point, deadline, submission mechanism or follow-up meeting unless it is explicitly recorded in the source. A reasonable suggestion is not evidence that the suggestion was made.
2. Do not describe a request or instruction as an agreement, acceptance or commitment. Record only the action requested and who requested it, where known.
3. Never use expressions such as "implicit commitment", "implied agreement" or "agreed to" to describe an unconfirmed action.
4. In Section 5, distinguish explicitly stated objectives from the purpose apparent in the discussion. If no official objective is recorded, state "Not specified in the source material." Do not label inferred purposes as official objectives.
5. Use the exact prescribed heading "8. CONTRIBUTIONS OF REPRESENTATIVES AND PARTICIPATING ORGANISATIONS".
6. Use the exact prescribed heading "12. CHALLENGES AND ISSUES REQUIRING FOLLOW-UP".
7. Include all 15 sections exactly once and in order, even when some sections contain only a short statement that information was not specified.
8. Before returning the report, check the complete report for contradictions, unsupported recommendations, invented commitments, duplicated sections and heading deviations. Correct these issues before returning the final report.
9. Do not sacrifice factual accuracy to make the report appear more comprehensive. Never fill a gap with a plausible but unrecorded fact.
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

ADDITIONAL PROFESSIONAL SUMMARY AND KEY POINTS REQUIREMENTS

PURPOSE AND REPORTING STANDARD

- Produce a concise, accurate and well-organised summary of the supplied transcript.
- Retain the existing four-section structure and use the headings in the prescribed order.
- Capture the most important information without reproducing the entire transcript.
- Give prominence to substantive statements, facts, figures, issues and explicitly stated conclusions.
- Adjust the length to the amount and importance of information in the source. Do not pad a short transcript or omit important details from a substantive one.
- Use clear, professional English appropriate to the transcript's context.

1. SUMMARY

- Present a balanced overview of the main subject, purpose and discussion as recorded in the transcript.
- Identify the principal speakers, organisations or events only where supplied and relevant.
- Distinguish the subject being discussed from any outcome actually achieved.
- Do not claim that the discussion was successful, productive, conclusive or effective unless the source explicitly supports that assessment.
- Do not introduce background information or context that is absent from the source.

2. KEY POINTS

- Extract the most important facts, statements, arguments, issues and developments explicitly mentioned.
- Organise related points into clear bullets without changing their original meaning.
- Preserve important qualifications, disagreements, conditions and uncertainties.
- Attribute a statement to a speaker or organisation only when the source supports the attribution.
- Do not turn a question into a fact, an opinion into a verified finding, a proposal into a decision or a request into a commitment.
- Do not add recommendations or action items merely because they appear to be sensible next steps.

3. IMPORTANT INFORMATION

- Preserve material names, organisations, designations, locations, dates, amounts, statistics, technical terms and other details provided in the transcript.
- Reproduce figures, units and numerical relationships accurately.
- Include deadlines, responsibilities and decisions only where they are explicitly stated.
- Distinguish confirmed information from allegations, estimates, predictions, opinions and proposals.
- Do not infer missing details or silently correct unclear figures, names or technical expressions.
- If a detail is unclear and materially affects the meaning, indicate that it is unclear rather than guessing.

4. CONCLUSIONS

- Include only conclusions explicitly stated in the transcript.
- Distinguish conclusions reached by an identified speaker from formal conclusions adopted by a meeting or organisation.
- Do not treat the end of a discussion, the absence of objections or a speaker's opinion as proof of a collective conclusion.
- Do not create recommendations, decisions, outcomes, causal explanations or implications that are not stated.
- If no explicit conclusion appears in the transcript, write "Not specified."

SOURCE ACCURACY AND QUALITY CONTROL

- Treat the supplied transcript as the primary source of evidence.
- Use additional information only when explicitly supplied as input.
- Never invent names, dates, events, quotations, figures, statistics, decisions, commitments, recommendations or outcomes.
- Preserve Nigerian names, local terminology, institutional names and technical expressions accurately.
- Correct obvious transcription errors only when the intended meaning is clear.
- If statements conflict, preserve the distinction and identify the inconsistency where material rather than choosing a version without evidence.
- If the transcript is incomplete, summarise only the available material and do not imply that the summary covers information that was not provided.
- Use "Not specified" when a section or relevant detail is unsupported by the source.
- Avoid repetition, generic filler, exaggerated language and unsupported interpretations.
- Before returning the summary, verify that all four sections appear once, in order, and that the summary, key points, important information and conclusions are consistent.
- Return only the completed summary, without drafting notes or explanations of these instructions.
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

ADDITIONAL PROFESSIONAL ACTION TRACKER REQUIREMENTS

PURPOSE AND REPORTING STANDARD

- Produce a clear, accurate and practical action tracker based strictly on the supplied transcript.
- Retain the existing table structure and use the five columns in the prescribed order.
- Capture only actionable tasks explicitly stated in the source.
- Use concise, specific wording that preserves the original meaning of each task.
- Do not create a task simply because a discussion suggests that further work might be useful.
- Do not add generic project-management advice or recommendations.

ACTION ITEM

- Record the task or action that the transcript explicitly identifies as required, assigned, directed or agreed.
- Preserve the scope of the task. Do not expand a narrowly stated task into a larger deliverable or project.
- Distinguish an assigned action from a proposal, suggestion, question, intention or general statement.
- Do not treat a request as an accepted commitment unless acceptance is explicitly recorded.
- Where the source identifies several distinct actions, list them separately when doing so improves accountability without changing their meaning.
- Do not split one action into multiple tasks unless the source establishes separate deliverables or responsibilities.

RESPONSIBLE PERSON

- Record the named individual, team, department or organisation only when the source explicitly assigns responsibility.
- Do not infer ownership from a person's job title, department, participation in the discussion or usual duties.
- If a chair directs a named person to act, record that assignment accurately.
- If a request is addressed to an unidentified group, do not invent individual owners.
- Where the responsible person or unit is absent, write "Not specified".

DEADLINE

- Record deadlines, dates or time periods only when explicitly stated in the transcript.
- Preserve the original date and time information accurately.
- Do not calculate or assume a deadline from phrases such as "soon", "urgently" or "before the next meeting" unless the source provides a specific deadline.
- If the transcript says "before the next meeting" but does not state its date, preserve that wording as the stated deadline condition without inventing a calendar date.
- If no deadline or deadline condition is provided, write "Not specified".

PRIORITY

- Record a priority such as High, Medium or Low only when the transcript explicitly assigns that priority or clearly uses an equivalent, unambiguous classification.
- Do not infer priority from urgency, importance, the speaker's seniority or the nature of the task.
- Do not convert words such as "urgent" into a formal priority classification unless the source establishes that meaning.
- If priority is not explicitly stated, write "Not specified".

NOTES

- Use this column for relevant context explicitly stated in the transcript, such as dependencies, conditions, deliverables, clarifications or recorded progress.
- Distinguish completed actions from outstanding tasks only where the source provides evidence of their status.
- Do not add interpretations, unsupported risks, recommendations, assumptions or additional instructions.
- If no relevant contextual information is recorded, write "Not specified".

OUTPUT AND VALIDATION

- If actionable tasks are found, return a table with these columns in this exact order:
  Action Item | Responsible Person | Deadline | Priority | Notes
- If the transcript contains no actionable tasks, state exactly:
  "No actionable items specified."
- Do not fill the table with discussion topics, observations, recommendations or hypothetical tasks.
- Do not create placeholder task rows simply to make the table look complete.
- Do not duplicate the same action in multiple rows unless the transcript establishes distinct assignments.
- Ensure that every action item can be traced to an explicit statement in the source.
- Before returning the result, verify that owners, deadlines, priorities and notes have not been invented.
- Return only the action tracker, without drafting notes or explanations of these instructions.
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
# TRANSLATION TEXT-TO-SPEECH (LANGA)
# ============================================================

@app.route("/api/translate/speech", methods=["POST"])
def translate_speech():
    user, auth_error = get_authenticated_user()

    if auth_error:
        return auth_error

    api_key = os.getenv("LANGA_API_KEY")
    if not api_key:
        app.logger.error("LANGA_API_KEY is not configured.")
        return jsonify({
            "success": False,
            "error": "Speech service is not configured."
        }), 503

    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    language = (data.get("language") or "").strip().lower()

    voice_codes = {
        "yoruba": "yo",
        "yo": "yo",
        "hausa": "ha",
        "ha": "ha",
        "igbo": "ig",
        "ig": "ig",
    }

    voice = voice_codes.get(language)

    if not text:
        return jsonify({
            "success": False,
            "error": "Text is required."
        }), 400

    if len(text) > 5000:
        return jsonify({
            "success": False,
            "error": "Text exceeds the 5000-character limit."
        }), 400

    if not voice:
        return jsonify({
            "success": False,
            "error": "Supported speech languages are Yoruba, Hausa and Igbo."
        }), 400

    try:
        response = httpx.post(
            "https://langa.julimer.com/v1/audio/speech",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "n-atlas-tts",
                "input": text,
                "voice": voice,
                "response_format": "mp3",
            },
            timeout=60.0,
        )

        if response.status_code != 200:
            app.logger.warning(
                "Langa speech request failed with status %s.",
                response.status_code,
            )
            return jsonify({
                "success": False,
                "error": "Unable to generate speech. Please try again."
            }), 502

        if not response.content:
            return jsonify({
                "success": False,
                "error": "Speech service returned empty audio."
            }), 502

        return Response(
            response.content,
            status=200,
            mimetype="audio/mpeg",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    except httpx.TimeoutException:
        return jsonify({
            "success": False,
            "error": "Speech generation timed out. Please try again."
        }), 504

    except httpx.RequestError:
        app.logger.exception("Unable to reach Langa speech service.")
        return jsonify({
            "success": False,
            "error": "Speech service is temporarily unavailable."
        }), 502


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
        port=int(os.getenv("PORT", "5001")),
        debug=os.getenv("FLASK_DEBUG", "false").lower() == "true"
    )
