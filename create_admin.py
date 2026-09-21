from getpass import getpass

from app import app, db
from models import User


def main():
    print()
    print("=" * 60)
    print("NABTRANSCRIBER ADMIN ACCOUNT SETUP")
    print("=" * 60)
    print()

    email = input("Admin email: ").strip().lower()

    if not email:
        print("Error: Email is required.")
        return

    if "@" not in email or "." not in email.split("@")[-1]:
        print("Error: Please enter a valid email address.")
        return

    password = getpass("Admin password: ")

    if not password:
        print("Error: Password is required.")
        return

    if len(password) < 8:
        print("Error: Password must be at least 8 characters.")
        return

    confirm_password = getpass("Confirm admin password: ")

    if password != confirm_password:
        print("Error: Passwords do not match.")
        return

    with app.app_context():

        user = User.query.filter_by(
            email=email
        ).first()

        if user:
            user.set_password(password)
            user.role = "admin"

            db.session.commit()

            print()
            print("Admin account updated successfully.")
            print(f"Email: {user.email}")
            print(f"Role: {user.role}")

        else:
            user = User(
                email=email,
                role="admin",
            )

            user.set_password(password)

            db.session.add(user)
            db.session.commit()

            print()
            print("Admin account created successfully.")
            print(f"Email: {user.email}")
            print(f"Role: {user.role}")

    print()
    print("Admin setup complete.")
    print()


if __name__ == "__main__":
    main()