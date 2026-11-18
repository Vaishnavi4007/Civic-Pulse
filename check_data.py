import sqlite3

conn = sqlite3.connect("civicpulse.db")

cursor = conn.cursor()

cursor.execute("SELECT id, latitude, longitude FROM reports")

data = cursor.fetchall()

for row in data:
    print(row)

conn.close()