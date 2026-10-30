
import os
import tempfile
import unittest

# Set test configuration before importing the Flask application.
TEST_DB_FILE = tempfile.NamedTemporaryFile(
    prefix="nabtranscriber_security_test_",
    suffix=".db",
    delete=False,
)
TEST_DB_FILE.close()

os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_FILE.name}"
os.environ["JWT_SECRET_KEY"] = "local-test-only-secret-not-for-production"

from app import app, db
from models import (
    User,
    AdminAuditLog,
    Subscription,
    Payment,
    TranscriptionHistory,
    UserCredit,
    CreditTransaction,
)


class AdminSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        cls.app_context = app.app_context()
        cls.app_context.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.app_context.pop()

        try:
            os.remove(TEST_DB_FILE.name)
        except OSError:
            pass

    def setUp(self):
        db.session.rollback()

        # Clear dependent records before deleting test users.
        TranscriptionHistory.query.delete(synchronize_session=False)
        Payment.query.delete(synchronize_session=False)
        CreditTransaction.query.delete(synchronize_session=False)
        UserCredit.query.delete(synchronize_session=False)
        Subscription.query.delete(synchronize_session=False)
        AdminAuditLog.query.delete(synchronize_session=False)
        User.query.delete(synchronize_session=False)

        db.session.commit()

        self.client = app.test_client()

        self.admin = User(
            email="admin-security-test@example.invalid",
            role="admin",
        )
        self.admin.set_password("TestOnlyPassword-Admin-123!")

        self.user = User(
            email="user-security-test@example.invalid",
            role="user",
        )
        self.user.set_password("TestOnlyPassword-User-123!")

        db.session.add_all([self.admin, self.user])
        db.session.commit()

    def token_for(self, user):
        import jwt
        from datetime import datetime, timedelta, timezone
        import app as app_module

        now = datetime.now(timezone.utc)
        payload = {
            "sub": str(user.id),
            "email": user.email,
            "iat": now,
            "exp": now + timedelta(hours=1),
        }
        return jwt.encode(
            payload,
            app_module.jwt_secret_key,
            algorithm=app_module.JWT_ALGORITHM,
        )

    def auth_headers(self, user):
        return {
            "Authorization": f"Bearer {self.token_for(user)}"
        }

    def test_blocked_user_cannot_login(self):
        self.user.is_blocked = True
        db.session.commit()

        response = self.client.post(
            "/api/auth/login",
            json={
                "email": self.user.email,
                "password": "TestOnlyPassword-User-123!",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertNotIn("token", response.get_json())

    def test_existing_token_rejected_after_block(self):
        token = self.token_for(self.user)
        self.user.is_blocked = True
        db.session.commit()

        response = self.client.get(
            "/api/admin/audit-logs",
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 403)

    def test_unauthenticated_admin_request_rejected(self):
        response = self.client.get("/api/admin/audit-logs")
        self.assertEqual(response.status_code, 401)

    def test_non_admin_cannot_block_user(self):
        response = self.client.post(
            f"/api/admin/users/{self.user.id}/block",
            headers=self.auth_headers(self.user),
            json={"reason": "Test-only reason"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            db.session.get(User, self.user.id).is_blocked
        )

    def test_admin_can_block_and_unblock_user(self):
        response = self.client.post(
            f"/api/admin/users/{self.user.id}/block",
            headers=self.auth_headers(self.admin),
            json={"reason": "Test-only security check"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            db.session.get(User, self.user.id).is_blocked
        )

        response = self.client.post(
            f"/api/admin/users/{self.user.id}/unblock",
            headers=self.auth_headers(self.admin),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            db.session.get(User, self.user.id).is_blocked
        )

        actions = [
            row.action
            for row in AdminAuditLog.query.order_by(
                AdminAuditLog.id
            ).all()
        ]
        self.assertIn("user_blocked", actions)
        self.assertIn("user_unblocked", actions)

    def test_admin_cannot_block_self(self):
        response = self.client.post(
            f"/api/admin/users/{self.admin.id}/block",
            headers=self.auth_headers(self.admin),
            json={"reason": "Test-only self-block attempt"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            db.session.get(User, self.admin.id).is_blocked
        )

    def test_missing_reason_does_not_block_user(self):
        response = self.client.post(
            f"/api/admin/users/{self.user.id}/block",
            headers=self.auth_headers(self.admin),
            json={},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            db.session.get(User, self.user.id).is_blocked
        )


    def test_admin_subscription_update_creates_audit_log(self):
        response = self.client.patch(
            f"/api/admin/users/{self.user.id}/subscription",
            headers=self.auth_headers(self.admin),
            json={
                "plan": "gold",
                "billing_cycle": "monthly",
            },
        )

        self.assertEqual(
            response.status_code,
            200,
            response.get_json(),
        )

        log = AdminAuditLog.query.filter_by(
            action="user_subscription_updated",
            affected_user_id=self.user.id,
        ).first()

        self.assertIsNotNone(log)
        self.assertEqual(log.admin_user_id, self.admin.id)
        self.assertEqual(log.outcome, "success")
        self.assertIn("gold/monthly", log.reason)



    def test_account_deletion_preserves_financial_records(self):
        from datetime import datetime, timedelta, timezone

        user_id = self.user.id
        original_email = self.user.email
        now = datetime.now(timezone.utc)

        # Create a subscription.
        subscription = Subscription(
            user_id=user_id,
            plan="gold",
            billing_cycle="monthly",
            amount=5000,
            start_date=now,
            end_date=now + timedelta(days=30),
            status="active",
        )
        db.session.add(subscription)
        db.session.flush()

        # Create a payment linked to that subscription.
        payment = Payment(
            user_id=user_id,
            subscription_id=subscription.id,
            plan="gold",
            billing_cycle="monthly",
            amount=5000,
            currency="NGN",
            status="success",
            transaction_reference=f"TEST-DELETE-{user_id}",
        )

        # Create a credit transaction.
        credit_transaction = CreditTransaction(
            user_id=user_id,
            action="test_account_deletion",
            credits_used=1,
        )

        # Create the user's credit balance.
        credit_balance = UserCredit(
            user_id=user_id,
            credits=4,
            used_credits=1,
        )

        # Create a saved transcription.
        transcription = TranscriptionHistory(
            user_id=user_id,
            title="Deletion test transcript",
            transcript="TEST-ONLY TRANSCRIPT CONTENT",
            expires_at=now + timedelta(days=30),
        )

        db.session.add_all([
            payment,
            credit_transaction,
            credit_balance,
            transcription,
        ])
        db.session.commit()

        payment_id = payment.id
        subscription_id = subscription.id
        transaction_id = credit_transaction.id
        transcription_id = transcription.id

        # Keep a JWT issued before account deletion.
        old_token = self.token_for(self.user)

        response = self.client.delete(
            "/api/account",
            headers={"Authorization": f"Bearer {old_token}"},
        )

        self.assertEqual(
            response.status_code,
            200,
            response.get_json(),
        )

        # The account ID remains to preserve foreign keys,
        # but the account is anonymised and blocked.
        retained_user = db.session.get(User, user_id)
        self.assertIsNotNone(retained_user)
        self.assertNotEqual(retained_user.email, original_email)
        self.assertTrue(retained_user.email.endswith("@deleted.invalid"))
        self.assertTrue(retained_user.is_blocked)
        self.assertEqual(retained_user.role, "user")

        # Payment, subscription and credit transaction survive.
        retained_payment = db.session.get(Payment, payment_id)
        self.assertIsNotNone(retained_payment)
        self.assertEqual(retained_payment.user_id, user_id)
        self.assertEqual(
            retained_payment.subscription_id,
            subscription_id,
        )

        retained_subscription = db.session.get(
            Subscription,
            subscription_id,
        )
        self.assertIsNotNone(retained_subscription)
        self.assertEqual(retained_subscription.user_id, user_id)

        retained_transaction = db.session.get(
            CreditTransaction,
            transaction_id,
        )
        self.assertIsNotNone(retained_transaction)
        self.assertEqual(retained_transaction.user_id, user_id)

        # Personal transcription and credit balance are removed.
        self.assertIsNone(
            db.session.get(TranscriptionHistory, transcription_id)
        )
        self.assertIsNone(
            UserCredit.query.filter_by(user_id=user_id).first()
        )
                # Successful account deletion should be audited.
        deletion_log = AdminAuditLog.query.filter_by(
            action="user_account_deleted",
            affected_user_id=user_id,
        ).first()

        self.assertIsNotNone(deletion_log)
        self.assertIsNone(deletion_log.admin_user_id)
        self.assertEqual(deletion_log.outcome, "success")
        self.assertEqual(
            deletion_log.reason,
            "User requested account deletion",
        )

        # A previously issued JWT must no longer grant access.
        token_response = self.client.get(
            "/api/admin/audit-logs",
            headers={"Authorization": f"Bearer {old_token}"},
        )
        self.assertEqual(token_response.status_code, 403)

        # The original password must not authenticate the account.
        login_response = self.client.post(
            "/api/auth/login",
            json={
                "email": original_email,
                "password": "TestOnlyPassword-User-123!",
            },
        )
        self.assertNotIn("token", login_response.get_json())


    def test_account_deletion_requires_authentication(self):
        response = self.client.delete("/api/account")
        self.assertEqual(response.status_code, 401)




if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        try:
            os.remove(TEST_DB_FILE.name)
        except OSError:
            pass
