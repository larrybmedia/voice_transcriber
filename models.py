from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash


db = SQLAlchemy()


# ============================================================
# USER
# ============================================================

class User(db.Model):
    __tablename__ = "users"

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    email = db.Column(
        db.String(255),
        unique=True,
        nullable=False,
        index=True
    )

    password_hash = db.Column(
        db.String(255),
        nullable=False
    )

    role = db.Column(
        db.String(50),
        nullable=False,
        default="user",
        server_default="user"
    )

    # --------------------------------------------------------
    # PASSWORD RESET
    # --------------------------------------------------------

    password_reset_token_hash = db.Column(
        db.String(255),
        nullable=True
    )

    password_reset_expires_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True
    )

    password_reset_used = db.Column(
        db.Boolean,
        default=False,
        nullable=False,
        server_default="0"
    )

    # --------------------------------------------------------
    # ACCOUNT DATES
    # --------------------------------------------------------

    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    updated_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(
            password
        )

    def check_password(self, password):
        return check_password_hash(
            self.password_hash,
            password
        )

    def to_dict(self):
        return {
            "id": self.id,
            "email": self.email,
            "created_at": (
                self.created_at.isoformat()
                if self.created_at
                else None
            )
        }


# ============================================================
# SUBSCRIPTION
# ============================================================

class Subscription(db.Model):
    __tablename__ = "subscriptions"

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False,
        index=True
    )

    # free / gold / enterprise
    plan = db.Column(
        db.String(50),
        nullable=False,
        default="free"
    )

    # monthly / six_months / yearly
    billing_cycle = db.Column(
        db.String(50),
        nullable=False,
        default="free"
    )

    # Amount paid in Nigerian Naira.
    amount = db.Column(
        db.Integer,
        nullable=False,
        default=0
    )

    start_date = db.Column(
        db.DateTime(timezone=True),
        nullable=False
    )

    end_date = db.Column(
        db.DateTime(timezone=True),
        nullable=True
    )

    # active / expired / cancelled
    status = db.Column(
        db.String(50),
        nullable=False,
        default="active"
    )

    # Payment gateway reference.
    # This will be used when we add Paystack.
    payment_reference = db.Column(
        db.String(255),
        nullable=True,
        index=True
    )

    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    updated_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    user = db.relationship(
        "User",
        backref=db.backref(
            "subscriptions",
            lazy=True
        )
    )

    def is_active(self):
        if self.status != "active":
            return False

        if self.end_date is None:
            return True

        end_date = self.end_date

        if end_date.tzinfo is None:
            end_date = end_date.replace(
                tzinfo=timezone.utc
            )

        now = datetime.now(timezone.utc)

        return end_date > now

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "plan": self.plan,
            "billing_cycle": self.billing_cycle,
            "amount": self.amount,
            "start_date": (
                self.start_date.isoformat()
                if self.start_date
                else None
            ),
            "end_date": (
                self.end_date.isoformat()
                if self.end_date
                else None
            ),
            "status": self.status,
            "payment_reference": self.payment_reference,
            "is_active": self.is_active(),
            "created_at": (
                self.created_at.isoformat()
                if self.created_at
                else None
            )
        }


class Payment(db.Model):
    __tablename__ = "payments"

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False
    )

    subscription_id = db.Column(
        db.Integer,
        db.ForeignKey("subscriptions.id"),
        nullable=True
    )

    plan = db.Column(
        db.String(50),
        nullable=False
    )

    billing_cycle = db.Column(
        db.String(50),
        nullable=False
    )

    amount = db.Column(
        db.Integer,
        nullable=False
    )

    currency = db.Column(
        db.String(10),
        nullable=False,
        default="NGN"
    )

    payment_gateway = db.Column(
        db.String(50),
        nullable=True
    )

    transaction_reference = db.Column(
        db.String(255),
        nullable=True,
        unique=True
    )

    status = db.Column(
        db.String(50),
        nullable=False,
        default="pending"
    )

    paid_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True
    )

    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "subscription_id": self.subscription_id,
            "plan": self.plan,
            "billing_cycle": self.billing_cycle,
            "amount": self.amount,
            "currency": self.currency,
            "payment_gateway": self.payment_gateway,
            "transaction_reference": self.transaction_reference,
            "status": self.status,
            "paid_at": (
                self.paid_at.isoformat()
                if self.paid_at
                else None
            ),
            "created_at": (
                self.created_at.isoformat()
                if self.created_at
                else None
            ),
        }


# ============================================================
# USER CREDITS
# ============================================================

class UserCredit(db.Model):
    __tablename__ = "user_credits"

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        unique=True,
        nullable=False,
        index=True
    )

    # Total credits currently available.
    credits = db.Column(
        db.Integer,
        nullable=False,
        default=5
    )

    # Total credits consumed.
    used_credits = db.Column(
        db.Integer,
        nullable=False,
        default=0
    )

    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    updated_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    user = db.relationship(
        "User",
        backref=db.backref(
            "credit_account",
            uselist=False
        )
    )

    def use_credit(self):
        if self.credits <= 0:
            return False

        self.credits -= 1
        self.used_credits += 1

        return True

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "credits": self.credits,
            "used_credits": self.used_credits,
            "created_at": (
                self.created_at.isoformat()
                if self.created_at
                else None
            )
        }


# ============================================================
# CREDIT TRANSACTION
# ============================================================

class CreditTransaction(db.Model):
    __tablename__ = "credit_transactions"

    id = db.Column(
        db.Integer,
        primary_key=True
    )

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False,
        index=True
    )

    # Examples:
    # registration
    # audio_record
    # meeting_record
    # file_upload
    # subscription
    action = db.Column(
        db.String(100),
        nullable=False
    )

    credits_used = db.Column(
        db.Integer,
        nullable=False,
        default=0
    )

    recording_id = db.Column(
        db.String(255),
        nullable=True
    )

    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    user = db.relationship(
        "User",
        backref=db.backref(
            "credit_transactions",
            lazy=True
        )
    )

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "action": self.action,
            "credits_used": self.credits_used,
            "recording_id": self.recording_id,
            "created_at": (
                self.created_at.isoformat()
                if self.created_at
                else None
            )
        }