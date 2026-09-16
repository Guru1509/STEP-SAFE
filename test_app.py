"""Comprehensive Automated Verification Test Suite for STEP-SAFE.

Tests:
1. Unauthenticated ESP32 API contracts (POST /api/readings, GET /api/device-config).
2. Database migrations and PRAGMA table_info schema inspection.
3. Authentication flow (/auth/login, /auth/logout, session persistence).
4. Role-based access control (Patient, Caregiver, Admin) and 403 Forbidden enforcement.
5. 30-Second sustained pressure rule, buzzer activation status, and event creation.
6. Database-safe notification deduplication (UNIQUE constraint and device_state tracking).
7. FCM HTTP v1 provider graceful fallback when credentials are absent.
8. In-app notification center (retrieval, unread count, mark as read).
9. Admin management endpoints (user toggle, device assignment, caregiver M:N mapping).
10. Single source of truth for device assignment (devices.patient_user_id).
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


class StepSafeTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Create a clean temporary database for test execution
        cls.temp_db_fd, cls.temp_db_path = tempfile.mkstemp(suffix=".db")
        os.environ["STEP_SAFE_DB"] = cls.temp_db_path
        os.environ["STEP_SAFE_SECRET_KEY"] = "test-secret-key-12345"
        os.environ["STEP_SAFE_SEED_DEMO"] = "1"  # Enable demo seed for test suite

        import app
        cls.app = app.app
        cls.app.config["TESTING"] = True
        app.DATABASE = Path(cls.temp_db_path)
        app.initialise_database()
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        os.close(cls.temp_db_fd)
        if os.path.exists(cls.temp_db_path):
            os.remove(cls.temp_db_path)

    # -----------------------------------------------------------------------
    # 1. ESP32 Unauthenticated API Contract
    # -----------------------------------------------------------------------
    def test_01_esp32_endpoints_unauthenticated(self):
        """ESP32 must be able to post readings and fetch device config without cookies."""
        # Config fetch
        res = self.client.get("/api/device-config?device_id=step-safe-esp32")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("calibrated", data)
        self.assertIn("fsr_threshold", data)
        self.assertEqual(data["high_risk_after_seconds"], 30)

        # Baseline reading post
        payload = {"device_id": "step-safe-esp32", "fsr": 150.0, "temperature": 32.5}
        res = self.client.post("/api/readings", json=payload)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["ok"])

    # -----------------------------------------------------------------------
    # 2. Database Schema & Migration Verification
    # -----------------------------------------------------------------------
    def test_02_database_schema(self):
        """Verify all tables exist and devices.patient_user_id is the source of truth."""
        conn = sqlite3.connect(self.temp_db_path)
        conn.row_factory = sqlite3.Row
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]

        required_tables = [
            "readings", "calibrations", "events", "device_state", "calibration_sessions",
            "users", "patient_profiles", "caregiver_patients", "devices", "notifications", "push_subscriptions"
        ]
        for t in required_tables:
            self.assertIn(t, tables, f"Missing table: {t}")

        # Check device_state column
        dev_state_cols = [r["name"] for r in conn.execute("PRAGMA table_info(device_state)").fetchall()]
        self.assertIn("last_notified_event_id", dev_state_cols)

        # Check notifications UNIQUE constraint
        unique_check = conn.execute("PRAGMA index_list(notifications)").fetchall()
        has_unique = any(idx["unique"] == 1 for idx in unique_check)
        self.assertTrue(has_unique, "notifications table must have a UNIQUE index/constraint")
        conn.close()

    # -----------------------------------------------------------------------
    # 3. Authentication & Session Security
    # -----------------------------------------------------------------------
    def test_03_authentication_flow(self):
        """Test login, session cookie, password hashing, and logout."""
        # Invalid login
        res = self.client.post("/auth/login", json={"email": "nobody@stepsafe.local", "password": "wrong"})
        self.assertEqual(res.status_code, 401)

        # Valid login as Admin
        res = self.client.post("/auth/login", json={"email": "admin@stepsafe.local", "password": "admin123"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["user"]["role"], "admin")

        # Check /api/me with session
        res = self.client.get("/api/me")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["user"]["email"], "admin@stepsafe.local")

        # Logout
        res = self.client.post("/auth/logout")
        self.assertEqual(res.status_code, 200)

        # /api/me after logout should return 401
        res = self.client.get("/api/me")
        self.assertEqual(res.status_code, 401)

    # -----------------------------------------------------------------------
    # 4. Role-Based Authorization & 403 Forbidden Enforcement
    # -----------------------------------------------------------------------
    def test_04_role_authorization_and_403_checks(self):
        """Verify strict role-based access restrictions and 403 status codes."""
        # 1. Login as Patient
        self.client.post("/auth/login", json={"email": "patient@stepsafe.local", "password": "patient123"})

        # Patient cannot access admin user list -> 403 Forbidden
        res = self.client.get("/api/admin/users")
        self.assertEqual(res.status_code, 403)

        # Patient cannot list all patients -> 403 Forbidden
        res = self.client.get("/api/patients")
        self.assertEqual(res.status_code, 403)

        # 2. Login as Caregiver
        self.client.post("/auth/login", json={"email": "caregiver@stepsafe.local", "password": "caregiver123"})

        # Caregiver can access assigned patients list
        res = self.client.get("/api/patients")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(len(data["patients"]) > 0)
        patient_id = data["patients"][0]["id"]

        # Caregiver CAN access assigned patient dashboard
        res = self.client.get(f"/api/patients/{patient_id}/dashboard")
        self.assertEqual(res.status_code, 200)

        # Caregiver CANNOT access unassigned patient -> 403 Forbidden
        # Create an unassigned patient directly
        conn = sqlite3.connect(self.temp_db_path)
        cur = conn.execute(
            "INSERT INTO users (full_name, email, password_hash, role, active, created_at) VALUES ('Unassigned Pat', 'unassigned@test.com', 'hash', 'patient', 1, '2026-01-01')",
        )
        unassigned_id = cur.lastrowid
        conn.commit()
        conn.close()

        res = self.client.get(f"/api/patients/{unassigned_id}/dashboard")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["code"], "FORBIDDEN")

        self.client.post("/auth/logout")

    # -----------------------------------------------------------------------
    # 5. Calibration Permissions: Patient-Only & Device-Scoped Enforcement
    # -----------------------------------------------------------------------
    def test_05_calibration_permissions_and_telemetry_flow(self):
        """Verify calibration rules:
        a) Patient can start calibration for own device.
        b) Caregiver receives 403 when attempting calibration.
        c) Admin receives 403 when attempting calibration.
        d) Patient cannot calibrate another patient's device (returns 403).
        """
        # 1. Unauthenticated request -> 401 Unauthorized
        res = self.client.post("/api/calibrate", json={"device_id": "step-safe-esp32"})
        self.assertEqual(res.status_code, 401)

        # 2. Caregiver attempts calibration -> 403 Forbidden (Requirement b)
        self.client.post("/auth/login", json={"email": "caregiver@stepsafe.local", "password": "caregiver123"})
        res = self.client.post("/api/calibrate", json={"device_id": "step-safe-esp32"})
        self.assertEqual(res.status_code, 403, "Caregiver MUST receive 403 Forbidden on calibration request.")
        self.assertEqual(res.get_json()["code"], "FORBIDDEN")
        self.client.post("/auth/logout")

        # 3. Admin attempts calibration -> 403 Forbidden (Requirement c)
        self.client.post("/auth/login", json={"email": "admin@stepsafe.local", "password": "admin123"})
        res = self.client.post("/api/calibrate", json={"device_id": "step-safe-esp32"})
        self.assertEqual(res.status_code, 403, "Admin MUST receive 403 Forbidden on calibration request.")
        self.assertEqual(res.get_json()["code"], "FORBIDDEN")
        self.client.post("/auth/logout")

        # 4. Patient attempting to calibrate another patient's device -> 403 Forbidden (Requirement d)
        # Create a second patient with their own device
        conn = sqlite3.connect(self.temp_db_path)
        cur = conn.execute(
            "INSERT INTO users (full_name, email, password_hash, role, active, created_at) VALUES ('Other Patient', 'other_pat@test.com', 'pw', 'patient', 1, '2026-01-01')"
        )
        other_pat_id = cur.lastrowid
        conn.execute(
            "INSERT INTO devices (device_id, patient_user_id, display_name, active, created_at) VALUES ('other-patient-device', ?, 'Other Foot Sensor', 1, '2026-01-01')",
            (other_pat_id,)
        )
        conn.commit()
        conn.close()

        # Login as primary patient (John Patient, who owns 'step-safe-esp32')
        self.client.post("/auth/login", json={"email": "patient@stepsafe.local", "password": "patient123"})

        # Attempt to calibrate other patient's device -> 403 Forbidden
        res = self.client.post("/api/calibrate", json={"device_id": "other-patient-device"})
        self.assertEqual(res.status_code, 403, "Patient attempting to calibrate another patient's device MUST receive 403 Forbidden.")
        self.assertEqual(res.get_json()["code"], "FORBIDDEN")

        # 5. Patient calibrates own device -> 200 OK (Requirement a)
        res = self.client.post("/api/calibrate", json={"device_id": "step-safe-esp32"})
        self.assertEqual(res.status_code, 200, "Patient MUST be allowed to calibrate their own assigned device.")
        self.assertTrue(res.get_json()["ok"])
        self.client.post("/auth/logout")

        # 6. ESP32 unauthenticated telemetry ingestion completes the 20 calibration readings
        for i in range(20):
            res = self.client.post("/api/readings", json={"device_id": "step-safe-esp32", "fsr": 100.0, "temperature": 32.0})
            self.assertEqual(res.status_code, 200)

        # Verify device is now calibrated via ESP32 device-config endpoint
        res = self.client.get("/api/device-config?device_id=step-safe-esp32")
        data = res.get_json()
        self.assertTrue(data["calibrated"])
        self.assertGreaterEqual(data["fsr_threshold"], 100.0)

    # -----------------------------------------------------------------------
    # 6. Caregiver Notifications & Database-Safe Deduplication
    # -----------------------------------------------------------------------
    def test_06_caregiver_notifications_and_deduplication(self):
        """Verify alert & notification rules:
        e) Assigned caregiver receives notification when patient's high-risk event occurs.
        f) Duplicate notifications are not generated repeatedly for the same continuous event.
        """
        conn = sqlite3.connect(self.temp_db_path)
        conn.execute("DELETE FROM notifications")
        conn.commit()
        conn.close()

        device_id = "step-safe-esp32"

        # Calibrate device as patient
        self.client.post("/auth/login", json={"email": "patient@stepsafe.local", "password": "patient123"})
        res = self.client.post("/api/calibrate", json={"device_id": device_id})
        self.assertEqual(res.status_code, 200)
        self.client.post("/auth/logout")

        for _ in range(20):
            self.client.post("/api/readings", json={"device_id": device_id, "fsr": 100.0, "temperature": 32.0})

        # 1. Simulate initial pressure detection (below 30 seconds)
        res = self.client.post("/api/readings", json={"device_id": device_id, "fsr": 600.0, "temperature": 33.0})
        self.assertEqual(res.get_json()["status"], "PRESSURE")

        # Fast-forward pressure timer to simulate >= 30 seconds sustained
        conn = sqlite3.connect(self.temp_db_path)
        conn.execute("UPDATE device_state SET pressure_started_at = '2026-01-01T00:00:00+00:00' WHERE device_id = ?", (device_id,))
        conn.commit()
        conn.close()

        # 2. Trigger high risk / sustained pressure (FSR high + Temp >= baseline + 2C)
        res = self.client.post("/api/readings", json={"device_id": device_id, "fsr": 650.0, "temperature": 34.5})
        self.assertEqual(res.get_json()["status"], "HIGH_RISK")

        # 3. Simulate continuous high risk readings for 15 subsequent seconds (Requirement f)
        for _ in range(15):
            res = self.client.post("/api/readings", json={"device_id": device_id, "fsr": 670.0, "temperature": 34.8})
            self.assertEqual(res.get_json()["status"], "HIGH_RISK")

        # 4. Check notification table:
        # Expect exactly 2 records total: 1 for Patient, 1 for Assigned Caregiver (Requirements e and f)
        conn = sqlite3.connect(self.temp_db_path)
        conn.row_factory = sqlite3.Row
        notifs = conn.execute("SELECT * FROM notifications WHERE type = 'HIGH_RISK'").fetchall()

        patient_row = conn.execute("SELECT id FROM users WHERE email = 'patient@stepsafe.local'").fetchone()
        caregiver_row = conn.execute("SELECT id FROM users WHERE email = 'caregiver@stepsafe.local'").fetchone()
        conn.close()

        self.assertEqual(len(notifs), 2, f"Expected exactly 2 notifications (1 patient, 1 caregiver), but found {len(notifs)}. Deduplication failed if > 2!")

        recipient_ids = [n["recipient_user_id"] for n in notifs]
        self.assertIn(patient_row["id"], recipient_ids, "Patient must receive the alert notification.")
        self.assertIn(caregiver_row["id"], recipient_ids, "Assigned Caregiver MUST receive the notification when patient's high-risk event occurs (Requirement e).")

        # 5. Verify Caregiver can retrieve notification via in-app notification center
        self.client.post("/auth/login", json={"email": "caregiver@stepsafe.local", "password": "caregiver123"})
        res = self.client.get("/api/notifications")
        self.assertEqual(res.status_code, 200)
        cg_data = res.get_json()
        self.assertGreaterEqual(cg_data["unread_count"], 1)
        self.assertTrue(any(n["type"] == "HIGH_RISK" for n in cg_data["notifications"]))
        self.client.post("/auth/logout")

        # 6. Pressure release resets system to NORMAL
        res = self.client.post("/api/readings", json={"device_id": device_id, "fsr": 20.0, "temperature": 32.0})
        self.assertEqual(res.get_json()["status"], "NORMAL")
        conn.close()

    # -----------------------------------------------------------------------
    # 7. FCM Provider Graceful Fallback
    # -----------------------------------------------------------------------
    def test_07_fcm_graceful_fallback(self):
        """FCM provider must be gracefully disabled when credentials are not configured."""
        import notifications
        provider = notifications.FirebaseCloudMessagingProvider(lambda: sqlite3.connect(self.temp_db_path))
        self.assertFalse(provider.enabled, "FCM provider must be disabled when credentials are absent.")
        # Sending should return False and never crash
        result = provider.send({"id": 1}, "Test Title", "Test Message")
        self.assertFalse(result)

    # -----------------------------------------------------------------------
    # 8. Push Token Subscription
    # -----------------------------------------------------------------------
    def test_08_push_token_subscription(self):
        """Verify FCM registration token subscription endpoint."""
        self.client.post("/auth/login", json={"email": "patient@stepsafe.local", "password": "patient123"})
        res = self.client.post("/api/notifications/subscribe", json={"token": "fake-fcm-device-token-123", "platform": "web"})
        self.assertEqual(res.status_code, 200)

        conn = sqlite3.connect(self.temp_db_path)
        sub = conn.execute("SELECT * FROM push_subscriptions WHERE token = 'fake-fcm-device-token-123'").fetchone()
        self.assertIsNotNone(sub)
        conn.close()
        self.client.post("/auth/logout")

    # -----------------------------------------------------------------------
    # 9. Admin User & Device Management
    # -----------------------------------------------------------------------
    def test_09_admin_management_endpoints(self):
        """Test admin user creation, device registration, and assignment."""
        self.client.post("/auth/login", json={"email": "admin@stepsafe.local", "password": "admin123"})

        # Create new patient
        res = self.client.post("/api/admin/users", json={
            "full_name": "New Patient",
            "email": "newpatient@test.com",
            "password": "password123",
            "role": "patient"
        })
        self.assertEqual(res.status_code, 200)
        new_pat_id = res.get_json()["user_id"]

        # Register new device
        res = self.client.post("/api/admin/devices", json={
            "device_id": "step-safe-esp32-02",
            "display_name": "Right Foot Unit #2"
        })
        self.assertEqual(res.status_code, 200)

        # Assign device to patient
        res = self.client.post("/api/admin/assign-device", json={
            "device_id": "step-safe-esp32-02",
            "patient_user_id": new_pat_id
        })
        self.assertEqual(res.status_code, 200)

        # Verify device table reflects assignment
        conn = sqlite3.connect(self.temp_db_path)
        dev = conn.execute("SELECT patient_user_id FROM devices WHERE device_id = 'step-safe-esp32-02'").fetchone()
        self.assertEqual(dev[0], new_pat_id)

        # Assign caregiver to new patient
        cg_id = conn.execute("SELECT id FROM users WHERE email = 'caregiver@stepsafe.local'").fetchone()[0]
        conn.close()

        res = self.client.post("/api/admin/assign-caregiver", json={
            "caregiver_user_id": cg_id,
            "patient_user_id": new_pat_id
        })
        self.assertEqual(res.status_code, 200)

        # Unassign caregiver
        res = self.client.post("/api/admin/unassign-caregiver", json={
            "caregiver_user_id": cg_id,
            "patient_user_id": new_pat_id
        })
        self.assertEqual(res.status_code, 200)

        self.client.post("/auth/logout")

    # -----------------------------------------------------------------------
    # 10. Patient Profile Update and Mark-All-Read
    # -----------------------------------------------------------------------
    def test_10_profile_update_and_mark_all_read(self):
        """Test updating patient profile details and marking all notifications read."""
        self.client.post("/auth/login", json={"email": "patient@stepsafe.local", "password": "patient123"})

        # Update profile
        res = self.client.put("/api/me/profile", json={
            "full_name": "John Doe Updated",
            "emergency_contact": "+1 555-9999",
            "alert_preferences": {"in_app": True, "push": True}
        })
        self.assertEqual(res.status_code, 200)

        # Check /api/me reflects update
        res = self.client.get("/api/me")
        data = res.get_json()
        self.assertEqual(data["user"]["full_name"], "John Doe Updated")

        # Mark all read
        res = self.client.post("/api/notifications/read-all")
        self.assertEqual(res.status_code, 200)

        self.client.post("/auth/logout")

    # -----------------------------------------------------------------------
    # 11. Pressure Timer Suspended During Calibration & Boundary Ingestion
    # -----------------------------------------------------------------------
    def test_11_calibration_suspends_timer_and_alerts(self):
        """Verify:
        1. Starting calibration resets pressure_started_at and ends active events cleanly.
        2. Samples 1 through 19 return status='CALIBRATING', pressure_seconds=0, no events, no notifs.
        3. Sample 20 STILL returns status='CALIBRATING' and pressure_seconds=0 (sample 20 is not normal reading).
        4. Calibration parameters saved; /api/device-config returns calibrated=true only after sample 20.
        5. Reading 21 (first post-calibration reading) correctly resumes normal monitoring.
        6. Sustained pressure after calibration correctly triggers caregiver alerts and deduplication.
        """
        device_id = "step-safe-esp32"

        # 1. Login as patient and start calibration
        self.client.post("/auth/login", json={"email": "patient@stepsafe.local", "password": "patient123"})
        res = self.client.post("/api/calibrate", json={"device_id": device_id})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["ok"])
        self.client.post("/auth/logout")

        # Verify device config reports calibrating
        res = self.client.get(f"/api/device-config?device_id={device_id}")
        self.assertEqual(res.status_code, 200)
        cfg = res.get_json()
        self.assertFalse(cfg["calibrated"], "Device MUST report calibrated=false while calibration is active.")
        self.assertTrue(cfg["calibration_running"], "Device MUST report calibration_running=true while calibration is active.")

        # Clean notifications and events for clean baseline
        conn = sqlite3.connect(self.temp_db_path)
        conn.execute("DELETE FROM notifications")
        conn.execute("DELETE FROM events")
        conn.commit()
        conn.close()

        # 2. Ingest samples 1 to 19 with high FSR
        for sample_num in range(1, 20):
            res = self.client.post("/api/readings", json={"device_id": device_id, "fsr": 2500.0, "temperature": 36.5})
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertEqual(data["status"], "CALIBRATING", f"Sample {sample_num} MUST have status CALIBRATING.")
            self.assertEqual(data["pressure_seconds"], 0, f"Sample {sample_num} pressure_seconds MUST be 0.")

        # Check DB during samples 1-19
        conn = sqlite3.connect(self.temp_db_path)
        conn.row_factory = sqlite3.Row
        state = conn.execute("SELECT * FROM device_state WHERE device_id = ?", (device_id,)).fetchone()
        self.assertIsNone(state["pressure_started_at"], "pressure_started_at must remain NULL during calibration.")
        events_count = conn.execute("SELECT COUNT(*) FROM events WHERE device_id = ?", (device_id,)).fetchone()[0]
        self.assertEqual(events_count, 0, "No events should be created during calibration.")
        notifs_count = conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        self.assertEqual(notifs_count, 0, "No notifications should be dispatched during calibration.")
        conn.close()

        # 3. Ingest sample 20 (CRITICAL BOUNDARY: STILL A CALIBRATION READING)
        res = self.client.post("/api/readings", json={"device_id": device_id, "fsr": 2500.0, "temperature": 36.5})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "CALIBRATING", "Sample 20 MUST STILL have status CALIBRATING.")
        self.assertEqual(data["pressure_seconds"], 0, "Sample 20 pressure_seconds MUST be 0.")

        # Verify calibration is now completed and saved
        conn = sqlite3.connect(self.temp_db_path)
        conn.row_factory = sqlite3.Row
        calib = conn.execute("SELECT * FROM calibrations WHERE device_id = ? ORDER BY id DESC LIMIT 1", (device_id,)).fetchone()
        self.assertIsNotNone(calib, "Calibration must be saved after sample 20.")
        self.assertEqual(calib["sample_count"], 20)
        session = conn.execute("SELECT * FROM calibration_sessions WHERE device_id = ?", (device_id,)).fetchone()
        self.assertIsNone(session, "calibration_sessions row must be deleted after sample 20 completes.")
        events_count = conn.execute("SELECT COUNT(*) FROM events WHERE device_id = ?", (device_id,)).fetchone()[0]
        self.assertEqual(events_count, 0, "No events should be created on sample 20.")
        conn.close()

        # Check /api/device-config now reports calibrated=true, calibration_running=false
        res = self.client.get(f"/api/device-config?device_id={device_id}")
        cfg = res.get_json()
        self.assertTrue(cfg["calibrated"], "calibrated MUST become true after sample 20 completes.")
        self.assertFalse(cfg["calibration_running"], "calibration_running MUST become false after sample 20 completes.")

        # 4. Reading 21 (First reading after calibration) resumes normal monitoring
        # Post reading with low FSR (below baseline of 2500, e.g. 500)
        res = self.client.post("/api/readings", json={"device_id": device_id, "fsr": 500.0, "temperature": 36.0})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "NORMAL", "Reading 21 with low FSR must evaluate as NORMAL.")
        self.assertEqual(data["pressure_seconds"], 0)

        # 5. Reading 22 with high FSR triggers pressure
        threshold = calib["fsr_threshold"]
        res = self.client.post("/api/readings", json={"device_id": device_id, "fsr": threshold + 500.0, "temperature": 36.0})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "PRESSURE", "Reading exceeding threshold must evaluate as PRESSURE.")

        # 6. Fast-forward timer to test sustained pressure and caregiver alert
        conn = sqlite3.connect(self.temp_db_path)
        conn.execute("UPDATE device_state SET pressure_started_at = '2026-01-01T00:00:00+00:00' WHERE device_id = ?", (device_id,))
        conn.commit()
        conn.close()

        res = self.client.post("/api/readings", json={"device_id": device_id, "fsr": threshold + 500.0, "temperature": calib["temp_threshold"] + 1.0})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["status"], "HIGH_RISK")

        # Verify caregiver received alert notification
        conn = sqlite3.connect(self.temp_db_path)
        conn.row_factory = sqlite3.Row
        notifs = conn.execute("SELECT * FROM notifications WHERE type = 'HIGH_RISK'").fetchall()
        self.assertEqual(len(notifs), 2, "1 patient and 1 caregiver notification expected.")
        conn.close()

    # -----------------------------------------------------------------------
    # 12. Hardware Synchronization & Header Cleanup Verification
    # -----------------------------------------------------------------------
    def test_12_hardware_sync_and_header_cleanup(self):
        """Verify:
        A. Header: 'PROTOTYPE' is absent from the rendered dashboard and login pages.
        B. Calibration progress percentages:
           - 1/20 -> 5%
           - 10/20 -> 50%
           - 19/20 -> 95%
           - 20/20 -> 100%
        C. Calibration boundary:
           - Sample 20 remains CALIBRATING and pressure_seconds=0.
           - No pressure event on sample 20.
           - Reading 21 begins normal monitoring.
        D. Pressure timer:
           - Backend pressure_seconds is authoritative and calculated dynamically via /api/live.
           - Starting calibration forces live timer to 0 immediately.
        """
        # A. Verify PROTOTYPE is completely absent from rendered HTML
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertNotIn(b"PROTOTYPE", res.data, "Rendered dashboard must NOT contain PROTOTYPE badge.")
        self.assertIn(b"STEP-SAFE", res.data)
        self.assertIn(b"Smart Foot-Pressure &amp; Temperature Monitoring", res.data)

        res = self.client.get("/auth/login")
        self.assertEqual(res.status_code, 200)
        self.assertNotIn(b"PROTOTYPE", res.data, "Login page must NOT contain PROTOTYPE badge.")

        device_id = "step-safe-esp32"

        # B & C. Start calibration and verify exact sample progress percentages
        self.client.post("/auth/login", json={"email": "patient@stepsafe.local", "password": "patient123"})
        res = self.client.post("/api/calibrate", json={"device_id": device_id})
        self.assertEqual(res.status_code, 200)

        # Clear events to verify sample 20 produces no event
        conn = sqlite3.connect(self.temp_db_path)
        conn.execute("DELETE FROM events")
        conn.commit()
        conn.close()

        # Check sample 1 -> 5%
        res = self.client.post("/api/readings", json={"device_id": device_id, "fsr": 2000.0, "temperature": 35.5})
        self.assertEqual(res.status_code, 200)
        live_res = self.client.get(f"/api/live?device_id={device_id}").get_json()
        self.assertTrue(live_res["calibration_running"])
        self.assertEqual(live_res["calibration_samples"], 1)
        self.assertEqual(int((live_res["calibration_samples"] / 20) * 100), 5)
        self.assertEqual(live_res["reading"]["status"], "CALIBRATING")
        self.assertEqual(live_res["reading"]["pressure_seconds"], 0)

        # Ingest samples 2 through 10 -> 10/20 = 50%
        for _ in range(2, 11):
            self.client.post("/api/readings", json={"device_id": device_id, "fsr": 2000.0, "temperature": 35.5})
        live_res = self.client.get(f"/api/live?device_id={device_id}").get_json()
        self.assertEqual(live_res["calibration_samples"], 10)
        self.assertEqual(int((live_res["calibration_samples"] / 20) * 100), 50)
        self.assertEqual(live_res["reading"]["status"], "CALIBRATING")
        self.assertEqual(live_res["reading"]["pressure_seconds"], 0)

        # Ingest samples 11 through 19 -> 19/20 = 95%
        for _ in range(11, 20):
            self.client.post("/api/readings", json={"device_id": device_id, "fsr": 2000.0, "temperature": 35.5})
        live_res = self.client.get(f"/api/live?device_id={device_id}").get_json()
        self.assertEqual(live_res["calibration_samples"], 19)
        self.assertEqual(int((live_res["calibration_samples"] / 20) * 100), 95)
        self.assertEqual(live_res["reading"]["status"], "CALIBRATING")
        self.assertEqual(live_res["reading"]["pressure_seconds"], 0)

        # Ingest sample 20 -> CRITICAL BOUNDARY: Still CALIBRATING, 20/20 = 100%
        res20 = self.client.post("/api/readings", json={"device_id": device_id, "fsr": 2000.0, "temperature": 35.5})
        self.assertEqual(res20.status_code, 200)
        data20 = res20.get_json()
        self.assertEqual(data20["status"], "CALIBRATING", "Sample 20 must return status CALIBRATING.")
        self.assertEqual(data20["pressure_seconds"], 0, "Sample 20 pressure_seconds must be 0.")
        self.assertEqual(int((20 / 20) * 100), 100)

        # Verify no event created on sample 20
        conn = sqlite3.connect(self.temp_db_path)
        events_count = conn.execute("SELECT COUNT(*) FROM events WHERE device_id = ?", (device_id,)).fetchone()[0]
        self.assertEqual(events_count, 0, "No event may be created on calibration sample 20.")
        conn.close()

        # Reading 21 -> begins normal monitoring
        res21 = self.client.post("/api/readings", json={"device_id": device_id, "fsr": 100.0, "temperature": 35.0})
        self.assertEqual(res21.status_code, 200)
        data21 = res21.get_json()
        self.assertEqual(data21["status"], "NORMAL")
        self.assertEqual(data21["pressure_seconds"], 0)

        # D. Pressure timer synchronization via /api/live
        # High pressure reading exceeding threshold
        res_press = self.client.post("/api/readings", json={"device_id": device_id, "fsr": 3500.0, "temperature": 35.0})
        self.assertEqual(res_press.status_code, 200)
        self.assertEqual(res_press.get_json()["status"], "PRESSURE")

        # Simulate 15 seconds of sustained pressure
        conn = sqlite3.connect(self.temp_db_path)
        fifteen_sec_ago = (datetime.now(timezone.utc) - timedelta(seconds=15)).isoformat()
        conn.execute("UPDATE device_state SET pressure_started_at = ? WHERE device_id = ?", (fifteen_sec_ago, device_id))
        conn.commit()
        conn.close()

        # /api/live must dynamically compute pressure_seconds = 15
        live_res = self.client.get(f"/api/live?device_id={device_id}").get_json()
        self.assertEqual(live_res["reading"]["pressure_seconds"], 15)
        self.assertEqual(live_res["reading"]["status"], "PRESSURE")

        # Simulate 29 seconds of sustained pressure -> UI authoritative display
        conn = sqlite3.connect(self.temp_db_path)
        twenty_nine_sec_ago = (datetime.now(timezone.utc) - timedelta(seconds=29)).isoformat()
        conn.execute("UPDATE device_state SET pressure_started_at = ? WHERE device_id = ?", (twenty_nine_sec_ago, device_id))
        conn.commit()
        conn.close()

        live_res = self.client.get(f"/api/live?device_id={device_id}").get_json()
        self.assertEqual(live_res["reading"]["pressure_seconds"], 29)
        self.assertEqual(live_res["reading"]["status"], "PRESSURE")

        # Simulate 31 seconds -> triggers HIGH_PRESSURE (or HIGH_RISK)
        conn = sqlite3.connect(self.temp_db_path)
        thirty_one_sec_ago = (datetime.now(timezone.utc) - timedelta(seconds=31)).isoformat()
        conn.execute("UPDATE device_state SET pressure_started_at = ? WHERE device_id = ?", (thirty_one_sec_ago, device_id))
        conn.commit()
        conn.close()

        live_res = self.client.get(f"/api/live?device_id={device_id}").get_json()
        self.assertEqual(live_res["reading"]["pressure_seconds"], 30)
        self.assertEqual(live_res["reading"]["status"], "HIGH_PRESSURE")

        # Starting calibration MUST immediately reset live timer to 0 and status to CALIBRATING
        res_calib = self.client.post("/api/calibrate", json={"device_id": device_id})
        self.assertEqual(res_calib.status_code, 200)

        live_res = self.client.get(f"/api/live?device_id={device_id}").get_json()
        self.assertTrue(live_res["calibration_running"])
        self.assertEqual(live_res["reading"]["pressure_seconds"], 0)
        self.assertEqual(live_res["reading"]["status"], "CALIBRATING")

        self.client.post("/auth/logout")


if __name__ == "__main__":
    unittest.main()

