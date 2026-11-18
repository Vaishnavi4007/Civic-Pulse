
import getpass

from database import get_db_connection
from werkzeug.security import generate_password_hash


def create_admin():

    print("=== CivicPulse Admin Setup ===")

    name = input("Enter admin name: ").strip()
    email = input("Enter admin email: ").strip().lower()
    password = getpass.getpass("Enter admin password: ")
    confirm = getpass.getpass("Confirm admin password: ")

    if not name or not email:
        print("Name and email are required.")
        return

    if len(password) < 8:
        print("Password must contain at least 8 characters.")
        return

    if password != confirm:
        print("Passwords do not match.")
        return

    connection = get_db_connection()

    try:
        existing = connection.execute(
            "SELECT id FROM users WHERE email = ?",
            (email,)
        ).fetchone()

        if existing:
            print("This email is already registered.")
            return

        password_hash = generate_password_hash(password)

        connection.execute(
            """
            INSERT INTO users (name, email, password_hash, role)
            VALUES (?, ?, ?, 'admin')
            """,
            (name, email, password_hash)
        )

        connection.commit()
        print("Admin account created successfully!")

    finally:
        connection.close()


if __name__ == "__main__":
    create_admin()
