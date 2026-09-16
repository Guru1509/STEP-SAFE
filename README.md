# STEP-SAFE

> An ESP32-based prototype for smart foot-pressure and temperature monitoring with role-based access control, device assignment, in-app notification center, and mobile push notification scaffold.

STEP-SAFE reads an Interlink 402 Force Sensing Resistor (FSR) on GPIO34 and an LM35 temperature sensor on GPIO35, displays live telemetry in a responsive browser dashboard, stores data in SQLite, and triggers an active buzzer alarm on GPIO25 when sustained high pressure is detected for 30 seconds.

The system includes role-based access control (Patient, Caregiver, Administrator), device-to-patient assignment, many-to-many caregiver monitoring, a local in-app notification center with database deduplication, and an optional Firebase Cloud Messaging (FCM HTTP v1) mobile push provider scaffold.

> **Important:** This is an educational and academic demonstration prototype. It is not clinically validated and must not be used for diagnosis, medical treatment, or safety-critical decisions.

---

## System Architecture

```text
┌───────────────────────────────┐
│   Hardware Layer (ESP32)      │
│   FSR (GPIO34) + LM35 (GPIO35)│
│   Buzzer Alarm (GPIO25)       │
└───────────────┬───────────────┘
                │ Wi-Fi SoftAP "STEP-SAFE" (192.168.4.1)
                │ HTTP POST /api/readings (No Internet Required)
                ▼
┌────────────────────────────────────────────────────────┐
│   Flask Backend & Storage (MacBook / Host)             │
│   - REST Endpoints (/api/readings, /api/live, etc.)    │
│   - Session Auth & RBAC (/auth/login, /auth/logout)    │
│   - SQLite Database (step_safe.db)                     │
│   - 30-Second Risk Classifier & Alert Deduplicator     │
└───────────────┬───────────────────────────┬────────────┘
                │ Local Telemetry & In-App   │ Internet WAN (Optional)
                ▼                            ▼
┌───────────────────────────────┐  ┌─────────────────────────────────┐
│   Web Dashboard & App         │  │   Firebase / APNs Push Gateway  │
│   - Responsive SVG Charts     │  │   - FCM HTTP v1 Service Account │
│   - In-App Notification Center│  │   - Push to Mobile Subscriptions│
│   - Patient / Caregiver Views │  │   - (Optional / Scaffolded)     │
│   - Admin Management Console  │  └─────────────────────────────────┘
└───────────────────────────────┘
```

---

## User Roles & Permissions

| Role | Description & Permissions |
|---|---|
| **Patient** | Has a personal clinical profile. Can view only their assigned ESP32 device's live telemetry, 30-second horizontal timer, custom SVG history graphs, calibration state, alerts, and notification history. **Calibration:** Patient is the **only role authorized to initiate calibration**, which applies strictly to their own assigned device. Cannot access Admin Console. |
| **Caregiver** | Monitors assigned patients (via many-to-many assignment). Can view assigned patients in a card grid, inspect each patient's live dashboard, sensor graphs, timer, and alert history. **Notifications:** Receives in-app notifications whenever an assigned patient's device generates a high-risk event. **Calibration:** Caregiver is **not allowed to calibrate** (action is hidden in UI; API returns HTTP `403 Forbidden`). Cannot modify devices, users, or assignments. Unauthorized access to unassigned patients returns `403 Forbidden`. |
| **Administrator** | Full administrative privileges. Manages user accounts, ESP32 hardware devices, device-to-patient assignments, and caregiver-patient links. **Calibration:** Administrator is **not allowed to calibrate** patient devices (action is hidden in UI; API returns HTTP `403 Forbidden`). |

---

## Sample / Demonstration Accounts

For demonstration and testing purposes, standard sample accounts are structured as follows:

```text
Admin: admin@example.com
Patient: patient@example.com
Caregiver: caregiver@example.com
```

> *(Note: In local development, you can quickly seed local demo accounts using `flask seed-demo` or by starting Flask with `STEP_SAFE_SEED_DEMO=1`, which seeds `admin@stepsafe.local`, `caregiver@stepsafe.local`, and `patient@stepsafe.local` with password `admin123` / `caregiver123` / `patient123`).*

---

## How to Use Profiles and Notifications

```text
Admin:
1. Log in.
2. Create a patient profile.
3. Create a caregiver profile.
4. Assign the ESP32 device to the patient.
5. Assign the caregiver to that patient.

Patient:
1. Log in.
2. View personal live readings, timer, graphs, alerts, and notifications.

Caregiver:
1. Log in.
2. Select an assigned patient.
3. View that patient’s live readings, timer, history, and alerts.

Notifications:
1. A sustained pressure event reaches 30 seconds.
2. The buzzer activates locally.
3. Flask creates one alert.
4. The patient and assigned caregivers receive an in-app notification.
5. If Firebase is configured, they also receive a mobile push notification.
6. Releasing pressure resolves the active alert.
```

---

## Hardware & Wiring

### Hardware Required
- ESP32 development board (NodeMCU-32S / ESP32 Dev Module)
- Interlink 402 FSR (Force Sensing Resistor)
- 10 kΩ resistor for the FSR voltage divider
- LM35 linear analog temperature sensor
- Active piezo buzzer (5V / 3.3V compatible)
- Breadboard and jumper wires
- Computer capable of running Python 3.10+ / Flask

### Pin Connections

| Component | ESP32 Connection | Notes |
|---|---|---|
| **FSR Divider** | `GPIO 34` (ADC1_CH6) | Analog input. One leg to `3V3`, other to `GPIO34` and `10 kΩ` to `GND`. |
| **LM35 Output** | `GPIO 35` (ADC1_CH7) | Analog input (10 mV/°C). Left pin: `5V`, Center pin: `GPIO35`, Right pin: `GND`. |
| **Buzzer Signal** | `GPIO 25` | Digital output. Driven `HIGH` after 30s sustained pressure. |
| **All Components** | Common `GND` | Shared ground reference across all sensors. |

---

## Software Setup & Environment Configuration

### 1. Clone & Prepare Virtual Environment

```bash
git clone https://github.com/YOUR-USERNAME/STEP-SAFE.git
cd STEP-SAFE

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Environment Variables (`.env.example`)

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

| Variable | Default / Example | Description |
|---|---|---|
| `STEP_SAFE_SECRET_KEY` | `your-secret-key-change-in-production` | Secret key used by Flask to cryptographically sign client-side session cookies. |
| `STEP_SAFE_DB` | `step_safe.db` | Path to the SQLite database file. |
| `STEP_SAFE_HTTPS` | `0` | Set to `1` when deployed under HTTPS to enforce `Secure` cookie attributes. |
| `STEP_SAFE_SEED_DEMO` | `0` | Set to `1` to automatically seed demo accounts on server startup. |
| `GOOGLE_APPLICATION_CREDENTIALS` | `/path/to/service-account.json` | Path to Google Service Account credentials for optional FCM HTTP v1 push notifications. |
| `FIREBASE_PROJECT_ID` | `step-safe-app` | Google Cloud / Firebase project ID for FCM HTTP v1. |

---

## Authentication & User Management

### Login & Logout Setup
- Authentication is handled via `/auth/login` and `/auth/logout`.
- Passwords are securely hashed with Werkzeug (`scrypt` / `pbkdf2:sha256`).
- Session cookies are cryptographically signed client-side cookies configured with:
  - `HttpOnly=True` (mitigates XSS cookie theft)
  - `SameSite='Lax'` (mitigates CSRF)
  - `Secure=True` (only when `STEP_SAFE_HTTPS=1`)
- The web interface at `http://127.0.0.1:5000` provides a login modal, session status indicator, and logout button.

### How to Create the First Admin Account
To create your primary administrator account, use the Click CLI bootstrap command in your terminal:

```bash
flask bootstrap-admin --email admin@example.com --name "System Administrator" --password "YourStrongPassword"
```
*(If options are omitted, the command prompts interactively with masked password input).*

### Quick Demo Account Seeding (Development Only)
If you want to quickly test all three roles without manual setup, run:

```bash
flask seed-demo
```
or launch Flask with:
```bash
STEP_SAFE_SEED_DEMO=1 python app.py
```
This populates:
- **Admin**: `admin@stepsafe.local` (password: `admin123`)
- **Caregiver**: `caregiver@stepsafe.local` (password: `caregiver123`)
- **Patient**: `patient@stepsafe.local` (password: `patient123`)

---

## Step-by-Step User Guide

### 1. How Admin Creates Patient and Caregiver Profiles
1. Log in as an Administrator (`admin@example.com` or `admin@stepsafe.local`).
2. Click the **Admin Console** tab in the top navigation bar.
3. In the **User Accounts** tab, click **+ Create User**.
4. Fill in the user's Full Name, Email Address, a temporary Password, and select the **Role** (`Patient` or `Caregiver`).
5. Click **Create User**. If the user is a patient, a corresponding `patient_profiles` record is automatically initialized.

### 2. How Admin Assigns an ESP32 Device to a Patient
1. In the **Admin Console**, switch to the **Hardware Devices** tab.
2. If the device isn't registered, click **+ Register Device**, input the device ID (e.g. `step-safe-esp32`) and a friendly display name (e.g. `Left Foot Sensor Unit #1`).
3. In the devices table, locate the device row and find the **Assigned Patient** column.
4. Select the patient from the dropdown. The system immediately updates `devices.patient_user_id` (the single source of truth for device ownership).

### 3. How Admin Links a Caregiver to a Patient
1. In the **Admin Console**, switch to the **Caregiver Assignments** tab.
2. Select the Caregiver from the first dropdown.
3. Select the Patient from the second dropdown.
4. Click **Assign Link**. The many-to-many relationship is recorded in `caregiver_patients`.

### 4. How Patient Views Their Live Dashboard and Alert History
1. Log in as a Patient (`patient@example.com` or `patient@stepsafe.local`).
2. The dashboard automatically focuses on your assigned device.
3. You can monitor:
   - Live FSR force reading and gauge.
   - Live temperature reading and status.
   - The **30-second horizontal timer progress bar**.
   - Custom SVG history charts for pressure and temperature.
   - Calibration status and 20-sample baseline calibration button.
   - The **Recent pressure / high-risk events** audit log.
4. Click **My Profile** in the top bar to update emergency contact details and notification preferences.

### 5. How Caregiver Views Assigned Patients and Their Alerts
1. Log in as a Caregiver (`caregiver@example.com` or `caregiver@stepsafe.local`).
2. The default landing view displays the **Assigned Patients** card grid.
3. Each patient card highlights:
   - Patient name and emergency contact.
   - Assigned device name and current operational state (`NORMAL`, `PRESSURE`, `HIGH PRESSURE`, `HIGH RISK`).
   - Latest sensor readings and active duration.
4. Click **View Live Dashboard** on any patient card to inspect their live telemetry, charts, and timer.
5. *Security guarantee:* Attempting to access an unassigned patient directly via `/api/patients/<id>/dashboard` returns HTTP `403 Forbidden`.

### 6. How to Use the In-App Notification Center
1. Located at the top right header (bell icon 🔔).
2. Displays a real-time badge with the count of unread notifications.
3. Click the bell to toggle the **Notification Drawer**.
4. Each notification shows:
   - Severity tag (`HIGH RISK`, `HIGH PRESSURE`, `SYSTEM`).
   - Title and informative message.
   - Timestamp.
   - Direct link to inspect the device.
5. Click **Mark Read** on an individual notification or **Mark All Read** at the top.

---

## Mobile Push Notifications (Optional / Scaffolded)

STEP-SAFE includes a production-grade **Firebase Cloud Messaging (FCM HTTP v1)** provider scaffold in [notifications.py](file:///Users/gururaghavraj/Documents/STEP-SAFE/notifications.py).

> [!NOTE]
> **Status: Scaffolded & Ready for Credentials.**
> Real mobile push notifications require valid Google Cloud service-account credentials. Without credentials, FCM is gracefully disabled with an informative log message, and 100% of alerts deliver through the built-in in-app notification center.

### Setting Up Real Mobile Push (Optional)
1. In the Google Cloud / Firebase Console, create a service account with the **Firebase Cloud Messaging API (V1)** permission.
2. Download the service account JSON key file (e.g. `service-account.json`).
3. Add to `.env`:
   ```bash
   GOOGLE_APPLICATION_CREDENTIALS=/absolute/path/to/service-account.json
   FIREBASE_PROJECT_ID=your-firebase-project-id
   ```
4. Client devices register their FCM registration tokens via:
   ```http
   POST /api/notifications/subscribe
   Content-Type: application/json

   {"token": "<FCM_REGISTRATION_TOKEN>", "platform": "android"}
   ```
5. When a 30-second high-pressure alert triggers, the backend will dispatch HTTP v1 push notifications to all active registered tokens for the patient and assigned caregivers.

---

## Network Architecture & Internet Requirement Note

- **Local Wi-Fi Monitoring (ESP32 SoftAP)**:
  The ESP32 SoftAP Wi-Fi network (`STEP-SAFE`, IP `192.168.4.1`, password `stepsafe123`) connects directly to the Flask host computer (typically assigned `192.168.4.2`). Sensor streaming (`POST /api/readings`), calibration, live dashboard, active buzzer actuation, and the in-app notification center run **entirely offline without an internet connection**.
- **Mobile Push Internet Requirement**:
  If external Firebase mobile push notifications are configured, the computer hosting the Flask backend requires access to the Internet (e.g., via a secondary network adapter, Ethernet, or cellular hotspot) to send push payloads to Google's FCM servers.

---

## ESP32 Firmware & Calibration Logic

### Hardware Firmware (`STEP_SAFE_ESP32.ino`)
1. Open `esp32/STEP_SAFE_ESP32/STEP_SAFE_ESP32.ino` in Arduino IDE.
2. Confirm `BACKEND_URL` points to your computer's IP (typically `http://192.168.4.2:5000`).
3. Flash the ESP32 at **115200 baud**.
4. The ESP32 starts the `STEP-SAFE` Wi-Fi Access Point, streams readings to `POST /api/readings`, and queries `GET /api/device-config` every 5 seconds.

### Calibration & Alert Logic
1. Connect your computer to Wi-Fi `STEP-SAFE` (password: `stepsafe123`).
2. Open `http://192.168.4.2:5000` (or `http://127.0.0.1:5000`) and log in as an authorized **Patient**.
3. Keep the foot sensor unloaded and click **Start calibration** (samples 20 incoming readings from the patient's assigned device).
   > *Security Note:* Only the authenticated Patient can initiate calibration, and only for their own assigned device. Requests from Caregiver or Admin roles, or requests targeting unassigned devices, return HTTP `403 Forbidden`.
4. Operational state transitions:

| Condition | State | Hardware & Notification Behavior |
|---|---|---|
| Sensor below threshold | `NORMAL` | Timer resets to `0 / 30 s`. Local buzzer remains `OFF`. |
| Pressure detected for $< 30$ s | `PRESSURE` | Horizontal timer bar advances. Buzzer remains `OFF`. |
| Sustained pressure $\ge 30$ s | `HIGH PRESSURE` | Local buzzer turns `ON` (GPIO25). 1 event logged. In-app & push notifications dispatched. |
| Sustained pressure $\ge 30$ s + Temp $\ge \text{Base}+2^\circ\text{C}$ | `HIGH RISK` | Local buzzer remains `ON`. High-risk event logged. Notifications dispatched. |
| Foot pressure released | `NORMAL` | Buzzer turns `OFF` (GPIO25). Timer resets. Active alert marked resolved in database. |

### Database-Level Notification Deduplication
- The SQLite table `notifications` enforces `UNIQUE(event_id, recipient_user_id, type)`.
- The telemetry loop tracks `last_notified_event_id` in `device_state`.
- Sustained high pressure generates **exactly one** notification per recipient for the duration of the incident episode.
- External push notification dispatch is decoupled and executes after the SQLite transaction commits, preventing database lock contention.

---

## How to Run & Test the Complete System Locally

### 1. Start the Flask Server
```bash
./.venv/bin/python app.py
```
*(Or with demo accounts pre-seeded: `STEP_SAFE_SEED_DEMO=1 ./.venv/bin/python app.py`)*

### 2. Log In
Open `http://127.0.0.1:5000` in your browser. Log in with your bootstrapped admin account or click a quick-demo button.

### 3. Simulate an ESP32 Telemetry Alert
Open a separate terminal window and run:

1. **Send a baseline reading:**
   ```bash
   curl -X POST http://127.0.0.1:5000/api/readings \
     -H 'Content-Type: application/json' \
     -d '{"device_id":"step-safe-esp32","fsr":100,"temperature":32.0}'
   ```
2. **Simulate continuous high pressure:**
   ```bash
   curl -X POST http://127.0.0.1:5000/api/readings \
     -H 'Content-Type: application/json' \
     -d '{"device_id":"step-safe-esp32","fsr":680,"temperature":33.0}'
   ```
3. Watch the horizontal timer bar advance on the dashboard. Once 30 seconds elapse:
   - System state becomes `HIGH PRESSURE`.
   - The buzzer state changes to `ON`.
   - The unread badge on the notification bell increments.
   - An in-app notification appears in the drawer.
4. **Simulate pressure release:**
   ```bash
   curl -X POST http://127.0.0.1:5000/api/readings \
     -H 'Content-Type: application/json' \
     -d '{"device_id":"step-safe-esp32","fsr":20,"temperature":32.0}'
   ```
   - System state returns to `NORMAL`.
   - Buzzer turns `OFF`.
   - Timer resets to 0.

### 4. Run Automated Unit Tests
```bash
./.venv/bin/python -m unittest test_app.py
```
All 10 unit tests validate auth, 403 authorization boundaries, notification deduplication, FCM graceful fallback, and hardware telemetry ingestion.

---

## API Endpoints Reference

| Method | Endpoint | Auth Required | Description |
|---|---|---|---|
| `POST` | `/api/readings` | None (IoT) | Ingest ESP32 sensor telemetry (`device_id`, `fsr`, `temperature`). |
| `GET` | `/api/device-config` | None (IoT) | Return calibration thresholds and timer configuration to ESP32. |
| `POST` | `/auth/login` | None | Authenticate user via JSON or Form `{email, password}`. |
| `POST` | `/auth/logout` | Session | Terminate session and clear cookie. |
| `GET` | `/api/me` | Session | Get current user details, role, and assigned device/patients. |
| `PUT` | `/api/me/profile` | Session | Update patient profile and notification preferences. |
| `GET` | `/api/live` | Optional | Get latest reading and calibration state (scoped if logged in). |
| `GET` | `/api/history` | Optional | Retrieve historical readings and events for SVG charts. |
| `GET` | `/api/summary` | Optional | Retrieve aggregate statistics. |
| `POST` | `/api/calibrate` | Patient Only | Start a 20-reading calibration for the patient's assigned device (returns 403 for Caregiver/Admin). |
| `GET` | `/api/notifications` | Session | List in-app notifications for the authenticated user. |
| `POST` | `/api/notifications/<id>/read` | Session | Mark a specific notification as read. |
| `POST` | `/api/notifications/read-all` | Session | Mark all notifications as read. |
| `POST` | `/api/notifications/subscribe` | Session | Register an FCM device token for push notifications. |
| `GET` | `/api/patients` | Caregiver/Admin | List assigned patients (caregivers) or all patients (admins). |
| `GET` | `/api/patients/<id>/dashboard` | Scoped | Get telemetry dashboard for a specific patient (enforces 403). |
| `GET` | `/api/admin/users` | Admin | List all user accounts. |
| `POST` | `/api/admin/users` | Admin | Create a new user profile. |
| `POST` | `/api/admin/users/<id>/toggle-active` | Admin | Activate or deactivate a user account. |
| `GET` | `/api/admin/devices` | Admin | List all hardware devices and assigned patient owners. |
| `POST` | `/api/admin/devices` | Admin | Register a new hardware device. |
| `POST` | `/api/admin/assign-device` | Admin | Assign or unassign a device to a patient. |
| `GET` | `/api/admin/assignments` | Admin | List all Caregiver-to-Patient mappings. |
| `POST` | `/api/admin/assign-caregiver` | Admin | Create a Caregiver-to-Patient assignment. |
| `POST` | `/api/admin/unassign-caregiver` | Admin | Remove a Caregiver-to-Patient assignment. |

---

## License & Disclaimer

This project is created for educational, academic, and demonstration purposes. It is not an FDA-cleared or CE-marked medical device.
