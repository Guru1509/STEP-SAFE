"""STEP-SAFE student demonstration backend with role-based access & notifications.
Not a medical device.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any

import click
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from notifications import NotificationService

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("step_safe.app")

BASE_DIR = Path(__file__).resolve().parent
DATABASE = Path(os.environ.get("STEP_SAFE_DB", BASE_DIR / "step_safe.db"))
CALIBRATION_SAMPLES = 20
PRESSURE_SECONDS_FOR_HIGH_RISK = 30

app = Flask(__name__)
app.config.update(
    # Cryptographically signed client-side session cookies
    SECRET_KEY=os.environ.get("STEP_SAFE_SECRET_KEY", "dev-secret-key-change-in-production-12345"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("STEP_SAFE_HTTPS", "0") == "1",
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE, timeout=15.0)
    connection.row_factory = sqlite3.Row
    return connection


def rows_as_dicts(rows: list[sqlite3.Row] | None) -> list[dict[str, Any]]:
    if not rows:
        return []
    return [dict(row) for row in rows]


# Initialize notification dispatcher
notification_service = NotificationService(db)


def initialise_database() -> None:
    """Safe SQLite migrations and initial table setup using PRAGMA inspection."""
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at TEXT NOT NULL,
            device_id TEXT NOT NULL,
            fsr REAL NOT NULL,
            temperature REAL NOT NULL,
            status TEXT NOT NULL,
            pressure_seconds INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS calibrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            completed_at TEXT NOT NULL,
            device_id TEXT NOT NULL,
            fsr_baseline REAL NOT NULL,
            fsr_threshold REAL NOT NULL,
            temp_baseline REAL NOT NULL,
            temp_threshold REAL NOT NULL,
            sample_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            device_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            peak_fsr REAL NOT NULL,
            peak_temperature REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS device_state (
            device_id TEXT PRIMARY KEY,
            pressure_started_at TEXT,
            active_event_id INTEGER,
            active_event_type TEXT
        );
        CREATE TABLE IF NOT EXISTS calibration_sessions (
            device_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            fsr_total REAL NOT NULL DEFAULT 0,
            temp_total REAL NOT NULL DEFAULT 0,
            sample_count INTEGER NOT NULL DEFAULT 0
        );

        -- User and Profile Tables
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL, -- 'patient', 'caregiver', 'admin'
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        -- Patient Profiles: single source of truth for device assignment is devices.patient_user_id
        CREATE TABLE IF NOT EXISTS patient_profiles (
            user_id INTEGER PRIMARY KEY,
            date_of_birth TEXT,
            emergency_contact TEXT,
            alert_preferences TEXT DEFAULT '{"in_app":true,"push":true}',
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        -- Caregiver-to-Patient Many-to-Many Assignment
        CREATE TABLE IF NOT EXISTS caregiver_patients (
            caregiver_user_id INTEGER NOT NULL,
            patient_user_id INTEGER NOT NULL,
            assigned_at TEXT NOT NULL,
            PRIMARY KEY (caregiver_user_id, patient_user_id),
            FOREIGN KEY (caregiver_user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (patient_user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        -- Devices: patient_user_id is the authoritative link between device and patient
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT UNIQUE NOT NULL,
            patient_user_id INTEGER,
            display_name TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY (patient_user_id) REFERENCES users(id) ON DELETE SET NULL
        );

        -- Notifications with database-level uniqueness constraint for strict deduplication
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER,
            recipient_user_id INTEGER NOT NULL,
            type TEXT NOT NULL, -- 'HIGH_PRESSURE', 'HIGH_RISK', 'SYSTEM'
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'unread', -- 'unread', 'read'
            sent_at TEXT NOT NULL,
            read_at TEXT,
            UNIQUE(event_id, recipient_user_id, type),
            FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE SET NULL,
            FOREIGN KEY (recipient_user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        -- Push Subscriptions for FCM HTTP v1 tokens
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token TEXT UNIQUE NOT NULL,
            platform TEXT NOT NULL DEFAULT 'web',
            active INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """)

        # Safe migration: inspect device_state columns via PRAGMA before altering
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(device_state)").fetchall()]
        if "last_notified_event_id" not in columns:
            logger.info("Migrating device_state: adding column last_notified_event_id")
            conn.execute("ALTER TABLE device_state ADD COLUMN last_notified_event_id INTEGER DEFAULT 0")

        # Opt-in demonstration seeding (strictly active only when STEP_SAFE_SEED_DEMO=1)
        if os.environ.get("STEP_SAFE_SEED_DEMO") == "1":
            seed_demo_accounts_if_empty(conn)


def seed_demo_accounts_if_empty(conn: sqlite3.Connection, force: bool = False) -> None:
    """Opt-in development seeder for demonstration accounts."""
    user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if user_count > 0 and not force:
        return

    logger.info("STEP_SAFE_SEED_DEMO active: Seeding development demo accounts...")
    ts = now()

    # 1. Admin
    existing_admin = conn.execute("SELECT id FROM users WHERE email = 'admin@stepsafe.local'").fetchone()
    if existing_admin:
        admin_id = existing_admin["id"]
    else:
        cursor = conn.execute(
            "INSERT INTO users (full_name, email, password_hash, role, active, created_at) VALUES (?, ?, ?, 'admin', 1, ?)",
            ("System Administrator", "admin@stepsafe.local", generate_password_hash("admin123"), ts),
        )
        admin_id = cursor.lastrowid

    # 2. Caregiver
    existing_cg = conn.execute("SELECT id FROM users WHERE email = 'caregiver@stepsafe.local'").fetchone()
    if existing_cg:
        caregiver_id = existing_cg["id"]
    else:
        cursor = conn.execute(
            "INSERT INTO users (full_name, email, password_hash, role, active, created_at) VALUES (?, ?, ?, 'caregiver', 1, ?)",
            ("Sarah Caregiver, RN", "caregiver@stepsafe.local", generate_password_hash("caregiver123"), ts),
        )
        caregiver_id = cursor.lastrowid

    # 3. Patient
    existing_pt = conn.execute("SELECT id FROM users WHERE email = 'patient@stepsafe.local'").fetchone()
    if existing_pt:
        patient_id = existing_pt["id"]
    else:
        cursor = conn.execute(
            "INSERT INTO users (full_name, email, password_hash, role, active, created_at) VALUES (?, ?, ?, 'patient', 1, ?)",
            ("John Patient", "patient@stepsafe.local", generate_password_hash("patient123"), ts),
        )
        patient_id = cursor.lastrowid

    # Create patient profile
    conn.execute(
        "INSERT OR IGNORE INTO patient_profiles (user_id, date_of_birth, emergency_contact, alert_preferences) VALUES (?, ?, ?, ?)",
        (patient_id, "1965-04-12", "Emergency Contact: +1 555-0192", json.dumps({"in_app": True, "push": True})),
    )

    # Register default device and assign to patient
    conn.execute(
        "INSERT OR IGNORE INTO devices (device_id, patient_user_id, display_name, active, created_at) VALUES (?, ?, ?, 1, ?)",
        ("step-safe-esp32", patient_id, "Left Foot Sensor Unit #1", ts),
    )

    # Assign caregiver to patient
    conn.execute(
        "INSERT OR IGNORE INTO caregiver_patients (caregiver_user_id, patient_user_id, assigned_at) VALUES (?, ?, ?)",
        (caregiver_id, patient_id, ts),
    )
    logger.info("Demo accounts seeded successfully: admin@stepsafe.local, caregiver@stepsafe.local, patient@stepsafe.local")


# CLI Command for initial production/real admin setup
@app.cli.command("bootstrap-admin")
@click.option("--email", prompt=True, default="admin@stepsafe.local", help="Admin email address")
@click.option("--name", prompt=True, default="System Administrator", help="Admin full name")
@click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True, help="Admin password")
def bootstrap_admin_cmd(email: str, name: str, password: str) -> None:
    """Create or reset the initial administrator account securely."""
    initialise_database()
    with db() as conn:
        existing = conn.execute("SELECT id FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
        pw_hash = generate_password_hash(password)
        if existing:
            conn.execute(
                "UPDATE users SET full_name = ?, password_hash = ?, role = 'admin', active = 1 WHERE id = ?",
                (name.strip(), pw_hash, existing["id"]),
            )
            click.echo(f"Admin account '{email}' updated successfully.")
        else:
            conn.execute(
                "INSERT INTO users (full_name, email, password_hash, role, active, created_at) VALUES (?, ?, ?, 'admin', 1, ?)",
                (name.strip(), email.strip().lower(), pw_hash, now()),
            )
            click.echo(f"Admin account '{email}' created successfully.")


# CLI Command to quickly populate demonstration accounts
@app.cli.command("seed-demo")
def seed_demo_cmd() -> None:
    """Seed demonstration accounts (Admin, Caregiver, Patient) for testing."""
    initialise_database()
    with db() as conn:
        seed_demo_accounts_if_empty(conn, force=True)
        click.echo("Demo accounts seeded successfully:")
        click.echo("  Admin:     admin@stepsafe.local     (Password: admin123)")
        click.echo("  Caregiver: caregiver@stepsafe.local (Password: caregiver123)")
        click.echo("  Patient:   patient@stepsafe.local   (Password: patient123)")


# ---------------------------------------------------------------------------
# Authentication & Authorization Helpers
# ---------------------------------------------------------------------------

def current_user(conn: sqlite3.Connection) -> dict[str, Any] | None:
    user_id = session.get("user_id")
    if not user_id:
        return None
    row = conn.execute(
        "SELECT id, full_name, email, role, active FROM users WHERE id = ? AND active = 1",
        (user_id,),
    ).fetchone()
    return dict(row) if row else None


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        with db() as conn:
            user = current_user(conn)
        if not user:
            if request.path.startswith("/api/"):
                return jsonify(error="Authentication required", code="UNAUTHORIZED"), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


def roles_accepted(*roles: str):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            with db() as conn:
                user = current_user(conn)
            if not user:
                if request.path.startswith("/api/"):
                    return jsonify(error="Authentication required", code="UNAUTHORIZED"), 401
                return redirect(url_for("login_page"))
            if user["role"] not in roles:
                return jsonify(error="Access denied: unauthorized role", code="FORBIDDEN"), 403
            return f(*args, **kwargs)
        return decorated
    return decorator


def check_patient_access(conn: sqlite3.Connection, user: dict[str, Any], patient_user_id: int) -> bool:
    """Verifies if user is authorized to view or manage patient_user_id data."""
    if user["role"] == "admin":
        return True
    if user["role"] == "patient":
        return user["id"] == patient_user_id
    if user["role"] == "caregiver":
        row = conn.execute(
            "SELECT 1 FROM caregiver_patients WHERE caregiver_user_id = ? AND patient_user_id = ?",
            (user["id"], patient_user_id),
        ).fetchone()
        return bool(row)
    return False


def get_patient_assigned_device(conn: sqlite3.Connection, patient_user_id: int) -> dict[str, Any] | None:
    """Single source of truth: queries devices where patient_user_id = patient_user_id."""
    row = conn.execute(
        "SELECT * FROM devices WHERE patient_user_id = ? AND active = 1 LIMIT 1",
        (patient_user_id,),
    ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Calibration & Sensor Telemetry Logic
# ---------------------------------------------------------------------------

def latest_calibration(conn: sqlite3.Connection, device_id: str):
    return conn.execute("SELECT * FROM calibrations WHERE device_id = ? ORDER BY id DESC LIMIT 1", (device_id,)).fetchone()


def finish_calibration_if_ready(conn: sqlite3.Connection, device_id: str):
    session_row = conn.execute("SELECT * FROM calibration_sessions WHERE device_id = ?", (device_id,)).fetchone()
    if not session_row or session_row["sample_count"] < CALIBRATION_SAMPLES:
        return None
    fsr_base = session_row["fsr_total"] / session_row["sample_count"]
    temp_base = session_row["temp_total"] / session_row["sample_count"]
    # 20% rule with demo minimum floor
    fsr_threshold = max(fsr_base * 1.20, 100.0)
    calibration = (now(), device_id, fsr_base, fsr_threshold, temp_base, temp_base + 2.0, session_row["sample_count"])
    conn.execute(
        """INSERT INTO calibrations
        (completed_at, device_id, fsr_baseline, fsr_threshold, temp_baseline, temp_threshold, sample_count)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        calibration,
    )
    conn.execute("DELETE FROM calibration_sessions WHERE device_id = ?", (device_id,))
    return calibration


def status_for(conn: sqlite3.Connection, device_id: str, fsr: float, temperature: float) -> tuple[str, int]:
    # Defensive guard: if a calibration session is active, pressure detection is suspended
    session_row = conn.execute("SELECT 1 FROM calibration_sessions WHERE device_id = ?", (device_id,)).fetchone()
    if session_row:
        conn.execute("UPDATE device_state SET pressure_started_at = NULL WHERE device_id = ?", (device_id,))
        return "CALIBRATING", 0

    calibration = latest_calibration(conn, device_id)
    if not calibration:
        return "UNCALIBRATED", 0
    state = conn.execute("SELECT * FROM device_state WHERE device_id = ?", (device_id,)).fetchone()
    if not state:
        conn.execute("INSERT INTO device_state (device_id) VALUES (?)", (device_id,))
        state = conn.execute("SELECT * FROM device_state WHERE device_id = ?", (device_id,)).fetchone()
    pressure = fsr >= calibration["fsr_threshold"]
    if pressure and not state["pressure_started_at"]:
        started = now()
        conn.execute("UPDATE device_state SET pressure_started_at = ? WHERE device_id = ?", (started, device_id))
        pressure_seconds = 0
    elif pressure:
        started = datetime.fromisoformat(state["pressure_started_at"])
        pressure_seconds = max(0, int((datetime.now(timezone.utc) - started).total_seconds()))
    else:
        conn.execute("UPDATE device_state SET pressure_started_at = NULL WHERE device_id = ?", (device_id,))
        pressure_seconds = 0

    if pressure:
        if pressure_seconds >= PRESSURE_SECONDS_FOR_HIGH_RISK:
            if temperature >= calibration["temp_threshold"]:
                return "HIGH_RISK", pressure_seconds
            return "HIGH_PRESSURE", pressure_seconds
        return "PRESSURE", pressure_seconds
    return "NORMAL", pressure_seconds


def update_event_and_dispatch_notifications(
    conn: sqlite3.Connection,
    device_id: str,
    status: str,
    fsr: float,
    temperature: float,
) -> None:
    """Updates the events table and creates database-deduplicated notifications.

    Deduplication Guarantee:
    - notifications table has UNIQUE(event_id, recipient_user_id, type)
    - device_state.last_notified_event_id prevents re-triggering during the same continuous alert episode.
    """
    event_type = status if status in ("PRESSURE", "HIGH_PRESSURE", "HIGH_RISK") else None
    state = conn.execute("SELECT * FROM device_state WHERE device_id = ?", (device_id,)).fetchone()
    if not state:
        conn.execute("INSERT INTO device_state (device_id) VALUES (?)", (device_id,))
        state = conn.execute("SELECT * FROM device_state WHERE device_id = ?", (device_id,)).fetchone()

    active_type = state["active_event_type"]
    active_id = state["active_event_id"]
    last_notified_id = state["last_notified_event_id"] or 0

    # Ensure device is known in devices table
    dev_row = conn.execute("SELECT patient_user_id, display_name FROM devices WHERE device_id = ?", (device_id,)).fetchone()
    if not dev_row:
        conn.execute("INSERT OR IGNORE INTO devices (device_id, display_name, active, created_at) VALUES (?, ?, 1, ?)", (device_id, device_id, now()))
        dev_row = conn.execute("SELECT patient_user_id, display_name FROM devices WHERE device_id = ?", (device_id,)).fetchone()

    patient_user_id = dev_row["patient_user_id"] if dev_row else None

    # Case 1: Same event continues
    if active_type == event_type and active_id:
        conn.execute(
            "UPDATE events SET peak_fsr = MAX(peak_fsr, ?), peak_temperature = MAX(peak_temperature, ?) WHERE id = ?",
            (fsr, temperature, active_id),
        )
    else:
        # Close previous event if type changed or pressure released
        if active_id:
            conn.execute("UPDATE events SET ended_at = ? WHERE id = ?", (now(), active_id))

        new_event_id = None
        if event_type:
            cursor = conn.execute(
                """INSERT INTO events (started_at, device_id, event_type, peak_fsr, peak_temperature)
                VALUES (?, ?, ?, ?, ?)""",
                (now(), device_id, event_type, fsr, temperature),
            )
            new_event_id = cursor.lastrowid

        # Update device state
        conn.execute(
            "UPDATE device_state SET active_event_id = ?, active_event_type = ? WHERE device_id = ?",
            (new_event_id, event_type, device_id),
        )
        active_id = new_event_id
        active_type = event_type

    # Reset last_notified_event_id if pressure returns to normal
    if not event_type:
        conn.execute("UPDATE device_state SET last_notified_event_id = 0 WHERE device_id = ?", (device_id,))
        return

    # Trigger notifications ONLY when entering HIGH_PRESSURE or HIGH_RISK, exactly once per event episode
    if active_type in ("HIGH_PRESSURE", "HIGH_RISK") and active_id and active_id != last_notified_id:
        recipients = []
        if patient_user_id:
            # 1. Assigned Patient
            pat = conn.execute("SELECT id, full_name, email FROM users WHERE id = ? AND active = 1", (patient_user_id,)).fetchone()
            if pat:
                recipients.append(dict(pat))
            # 2. All Caregivers assigned to this patient (M:N)
            cg_rows = conn.execute(
                """SELECT u.id, u.full_name, u.email
                FROM users u
                JOIN caregiver_patients cp ON cp.caregiver_user_id = u.id
                WHERE cp.patient_user_id = ? AND u.active = 1""",
                (patient_user_id,),
            ).fetchall()
            for cg in cg_rows:
                recipients.append(dict(cg))

        if recipients:
            title = f"STEP-SAFE Alert: {active_type.replace('_', ' ')}"
            device_label = dev_row["display_name"] if dev_row else device_id
            message = f"Sustained pressure alert ({active_type.replace('_', ' ')}) detected on {device_label}. Please check the STEP-SAFE dashboard."

            # Persist transactional in-app notifications
            for r in recipients:
                conn.execute(
                    """INSERT OR IGNORE INTO notifications
                    (event_id, recipient_user_id, type, title, message, status, sent_at)
                    VALUES (?, ?, ?, ?, ?, 'unread', ?)""",
                    (active_id, r["id"], active_type, title, message, now()),
                )

            # Mark event as notified in device_state
            conn.execute("UPDATE device_state SET last_notified_event_id = ? WHERE device_id = ?", (active_id, device_id))

            # Return pending push dispatch tuple to be executed after transaction commits
            return recipients, title, message, {"event_id": active_id, "type": active_type, "device_id": device_id}

    return None


# ---------------------------------------------------------------------------
# Web Presentation Routes (Login & Dashboard)
# ---------------------------------------------------------------------------

@app.get("/")
def dashboard():
    with db() as conn:
        user = current_user(conn)
    if not user:
        return render_template("index.html", user=None, seed_demo=os.environ.get("STEP_SAFE_SEED_DEMO") == "1")
    return render_template("index.html", user=user, seed_demo=os.environ.get("STEP_SAFE_SEED_DEMO") == "1")


@app.get("/auth/login")
def login_page():
    with db() as conn:
        user = current_user(conn)
    if user:
        return redirect(url_for("dashboard"))
    return render_template("index.html", user=None, view="login", seed_demo=os.environ.get("STEP_SAFE_SEED_DEMO") == "1")


@app.get("/login")
def legacy_login_redirect():
    return redirect("/auth/login")


# ---------------------------------------------------------------------------
# Authentication Endpoints (/auth/login, /auth/logout, /api/me)
# ---------------------------------------------------------------------------

@app.post("/auth/login")
def login():
    data = request.get_json(silent=True) or request.form or {}
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))

    if not email or not password:
        return jsonify(error="Email and password are required."), 400

    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not user or not check_password_hash(user["password_hash"], password):
            return jsonify(error="Invalid email or password.", code="INVALID_CREDENTIALS"), 401
        if not user["active"]:
            return jsonify(error="Account is deactivated. Contact an administrator.", code="DEACTIVATED"), 403

        session.clear()
        session["user_id"] = user["id"]
        session["role"] = user["role"]

        return jsonify(
            ok=True,
            user={
                "id": user["id"],
                "email": user["email"],
                "full_name": user["full_name"],
                "role": user["role"],
            },
        )


@app.post("/auth/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
@login_required
def get_me():
    with db() as conn:
        user = current_user(conn)
        if not user:
            return jsonify(error="User not found"), 404

        info: dict[str, Any] = {"user": user}

        if user["role"] == "patient":
            profile = conn.execute("SELECT * FROM patient_profiles WHERE user_id = ?", (user["id"],)).fetchone()
            device = get_patient_assigned_device(conn, user["id"])
            info["patient_profile"] = dict(profile) if profile else None
            info["assigned_device"] = device
        elif user["role"] == "caregiver":
            assigned_patients = conn.execute(
                """SELECT u.id, u.full_name, u.email, d.device_id, d.display_name
                FROM users u
                JOIN caregiver_patients cp ON cp.patient_user_id = u.id
                LEFT JOIN devices d ON d.patient_user_id = u.id AND d.active = 1
                WHERE cp.caregiver_user_id = ? AND u.active = 1""",
                (user["id"],),
            ).fetchall()
            info["assigned_patients"] = rows_as_dicts(assigned_patients)

        return jsonify(ok=True, **info)


@app.put("/api/me/profile")
@login_required
def update_my_profile():
    data = request.get_json(silent=True) or {}
    with db() as conn:
        user = current_user(conn)
        if not user:
            return jsonify(error="User not found"), 404

        full_name = data.get("full_name")
        if full_name:
            conn.execute("UPDATE users SET full_name = ? WHERE id = ?", (str(full_name).strip(), user["id"]))

        if user["role"] == "patient":
            dob = data.get("date_of_birth")
            contact = data.get("emergency_contact")
            prefs = data.get("alert_preferences")
            prefs_json = json.dumps(prefs) if isinstance(prefs, dict) else None

            existing = conn.execute("SELECT user_id FROM patient_profiles WHERE user_id = ?", (user["id"],)).fetchone()
            if existing:
                conn.execute(
                    """UPDATE patient_profiles
                    SET date_of_birth = COALESCE(?, date_of_birth),
                        emergency_contact = COALESCE(?, emergency_contact),
                        alert_preferences = COALESCE(?, alert_preferences)
                    WHERE user_id = ?""",
                    (dob, contact, prefs_json, user["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO patient_profiles (user_id, date_of_birth, emergency_contact, alert_preferences)
                    VALUES (?, ?, ?, ?)""",
                    (user["id"], dob, contact, prefs_json or '{"in_app":true,"push":true}'),
                )

    return jsonify(ok=True, message="Profile updated successfully.")


# ---------------------------------------------------------------------------
# Notification Endpoints (/api/notifications)
# ---------------------------------------------------------------------------

@app.get("/api/notifications")
@login_required
def get_notifications():
    with db() as conn:
        user = current_user(conn)
        limit = min(max(int(request.args.get("limit", 50)), 1), 200)
        rows = conn.execute(
            """SELECT n.*, e.device_id, e.peak_fsr, e.peak_temperature, e.started_at AS event_started_at
            FROM notifications n
            LEFT JOIN events e ON e.id = n.event_id
            WHERE n.recipient_user_id = ?
            ORDER BY n.id DESC LIMIT ?""",
            (user["id"], limit),
        ).fetchall()
        unread_count = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE recipient_user_id = ? AND status = 'unread'",
            (user["id"],),
        ).fetchone()[0]

    return jsonify(ok=True, notifications=rows_as_dicts(rows), unread_count=unread_count)


@app.post("/api/notifications/<int:notif_id>/read")
@login_required
def mark_notification_read(notif_id: int):
    with db() as conn:
        user = current_user(conn)
        conn.execute(
            "UPDATE notifications SET status = 'read', read_at = ? WHERE id = ? AND recipient_user_id = ?",
            (now(), notif_id, user["id"]),
        )
    return jsonify(ok=True)


@app.post("/api/notifications/read-all")
@login_required
def mark_all_notifications_read():
    with db() as conn:
        user = current_user(conn)
        conn.execute(
            "UPDATE notifications SET status = 'read', read_at = ? WHERE recipient_user_id = ? AND status = 'unread'",
            (now(), user["id"]),
        )
    return jsonify(ok=True)


@app.post("/api/notifications/subscribe")
@login_required
def register_push_token():
    """Register active FCM registration token for HTTP v1 push notifications."""
    data = request.get_json(silent=True) or {}
    token = str(data.get("token", "")).strip()
    platform = str(data.get("platform", "web")).strip().lower()

    if not token:
        return jsonify(error="Token is required."), 400

    with db() as conn:
        user = current_user(conn)
        conn.execute(
            """INSERT INTO push_subscriptions (user_id, token, platform, active, updated_at)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(token) DO UPDATE SET user_id = excluded.user_id, active = 1, updated_at = excluded.updated_at""",
            (user["id"], token, platform, now()),
        )
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Patient & Caregiver Scoped Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/patients")
@roles_accepted("caregiver", "admin")
def list_patients():
    with db() as conn:
        user = current_user(conn)
        if user["role"] == "admin":
            patients = conn.execute(
                """SELECT u.id, u.full_name, u.email, u.active, u.created_at,
                          p.date_of_birth, p.emergency_contact,
                          d.device_id, d.display_name AS device_name,
                          (SELECT COUNT(*) FROM caregiver_patients WHERE patient_user_id = u.id) AS caregiver_count
                FROM users u
                LEFT JOIN patient_profiles p ON p.user_id = u.id
                LEFT JOIN devices d ON d.patient_user_id = u.id AND d.active = 1
                WHERE u.role = 'patient'
                ORDER BY u.full_name ASC"""
            ).fetchall()
        else:  # caregiver
            patients = conn.execute(
                """SELECT u.id, u.full_name, u.email,
                          p.date_of_birth, p.emergency_contact,
                          d.device_id, d.display_name AS device_name,
                          s.active_event_type, s.pressure_started_at
                FROM users u
                JOIN caregiver_patients cp ON cp.patient_user_id = u.id
                LEFT JOIN patient_profiles p ON p.user_id = u.id
                LEFT JOIN devices d ON d.patient_user_id = u.id AND d.active = 1
                LEFT JOIN device_state s ON s.device_id = d.device_id
                WHERE cp.caregiver_user_id = ? AND u.active = 1
                ORDER BY u.full_name ASC""",
                (user["id"],),
            ).fetchall()

    return jsonify(ok=True, patients=rows_as_dicts(patients))


@app.get("/api/patients/<int:patient_id>/dashboard")
@login_required
def get_patient_dashboard(patient_id: int):
    with db() as conn:
        user = current_user(conn)
        if not check_patient_access(conn, user, patient_id):
            return jsonify(error="Access denied to this patient dashboard", code="FORBIDDEN"), 403

        pat = conn.execute("SELECT id, full_name, email FROM users WHERE id = ? AND role = 'patient'", (patient_id,)).fetchone()
        if not pat:
            return jsonify(error="Patient not found"), 404

        device = get_patient_assigned_device(conn, patient_id)
        device_id = device["device_id"] if device else None

        reading = None
        calibration = None
        session_row = None
        stats = {}
        events: list[dict[str, Any]] = []

        if device_id:
            reading = conn.execute("SELECT * FROM readings WHERE device_id = ? ORDER BY id DESC LIMIT 1", (device_id,)).fetchone()
            calibration = latest_calibration(conn, device_id)
            session_row = conn.execute("SELECT sample_count FROM calibration_sessions WHERE device_id = ?", (device_id,)).fetchone()
            stats_row = conn.execute(
                "SELECT COUNT(*) AS readings, ROUND(AVG(fsr), 1) AS avg_fsr, ROUND(AVG(temperature), 2) AS avg_temperature, SUM(status = 'HIGH_RISK') AS high_risk_readings FROM readings WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            stats = dict(stats_row) if stats_row else {}
            events = rows_as_dicts(conn.execute("SELECT * FROM events WHERE device_id = ? ORDER BY id DESC LIMIT 20", (device_id,)).fetchall())

        profile = conn.execute("SELECT * FROM patient_profiles WHERE user_id = ?", (patient_id,)).fetchone()

    return jsonify(
        ok=True,
        patient=dict(pat),
        profile=dict(profile) if profile else None,
        assigned_device=device,
        reading=dict(reading) if reading else None,
        calibration=dict(calibration) if calibration else None,
        calibration_running=bool(session_row),
        calibration_samples=session_row["sample_count"] if session_row else 0,
        stats=stats,
        recent_events=events,
    )


# ---------------------------------------------------------------------------
# Admin Management Endpoints (@roles_accepted('admin'))
# ---------------------------------------------------------------------------

@app.get("/api/admin/users")
@roles_accepted("admin")
def admin_list_users():
    with db() as conn:
        users = conn.execute(
            """SELECT u.id, u.full_name, u.email, u.role, u.active, u.created_at,
                      d.device_id AS assigned_device_id, d.display_name AS assigned_device_name
            FROM users u
            LEFT JOIN devices d ON d.patient_user_id = u.id AND d.active = 1
            ORDER BY u.id DESC"""
        ).fetchall()
    return jsonify(ok=True, users=rows_as_dicts(users))


@app.post("/api/admin/users")
@roles_accepted("admin")
def admin_create_user():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    full_name = str(data.get("full_name", "")).strip()
    password = str(data.get("password", ""))
    role = str(data.get("role", "patient")).strip().lower()

    if not email or not password or not full_name or role not in ("patient", "caregiver", "admin"):
        return jsonify(error="Valid email, full_name, password, and role (patient/caregiver/admin) required."), 400

    with db() as conn:
        existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing:
            return jsonify(error="A user with this email already exists."), 400

        cursor = conn.execute(
            "INSERT INTO users (full_name, email, password_hash, role, active, created_at) VALUES (?, ?, ?, ?, 1, ?)",
            (full_name, email, generate_password_hash(password), role, now()),
        )
        new_id = cursor.lastrowid
        if role == "patient":
            conn.execute("INSERT OR IGNORE INTO patient_profiles (user_id) VALUES (?)", (new_id,))

    return jsonify(ok=True, user_id=new_id, message="User created successfully.")


@app.post("/api/admin/users/<int:user_id>/toggle-active")
@roles_accepted("admin")
def admin_toggle_user_active(user_id: int):
    with db() as conn:
        user = conn.execute("SELECT active FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            return jsonify(error="User not found"), 404
        new_state = 0 if user["active"] else 1
        conn.execute("UPDATE users SET active = ? WHERE id = ?", (new_state, user_id))
    return jsonify(ok=True, active=bool(new_state))


@app.get("/api/admin/devices")
@roles_accepted("admin")
def admin_list_devices():
    with db() as conn:
        devices = conn.execute(
            """SELECT d.*, u.full_name AS patient_name, u.email AS patient_email
            FROM devices d
            LEFT JOIN users u ON u.id = d.patient_user_id
            ORDER BY d.id DESC"""
        ).fetchall()
    return jsonify(ok=True, devices=rows_as_dicts(devices))


@app.post("/api/admin/devices")
@roles_accepted("admin")
def admin_create_device():
    data = request.get_json(silent=True) or {}
    device_id = str(data.get("device_id", "")).strip()
    display_name = str(data.get("display_name", "")).strip() or device_id

    if not device_id:
        return jsonify(error="device_id is required."), 400

    with db() as conn:
        try:
            conn.execute(
                "INSERT INTO devices (device_id, display_name, active, created_at) VALUES (?, ?, 1, ?)",
                (device_id, display_name, now()),
            )
        except sqlite3.IntegrityError:
            return jsonify(error="Device already registered."), 400
    return jsonify(ok=True, message="Device registered successfully.")


@app.post("/api/admin/assign-device")
@roles_accepted("admin")
def admin_assign_device():
    """Single source of truth: updates devices.patient_user_id."""
    data = request.get_json(silent=True) or {}
    device_id = str(data.get("device_id", "")).strip()
    patient_user_id = data.get("patient_user_id")

    if not device_id:
        return jsonify(error="device_id is required."), 400

    with db() as conn:
        dev = conn.execute("SELECT id FROM devices WHERE device_id = ?", (device_id,)).fetchone()
        if not dev:
            return jsonify(error="Device not found."), 404

        if patient_user_id:
            pat = conn.execute("SELECT id FROM users WHERE id = ? AND role = 'patient'", (patient_user_id,)).fetchone()
            if not pat:
                return jsonify(error="Patient user not found."), 404
            # Clear previous device assignment for this patient
            conn.execute("UPDATE devices SET patient_user_id = NULL WHERE patient_user_id = ?", (patient_user_id,))
            conn.execute("UPDATE devices SET patient_user_id = ? WHERE device_id = ?", (patient_user_id, device_id))
        else:
            conn.execute("UPDATE devices SET patient_user_id = NULL WHERE device_id = ?", (device_id,))

    return jsonify(ok=True, message="Device assignment updated.")


@app.get("/api/admin/assignments")
@roles_accepted("admin")
def admin_list_assignments():
    with db() as conn:
        assignments = conn.execute(
            """SELECT cp.assigned_at,
                      cg.id AS caregiver_id, cg.full_name AS caregiver_name, cg.email AS caregiver_email,
                      pat.id AS patient_id, pat.full_name AS patient_name, pat.email AS patient_email
            FROM caregiver_patients cp
            JOIN users cg ON cg.id = cp.caregiver_user_id
            JOIN users pat ON pat.id = cp.patient_user_id
            ORDER BY cp.assigned_at DESC"""
        ).fetchall()
    return jsonify(ok=True, assignments=rows_as_dicts(assignments))


@app.post("/api/admin/assign-caregiver")
@roles_accepted("admin")
def admin_assign_caregiver():
    data = request.get_json(silent=True) or {}
    caregiver_id = data.get("caregiver_user_id")
    patient_id = data.get("patient_user_id")

    if not caregiver_id or not patient_id:
        return jsonify(error="caregiver_user_id and patient_user_id are required."), 400

    with db() as conn:
        cg = conn.execute("SELECT id FROM users WHERE id = ? AND role = 'caregiver'", (caregiver_id,)).fetchone()
        pat = conn.execute("SELECT id FROM users WHERE id = ? AND role = 'patient'", (patient_id,)).fetchone()
        if not cg or not pat:
            return jsonify(error="Caregiver or Patient user not found."), 404

        conn.execute(
            """INSERT OR IGNORE INTO caregiver_patients (caregiver_user_id, patient_user_id, assigned_at)
            VALUES (?, ?, ?)""",
            (caregiver_id, patient_id, now()),
        )
    return jsonify(ok=True, message="Caregiver assigned to patient.")


@app.post("/api/admin/unassign-caregiver")
@roles_accepted("admin")
def admin_unassign_caregiver():
    data = request.get_json(silent=True) or {}
    caregiver_id = data.get("caregiver_user_id")
    patient_id = data.get("patient_user_id")

    with db() as conn:
        conn.execute(
            "DELETE FROM caregiver_patients WHERE caregiver_user_id = ? AND patient_user_id = ?",
            (caregiver_id, patient_id),
        )
    return jsonify(ok=True, message="Caregiver unassigned from patient.")


# ---------------------------------------------------------------------------
# Preserved Core Sensor Telemetry APIs (ESP32 Contract)
# ---------------------------------------------------------------------------

@app.post("/api/readings")
def receive_reading():
    """Unauthenticated ingestion contract for ESP32 IoT telemetry."""
    data = request.get_json(silent=True) or {}
    try:
        device_id = str(data.get("device_id", "step-safe-esp32"))[:80]
        fsr, temperature = float(data["fsr"]), float(data["temperature"])
    except (KeyError, TypeError, ValueError):
        return jsonify(error="JSON must include numeric fsr and temperature."), 400
    if not (0 <= fsr <= 4095 and -20 <= temperature <= 100):
        return jsonify(error="Reading outside accepted demo range."), 400

    with db() as conn:
        # Check active calibration session
        session_row = conn.execute("SELECT * FROM calibration_sessions WHERE device_id = ?", (device_id,)).fetchone()
        if session_row:
            conn.execute(
                "UPDATE calibration_sessions SET fsr_total = fsr_total + ?, temp_total = temp_total + ?, sample_count = sample_count + 1 WHERE device_id = ?",
                (fsr, temperature, device_id),
            )
            finish_calibration_if_ready(conn, device_id)
            # CRITICAL: Samples 1 through 20 are calibration readings.
            # Sample 20 completes calibration, but is still recorded as CALIBRATING.
            # Normal pressure monitoring strictly resumes on reading #21.
            status = "CALIBRATING"
            pressure_seconds = 0
            conn.execute("UPDATE device_state SET pressure_started_at = NULL WHERE device_id = ?", (device_id,))
        else:
            status, pressure_seconds = status_for(conn, device_id, fsr, temperature)

        conn.execute(
            "INSERT INTO readings (recorded_at, device_id, fsr, temperature, status, pressure_seconds) VALUES (?, ?, ?, ?, ?, ?)",
            (now(), device_id, fsr, temperature, status, pressure_seconds),
        )
        pending_alert = update_event_and_dispatch_notifications(conn, device_id, status, fsr, temperature)

    # Dispatch external push notifications after SQLite write transaction has committed
    if pending_alert:
        recipients, title, message, alert_data = pending_alert
        try:
            notification_service.dispatch(recipients, title, message, data=alert_data)
        except Exception as exc:
            logger.error("Error during push notification dispatch: %s", exc)

    return jsonify(ok=True, status=status, pressure_seconds=pressure_seconds)


@app.get("/api/device-config")
def device_config():
    """Unauthenticated configuration polling contract for ESP32 firmware."""
    device_id = request.args.get("device_id", "step-safe-esp32")
    with db() as conn:
        calibration = latest_calibration(conn, device_id)
        session_row = conn.execute("SELECT sample_count FROM calibration_sessions WHERE device_id = ?", (device_id,)).fetchone()
    is_calibrating = bool(session_row)
    return jsonify(
        calibrated=bool(calibration) and not is_calibrating,
        fsr_threshold=calibration["fsr_threshold"] if (calibration and not is_calibrating) else 0,
        temp_threshold=calibration["temp_threshold"] if (calibration and not is_calibrating) else 0,
        calibration_running=is_calibrating,
        calibration_samples=session_row["sample_count"] if session_row else 0,
        high_risk_after_seconds=PRESSURE_SECONDS_FOR_HIGH_RISK,
    )


@app.get("/api/live")
def live():
    """Returns live telemetry, enforcing patient-scoping if logged in."""
    device_id = request.args.get("device_id", "step-safe-esp32")
    with db() as conn:
        user = current_user(conn)
        if user and user["role"] == "patient":
            dev = get_patient_assigned_device(conn, user["id"])
            if dev and dev["device_id"] != device_id:
                return jsonify(error="Access denied to other devices", code="FORBIDDEN"), 403

        reading = conn.execute("SELECT * FROM readings WHERE device_id = ? ORDER BY id DESC LIMIT 1", (device_id,)).fetchone()
        calibration = latest_calibration(conn, device_id)
        session_row = conn.execute("SELECT sample_count FROM calibration_sessions WHERE device_id = ?", (device_id,)).fetchone()
        state = conn.execute("SELECT * FROM device_state WHERE device_id = ?", (device_id,)).fetchone()

    is_calibrating = bool(session_row)
    reading_dict = dict(reading) if reading else None
    pressure_started_at = None

    if is_calibrating:
        if reading_dict:
            reading_dict["status"] = "CALIBRATING"
            reading_dict["pressure_seconds"] = 0
    elif state and state["pressure_started_at"] and reading_dict and reading_dict.get("status") in ("PRESSURE", "HIGH_PRESSURE", "HIGH_RISK"):
        pressure_started_at = state["pressure_started_at"]
        started = datetime.fromisoformat(pressure_started_at)
        live_elapsed = max(0, int((datetime.now(timezone.utc) - started).total_seconds()))
        reading_dict["pressure_seconds"] = min(30, live_elapsed)
        if live_elapsed >= PRESSURE_SECONDS_FOR_HIGH_RISK and calibration:
            if reading_dict.get("temperature", 0) >= calibration["temp_threshold"]:
                reading_dict["status"] = "HIGH_RISK"
            else:
                reading_dict["status"] = "HIGH_PRESSURE"

    return jsonify(
        reading=reading_dict,
        calibration=dict(calibration) if calibration else None,
        calibration_running=is_calibrating,
        calibration_samples=session_row["sample_count"] if session_row else 0,
        pressure_started_at=pressure_started_at,
    )


@app.get("/api/history")
def history():
    device_id = request.args.get("device_id", "step-safe-esp32")
    limit = min(max(int(request.args.get("limit", 300)), 1), 1000)
    with db() as conn:
        user = current_user(conn)
        if user and user["role"] == "patient":
            dev = get_patient_assigned_device(conn, user["id"])
            if dev and dev["device_id"] != device_id:
                return jsonify(error="Access denied to other devices", code="FORBIDDEN"), 403

        readings = conn.execute(
            "SELECT recorded_at, fsr, temperature, status, pressure_seconds FROM readings WHERE device_id = ? ORDER BY id DESC LIMIT ?",
            (device_id, limit),
        ).fetchall()
        events = conn.execute("SELECT * FROM events WHERE device_id = ? ORDER BY id DESC LIMIT 50", (device_id,)).fetchall()
    return jsonify(readings=list(reversed(rows_as_dicts(readings))), events=rows_as_dicts(events))


@app.get("/api/summary")
def summary():
    device_id = request.args.get("device_id", "step-safe-esp32")
    with db() as conn:
        stats = conn.execute(
            "SELECT COUNT(*) AS readings, ROUND(AVG(fsr), 1) AS avg_fsr, ROUND(AVG(temperature), 2) AS avg_temperature, SUM(status = 'HIGH_RISK') AS high_risk_readings FROM readings WHERE device_id = ?",
            (device_id,),
        ).fetchone()
        events = conn.execute("SELECT COUNT(*) FROM events WHERE device_id = ?", (device_id,)).fetchone()[0]
    return jsonify(**dict(stats), event_count=events)


@app.post("/api/calibrate")
@roles_accepted("patient")
def begin_calibration():
    with db() as conn:
        user = current_user(conn)
        if not user:
            return jsonify(error="Authentication required", code="UNAUTHORIZED"), 401

        payload = request.get_json(silent=True) or {}
        device_id = payload.get("device_id")

        # Patient can only calibrate their own assigned device
        assigned_device = get_patient_assigned_device(conn, user["id"])
        if not assigned_device:
            return jsonify(error="No active device assigned to your patient profile.", code="NO_DEVICE"), 400

        if device_id and device_id != assigned_device["device_id"]:
            return jsonify(error="Access denied: You can only calibrate your own assigned device.", code="FORBIDDEN"), 403

        target_device_id = assigned_device["device_id"]
        conn.execute(
            "INSERT OR REPLACE INTO calibration_sessions (device_id, started_at, fsr_total, temp_total, sample_count) VALUES (?, ?, 0, 0, 0)",
            (target_device_id, now()),
        )
        # Reset pressure timer state and close any open event without notifications
        state = conn.execute("SELECT * FROM device_state WHERE device_id = ?", (target_device_id,)).fetchone()
        if state and state["active_event_id"]:
            conn.execute("UPDATE events SET ended_at = ? WHERE id = ?", (now(), state["active_event_id"]))
        conn.execute(
            "UPDATE device_state SET pressure_started_at = NULL, active_event_id = NULL, active_event_type = NULL, last_notified_event_id = 0 WHERE device_id = ?",
            (target_device_id,),
        )
    return jsonify(ok=True, device_id=target_device_id, message=f"Collecting {CALIBRATION_SAMPLES} readings. Keep sensors in baseline condition.")


if __name__ == "__main__":
    initialise_database()
    app.run(host="0.0.0.0", port=5000, debug=True)
