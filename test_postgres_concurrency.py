
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

# Only allow the dedicated local test database.
TEST_DATABASE_URL = os.getenv("NAB_TEST_DATABASE_URL", "")

if not TEST_DATABASE_URL:
    raise RuntimeError(
        "NAB_TEST_DATABASE_URL is not configured."
    )

if "nabtranscriber_security_test" not in TEST_DATABASE_URL:
    raise RuntimeError(
        "Refusing to run against an unapproved database."
    )

# Must be set before importing app.py.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["JWT_SECRET_KEY"] = (
    "local-postgres-concurrency-test-secret"
)

from app import app, db
from models import User, Payment, Subscription
from subscription_service import (
    activate_subscription_from_payment,
)


class PostgreSQLConcurrencyTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        cls.app_context = app.app_context()
        cls.app_context.push()

        # Create tables only in the dedicated test database.
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.app_context.pop()

    def setUp(self):
        db.session.remove()

        Payment.query.delete(synchronize_session=False)
        Subscription.query.delete(synchronize_session=False)
        User.query.delete(synchronize_session=False)
        db.session.commit()

        self.user = User(
            email="postgres-concurrency@example.test"
        )
        self.user.set_password("LocalTest-Password-123!")

        db.session.add(self.user)
        db.session.commit()

        self.user_id = self.user.id

        self.reference = "LOCAL-CONCURRENCY-TEST-001"

        payment = Payment(
            user_id=self.user_id,
            plan="gold",
            billing_cycle="monthly",
            amount=7000,
            currency="NGN",
            payment_gateway="paystack",
            transaction_reference=self.reference,
            status="pending",
        )

        db.session.add(payment)
        db.session.commit()

    def activate_payment(self, barrier):
        # Each worker gets its own Flask context and DB session.
        with app.app_context():
            db.session.remove()

            try:
                barrier.wait(timeout=10)

                payment, subscription = (
                    activate_subscription_from_payment(
                        user_id=self.user_id,
                        plan="gold",
                        billing_cycle="monthly",
                        amount=7000,
                        payment_gateway="paystack",
                        transaction_reference=self.reference,
                    )
                )

                return {
                    "payment_id": payment.id,
                    "subscription_id": subscription.id,
                    "status": payment.status,
                }

            finally:
                db.session.remove()

    def test_simultaneous_activation_is_idempotent(self):
        barrier = threading.Barrier(2)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(self.activate_payment, barrier)
                for _ in range(2)
            ]

            results = [
                future.result(timeout=30)
                for future in futures
            ]

        db.session.remove()

        payments = Payment.query.filter_by(
            transaction_reference=self.reference
        ).all()

        subscriptions = Subscription.query.filter_by(
            user_id=self.user_id
        ).all()

        self.assertEqual(len(payments), 1)
        self.assertEqual(len(subscriptions), 1)

        self.assertEqual(payments[0].status, "successful")

        # Both workers should refer to the same payment
        # and subscription.
        self.assertEqual(
            results[0]["payment_id"],
            results[1]["payment_id"],
        )

        self.assertEqual(
            results[0]["subscription_id"],
            results[1]["subscription_id"],
        )

        # One monthly activation must produce one
        # 30-day subscription period, not two.
        duration = (
            subscriptions[0].end_date
            - subscriptions[0].start_date
        )

        self.assertEqual(duration.days, 30)

    def test_two_different_payments_extend_subscription_once_each(self):
        # Create a second pending payment for the same user.
        second_reference = "LOCAL-CONCURRENCY-TEST-002"

        second_payment = Payment(
            user_id=self.user_id,
            plan="gold",
            billing_cycle="monthly",
            amount=7000,
            currency="NGN",
            payment_gateway="paystack",
            transaction_reference=second_reference,
            status="pending",
        )

        db.session.add(second_payment)
        db.session.commit()

        barrier = threading.Barrier(2)

        def activate(reference):
            with app.app_context():
                db.session.remove()

                try:
                    barrier.wait(timeout=10)

                    payment, subscription = (
                        activate_subscription_from_payment(
                            user_id=self.user_id,
                            plan="gold",
                            billing_cycle="monthly",
                            amount=7000,
                            payment_gateway="paystack",
                            transaction_reference=reference,
                        )
                    )

                    return payment.id, subscription.id

                finally:
                    db.session.remove()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    activate,
                    self.reference,
                ),
                executor.submit(
                    activate,
                    second_reference,
                ),
            ]

            results = [
                future.result(timeout=30)
                for future in futures
            ]

        db.session.remove()

        payments = Payment.query.filter(
            Payment.transaction_reference.in_([
                self.reference,
                second_reference,
            ])
        ).all()

        subscriptions = Subscription.query.filter_by(
            user_id=self.user_id
        ).all()

        self.assertEqual(len(payments), 2)
        self.assertEqual(len(subscriptions), 1)

        self.assertTrue(
            all(payment.status == "successful" for payment in payments)
        )

        self.assertEqual(
            results[0][1],
            results[1][1],
        )

        duration = (
            subscriptions[0].end_date
            - subscriptions[0].start_date
        )

        self.assertEqual(duration.days, 60)



if __name__ == "__main__":
    unittest.main()
