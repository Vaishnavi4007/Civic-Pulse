
import getpass
from werkzeug.security import generate_password_hash
from database import get_db_connection


def reset_admin():

    print("=== CivicPulse Admin Password Reset ===")

    email = input("Enter admin email: ").strip().lower()

    password = getpass.getpass("Enter new admin password: ")
    confirm = getpass.getpass("Confirm new password: ")

    if len(password) < 8:
        print("Password must contain at least 8 characters.")
        return

    if password != confirm:
        print("Passwords do not match.")
        return

    connection = get_db_connection()

    try:
        user = connection.execute(
            "SELECT * FROM users WHERE email = ?",
            (email,)
        ).fetchone()

        if not user:
            print("Email not found in database.")
            return

        print("Existing account found:", user["name"])

        confirm_admin = input(
            "Make this account an admin and reset password? (yes/no): "
        ).strip().lower()

        if confirm_admin != "yes":
            print("Operation cancelled.")
            return

        password_hash = generate_password_hash(password)

        connection.execute(
            """
            UPDATE users
            SET password_hash = ?, role = 'admin'
            WHERE email = ?
            """,
            (password_hash, email)
        )

        connection.commit()

        print("Admin password updated successfully!")
        print("You can now login using your new password.")

    finally:
        connection.close()


if __name__ == "__main__":
    reset_admin()
