/**
 * STEP-SAFE Dynamic Frontend Controller
 * Role-Based Telemetry, In-App Notification Center, and Responsive SVG Charts
 */

const $ = id => document.getElementById(id);
let currentUser = null;
let activeDeviceId = "step-safe-esp32";
let activePatientId = null;
let activeView = "telemetry"; // 'telemetry', 'login', 'caregiver', 'admin', 'profile'
let refreshTimer = null;
let notifPollTimer = null;

function value(n, digits = 1) {
  return n == null ? '—' : Number(n).toFixed(digits);
}

// Custom SVG History Charts with subtle area gradient
function drawChart(id, values, color) {
  const box = $(id), w = 520, h = 240, p = 28;
  if (!box) return;
  if (!values || !values.length) {
    box.textContent = 'Waiting for telemetry readings…';
    return;
  }
  const low = Math.min(...values), high = Math.max(...values), range = (high - low) || 1;
  const coords = values.map((v, i) => ({
    x: p + i * (w - 2 * p) / Math.max(values.length - 1, 1),
    y: h - p - (v - low) * (h - 2 * p) / range
  }));
  const points = coords.map(pt => `${pt.x.toFixed(1)},${pt.y.toFixed(1)}`).join(' ');
  const gradId = `areaGrad_${id}`;
  const firstX = coords[0].x.toFixed(1), lastX = coords[coords.length - 1].x.toFixed(1);
  const areaPoints = `${firstX},${h - p} ${points} ${lastX},${h - p}`;
  const midY = (h - p + p) / 2;

  box.innerHTML = `
    <svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Sensor reading history">
      <defs>
        <linearGradient id="${gradId}" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="${color}" stop-opacity="0.2"/>
          <stop offset="100%" stop-color="${color}" stop-opacity="0.0"/>
        </linearGradient>
      </defs>
      <line x1="${p}" y1="${midY}" x2="${w - p}" y2="${midY}" stroke="#143640" stroke-dasharray="3 3"/>
      <line x1="${p}" y1="${h - p}" x2="${w - p}" y2="${h - p}" stroke="#1d4a54"/>
      <line x1="${p}" y1="${p}" x2="${p}" y2="${h - p}" stroke="#1d4a54"/>
      <polygon points="${areaPoints}" fill="url(#${gradId})"/>
      <polyline points="${points}" fill="none" stroke="${color}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
      <text x="${p + 4}" y="18" fill="#92b0b6" font-size="11" font-family="inherit">max ${high.toFixed(1)}</text>
      <text x="${p + 4}" y="${h - 8}" fill="#64888f" font-size="11" font-family="inherit">min ${low.toFixed(1)}</text>
    </svg>`;
}

// =========================================================================
// Telemetry & Sensor Polling
// =========================================================================

let pollCount = 0;
let wasCalibrating = false;
let calibDoneHideTimeout = null;

// Backend-authoritative pressure timer state.
// The displayed timer is derived from pressure_started_at, never from
// an independent frontend start/stop counter.
let livePressureStartedAt = null;
let livePressureActive = false;
let livePressureCalibrating = false;

function updatePressureTimerDisplay() {
  const durationEl = $('duration');
  const progressEl = $('timerProgress');
  if (!durationEl || !progressEl) return;

  if (livePressureCalibrating || !livePressureActive || !livePressureStartedAt) {
    durationEl.textContent = '0 / 30 s';
    progressEl.style.width = '0%';
    return;
  }

  const started = new Date(livePressureStartedAt).getTime();
  if (!Number.isFinite(started)) {
    durationEl.textContent = '0 / 30 s';
    progressEl.style.width = '0%';
    return;
  }

  const elapsed = Math.min(
    30,
    Math.max(0, (Date.now() - started) / 1000)
  );

  durationEl.textContent = `${Math.floor(elapsed)} / 30 s`;
  progressEl.style.width = `${(elapsed / 30) * 100}%`;
}

function startPressureTimerDisplayLoop() {
  if (window.stepSafePressureTimer) {
    cancelAnimationFrame(window.stepSafePressureTimer);
  }

  const tick = () => {
    updatePressureTimerDisplay();
    window.stepSafePressureTimer = requestAnimationFrame(tick);
  };

  tick();
}

startPressureTimerDisplayLoop();


async function refreshTelemetry() {
  if (activeView !== 'telemetry' && activeView !== 'caregiver') return;

  try {
    const devParam = activeDeviceId ? `?device_id=${encodeURIComponent(activeDeviceId)}` : '';
    
    // Always poll /api/live for ultra-fast, authoritative telemetry & calibration progress
    const liveReq = fetch(`/api/live${devParam}`);
    
    // Periodically fetch heavier aggregate history & stats (every ~2.4s)
    const fetchHeavy = (pollCount % 4 === 0);
    pollCount++;

    const promises = [liveReq];
    if (fetchHeavy) {
      promises.push(fetch(`/api/history${devParam}`));
      promises.push(fetch(`/api/summary${devParam}`));
    }

    const responses = await Promise.all(promises);
    const liveRes = responses[0];
    if (!liveRes.ok) return;

    const live = await liveRes.json();
    const r = live.reading, c = live.calibration;
    const isCalibrating = Boolean(live.calibration_running);
    const samples = Number(live.calibration_samples) || 0;
    const pctCalib = Math.min(100, Math.round((samples / 20) * 100));
    // Backend is the source of truth for the pressure timer.
    livePressureCalibrating = isCalibrating;
    livePressureStartedAt = live.pressure_started_at || null;
    livePressureActive = Boolean(
      r && ['PRESSURE', 'HIGH_PRESSURE', 'HIGH_RISK'].includes(r.status)
    );

    if (isCalibrating) {
      // While in calibration, pressure timer is strictly suspended
      $('duration').textContent = '0 / 30 s';
      $('timerProgress').style.width = '0%';
      $('timerProgress').classList.add('paused');
      $('timerHint').textContent = 'Calibration in progress — pressure timer paused.';
      $('status').textContent = 'CALIBRATING';
      $('status').className = 'status CALIBRATING';
      if ($('alertBanner')) $('alertBanner').style.display = 'none';

      if (r) {
        $('fsr').textContent = value(r.fsr, 0);
        $('temp').textContent = value(r.temperature) + ' °C';
        $('updated').textContent = 'Received ' + new Date(r.recorded_at).toLocaleTimeString();
      }
    } else if (r) {
      $('timerProgress').classList.remove('paused');
      const pressureActive = ['PRESSURE', 'HIGH_PRESSURE', 'HIGH_RISK'].includes(r.status);

      $('fsr').textContent = value(r.fsr, 0);
      $('temp').textContent = value(r.temperature) + ' °C';
      updatePressureTimerDisplay();
      
      const timerStarted = livePressureStartedAt
        ? new Date(livePressureStartedAt).getTime()
        : NaN;
      const displayElapsed = Number.isFinite(timerStarted)
        ? Math.min(30, Math.max(0, (Date.now() - timerStarted) / 1000))
        : 0;

      $('timerHint').textContent = pressureActive
        ? (displayElapsed < 30 ? 'Pressure detected — buzzer activates after 30 seconds.' : '30 seconds reached — buzzer & sustained-pressure alert active!')
        : 'Waiting for pressure';

      $('status').textContent = r.status.replace('_', ' ');
      $('status').className = 'status ' + r.status;
      $('updated').textContent = 'Received ' + new Date(r.recorded_at).toLocaleTimeString();

      // Top Alert Banner
      if (['HIGH_PRESSURE', 'HIGH_RISK'].includes(r.status)) {
        $('alertBanner').style.display = 'block';
        $('alertBannerTitle').textContent = r.status === 'HIGH_RISK' ? 'HIGH RISK ALERT: SUSTAINED PRESSURE + ELEVATED TEMPERATURE' : 'HIGH PRESSURE ALERT: SUSTAINED FOR 30+ SECONDS';
        $('alertBannerMsg').textContent = `Device: ${activeDeviceId}. Active buzzer alarming on GPIO25. Check patient posture.`;
      } else {
        $('alertBanner').style.display = 'none';
      }
    } else {
      $('alertBanner').style.display = 'none';
    }

    $('fsrInfo').textContent = c ? `Baseline: ${value(c.fsr_baseline, 0)} · Threshold: ${value(c.fsr_threshold, 0)}` : 'Calibration required';
    $('tempInfo').textContent = c ? `Baseline: ${value(c.temp_baseline)}°C · Threshold: ${value(c.temp_threshold)}°C` : 'Calibration required';
    $('fsrBaseline').textContent = c ? value(c.fsr_baseline, 0) : 'Not calibrated';
    $('fsrThreshold').textContent = c ? value(c.fsr_threshold, 0) : 'Not calibrated';
    $('tempBaseline').textContent = c ? value(c.temp_baseline) + ' °C' : 'Not calibrated';
    $('tempThreshold').textContent = c ? value(c.temp_threshold) + ' °C' : 'Not calibrated';

    // Live Calibration Progress Bar (backend truth, no 0% reset on poll, survives browser refresh)
    const isPatient = currentUser && currentUser.role === 'patient';
    if (isCalibrating) {
      wasCalibrating = true;
      if (calibDoneHideTimeout) {
        clearTimeout(calibDoneHideTimeout);
        calibDoneHideTimeout = null;
      }
      if ($('calibProgressWrap')) $('calibProgressWrap').style.display = 'block';
      if ($('calibStatusBadge')) {
        $('calibStatusBadge').style.display = 'inline-block';
        $('calibStatusBadge').textContent = 'In Progress';
        $('calibStatusBadge').className = 'calib-badge';
      }
      if ($('calibProgressBar')) $('calibProgressBar').style.width = pctCalib + '%';
      if ($('calibProgressText')) $('calibProgressText').textContent = `${samples} / 20 readings (${pctCalib}%)`;
      $('calibrationText').textContent = `Collecting baseline readings: ${samples} / 20 received. Keep sensor in relaxed resting position.`;
      if (isPatient) {
        $('calibrate').disabled = true;
        $('calibrate').textContent = `Calibrating (${samples}/20)...`;
        $('calibrate').classList.add('is-calibrating');
      }
    } else if (wasCalibrating) {
      // Just completed calibration transition: show 20/20 (100%) cleanly
      wasCalibrating = false;
      if ($('calibProgressWrap')) $('calibProgressWrap').style.display = 'block';
      if ($('calibStatusBadge')) {
        $('calibStatusBadge').style.display = 'inline-block';
        $('calibStatusBadge').textContent = 'Complete';
        $('calibStatusBadge').className = 'calib-badge complete';
      }
      if ($('calibProgressBar')) $('calibProgressBar').style.width = '100%';
      if ($('calibProgressText')) $('calibProgressText').textContent = '20 / 20 readings (100%)';
      $('calibrationText').textContent = c
        ? `Calibration complete! New baseline: FSR ${value(c.fsr_baseline, 0)}, temperature ${value(c.temp_baseline)}°C.`
        : 'Calibration complete!';

      if (isPatient) {
        $('calibrate').disabled = false;
        $('calibrate').textContent = 'Start calibration';
        $('calibrate').classList.remove('is-calibrating');
      }

      if (calibDoneHideTimeout) clearTimeout(calibDoneHideTimeout);
      calibDoneHideTimeout = setTimeout(() => {
        if ($('calibProgressWrap')) $('calibProgressWrap').style.display = 'none';
        if ($('calibStatusBadge')) $('calibStatusBadge').style.display = 'none';
        if (c) {
          $('calibrationText').textContent = `Last baseline: FSR ${value(c.fsr_baseline, 0)}, temperature ${value(c.temp_baseline)}°C.`;
        }
      }, 2500);
    } else {
      if (!calibDoneHideTimeout) {
        if ($('calibProgressWrap')) $('calibProgressWrap').style.display = 'none';
        if ($('calibStatusBadge')) $('calibStatusBadge').style.display = 'none';
      }
      if (!calibDoneHideTimeout) {
        $('calibrationText').textContent = c
          ? `Last baseline: FSR ${value(c.fsr_baseline, 0)}, temperature ${value(c.temp_baseline)}°C.`
          : 'Collects 20 incoming readings to establish an individual demo baseline.';
      }
      if (isPatient) {
        $('calibrate').disabled = false;
        $('calibrate').textContent = 'Start calibration';
        $('calibrate').classList.remove('is-calibrating');
      }
    }

    // Heavy history and summary updates when fetched
    if (fetchHeavy && responses[1] && responses[2] && responses[1].ok && responses[2].ok) {
      const hist = await responses[1].json();
      const sum = await responses[2].json();

      $('readingCount').textContent = sum.readings || 0;
      $('avgFsr').textContent = value(sum.avg_fsr, 0);
      $('avgTemp').textContent = value(sum.avg_temperature) + ' °C';
      $('eventCount').textContent = sum.event_count || 0;

      if (hist.readings) {
        drawChart('fsrChart', hist.readings.map(x => x.fsr), '#65e0c6');
        drawChart('tempChart', hist.readings.map(x => x.temperature), '#ffd166');
      }

      if (hist.events) {
        const badge = $('eventLogCountBadge');
        if (badge) {
          badge.textContent = hist.events.length
            ? `${hist.events.length} event${hist.events.length === 1 ? '' : 's'}`
            : '';
        }
        $('events').innerHTML = hist.events.length
          ? hist.events.map(e => `
              <div class="event-row">
                <div class="event-main-info">
                  <span class="event-tag ${e.event_type}">${e.event_type.replace('_', ' ')}</span>
                  <span class="event-time">started ${new Date(e.started_at).toLocaleTimeString()}</span>
                </div>
                <div class="event-details">
                  <span>peak FSR <strong>${value(e.peak_fsr, 0)}</strong></span>
                  <span class="event-separator">·</span>
                  <span>temp <strong>${value(e.peak_temperature)}°C</strong></span>
                  <span class="event-separator">·</span>
                  ${e.ended_at ? '<span class="event-status-resolved">(resolved)</span>' : '<span class="event-status-active">(ACTIVE)</span>'}
                </div>
              </div>
            `).join('')
          : '<div class="events-empty">No events recorded yet.</div>';
      }
    }
  } catch (err) {
    console.debug("Telemetry refresh error:", err);
  }
}

// =========================================================================
// Authentication & Session Management
// =========================================================================

async function checkSession() {
  try {
    const res = await fetch('/api/me');
    if (res.ok) {
      const data = await res.json();
      currentUser = data.user;
      renderUserInterface(data);
    } else {
      currentUser = null;
      renderGuestInterface();
    }
  } catch (err) {
    currentUser = null;
    renderGuestInterface();
  }
}

function renderUserInterface(data) {
  $('userBadge').style.display = 'inline-block';
  $('userBadge').textContent = currentUser.role;
  $('userBadge').className = 'role-pill ' + currentUser.role;
  $('userName').style.display = 'inline-block';
  $('userName').textContent = currentUser.full_name;
  $('logoutBtn').style.display = 'inline-block';
  $('loginModalBtn').style.display = 'none';
  $('notifWrapper').style.display = 'block';

  // Build role-specific navigation tabs
  const nav = $('roleNav');
  nav.style.display = 'flex';
  nav.innerHTML = '';

  if (currentUser.role === 'patient') {
    if (data.assigned_device) {
      activeDeviceId = data.assigned_device.device_id;
      $('activeDeviceLabel').textContent = `${data.assigned_device.display_name} (${activeDeviceId})`;
    }
    nav.appendChild(createNavBtn('Telemetry', () => switchView('telemetry'), true));
    nav.appendChild(createNavBtn('My Profile', () => loadPatientProfile()));
  } else if (currentUser.role === 'caregiver') {
    nav.appendChild(createNavBtn('Assigned Patients', () => loadCaregiverPatients(), true));
    nav.appendChild(createNavBtn('Patient Telemetry', () => switchView('telemetry')));
  } else if (currentUser.role === 'admin') {
    nav.appendChild(createNavBtn('Telemetry', () => switchView('telemetry'), true));
    nav.appendChild(createNavBtn('Admin Console', () => loadAdminConsole()));
  }

  // Start polling notifications for logged in user
  pollNotifications();
  if (notifPollTimer) clearInterval(notifPollTimer);
  notifPollTimer = setInterval(pollNotifications, 3000);

  // Default view per role
  if (currentUser.role === 'caregiver') {
    loadCaregiverPatients();
  } else {
    switchView('telemetry');
  }
}

function renderGuestInterface() {
  $('userBadge').style.display = 'none';
  $('userName').style.display = 'none';
  $('logoutBtn').style.display = 'none';
  $('loginModalBtn').style.display = 'inline-block';
  $('notifWrapper').style.display = 'none';
  $('roleNav').style.display = 'none';
  $('calibrationPanel').style.display = 'none';
  $('calibrate').style.display = 'none';
  $('calibrate').disabled = true;
  if (notifPollTimer) clearInterval(notifPollTimer);
  switchView('telemetry');
}

function createNavBtn(label, onClick, active = false) {
  const btn = document.createElement('button');
  btn.className = 'role-nav-btn' + (active ? ' active' : '');
  btn.textContent = label;
  btn.onclick = () => {
    document.querySelectorAll('.role-nav-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    onClick();
  };
  return btn;
}

function switchView(viewName) {
  activeView = viewName;
  $('telemetryView').style.display = viewName === 'telemetry' ? 'block' : 'none';
  $('loginView').style.display = viewName === 'login' ? 'flex' : 'none';
  $('caregiverView').style.display = viewName === 'caregiver' ? 'block' : 'none';
  $('adminView').style.display = viewName === 'admin' ? 'block' : 'none';
  $('profileView').style.display = viewName === 'profile' ? 'block' : 'none';

  if (viewName === 'telemetry') {
    $('deviceContextBanner').style.display = currentUser ? 'flex' : 'none';
    const isPatient = currentUser && currentUser.role === 'patient';
    $('calibrationPanel').style.display = isPatient ? 'flex' : 'none';
    $('calibrate').style.display = isPatient ? 'inline-block' : 'none';
    $('calibrate').disabled = !isPatient;
    refreshTelemetry();
  }
}

// =========================================================================
// Login / Logout Flows
// =========================================================================

$('loginModalBtn').onclick = () => switchView('login');

$('loginForm').onsubmit = async e => {
  e.preventDefault();
  const email = $('loginEmail').value.trim();
  const password = $('loginPassword').value;
  const errBox = $('loginError');
  errBox.style.display = 'none';

  try {
    const res = await fetch('/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password })
    });
    const data = await res.json();
    if (res.ok && data.ok) {
      checkSession();
    } else {
      let msg = data.error || 'Login failed.';
      if (email.includes('@stepsafe.local') || email.includes('@example.com')) {
        msg += ' (If demo accounts are not seeded yet, run "flask seed-demo" or "flask bootstrap-admin" in terminal.)';
      }
      errBox.textContent = msg;
      errBox.style.display = 'block';
    }
  } catch (err) {
    errBox.textContent = 'Network error during login.';
    errBox.style.display = 'block';
  }
};

window.quickLogin = async (email, password) => {
  $('loginEmail').value = email;
  $('loginPassword').value = password;
  $('loginForm').dispatchEvent(new Event('submit'));
};

$('logoutBtn').onclick = async () => {
  await fetch('/auth/logout', { method: 'POST' });
  currentUser = null;
  checkSession();
};

// =========================================================================
// In-App Notification Center
// =========================================================================

$('notifBell').onclick = () => {
  const d = $('notifDrawer');
  d.style.display = d.style.display === 'block' ? 'none' : 'block';
};

document.addEventListener('click', e => {
  if (!$('notifWrapper').contains(e.target)) {
    $('notifDrawer').style.display = 'none';
  }
});

async function pollNotifications() {
  if (!currentUser) return;
  try {
    const res = await fetch('/api/notifications');
    if (!res.ok) return;
    const data = await res.json();
    const badge = $('notifBadge');
    if (data.unread_count > 0) {
      badge.textContent = data.unread_count;
      badge.style.display = 'flex';
    } else {
      badge.style.display = 'none';
    }

    const list = $('notifList');
    if (!data.notifications || !data.notifications.length) {
      list.innerHTML = '<div class="notif-empty">No notifications yet.</div>';
      return;
    }

    list.innerHTML = data.notifications.map(n => `
      <div class="notif-item ${n.status === 'unread' ? 'unread' : ''}">
        <div class="notif-meta">
          <span class="notif-tag ${n.type}">${n.type.replace('_', ' ')}</span>
          <span>${new Date(n.sent_at).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})}</span>
        </div>
        <div class="notif-title">${n.title}</div>
        <div class="notif-body">${n.message}</div>
        <div class="notif-actions">
          ${n.status === 'unread' ? `<button class="btn-text" onclick="markRead(${n.id})">Mark read</button>` : ''}
          ${n.device_id ? `<button class="btn-text" onclick="inspectDevice('${n.device_id}')">View telemetry &rarr;</button>` : ''}
        </div>
      </div>
    `).join('');
  } catch (err) {
    console.debug("Notification poll error:", err);
  }
}

window.markRead = async id => {
  await fetch(`/api/notifications/${id}/read`, { method: 'POST' });
  pollNotifications();
};

$('markAllRead').onclick = async () => {
  await fetch('/api/notifications/read-all', { method: 'POST' });
  pollNotifications();
};

window.inspectDevice = deviceId => {
  activeDeviceId = deviceId;
  $('activeDeviceLabel').textContent = deviceId;
  $('notifDrawer').style.display = 'none';
  switchView('telemetry');
};

// =========================================================================
// Caregiver Patients View
// =========================================================================

async function loadCaregiverPatients() {
  switchView('caregiver');
  const grid = $('patientCardsGrid');
  grid.innerHTML = '<div class="loading-state">Loading assigned patients…</div>';

  try {
    const res = await fetch('/api/patients');
    const data = await res.json();
    if (!res.ok || !data.patients.length) {
      grid.innerHTML = '<div class="notif-empty">No patients assigned to your care yet. Contact administrator.</div>';
      return;
    }

    grid.innerHTML = data.patients.map(p => `
      <div class="patient-card" onclick="openPatientDashboard(${p.id}, '${p.device_id || ''}', '${p.full_name}')">
        <div class="patient-card-header">
          <div>
            <h3>${p.full_name}</h3>
            <span class="patient-card-details">${p.email}</span>
          </div>
          <span class="notif-tag ${p.active_event_type || 'SYSTEM'}">${p.active_event_type ? p.active_event_type.replace('_', ' ') : 'NORMAL'}</span>
        </div>
        <div class="patient-card-details">
          <div><strong>Device:</strong> ${p.device_name || p.device_id || 'Not Assigned'}</div>
          <div><strong>Emergency:</strong> ${p.emergency_contact || 'None listed'}</div>
        </div>
        <button class="btn-primary btn-sm full-width">View Live Dashboard &rarr;</button>
      </div>
    `).join('');
  } catch (err) {
    grid.innerHTML = '<div class="form-error">Error loading assigned patients.</div>';
  }
}

window.openPatientDashboard = (patientId, deviceId, patientName) => {
  activePatientId = patientId;
  if (deviceId) activeDeviceId = deviceId;
  $('activeDeviceLabel').textContent = deviceId || 'No Device Assigned';
  $('activePatientLabel').textContent = `(Patient: ${patientName})`;
  $('backToPatientsBtn').style.display = 'inline-block';
  switchView('telemetry');
  // Explicitly ensure Caregiver cannot calibrate assigned patient
  $('calibrationPanel').style.display = 'none';
  $('calibrate').style.display = 'none';
  $('calibrate').disabled = true;
};

$('backToPatientsBtn').onclick = () => {
  loadCaregiverPatients();
};

// =========================================================================
// Patient Profile & Preferences View
// =========================================================================

async function loadPatientProfile() {
  switchView('profile');
  const res = await fetch('/api/me');
  if (!res.ok) return;
  const data = await res.json();
  $('profName').value = data.user.full_name || '';
  $('profEmail').value = data.user.email || '';
  if (data.patient_profile) {
    $('profDob').value = data.patient_profile.date_of_birth || '';
    $('profEmergency').value = data.patient_profile.emergency_contact || '';
    try {
      const prefs = JSON.parse(data.patient_profile.alert_preferences || '{}');
      $('prefInApp').checked = prefs.in_app !== false;
      $('prefPush').checked = prefs.push !== false;
    } catch (_) {}
  }
}

$('profileForm').onsubmit = async e => {
  e.preventDefault();
  const feedback = $('profFeedback');
  feedback.style.display = 'none';

  const payload = {
    full_name: $('profName').value.trim(),
    date_of_birth: $('profDob').value,
    emergency_contact: $('profEmergency').value.trim(),
    alert_preferences: {
      in_app: $('prefInApp').checked,
      push: $('prefPush').checked
    }
  };

  const res = await fetch('/api/me/profile', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });

  if (res.ok) {
    feedback.textContent = 'Profile updated successfully!';
    feedback.style.display = 'block';
    setTimeout(() => { feedback.style.display = 'none'; }, 3000);
  }
};

// =========================================================================
// Admin Console (Users, Devices, Assignments)
// =========================================================================

async function loadAdminConsole() {
  switchView('admin');
  loadAdminUsers();
  loadAdminDevices();
  loadAdminAssignments();
}

window.switchAdminSubTab = tabId => {
  document.querySelectorAll('.admin-tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.admin-subpanel').forEach(p => p.style.display = 'none');
  $(tabId).style.display = 'block';
  event.target.classList.add('active');
};

async function loadAdminUsers() {
  const tbody = $('adminUsersTable');
  const res = await fetch('/api/admin/users');
  if (!res.ok) return;
  const data = await res.json();

  tbody.innerHTML = data.users.map(u => `
    <tr>
      <td>${u.id}</td>
      <td><strong>${u.full_name}</strong></td>
      <td>${u.email}</td>
      <td><span class="role-pill ${u.role}">${u.role}</span></td>
      <td>${u.assigned_device_name ? `${u.assigned_device_name} (${u.assigned_device_id})` : '—'}</td>
      <td>${u.active ? '<span style="color:#65e0c6">Active</span>' : '<span style="color:#ff7777">Inactive</span>'}</td>
      <td>
        <button class="btn-secondary btn-sm" onclick="toggleUserActive(${u.id})">
          ${u.active ? 'Deactivate' : 'Activate'}
        </button>
      </td>
    </tr>
  `).join('');
}

window.toggleUserActive = async userId => {
  await fetch(`/api/admin/users/${userId}/toggle-active`, { method: 'POST' });
  loadAdminUsers();
};

async function loadAdminDevices() {
  const tbody = $('adminDevicesTable');
  const res = await fetch('/api/admin/devices');
  const userRes = await fetch('/api/admin/users');
  if (!res.ok || !userRes.ok) return;
  const devData = await res.json();
  const userData = await userRes.json();
  const patients = userData.users.filter(u => u.role === 'patient');

  tbody.innerHTML = devData.devices.map(d => `
    <tr>
      <td><code>${d.device_id}</code></td>
      <td><strong>${d.display_name}</strong></td>
      <td>${d.patient_name ? `${d.patient_name} (${d.patient_email})` : '<em style="color:#8fa9ae">Unassigned</em>'}</td>
      <td>${d.active ? '<span style="color:#65e0c6">Active</span>' : '<span style="color:#ff7777">Inactive</span>'}</td>
      <td>
        <select onchange="assignDevice('${d.device_id}', this.value)">
          <option value="">Unassign</option>
          ${patients.map(p => `
            <option value="${p.id}" ${d.patient_user_id === p.id ? 'selected' : ''}>
              ${p.full_name}
            </option>
          `).join('')}
        </select>
      </td>
    </tr>
  `).join('');
}

window.assignDevice = async (deviceId, patientUserId) => {
  await fetch('/api/admin/assign-device', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ device_id: deviceId, patient_user_id: patientUserId ? parseInt(patientUserId) : null })
  });
  loadAdminDevices();
  loadAdminUsers();
};

async function loadAdminAssignments() {
  const tbody = $('adminAssignmentsTable');
  const res = await fetch('/api/admin/assignments');
  const userRes = await fetch('/api/admin/users');
  if (!res.ok || !userRes.ok) return;
  const assignData = await res.json();
  const userData = await userRes.json();

  // Populate dropdowns
  const caregivers = userData.users.filter(u => u.role === 'caregiver' && u.active);
  const patients = userData.users.filter(u => u.role === 'patient' && u.active);

  $('assignCaregiverSelect').innerHTML = '<option value="">Select Caregiver…</option>' +
    caregivers.map(c => `<option value="${c.id}">${c.full_name} (${c.email})</option>`).join('');
  $('assignPatientSelect').innerHTML = '<option value="">Select Patient…</option>' +
    patients.map(p => `<option value="${p.id}">${p.full_name} (${p.email})</option>`).join('');

  tbody.innerHTML = assignData.assignments.length ? assignData.assignments.map(a => `
    <tr>
      <td><strong>${a.caregiver_name}</strong> (${a.caregiver_email})</td>
      <td><strong>${a.patient_name}</strong> (${a.patient_email})</td>
      <td>${new Date(a.assigned_at).toLocaleDateString()}</td>
      <td>
        <button class="btn-secondary btn-sm" onclick="unassignCaregiver(${a.caregiver_id}, ${a.patient_id})">
          Unassign
        </button>
      </td>
    </tr>
  `).join('') : '<tr><td colspan="4">No active caregiver-to-patient assignments.</td></tr>';
}

$('assignCaregiverForm').onsubmit = async e => {
  e.preventDefault();
  const caregiver_user_id = parseInt($('assignCaregiverSelect').value);
  const patient_user_id = parseInt($('assignPatientSelect').value);
  await fetch('/api/admin/assign-caregiver', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ caregiver_user_id, patient_user_id })
  });
  loadAdminAssignments();
};

window.unassignCaregiver = async (cgId, patId) => {
  await fetch('/api/admin/unassign-caregiver', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ caregiver_user_id: cgId, patient_user_id: patId })
  });
  loadAdminAssignments();
};

// Modal helpers
window.closeModal = id => { $(id).style.display = 'none'; };
$('openCreateUserModal').onclick = () => { $('createUserModal').style.display = 'flex'; };
$('openCreateDeviceModal').onclick = () => { $('createDeviceModal').style.display = 'flex'; };

$('createUserForm').onsubmit = async e => {
  e.preventDefault();
  const payload = {
    full_name: $('newUserName').value.trim(),
    email: $('newUserEmail').value.trim().toLowerCase(),
    password: $('newUserPassword').value,
    role: $('newUserRole').value
  };
  const res = await fetch('/api/admin/users', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  if (res.ok) {
    closeModal('createUserModal');
    $('createUserForm').reset();
    loadAdminUsers();
  } else {
    const d = await res.json();
    $('createUserError').textContent = d.error || 'Failed to create user.';
    $('createUserError').style.display = 'block';
  }
};

$('createDeviceForm').onsubmit = async e => {
  e.preventDefault();
  const payload = {
    device_id: $('newDeviceId').value.trim(),
    display_name: $('newDeviceName').value.trim()
  };
  const res = await fetch('/api/admin/devices', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  if (res.ok) {
    closeModal('createDeviceModal');
    $('createDeviceForm').reset();
    loadAdminDevices();
  } else {
    const d = await res.json();
    $('createDeviceError').textContent = d.error || 'Failed to register device.';
    $('createDeviceError').style.display = 'block';
  }
};

// =========================================================================
// Calibration Button Handler
// =========================================================================

$('calibrate').onclick = async () => {
  if (!currentUser || currentUser.role !== 'patient') {
    alert('Access denied: Only authenticated patients can calibrate their assigned device.');
    return;
  }
  const b = $('calibrate');
  b.disabled = true;
  b.textContent = 'Starting calibration...';
  try {
    const res = await fetch('/api/calibrate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ device_id: activeDeviceId })
    });
    const data = await res.json();
    if (!res.ok) {
      alert(data.error || 'Failed to start calibration.');
      b.disabled = false;
      b.textContent = 'Start calibration';
    } else {
      $('calibrationText').textContent = data.message || 'Calibration started...';
      if ($('calibProgressWrap')) $('calibProgressWrap').style.display = 'block';
      if ($('calibStatusBadge')) $('calibStatusBadge').style.display = 'inline-block';
      if ($('calibProgressBar')) $('calibProgressBar').style.width = '0%';
      if ($('calibProgressText')) $('calibProgressText').textContent = '0 / 20 readings (0%)';
      b.textContent = 'Calibrating (0/20)...';
      b.classList.add('is-calibrating');
      await refreshTelemetry();
    }
  } catch (err) {
    alert('Network error while starting calibration.');
    b.disabled = false;
    b.textContent = 'Start calibration';
  }
};

// Initialization
checkSession();
refreshTelemetry();
refreshTimer = setInterval(refreshTelemetry, 600);
