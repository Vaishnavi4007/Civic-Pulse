import sqlite3

connection = sqlite3.connect("civicpulse.db")

users = connection.execute(
    "SELECT id, name, email, role FROM users"
).fetchall()

for user in users:
    print(user)

connection.close()