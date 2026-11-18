import os
import math
import sqlite3
import uuid
from functools import wraps

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    session,
    send_from_directory,
    abort
)

from werkzeug.utils import secure_filename
from werkzeug.security import (
    generate_password_hash,
    check_password_hash
)

from database import get_db_connection, init_db
from ai_model.predict import predict_pothole


# =====================================================
# FLASK CONFIGURATION
# =====================================================

app = Flask(__name__)

app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY",
    "civicpulse-local-dev-secret"
)

app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}

AREA_GROUP_RADIUS = 2000
DUPLICATE_RADIUS = 50

UPLOAD_ROOT = os.path.join(app.root_path, "uploads")

os.makedirs(os.path.join(UPLOAD_ROOT, "potholes"), exist_ok=True)
os.makedirs(os.path.join(UPLOAD_ROOT, "repairs"), exist_ok=True)

init_db()


# =====================================================
# DATABASE HELPERS
# =====================================================

def get_connection():
    return get_db_connection()
def promote_initial_super_admin():

    email = os.environ.get("INITIAL_SUPER_ADMIN_EMAIL")

    if not email:
        return

    connection = get_connection()

    try:
        cursor = connection.execute(
            """
            UPDATE users
            SET role = 'super_admin',
                region_id = NULL
            WHERE LOWER(email) = LOWER(?)
            """,
            (email,)
        )

        connection.commit()

        if cursor.rowcount == 0:
            app.logger.warning(
                "Initial Super Admin email not found."
            )
        else:
            app.logger.info(
                "Initial Super Admin role updated."
            )

    finally:
        connection.close()


promote_initial_super_admin()

def get_user_role():
    """
    Legacy 'admin' accounts are temporarily treated
    as Super Admins until their roles are updated.
    """
    role = session.get("role")

    if role == "admin":
        return "super_admin"

    return role


def get_user_region_id():
    return session.get("region_id")


def get_report(report_id, connection):
    return connection.execute(
        "SELECT * FROM reports WHERE id = ?",
        (report_id,)
    ).fetchone()


def report_is_in_region(report_id, region_id, connection):
    report_row = connection.execute(
        """
        SELECT id
        FROM reports
        WHERE id = ? AND region_id = ?
        """,
        (report_id, region_id)
    ).fetchone()

    return report_row is not None


def can_access_report(report_row, connection):
    """
    Check whether the logged-in user can access a report.
    """

    if not report_row:
        return False

    role = get_user_role()
    user_id = session.get("user_id")

    if role == "super_admin":
        return True

    if role == "regional_admin":
        return report_row["region_id"] == get_user_region_id()

    if role == "citizen":
        return report_row["user_id"] == user_id

    if role == "worker":
        assignment = connection.execute(
            """
            SELECT id
            FROM worker_assignments
            WHERE report_id = ?
              AND worker_id = ?
            """,
            (report_row["id"], user_id)
        ).fetchone()

        return assignment is not None

    return False


# =====================================================
# DATABASE SCHEMA CHECKS
# =====================================================

def ensure_duplicate_schema():

    connection = get_connection()

    try:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(reports)"
            ).fetchall()
        }

        if "duplicate_status" not in columns:
            connection.execute(
                """
                ALTER TABLE reports
                ADD COLUMN duplicate_status TEXT
                NOT NULL DEFAULT 'Not a Duplicate'
                """
            )

        connection.commit()

    finally:
        connection.close()


ensure_duplicate_schema()


# =====================================================
# DISTANCE CALCULATION
# =====================================================

def calculate_distance(lat1, lon1, lat2, lon2):

    lat1 = math.radians(float(lat1))
    lon1 = math.radians(float(lon1))
    lat2 = math.radians(float(lat2))
    lon2 = math.radians(float(lon2))

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1)
        * math.cos(lat2)
        * math.sin(dlon / 2) ** 2
    )

    a = max(0.0, min(1.0, a))

    return (
        6371000
        * 2
        * math.atan2(
            math.sqrt(a),
            math.sqrt(1 - a)
        )
    )


# =====================================================
# PRIORITY SCORE
# =====================================================

def calculate_priority_score(
    severity,
    ai_prediction,
    ai_confidence,
    verification_status
):

    if verification_status == "Not a Pothole":
        return 0

    severity_points = {
        "High": 40,
        "Medium": 25,
        "Low": 10
    }

    verification_points = {
        "Confirmed Pothole": 30,
        "Pending Review": 10,
        "Needs More Information": 5,
        "Not a Pothole": 0
    }

    score = severity_points.get(severity, 10)

    score += verification_points.get(
        verification_status,
        10
    )

    if ai_prediction == "Pothole" and ai_confidence is not None:

        score += round(
            min(max(float(ai_confidence), 0), 100) * 0.20
        )

    return score


# =====================================================
# GPS DUPLICATE DETECTION
# =====================================================

def find_duplicate_report(
    connection,
    latitude,
    longitude,
    threshold_m=DUPLICATE_RADIUS
):

    if latitude is None or longitude is None:
        return None

    existing_reports = connection.execute(
        """
        SELECT id, latitude, longitude
        FROM reports
        WHERE latitude IS NOT NULL
          AND longitude IS NOT NULL
          AND duplicate_of_id IS NULL
          AND COALESCE(
                verification_status,
                'Pending Review'
              ) != 'Not a Pothole'
        """
    ).fetchall()

    nearest_report_id = None
    minimum_distance = threshold_m

    for report in existing_reports:

        distance = calculate_distance(
            latitude,
            longitude,
            report["latitude"],
            report["longitude"]
        )

        if distance < minimum_distance:
            minimum_distance = distance
            nearest_report_id = report["id"]

    return nearest_report_id


# =====================================================
# AREA GROUPING - 2 KM RADIUS
# =====================================================


def assign_area_group(
    connection,
    region_id,
    latitude,
    longitude
):

    if (
        region_id is None
        or latitude is None
        or longitude is None
    ):
        return None

    groups = connection.execute(
        """
        SELECT id, center_latitude, center_longitude
        FROM area_groups
        WHERE region_id = ?
        """,
        (region_id,)
    ).fetchall()

    for group in groups:

        distance = calculate_distance(
            latitude,
            longitude,
            group["center_latitude"],
            group["center_longitude"]
        )

        if distance <= AREA_GROUP_RADIUS:
            return group["id"]

    # Generate a name for the new area group
    group_name = f"Region {region_id} - Area Group {len(groups) + 1}"

    cursor = connection.execute(
        """
        INSERT INTO area_groups
        (
            group_name,
            region_id,
            center_latitude,
            center_longitude,
            radius_meters
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            group_name,
            region_id,
            latitude,
            longitude,
            AREA_GROUP_RADIUS
        )
    )

    return cursor.lastrowid


# =====================================================
# AUTHENTICATION DECORATORS
# =====================================================

def login_required(view):

    @wraps(view)
    def wrapped(*args, **kwargs):

        if "user_id" not in session:

            flash("Please login first.", "warning")

            return redirect(url_for("login"))

        return view(*args, **kwargs)

    return wrapped


def role_required(*allowed_roles):

    def decorator(view):

        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):

            current_role = get_user_role()

            if current_role not in allowed_roles:

                flash(
                    "You do not have permission to access this page.",
                    "danger"
                )

                return redirect(url_for("dashboard"))

            return view(*args, **kwargs)

        return wrapped

    return decorator


def super_admin_required(view):
    return role_required("super_admin")(view)


def regional_admin_required(view):
    return role_required("regional_admin")(view)
def worker_required(view):
    return role_required("worker")(view)


def management_required(view):
    return role_required(
        "super_admin",
        "regional_admin"
    )(view)


# =====================================================
# LOGIN REDIRECTION
# =====================================================

def get_dashboard_url():

    role = get_user_role()

    if role == "super_admin":
        return url_for("super_admin_dashboard")

    if role == "regional_admin":
        return url_for("regional_admin_dashboard")

    if role == "worker":
        return url_for("worker_dashboard")

    return url_for("dashboard")


# =====================================================
# HOME PAGE
# =====================================================

@app.route("/")
def home():
    return render_template("index.html")


# =====================================================
# CITIZEN REGISTRATION
# =====================================================

@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "POST":

        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get(
            "confirm_password",
            ""
        )

        if not name or not email or not password:

            flash(
                "Please fill in all required fields.",
                "danger"
            )

            return redirect(url_for("register"))

        if password != confirm_password:

            flash("Passwords do not match.", "danger")

            return redirect(url_for("register"))

        if len(password) < 8:

            flash(
                "Password must contain at least 8 characters.",
                "danger"
            )

            return redirect(url_for("register"))

        connection = get_connection()

        try:

            connection.execute(
                """
                INSERT INTO users
                (name, email, password_hash, role)
                VALUES (?, ?, ?, 'citizen')
                """,
                (
                    name,
                    email,
                    generate_password_hash(password)
                )
            )

            connection.commit()

            flash(
                "Registration successful. Please login.",
                "success"
            )

            return redirect(url_for("login"))

        except sqlite3.IntegrityError:

            flash(
                "This email is already registered.",
                "danger"
            )

        finally:
            connection.close()

    return render_template("register.html")


# =====================================================
# LOGIN
# =====================================================

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        email = request.form.get(
            "email",
            ""
        ).strip().lower()

        password = request.form.get("password", "")

        connection = get_connection()

        try:

            user = connection.execute(
                """
                SELECT *
                FROM users
                WHERE email = ?
                """,
                (email,)
            ).fetchone()

        finally:
            connection.close()

        if user and check_password_hash(
            user["password_hash"],
            password
        ):

            session.clear()

            session["user_id"] = user["id"]
            session["name"] = user["name"]
            session["role"] = user["role"]
            session["region_id"] = user["region_id"]

            flash("Login successful!", "success")

            return redirect(get_dashboard_url())

        flash(
            "Invalid email or password.",
            "danger"
        )

    return render_template("login.html")


# =====================================================
# CITIZEN: SUBMIT POTHOLE REPORT
# =====================================================

@app.route("/report", methods=["GET", "POST"])
@role_required("citizen")
def report():

    connection = get_connection()

    if request.method == "GET":

        try:
            regions = connection.execute(
                """
                SELECT id, city_name, state_name
                FROM regions
                ORDER BY city_name
                """
            ).fetchall()

        finally:
            connection.close()

        return render_template(
            "report.html",
            regions=regions
        )

    image_path = None

    try:

        description = request.form.get(
            "description",
            ""
        ).strip()

        severity = request.form.get(
            "severity",
            "Medium"
        )

        region_id_text = request.form.get(
            "region_id",
            ""
        ).strip()

        image = request.files.get("image")

        latitude_text = request.form.get(
            "latitude",
            ""
        ).strip()

        longitude_text = request.form.get(
            "longitude",
            ""
        ).strip()

        if not description or not image or not image.filename:

            flash(
                "Please provide a description and image.",
                "danger"
            )

            return redirect(url_for("report"))

        if severity not in ["Low", "Medium", "High"]:

            flash("Invalid severity level.", "danger")

            return redirect(url_for("report"))

        if not region_id_text:

            flash(
                "Please select your city or region.",
                "warning"
            )

            return redirect(url_for("report"))

        try:
            region_id = int(region_id_text)
        except ValueError:

            flash("Invalid region selected.", "danger")

            return redirect(url_for("report"))

        region = connection.execute(
            """
            SELECT id
            FROM regions
            WHERE id = ?
            """,
            (region_id,)
        ).fetchone()

        if not region:

            flash("Selected region does not exist.", "danger")

            return redirect(url_for("report"))

        filename = secure_filename(image.filename)

        if "." not in filename:

            flash("Invalid image filename.", "danger")

            return redirect(url_for("report"))

        extension = filename.rsplit(".", 1)[1].lower()

        if extension not in ALLOWED_EXTENSIONS:

            flash(
                "Only JPG, JPEG, PNG and WEBP images are allowed.",
                "danger"
            )

            return redirect(url_for("report"))
        try:

            if not latitude_text or not longitude_text:
                raise ValueError

            latitude = float(latitude_text)
            longitude = float(longitude_text)

            if (
                not math.isfinite(latitude)
                or not -90 <= latitude <= 90
            ):
                raise ValueError

            if (
                not math.isfinite(longitude)
                or not -180 <= longitude <= 180
            ):
                raise ValueError

        except ValueError:

            flash(
                "Please provide valid GPS coordinates.",
                "danger"
            )

            return redirect(url_for("report"))

        filename = f"{uuid.uuid4().hex}.{extension}"

        image_path = os.path.join(
            UPLOAD_ROOT,
            "potholes",
            filename
        )

        relative_image_path = (
            f"uploads/potholes/{filename}"
        )

        image.save(image_path)

        # CNN prediction
        ai_prediction, ai_confidence = predict_pothole(
            image_path
        )

        verification_status = "Pending Review"

        duplicate_report_id = find_duplicate_report(
            connection,
            latitude,
            longitude
        )

        duplicate_status = (
            "Pending Review"
            if duplicate_report_id is not None
            else "Not a Duplicate"
        )

        priority_score = calculate_priority_score(
            severity,
            ai_prediction,
            ai_confidence,
            verification_status
        )

        area_group_id = assign_area_group(
            connection,
            region_id,
            latitude,
            longitude
        )

        connection.execute(
            """
            INSERT INTO reports
            (
                user_id,
                description,
                image_path,
                latitude,
                longitude,
                severity,
                status,
                ai_prediction,
                ai_confidence,
                priority_score,
                verification_status,
                duplicate_of_id,
                duplicate_status,
                region_id,
                area_group_id
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, 'Pending',
                ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                session["user_id"],
                description,
                relative_image_path,
                latitude,
                longitude,
                severity,
                ai_prediction,
                ai_confidence,
                priority_score,
                verification_status,
                duplicate_report_id,
                duplicate_status,
                region_id,
                area_group_id
            )
        )

        connection.commit()

        if duplicate_report_id:

            flash(
                f"Possible duplicate detected. Linked to Report #{duplicate_report_id}.",
                "warning"
            )

        else:

            flash(
                "Pothole report submitted successfully!",
                "success"
            )

        return redirect(url_for("dashboard"))

    except Exception:

        connection.rollback()

        if image_path and os.path.exists(image_path):
            os.remove(image_path)

        app.logger.exception("Report submission failed")

        flash(
            "Unable to submit report. Please try again.",
            "danger"
        )

        return redirect(url_for("report"))

    finally:
        connection.close()
        # =====================================================
# SHARED REPORT QUERY
# =====================================================

def get_reports_for_management(connection, role):

    base_query = """
        SELECT
            reports.*,
            users.name AS citizen_name,
            regions.city_name,
            (
                SELECT COUNT(*)
                FROM reports AS duplicates
                WHERE duplicates.duplicate_of_id = reports.id
                  AND duplicates.duplicate_status = 'Confirmed Duplicate'
            ) AS duplicate_count,
            (
                SELECT repair_image_path
                FROM repair_updates
                WHERE repair_updates.report_id = reports.id
                  AND repair_image_path IS NOT NULL
                ORDER BY repair_updates.id DESC
                LIMIT 1
            ) AS latest_repair_image,
            (
                SELECT repair_status
                FROM repair_updates
                WHERE repair_updates.report_id = reports.id
                ORDER BY repair_updates.id DESC
                LIMIT 1
            ) AS latest_repair_status
        FROM reports
        JOIN users ON reports.user_id = users.id
        LEFT JOIN regions ON reports.region_id = regions.id
    """

    if role == "regional_admin":

        base_query += """
            WHERE reports.region_id = ?
            ORDER BY reports.priority_score DESC,
                     reports.created_at DESC
        """

        return connection.execute(
            base_query,
            (get_user_region_id(),)
        ).fetchall()

    base_query += """
        ORDER BY reports.priority_score DESC,
                 reports.created_at DESC
    """

    return connection.execute(base_query).fetchall()


def get_report_statistics(connection, region_id=None):

    query = """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'Pending' THEN 1 ELSE 0 END) AS pending,
            SUM(CASE WHEN status = 'In Progress' THEN 1 ELSE 0 END) AS in_progress,
            SUM(CASE WHEN status = 'Resolved' THEN 1 ELSE 0 END) AS resolved
        FROM reports
    """

    if region_id is not None:

        query += " WHERE region_id = ?"

        return connection.execute(
            query,
            (region_id,)
        ).fetchone()

    return connection.execute(query).fetchone()


# =====================================================
# SUPER ADMIN DASHBOARD
# Existing Admin Dashboard becomes Super Admin
# =====================================================

@app.route("/super-admin")
@super_admin_required
def super_admin_dashboard():

    connection = get_connection()

    try:

        reports = get_reports_for_management(
            connection,
            "super_admin"
        )

        stats = get_report_statistics(connection)

        regions = connection.execute(
            """
            SELECT *
            FROM regions
            ORDER BY city_name
            """
        ).fetchall()

        regional_admins = connection.execute(
            """
            SELECT
                users.id,
                users.name,
                users.email,
                users.region_id,
                regions.city_name
            FROM users
            LEFT JOIN regions ON users.region_id = regions.id
            WHERE users.role = 'regional_admin'
            """
        ).fetchall()

        workers = connection.execute(
            """
            SELECT
                users.id,
                users.name,
                users.email,
                users.region_id,
                regions.city_name
            FROM users
            LEFT JOIN regions ON users.region_id = regions.id
            WHERE users.role = 'worker'
            """
        ).fetchall()

    finally:
        connection.close()

    return render_template(
        "super_admin_dashboard.html",
        reports=reports,
        stats=stats,
        regions=regions,
        regional_admins=regional_admins,
        workers=workers,
        user_name=session["name"],
        user_role="super_admin"
    )


# =====================================================
# REGIONAL ADMIN DASHBOARD
# =====================================================

@app.route("/regional-admin")
@regional_admin_required
def regional_admin_dashboard():

    region_id = get_user_region_id()

    if not region_id:

        flash(
            "Your account is not assigned to a region.",
            "danger"
        )

        return redirect(url_for("login"))

    connection = get_connection()

    try:

        reports = get_reports_for_management(
            connection,
            "regional_admin"
        )

        stats = get_report_statistics(
            connection,
            region_id
        )

        workers = connection.execute(
            """
            SELECT id, name, email
            FROM users
            WHERE role = 'worker'
              AND region_id = ?
            ORDER BY name
            """,
            (region_id,)
        ).fetchall()

        region = connection.execute(
            """
            SELECT *
            FROM regions
            WHERE id = ?
            """,
            (region_id,)
        ).fetchone()

        repair_updates = connection.execute(
            """
            SELECT
                repair_updates.*,
                reports.description,
                reports.id AS report_number,
                users.name AS worker_name
            FROM repair_updates
            JOIN reports
                ON repair_updates.report_id = reports.id
            JOIN users
                ON repair_updates.worker_id = users.id
            WHERE reports.region_id = ?
            ORDER BY repair_updates.id DESC
            """,
            (region_id,)
        ).fetchall()

    finally:
        connection.close()

    return render_template(
        "regional_admin_dashboard.html",
        reports=reports,
        stats=stats,
        workers=workers,
        region=region,
        repair_updates=repair_updates,
        user_name=session["name"],
        user_role="regional_admin"
    )


# =====================================================
# WORKER DASHBOARD
# =====================================================

@app.route("/worker")
@worker_required
def worker_dashboard():

    connection = get_connection()

    try:

        # Show only the latest assignment for each report assigned to
        # this worker. This prevents duplicate report cards when older
        # assignment records exist for the same worker and report.
        reports = connection.execute(
            """
            SELECT
                reports.*,
                worker_assignments.assignment_status,
                regions.city_name,
                (
                    SELECT repair_image_path
                    FROM repair_updates
                    WHERE repair_updates.report_id = reports.id
                    ORDER BY repair_updates.id DESC
                    LIMIT 1
                ) AS latest_repair_image,
                (
                    SELECT repair_status
                    FROM repair_updates
                    WHERE repair_updates.report_id = reports.id
                    ORDER BY repair_updates.id DESC
                    LIMIT 1
                ) AS latest_repair_status
            FROM worker_assignments
            JOIN reports
                ON worker_assignments.report_id = reports.id
            LEFT JOIN regions
                ON reports.region_id = regions.id
            WHERE worker_assignments.worker_id = ?
              AND worker_assignments.assignment_status != 'Reassigned'
              AND worker_assignments.id = (
                    SELECT MAX(wa2.id)
                    FROM worker_assignments AS wa2
                    WHERE wa2.report_id = worker_assignments.report_id
                      AND wa2.worker_id = worker_assignments.worker_id
              )
            ORDER BY reports.priority_score DESC,
                     reports.created_at DESC
            """,
            (session["user_id"],)
        ).fetchall()

    finally:
        connection.close()

    return render_template(
        "worker_dashboard.html",
        reports=reports,
        user_name=session["name"],
        user_role="worker"
    )


# =====================================================
# CITIZEN DASHBOARD
# =====================================================

@app.route("/dashboard")
@login_required
def dashboard():

    role = get_user_role()

    if role == "super_admin":
        return redirect(url_for("super_admin_dashboard"))

    if role == "regional_admin":
        return redirect(url_for("regional_admin_dashboard"))

    if role == "worker":
        return redirect(url_for("worker_dashboard"))

    connection = get_connection()

    try:

        reports = connection.execute(
            """
            SELECT
                reports.*,
                regions.city_name,
                (
                    SELECT repair_image_path
                    FROM repair_updates
                    WHERE repair_updates.report_id = reports.id
                    ORDER BY repair_updates.id DESC
                    LIMIT 1
                ) AS latest_repair_image,
                (
                    SELECT repair_status
                    FROM repair_updates
                    WHERE repair_updates.report_id = reports.id
                    ORDER BY repair_updates.id DESC
                    LIMIT 1
                ) AS latest_repair_status
            FROM reports
            LEFT JOIN regions
                ON reports.region_id = regions.id
            WHERE reports.user_id = ?
            ORDER BY reports.created_at DESC
            """,
            (session["user_id"],)
        ).fetchall()

    finally:
        connection.close()

    return render_template(
        "dashboard.html",
        reports=reports,
        user_name=session["name"],
        user_role="citizen"
    )


# =====================================================
# MAP VIEW
# =====================================================

@app.route("/map-view")
@management_required
def map_view():

    connection = get_connection()

    try:

        query = """
            SELECT
                id,
                description,
                latitude,
                longitude,
                severity,
                status,
                priority_score,
                verification_status,
                ai_prediction,
                region_id
            FROM reports
            WHERE latitude IS NOT NULL
              AND longitude IS NOT NULL
        """

        params = ()

        if get_user_role() == "regional_admin":

            query += " AND region_id = ?"

            params = (get_user_region_id(),)

        query += " ORDER BY priority_score DESC"

        rows = connection.execute(
            query,
            params
        ).fetchall()

        reports = [dict(row) for row in rows]

    finally:
        connection.close()

    return render_template(
        "map.html",
        reports=reports,
        user_name=session["name"]
    )


# =====================================================
# SUPER ADMIN: CREATE REGIONAL ADMIN OR WORKER
# =====================================================

@app.route("/create-user", methods=["POST"])
@super_admin_required
def create_user():

    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    role = request.form.get("role", "").strip()
    region_id_text = request.form.get("region_id", "").strip()

    if not name or not email or not password:

        flash(
            "Please fill in all user details.",
            "danger"
        )

        return redirect(url_for("super_admin_dashboard"))

    if len(password) < 8:

        flash(
            "Password must contain at least 8 characters.",
            "danger"
        )

        return redirect(url_for("super_admin_dashboard"))

    if role not in ["regional_admin", "worker"]:

        flash(
            "Only Regional Admin and Worker accounts can be created here.",
            "danger"
        )

        return redirect(url_for("super_admin_dashboard"))

    try:
        region_id = int(region_id_text)
    except ValueError:

        flash("Please select a valid region.", "danger")

        return redirect(url_for("super_admin_dashboard"))

    connection = get_connection()

    try:

        region = connection.execute(
            "SELECT id FROM regions WHERE id = ?",
            (region_id,)
        ).fetchone()

        if not region:

            flash("Selected region does not exist.", "danger")

            return redirect(url_for("super_admin_dashboard"))

        connection.execute(
            """
            INSERT INTO users
            (name, email, password_hash, role, region_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                name,
                email,
                generate_password_hash(password),
                role,
                region_id
            )
        )

        connection.commit()

        flash(
            f"{role.replace('_', ' ').title()} account created successfully.",
            "success"
        )

    except sqlite3.IntegrityError:

        flash(
            "This email is already registered.",
            "danger"
        )

    finally:
        connection.close()

    return redirect(url_for("super_admin_dashboard"))


# =====================================================
# SUPER ADMIN: UPDATE USER REGION
# =====================================================

@app.route(
    "/update-user-region/<int:user_id>",
    methods=["POST"]
)
@super_admin_required
def update_user_region(user_id):

    try:
        region_id = int(
            request.form.get("region_id", "")
        )
    except ValueError:

        flash("Invalid region selected.", "danger")

        return redirect(url_for("super_admin_dashboard"))

    connection = get_connection()

    try:

        user = connection.execute(
            """
            SELECT id, role
            FROM users
            WHERE id = ?
            """,
            (user_id,)
        ).fetchone()

        region = connection.execute(
            """
            SELECT id
            FROM regions
            WHERE id = ?
            """,
            (region_id,)
        ).fetchone()

        if not user or user["role"] not in [
            "regional_admin",
            "worker"
        ]:

            flash("User not found or cannot be updated.", "danger")

            return redirect(url_for("super_admin_dashboard"))

        if not region:

            flash("Region not found.", "danger")

            return redirect(url_for("super_admin_dashboard"))

        connection.execute(
            """
            UPDATE users
            SET region_id = ?
            WHERE id = ?
            """,
            (region_id, user_id)
        )

        if user["role"] == "regional_admin":

            connection.execute(
                """
                UPDATE regions
                SET region_admin_id = ?
                WHERE id = ?
                """,
                (user_id, region_id)
            )

        connection.commit()

        flash(
            "User region updated successfully.",
            "success"
        )

    finally:
        connection.close()

    return redirect(url_for("super_admin_dashboard"))


# =====================================================
# SUPER ADMIN / REGIONAL ADMIN: VERIFY REPORT
# =====================================================

@app.route(
    "/verify-report/<int:report_id>",
    methods=["POST"]
)
@management_required
def verify_report(report_id):

    verification_status = request.form.get(
        "verification_status",
        ""
    ).strip()

    allowed_statuses = [
        "Confirmed Pothole",
        "Not a Pothole",
        "Needs More Information"
    ]

    if verification_status not in allowed_statuses:

        flash("Invalid verification status.", "danger")

        return redirect(get_dashboard_url())

    connection = get_connection()

    try:

        report_row = get_report(report_id, connection)

        if not report_row or not can_access_report(
            report_row,
            connection
        ):

            flash("Report not found or access denied.", "danger")

            return redirect(get_dashboard_url())

        priority_score = calculate_priority_score(
            report_row["severity"],
            report_row["ai_prediction"],
            report_row["ai_confidence"],
            verification_status
        )

        connection.execute(
            """
            UPDATE reports
            SET verification_status = ?,
                priority_score = ?
            WHERE id = ?
            """,
            (
                verification_status,
                priority_score,
                report_id
            )
        )

        connection.commit()

        flash(
            f"Report #{report_id} verified successfully.",
            "success"
        )

    except Exception:

        connection.rollback()

        app.logger.exception("Report verification failed")

        flash(
            "Unable to verify report.",
            "danger"
        )

    finally:
        connection.close()

    return redirect(get_dashboard_url())


# =====================================================
# SUPER ADMIN / REGIONAL ADMIN: REVIEW DUPLICATE
# =====================================================

@app.route(
    "/review-duplicate/<int:report_id>",
    methods=["POST"]
)
@management_required
def review_duplicate(report_id):

    decision = request.form.get(
        "decision",
        ""
    ).strip()

    if decision not in [
        "Confirmed Duplicate",
        "Not a Duplicate"
    ]:

        flash("Invalid duplicate decision.", "danger")

        return redirect(get_dashboard_url())

    connection = get_connection()

    try:

        report_row = get_report(report_id, connection)

        if not report_row or not can_access_report(
            report_row,
            connection
        ):

            flash("Report not found or access denied.", "danger")

            return redirect(get_dashboard_url())

        if report_row["duplicate_of_id"] is None:

            flash(
                "This report has no duplicate link to review.",
                "warning"
            )

            return redirect(get_dashboard_url())

        connection.execute(
            """
            UPDATE reports
            SET duplicate_status = ?
            WHERE id = ?
            """,
            (decision, report_id)
        )
        connection.commit()

        flash(
            f"Duplicate review completed for Report #{report_id}.",
            "success"
        )

    except Exception:

        connection.rollback()

        app.logger.exception("Duplicate review failed")

        flash(
            "Unable to update duplicate review.",
            "danger"
        )

    finally:
        connection.close()

    return redirect(get_dashboard_url())


# =====================================================
# REGIONAL ADMIN: ASSIGN WORKER
# =====================================================

@app.route(
    "/assign-worker/<int:report_id>",
    methods=["POST"]
)
@regional_admin_required
def assign_worker(report_id):

    worker_id_text = request.form.get(
        "worker_id",
        ""
    ).strip()

    try:
        worker_id = int(worker_id_text)
    except ValueError:

        flash("Please select a valid worker.", "danger")

        return redirect(url_for("regional_admin_dashboard"))

    connection = get_connection()

    try:

        report_row = get_report(report_id, connection)

        if not report_row or not report_is_in_region(
            report_id,
            get_user_region_id(),
            connection
        ):

            flash("Report not found in your region.", "danger")

            return redirect(url_for("regional_admin_dashboard"))

        if report_row["verification_status"] != "Confirmed Pothole":

            flash(
                "Only confirmed pothole reports can be assigned.",
                "warning"
            )

            return redirect(url_for("regional_admin_dashboard"))

        worker = connection.execute(
            """
            SELECT id
            FROM users
            WHERE id = ?
              AND role = 'worker'
              AND region_id = ?
            """,
            (
                worker_id,
                get_user_region_id()
            )
        ).fetchone()

        if not worker:

            flash(
                "Selected worker does not belong to your region.",
                "danger"
            )

            return redirect(url_for("regional_admin_dashboard"))

        existing_assignment = connection.execute(
            """
            SELECT id
            FROM worker_assignments
            WHERE report_id = ?
              AND assignment_status NOT IN ('Completed', 'Reassigned')
            """,
            (report_id,)
        ).fetchone()

        if existing_assignment:

            connection.execute(
                """
                UPDATE worker_assignments
                SET assignment_status = 'Reassigned'
                WHERE report_id = ?
                  AND assignment_status NOT IN ('Completed', 'Reassigned')
                """,
                (report_id,)
            )

        connection.execute(
            """
            INSERT INTO worker_assignments
            (
                report_id,
                worker_id,
                assigned_by,
                assignment_status
            )
            VALUES (?, ?, ?, 'Assigned')
            """,
            (
                report_id,
                worker_id,
                session["user_id"]
            )
        )

        connection.execute(
            """
            UPDATE reports
            SET assigned_worker_id = ?,
                status = 'In Progress'
            WHERE id = ?
            """,
            (worker_id, report_id)
        )

        connection.commit()

        flash(
            f"Report #{report_id} assigned to worker successfully.",
            "success"
        )

    except Exception:

        connection.rollback()

        app.logger.exception("Worker assignment failed")

        flash(
            "Unable to assign worker.",
            "danger"
        )

    finally:
        connection.close()

    return redirect(url_for("regional_admin_dashboard"))


# =====================================================
# WORKER: ACCEPT ASSIGNMENT
# =====================================================

@app.route(
    "/worker/accept/<int:report_id>",
    methods=["POST"]
)
@worker_required
def accept_assignment(report_id):

    connection = get_connection()

    try:

        assignment = connection.execute(
            """
            SELECT id
            FROM worker_assignments
            WHERE report_id = ?
              AND worker_id = ?
              AND assignment_status = 'Assigned'
            """,
            (
                report_id,
                session["user_id"]
            )
        ).fetchone()

        if not assignment:

            flash("Assignment not found.", "danger")

            return redirect(url_for("worker_dashboard"))

        connection.execute(
            """
            UPDATE worker_assignments
            SET assignment_status = 'Accepted'
            WHERE id = ?
            """,
            (assignment["id"],)
        )

        connection.commit()

        flash("Assignment accepted.", "success")

    finally:
        connection.close()

    return redirect(url_for("worker_dashboard"))


# =====================================================
# WORKER: UPDATE REPAIR PROGRESS
# =====================================================

@app.route(
    "/worker/start-repair/<int:report_id>",
    methods=["POST"]
)
@worker_required
def start_repair(report_id):

    connection = get_connection()

    try:

        assignment = connection.execute(
            """
            SELECT id
            FROM worker_assignments
            WHERE report_id = ?
              AND worker_id = ?
              AND assignment_status IN ('Assigned', 'Accepted')
            """,
            (
                report_id,
                session["user_id"]
            )
        ).fetchone()

        if not assignment:

            flash("You cannot update this assignment.", "danger")

            return redirect(url_for("worker_dashboard"))

        connection.execute(
            """
            UPDATE worker_assignments
            SET assignment_status = 'In Progress'
            WHERE id = ?
            """,
            (assignment["id"],)
        )

        connection.execute(
            """
            UPDATE reports
            SET status = 'In Progress'
            WHERE id = ?
            """,
            (report_id,)
        )

        connection.commit()

        flash("Repair marked as in progress.", "success")

    finally:
        connection.close()

    return redirect(url_for("worker_dashboard"))


# =====================================================
# WORKER: SUBMIT REPAIR IMAGE
# =====================================================

@app.route(
    "/worker/submit-repair/<int:report_id>",
    methods=["POST"]
)
@worker_required
def submit_repair(report_id):

    repair_image = request.files.get("repair_image")

    repair_notes = request.form.get(
        "repair_notes",
        ""
    ).strip()

    if not repair_image or not repair_image.filename:

        flash(
            "Please upload an after-repair image.",
            "warning"
        )

        return redirect(url_for("worker_dashboard"))

    filename = secure_filename(repair_image.filename)

    if "." not in filename:

        flash("Invalid image filename.", "danger")

        return redirect(url_for("worker_dashboard"))

    extension = filename.rsplit(".", 1)[1].lower()

    if extension not in ALLOWED_EXTENSIONS:

        flash(
            "Only JPG, JPEG, PNG and WEBP images are allowed.",
            "danger"
        )

        return redirect(url_for("worker_dashboard"))

    connection = get_connection()

    image_path = None

    try:

        assignment = connection.execute(
            """
            SELECT id
            FROM worker_assignments
            WHERE report_id = ?
              AND worker_id = ?
              AND assignment_status IN (
                    'Assigned',
                    'Accepted',
                    'In Progress',
                    'Rejected'
              )
            ORDER BY id DESC
            LIMIT 1
            """,
            (
                report_id,
                session["user_id"]
            )
        ).fetchone()

        if not assignment:

            flash(
                "You are not assigned to this pothole.",
                "danger"
            )

            return redirect(url_for("worker_dashboard"))

        unique_filename = f"{uuid.uuid4().hex}.{extension}"

        image_path = os.path.join(
            UPLOAD_ROOT,
            "repairs",
            unique_filename
        )

        relative_image_path = (
            f"uploads/repairs/{unique_filename}"
        )

        repair_image.save(image_path)

        connection.execute(
            """
            INSERT INTO repair_updates
            (
                report_id,
                worker_id,
                repair_image_path,
                repair_notes,
                repair_status
            )
            VALUES (?, ?, ?, ?, 'Repair Submitted')
            """,
            (
                report_id,
                session["user_id"],
                relative_image_path,
                repair_notes
            )
        )

        connection.execute(
            """
            UPDATE worker_assignments
            SET assignment_status = 'Repair Submitted'
            WHERE id = ?
            """,
            (assignment["id"],)
        )

        # The report remains In Progress until Regional Admin approval.
        connection.execute(
            """
            UPDATE reports
            SET status = 'In Progress'
            WHERE id = ?
            """,
            (report_id,)
        )

        connection.commit()

        flash(
            "Repair image submitted. Waiting for Regional Admin approval.",
            "success"
        )

    except Exception:

        connection.rollback()

        if image_path and os.path.exists(image_path):
            os.remove(image_path)

        app.logger.exception("Repair submission failed")

        flash(
            "Unable to submit repair details.",
            "danger"
        )

    finally:
        connection.close()

    return redirect(url_for("worker_dashboard"))


# =====================================================
# REGIONAL ADMIN: APPROVE OR REJECT REPAIR
# =====================================================

@app.route(
    "/review-repair/<int:repair_id>",
    methods=["POST"]
)
@regional_admin_required
def review_repair(repair_id):

    decision = request.form.get(
        "decision",
        ""
    ).strip()

    remarks = request.form.get(
        "admin_remarks",
        ""
    ).strip()

    if decision not in ["Approved", "Rejected"]:

        flash("Invalid repair decision.", "danger")

        return redirect(url_for("regional_admin_dashboard"))

    connection = get_connection()

    try:

        repair = connection.execute(
            """
            SELECT
                repair_updates.*,
                reports.region_id
            FROM repair_updates
            JOIN reports
                ON repair_updates.report_id = reports.id
            WHERE repair_updates.id = ?
            """,
            (repair_id,)
        ).fetchone()

        if (
            not repair
            or repair["region_id"] != get_user_region_id()
        ):

            flash(
                "Repair submission not found in your region.",
                "danger"
            )

            return redirect(url_for("regional_admin_dashboard"))

        if repair["repair_status"] != "Repair Submitted":

            flash(
                "This repair submission has already been reviewed.",
                "warning"
            )

            return redirect(url_for("regional_admin_dashboard"))

        connection.execute(
            """
            UPDATE repair_updates
            SET repair_status = ?,
                admin_remarks = ?
            WHERE id = ?
            """,
            (
                decision,
                remarks,
                repair_id
            )
        )

        if decision == "Approved":

            connection.execute(
                """
                UPDATE reports
                SET status = 'Resolved',
                    resolved_image_path = ?
                WHERE id = ?
                """,
                (
                    repair["repair_image_path"],
                    repair["report_id"]
                )
            )

            connection.execute(
                """
                UPDATE worker_assignments
                SET assignment_status = 'Completed'
                WHERE report_id = ?
                  AND worker_id = ?
                """,
                (
                    repair["report_id"],
                    repair["worker_id"]
                )
            )

            flash(
                "Repair approved. Report marked as Resolved.",
                "success"
            )

        else:

            connection.execute(
                """
                UPDATE worker_assignments
                SET assignment_status = 'In Progress'
                WHERE report_id = ?
                  AND worker_id = ?
                """,
                (
                    repair["report_id"],
                    repair["worker_id"]
                )
            )

            connection.execute(
                """
                UPDATE reports
                SET status = 'In Progress'
                WHERE id = ?
                """,
                (repair["report_id"],)
            )

            flash(
                "Repair rejected. Worker can submit another repair update.",
                "warning"
            )

        connection.commit()

    except Exception:

        connection.rollback()

        app.logger.exception("Repair review failed")

        flash(
            "Unable to review repair submission.",
            "danger"
        )

    finally:
        connection.close()

    return redirect(url_for("regional_admin_dashboard"))


# =====================================================
# REPORT STATUS UPDATE
# Only Pending / In Progress
# Resolved is controlled by repair approval
# =====================================================

@app.route(
    "/update-report/<int:report_id>",
    methods=["POST"]
)
@management_required
def update_report(report_id):

    new_status = request.form.get(
        "status",
        ""
    ).strip()

    if new_status not in ["Pending", "In Progress"]:

        flash(
            "Resolved status can only be set after repair approval.",
            "warning"
        )

        return redirect(get_dashboard_url())

    connection = get_connection()

    try:

        report_row = get_report(report_id, connection)

        if not report_row or not can_access_report(
            report_row,
            connection
        ):

            flash("Report not found or access denied.", "danger")

            return redirect(get_dashboard_url())

        connection.execute(
            """
            UPDATE reports
            SET status = ?
            WHERE id = ?
            """,
            (new_status, report_id)
        )

        connection.commit()

        flash(
            f"Report #{report_id} status updated.",
            "success"
        )

    finally:
        connection.close()

    return redirect(get_dashboard_url())


# =====================================================
# ADMIN SHORTCUT
# =====================================================

@app.route("/admin")
@super_admin_required
def admin_dashboard():

    return redirect(url_for("super_admin_dashboard"))


# =====================================================
# SERVE UPLOADED IMAGES SECURELY
# =====================================================

@app.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):

    relative_path = f"uploads/{filename}"

    connection = get_connection()

    try:

        report_rows = connection.execute(
            """
            SELECT *
            FROM reports
            WHERE image_path = ?
               OR resolved_image_path = ?
            """,
            (
                relative_path,
                relative_path
            )
        ).fetchall()

        repair_rows = connection.execute(
            """
            SELECT
                repair_updates.report_id
            FROM repair_updates
            WHERE repair_updates.repair_image_path = ?
            """,
            (relative_path,)
        ).fetchall()

        allowed = False

        for report_row in report_rows:

            if can_access_report(report_row, connection):
                allowed = True
                break

        if not allowed:

            for repair_row in repair_rows:

                report_row = get_report(
                    repair_row["report_id"],
                    connection
                )

                if can_access_report(report_row, connection):
                    allowed = True
                    break

        if not allowed:
            abort(403)

    finally:
        connection.close()

    return send_from_directory(
        UPLOAD_ROOT,
        filename
    )


# =====================================================
# LOGOUT
# =====================================================

@app.route("/logout", methods=["POST"])
@login_required
def logout():

    session.clear()

    flash(
        "You have logged out successfully.",
        "success"
    )

    return redirect(url_for("login"))


# =====================================================
# ERROR HANDLER: FILE TOO LARGE
# =====================================================

@app.errorhandler(413)
def file_too_large(error):

    flash(
        "Image size is too large. Maximum allowed size is 5 MB.",
        "danger"
    )

    return redirect(url_for("report"))


# =====================================================
# RUN APPLICATION
# =====================================================

if __name__ == "__main__":

    app.run(debug=False)
    