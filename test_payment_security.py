
import os
import tempfile
import unittest
import hmac
import hashlib
import json

from unittest.mock import patch

TEST_DB_FILE = tempfile.NamedTemporaryFile(
    suffix=".db",
    delete=False,
)
TEST_DB_FILE.close()

os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_FILE.name}"
os.environ["JWT_SECRET_KEY"] = "local-payment-test-secret-at-least-32-bytes"

from app import app, db
from models import User, Payment, Subscription
from subscription_service import (
    activate_subscription_from_payment,
)


class PaymentSecurityTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
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
        # Start each test with a fresh SQLAlchemy session.
        db.session.remove()

        # Clear records from previous tests.
        Payment.query.delete(synchronize_session=False)
        Subscription.query.delete(synchronize_session=False)
        User.query.delete(synchronize_session=False)
        db.session.commit()

        # Create a fresh Flask test client.
        self.client = app.test_client()

        # Create a test user.
        self.user = User(
            email="payment-test@example.com",
        )
        self.user.set_password("PaymentTest-123!")

        db.session.add(self.user)
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

    def auth_headers(self):
        return {
            "Authorization": f"Bearer {self.token_for(self.user)}"
        }

    def create_pending_payment(self, reference):
        payment = Payment(
            user_id=self.user.id,
            plan="gold",
            billing_cycle="monthly",
            amount=7000,
            currency="NGN",
            payment_gateway="paystack",
            transaction_reference=reference,
            status="pending",
        )

        db.session.add(payment)
        db.session.commit()

        return payment

    def signed_webhook_request(self, payload, test_secret):
        signature = hmac.new(
            test_secret.encode("utf-8"),
            payload,
            hashlib.sha512,
        ).hexdigest()

        return self.client.post(
            "/api/payment/webhook",
            data=payload,
            content_type="application/json",
            headers={
                "x-paystack-signature": signature
            },
        )

    def test_successful_gold_payment(self):
        payment, subscription = (
            activate_subscription_from_payment(
                user_id=self.user.id,
                plan="gold",
                billing_cycle="monthly",
                amount=7000,
                payment_gateway="paystack",
                transaction_reference="TEST-GOLD-001",
            )
        )

        self.assertEqual(payment.status, "successful")
        self.assertEqual(payment.amount, 7000)
        self.assertEqual(subscription.plan, "gold")
        self.assertEqual(subscription.status, "active")

    def test_duplicate_reference_does_not_create_second_payment(self):
        activate_subscription_from_payment(
            user_id=self.user.id,
            plan="gold",
            billing_cycle="monthly",
            amount=7000,
            payment_gateway="paystack",
            transaction_reference="TEST-DUPLICATE-001",
        )

        original_end_date = (
            Subscription.query.filter_by(
                user_id=self.user.id,
                status="active",
            ).first().end_date
        )

        payment, subscription = (
            activate_subscription_from_payment(
                user_id=self.user.id,
                plan="gold",
                billing_cycle="monthly",
                amount=7000,
                payment_gateway="paystack",
                transaction_reference="TEST-DUPLICATE-001",
            )
        )

        self.assertEqual(
            Payment.query.filter_by(
                transaction_reference="TEST-DUPLICATE-001"
            ).count(),
            1,
        )

        self.assertEqual(
            subscription.end_date,
            original_end_date,
        )

    def test_wrong_amount_is_rejected(self):
        with self.assertRaises(ValueError):
            activate_subscription_from_payment(
                user_id=self.user.id,
                plan="gold",
                billing_cycle="monthly",
                amount=5000,
                payment_gateway="paystack",
                transaction_reference="TEST-WRONG-AMOUNT",
            )

        self.assertEqual(Payment.query.count(), 0)
        self.assertEqual(Subscription.query.count(), 0)

    def test_invalid_plan_is_rejected(self):
        with self.assertRaises(ValueError):
            activate_subscription_from_payment(
                user_id=self.user.id,
                plan="free",
                billing_cycle="monthly",
                amount=0,
                payment_gateway="paystack",
                transaction_reference="TEST-FREE-001",
            )

    def test_invalid_billing_cycle_is_rejected(self):
        with self.assertRaises(ValueError):
            activate_subscription_from_payment(
                user_id=self.user.id,
                plan="gold",
                billing_cycle="weekly",
                amount=7000,
                payment_gateway="paystack",
                transaction_reference="TEST-WEEKLY-001",
            )

    def test_reference_belonging_to_another_user_is_rejected(self):
        activate_subscription_from_payment(
            user_id=self.user.id,
            plan="gold",
            billing_cycle="monthly",
            amount=7000,
            payment_gateway="paystack",
            transaction_reference="TEST-OWNER-001",
        )

        another_user = User(
            email="another-payment-test@example.com",
        )
        another_user.set_password("AnotherTest-123!")
        db.session.add(another_user)
        db.session.commit()

        with self.assertRaises(ValueError):
            activate_subscription_from_payment(
                user_id=another_user.id,
                plan="gold",
                billing_cycle="monthly",
                amount=7000,
                payment_gateway="paystack",
                transaction_reference="TEST-OWNER-001",
            )

    def test_enterprise_yearly_payment(self):
        payment, subscription = (
            activate_subscription_from_payment(
                user_id=self.user.id,
                plan="enterprise",
                billing_cycle="yearly",
                amount=110000,
                payment_gateway="paystack",
                transaction_reference="TEST-ENTERPRISE-001",
            )
        )

        self.assertEqual(payment.amount, 110000)
        self.assertEqual(subscription.plan, "enterprise")
        self.assertEqual(
            subscription.billing_cycle,
            "yearly",
        )


    def test_webhook_rejects_missing_signature(self):
        response = self.client.post(
            "/api/payment/webhook",
            json={"event": "charge.success", "data": {}},
        )

        self.assertEqual(response.status_code, 401)

    def test_webhook_rejects_invalid_signature(self):
        response = self.client.post(
            "/api/payment/webhook",
            data=json.dumps({
                "event": "charge.success",
                "data": {},
            }),
            content_type="application/json",
            headers={
                "x-paystack-signature": "invalid-signature"
            },
        )

        self.assertEqual(response.status_code, 401)

    def test_webhook_rejects_malformed_signed_payload(self):
        test_secret = "local-fake-paystack-secret"
        payload = b"not-valid-json"

        signature = hmac.new(
            test_secret.encode("utf-8"),
            payload,
            hashlib.sha512,
        ).hexdigest()

        with patch("app.PAYSTACK_SECRET_KEY", test_secret):
            response = self.client.post(
                "/api/payment/webhook",
                data=payload,
                content_type="application/json",
                headers={
                    "x-paystack-signature": signature
                },
            )

        self.assertEqual(response.status_code, 400)

    def test_webhook_ignores_unsupported_event(self):
        test_secret = "local-fake-paystack-secret"

        payload = json.dumps({
            "event": "transfer.success",
            "data": {},
        }).encode("utf-8")

        signature = hmac.new(
            test_secret.encode("utf-8"),
            payload,
            hashlib.sha512,
        ).hexdigest()

        with patch("app.PAYSTACK_SECRET_KEY", test_secret):
            response = self.client.post(
                "/api/payment/webhook",
                data=payload,
                content_type="application/json",
                headers={
                    "x-paystack-signature": signature
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Payment.query.count(), 0)
        self.assertEqual(Subscription.query.count(), 0)

    def test_payment_verify_rejects_wrong_currency(self):
        import app as app_module

        self.create_pending_payment("TEST-CURRENCY-001")

        paystack_response = {
            "status": "success",
            "reference": "TEST-CURRENCY-001",
            "amount": 700000,
            "currency": "USD",
        }

        with patch.object(
            app_module,
            "verify_transaction",
            return_value=paystack_response,
        ):
            response = self.client.post(
                "/api/payment/verify",
                json={"reference": "TEST-CURRENCY-001"},
                headers=self.auth_headers(),
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            Payment.query.filter_by(
                transaction_reference="TEST-CURRENCY-001"
            ).first().status,
            "pending",
        )
        self.assertEqual(Subscription.query.count(), 0)

    def test_payment_verify_accepts_valid_ngn_currency(self):
        import app as app_module

        self.create_pending_payment("TEST-CURRENCY-002")

        paystack_response = {
            "status": "success",
            "reference": "TEST-CURRENCY-002",
            "amount": 700000,
            "currency": "NGN",
        }

        with patch.object(
            app_module,
            "verify_transaction",
            return_value=paystack_response,
        ):
            response = self.client.post(
                "/api/payment/verify",
                json={"reference": "TEST-CURRENCY-002"},
                headers=self.auth_headers(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            Payment.query.filter_by(
                transaction_reference="TEST-CURRENCY-002"
            ).first().status,
            "successful",
        )
        self.assertEqual(Subscription.query.count(), 1)

    def test_webhook_rejects_unsuccessful_transaction_status(self):
        test_secret = "local-fake-paystack-secret"

        self.create_pending_payment("TEST-WEBHOOK-STATUS-001")

        payload = json.dumps({
            "event": "charge.success",
            "data": {
                "status": "failed",
                "reference": "TEST-WEBHOOK-STATUS-001",
                "amount": 700000,
                "currency": "NGN",
            },
        }).encode("utf-8")

        with patch("app.PAYSTACK_SECRET_KEY", test_secret):
            response = self.signed_webhook_request(
                payload,
                test_secret,
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            Payment.query.filter_by(
                transaction_reference="TEST-WEBHOOK-STATUS-001"
            ).first().status,
            "pending",
        )
        self.assertEqual(Subscription.query.count(), 0)

    def test_webhook_rejects_wrong_currency(self):
        test_secret = "local-fake-paystack-secret"

        self.create_pending_payment("TEST-WEBHOOK-CURRENCY-001")

        payload = json.dumps({
            "event": "charge.success",
            "data": {
                "status": "success",
                "reference": "TEST-WEBHOOK-CURRENCY-001",
                "amount": 700000,
                "currency": "USD",
            },
        }).encode("utf-8")

        with patch("app.PAYSTACK_SECRET_KEY", test_secret):
            response = self.signed_webhook_request(
                payload,
                test_secret,
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            Payment.query.filter_by(
                transaction_reference="TEST-WEBHOOK-CURRENCY-001"
            ).first().status,
            "pending",
        )
        self.assertEqual(Subscription.query.count(), 0)

    def test_webhook_accepts_valid_ngn_payment(self):
        test_secret = "local-fake-paystack-secret"

        reference = "TEST-WEBHOOK-VALID-001"

        self.create_pending_payment(reference)

        payload = json.dumps({
            "event": "charge.success",
            "data": {
                "status": "success",
                "reference": reference,
                "amount": 700000,
                "currency": "NGN",
            },
        }).encode("utf-8")

        verified_transaction = {
            "status": "success",
            "reference": reference,
            "amount": 700000,
            "currency": "NGN",
        }

        with patch("app.PAYSTACK_SECRET_KEY", test_secret), \
            patch(
                "app.verify_transaction",
                return_value=verified_transaction,
            ) as mock_verify:

            response = self.signed_webhook_request(
                payload,
                test_secret,
            )

        self.assertEqual(response.status_code, 200)

        mock_verify.assert_called_once_with(reference)

        self.assertEqual(
            Payment.query.filter_by(
                transaction_reference=reference
            ).first().status,
            "successful",
        )

        self.assertEqual(Subscription.query.count(), 1)


    def test_webhook_rejects_verified_reference_mismatch(self):
        test_secret = "local-fake-paystack-secret"

        reference = "TEST-WEBHOOK-MISMATCH-001"

        self.create_pending_payment(reference)

        payload = json.dumps({
            "event": "charge.success",
            "data": {
                "status": "success",
                "reference": reference,
                "amount": 700000,
                "currency": "NGN",
            },
        }).encode("utf-8")

        verified_transaction = {
            "status": "success",
            "reference": "DIFFERENT-REFERENCE",
            "amount": 700000,
            "currency": "NGN",
        }

        with patch("app.PAYSTACK_SECRET_KEY", test_secret), \
            patch(
                "app.verify_transaction",
                return_value=verified_transaction,
            ):

            response = self.signed_webhook_request(
                payload,
                test_secret,
            )

        self.assertEqual(response.status_code, 400)

        self.assertEqual(
            Payment.query.filter_by(
                transaction_reference=reference
            ).first().status,
            "pending",
        )

        self.assertEqual(Subscription.query.count(), 0)


    def test_webhook_handles_paystack_verification_failure(self):
        test_secret = "local-fake-paystack-secret"

        reference = "TEST-WEBHOOK-VERIFY-FAIL-001"

        self.create_pending_payment(reference)

        payload = json.dumps({
            "event": "charge.success",
            "data": {
                "status": "success",
                "reference": reference,
                "amount": 700000,
                "currency": "NGN",
            },
        }).encode("utf-8")

        with patch("app.PAYSTACK_SECRET_KEY", test_secret), \
            patch(
                "app.verify_transaction",
                side_effect=Exception("Simulated Paystack failure"),
            ):

            response = self.signed_webhook_request(
                payload,
                test_secret,
            )

        self.assertEqual(response.status_code, 502)

        self.assertEqual(
            Payment.query.filter_by(
                transaction_reference=reference
            ).first().status,
            "pending",
        )

        self.assertEqual(Subscription.query.count(), 0)





if __name__ == "__main__":
    unittest.main(verbosity=2)
