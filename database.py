
import sqlite3
from pathlib import Path

# ==========================================
# DATABASE CONFIGURATION
# ==========================================

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "civicpulse.db"

# Area grouping radius: 2 kilometres
AREA_GROUP_RADIUS = 2000


def get_db_connection():
    """Create a connection to the SQLite database."""

    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")

    return connection


# ==========================================
# HELPER: CHECK TABLE COLUMNS
# ==========================================

def get_columns(connection, table_name):

    return {
        row["name"]
        for row in connection.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
    }


# ==========================================
# MIGRATE USER ROLES
# ==========================================

def migrate_user_roles(connection):

    table_info = connection.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table' AND name = 'users'
        """
    ).fetchone()

    if not table_info:
        return

    table_sql = table_info["sql"] or ""

    required_roles = [
        "super_admin",
        "regional_admin",
        "worker"
    ]

    if all(role in table_sql for role in required_roles):
        return

    print("Updating user role permissions...")

    # Disable foreign keys during the table migration.
    connection.execute("PRAGMA foreign_keys = OFF")

    try:

        connection.execute("BEGIN")

        connection.execute("""
            CREATE TABLE users_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,

                role TEXT NOT NULL DEFAULT 'citizen'
                    CHECK (
                        role IN (
                            'citizen',
                            'admin',
                            'super_admin',
                            'regional_admin',
                            'worker'
                        )
                    ),

                region_id INTEGER,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        connection.execute("""
            INSERT INTO users_new (
                id,
                name,
                email,
                password_hash,
                role,
                created_at
            )
            SELECT
                id,
                name,
                email,
                password_hash,
                role,
                created_at
            FROM users
        """)

        connection.execute("DROP TABLE users")

        connection.execute(
            "ALTER TABLE users_new RENAME TO users"
        )

        connection.commit()

        print("User role migration completed.")

    except Exception:

        connection.rollback()
        raise

    finally:

        connection.execute("PRAGMA foreign_keys = ON")


# ==========================================
# INITIALIZE DATABASE
# ==========================================

def init_db():

    connection = get_db_connection()

    try:

        # ----------------------------------
        # REGIONS TABLE
        # ----------------------------------

        connection.execute("""
            CREATE TABLE IF NOT EXISTS regions (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                city_name TEXT NOT NULL UNIQUE,

                state_name TEXT NOT NULL,

                region_admin_id INTEGER,

                area_group_radius REAL NOT NULL DEFAULT 2000,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY(region_admin_id)
                    REFERENCES users(id)
            )
        """)

        # ----------------------------------
        # USERS TABLE
        # ----------------------------------

        connection.execute("""
            CREATE TABLE IF NOT EXISTS users (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                name TEXT NOT NULL,

                email TEXT NOT NULL UNIQUE,

                password_hash TEXT NOT NULL,

                role TEXT NOT NULL DEFAULT 'citizen'
                    CHECK (
                        role IN (
                            'citizen',
                            'admin',
                            'super_admin',
                            'regional_admin',
                            'worker'
                        )
                    ),

                region_id INTEGER,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY(region_id)
                    REFERENCES regions(id)
            )
        """)

        # Upgrade old users table if necessary.
        migrate_user_roles(connection)

        # ----------------------------------
        # REPORTS TABLE
        # ----------------------------------

        connection.execute("""
            CREATE TABLE IF NOT EXISTS reports (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                user_id INTEGER NOT NULL,

                description TEXT NOT NULL,

                image_path TEXT NOT NULL,

                latitude REAL,

                longitude REAL,

                severity TEXT NOT NULL DEFAULT 'Medium'
                    CHECK (
                        severity IN ('Low', 'Medium', 'High')
                    ),

                status TEXT NOT NULL DEFAULT 'Pending'
                    CHECK (
                        status IN (
                            'Pending',
                            'Assigned',
                            'In Progress',
                            'Repair Submitted',
                            'Resolved'
                        )
                    ),

                priority_score REAL DEFAULT 0,

                duplicate_of_id INTEGER,

                resolved_image_path TEXT,

                region_id INTEGER,

                area_group_id INTEGER,

                assigned_worker_id INTEGER,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY(user_id)
                    REFERENCES users(id),

                FOREIGN KEY(duplicate_of_id)
                    REFERENCES reports(id),

                FOREIGN KEY(region_id)
                    REFERENCES regions(id),

                FOREIGN KEY(assigned_worker_id)
                    REFERENCES users(id)
            )
        """)

        # ----------------------------------
        # MIGRATE EXISTING REPORT COLUMNS
        # ----------------------------------

        report_columns = get_columns(
            connection,
            "reports"
        )

        report_migrations = {

            "ai_prediction": "TEXT",

            "ai_confidence": "REAL",

            "verification_status":
                "TEXT NOT NULL DEFAULT 'Pending Review'",

            "duplicate_status":
                "TEXT NOT NULL DEFAULT 'Not a Duplicate'",

            "region_id": "INTEGER",

            "area_group_id": "INTEGER",

            "assigned_worker_id": "INTEGER"

        }

        for column, data_type in report_migrations.items():

            if column not in report_columns:

                connection.execute(
                    f"""
                    ALTER TABLE reports
                    ADD COLUMN {column} {data_type}
                    """
                )

        # ----------------------------------
        # AREA GROUPS TABLE
        # ----------------------------------

        connection.execute("""
            CREATE TABLE IF NOT EXISTS area_groups (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                region_id INTEGER NOT NULL,

                group_name TEXT NOT NULL,

                center_latitude REAL NOT NULL,

                center_longitude REAL NOT NULL,

                radius_meters REAL NOT NULL DEFAULT 2000,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY(region_id)
                    REFERENCES regions(id)
            )
        """)

        # ----------------------------------
        # WORKER ASSIGNMENTS TABLE
        # ----------------------------------

        connection.execute("""
            CREATE TABLE IF NOT EXISTS worker_assignments (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                report_id INTEGER NOT NULL,

                worker_id INTEGER NOT NULL,

                assigned_by INTEGER NOT NULL,

                assignment_status TEXT NOT NULL DEFAULT 'Assigned'
                    CHECK (
                        assignment_status IN (
                            'Assigned',
                            'Accepted',
                            'In Progress',
                            'Repair Submitted',
                            'Completed',
                            'Reassigned'
                        )
                    ),

                assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY(report_id)
                    REFERENCES reports(id),

                FOREIGN KEY(worker_id)
                    REFERENCES users(id),

                FOREIGN KEY(assigned_by)
                    REFERENCES users(id)
            )
        """)

        # ----------------------------------
        # REPAIR UPDATES TABLE
        # ----------------------------------

        connection.execute("""
            CREATE TABLE IF NOT EXISTS repair_updates (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                report_id INTEGER NOT NULL,

                worker_id INTEGER NOT NULL,

                repair_image_path TEXT,

                repair_notes TEXT,

                repair_status TEXT NOT NULL DEFAULT 'In Progress'
                    CHECK (
                        repair_status IN (
                            'In Progress',
                            'Repair Submitted',
                            'Approved',
                            'Rejected'
                        )
                    ),

                admin_remarks TEXT,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY(report_id)
                    REFERENCES reports(id),

                FOREIGN KEY(worker_id)
                    REFERENCES users(id)
            )
        """)

        # ----------------------------------
        # DATABASE INDEXES
        # ----------------------------------

        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_reports_user
            ON reports(user_id)
        """)

        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_reports_status
            ON reports(status)
        """)

        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_reports_region
            ON reports(region_id)
        """)

        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_reports_area_group
            ON reports(area_group_id)
        """)

        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_reports_worker
            ON reports(assigned_worker_id)
        """)

        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_users_role
            ON users(role)
        """)

        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_users_region
            ON users(region_id)
        """)

        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_assignments_worker
            ON worker_assignments(worker_id)
        """)

        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_repair_updates_report
            ON repair_updates(report_id)
        """)

        # ----------------------------------
        # DEFAULT CITY / REGION RECORDS
        # ----------------------------------

        default_regions = [
            ("Hyderabad", "Telangana"),
            ("Visakhapatnam", "Andhra Pradesh"),
            ("Vijayawada", "Andhra Pradesh"),
            ("Guntur", "Andhra Pradesh")
        ]

        for city, state in default_regions:

            connection.execute("""
                INSERT OR IGNORE INTO regions (
                    city_name,
                    state_name,
                    area_group_radius
                )
                VALUES (?, ?, ?)
            """, (
                city,
                state,
                AREA_GROUP_RADIUS
            ))

        # ----------------------------------
        # LEGACY DUPLICATE STATUS MIGRATION
        # ----------------------------------

        connection.execute("""
            UPDATE reports
            SET duplicate_status = 'Pending Review'
            WHERE duplicate_of_id IS NOT NULL
              AND duplicate_status = 'Not a Duplicate'
        """)

        connection.commit()

        print("CivicPulse database initialized successfully!")
        print("Roles: Citizen, Super Admin, Regional Admin, Worker")
        print("Area grouping radius: 2000 metres")

    except Exception:

        connection.rollback()

        raise

    finally:

        connection.close()


# ==========================================
# RUN DATABASE INITIALIZATION
# ==========================================

if __name__ == "__main__":

    init_db()
