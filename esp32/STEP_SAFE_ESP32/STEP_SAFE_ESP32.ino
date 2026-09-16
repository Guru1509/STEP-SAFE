// STEP-SAFE educational demonstration firmware.
// ESP32 SoftAP -> Mac Flask backend -> SQLite dashboard.
// Hardware: FSR GPIO34, LM35 GPIO35, buzzer GPIO25.

#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <WiFi.h>

// The ESP32 creates this local-only Wi-Fi network. No internet is required.
const char *AP_SSID = "STEP-SAFE";
const char *AP_PASSWORD = "stepsafe123";
const char *BACKEND_URL =
    "http://192.168.4.2:5000"; // Verified Mac address on STEP-SAFE.
const char *DEVICE_ID = "step-safe-esp32";

const int FSR_PIN = 34;
const int LM35_PIN = 35;
const int BUZZER_PIN = 25;

const unsigned long SEND_EVERY_MS = 1000;
const unsigned long CONFIG_EVERY_MS = 5000;
const unsigned long HIGH_RISK_AFTER_MS = 30000;

float fsrThreshold = 0.0;
float tempThreshold = 0.0;
bool calibrated = false;

unsigned long pressureStarted = 0;
unsigned long lastSend = 0;
unsigned long lastConfig = 0;

float readFSR() {
  long total = 0;
  for (int i = 0; i < 5; i++) {
    total += analogRead(FSR_PIN);
    delay(2);
  }
  return total / 5.0;
}

float readTemperatureC() {
  long total = 0;
  for (int i = 0; i < 10; i++) {
    total += analogRead(LM35_PIN);
    delay(2);
  }
  const float raw = total / 10.0;
  const float voltage = raw * (3.3 / 4095.0);
  return voltage * 100.0; // LM35: 10 mV per degree C.
}

bool startAccessPoint() {
  WiFi.mode(WIFI_AP);
  if (!WiFi.softAP(AP_SSID, AP_PASSWORD)) {
    Serial.println("Failed to start STEP-SAFE Wi-Fi.");
    return false;
  }

  Serial.println("STEP-SAFE Wi-Fi started.");
  Serial.print("ESP32 access-point IP: ");
  Serial.println(WiFi.softAPIP());
  Serial.print("Flask backend: ");
  Serial.println(BACKEND_URL);
  return true;
}

void loadConfig() {
  HTTPClient http;
  const String url =
      String(BACKEND_URL) + "/api/device-config?device_id=" + DEVICE_ID;

  http.begin(url);
  const int responseCode = http.GET();
  if (responseCode == HTTP_CODE_OK) {
    JsonDocument doc;
    const DeserializationError error = deserializeJson(doc, http.getString());
    if (!error) {
      const bool running = doc["calibration_running"] | false;
      calibrated = (doc["calibrated"] | false) && !running;
      fsrThreshold = doc["fsr_threshold"] | 0.0;
      tempThreshold = doc["temp_threshold"] | 0.0;
      if (running || !calibrated) {
        pressureStarted = 0;
        digitalWrite(BUZZER_PIN, LOW);
      }
      Serial.print("Config loaded | Calibrated: ");
      Serial.print(calibrated ? "YES" : "NO");
      Serial.print(" | Calibration running: ");
      Serial.println(running ? "YES" : "NO");
    } else {
      Serial.print("Config JSON error: ");
      Serial.println(error.c_str());
    }
  } else {
    Serial.print("Config request failed | HTTP ");
    Serial.println(responseCode);
  }
  http.end();
}

void sendReading(float fsr, float temperature) {
  HTTPClient http;
  http.begin(String(BACKEND_URL) + "/api/readings");
  http.addHeader("Content-Type", "application/json");

  JsonDocument doc;
  doc["device_id"] = DEVICE_ID;
  doc["fsr"] = fsr;
  doc["temperature"] = temperature;

  String body;
  serializeJson(doc, body);
  const int responseCode = http.POST(body);

  if (responseCode > 0) {
    Serial.print("Reading sent | HTTP ");
    Serial.println(responseCode);

    if (responseCode == HTTP_CODE_OK) {
      JsonDocument resDoc;
      const DeserializationError resErr = deserializeJson(resDoc, http.getString());
      if (!resErr) {
        const char *statusStr = resDoc["status"];
        if (statusStr && strcmp(statusStr, "CALIBRATING") == 0) {
          calibrated = false;
          pressureStarted = 0;
          digitalWrite(BUZZER_PIN, LOW);
        }
      }
    }
  } else {
    Serial.print("Reading POST failed: ");
    Serial.println(http.errorToString(responseCode));
  }
  http.end();
}

void setup() {
  Serial.begin(115200);
  delay(1000);

  Serial.println();
  Serial.println("================================");
  Serial.println("       STEP-SAFE ESP32");
  Serial.println("================================");

  analogReadResolution(12);
  pinMode(FSR_PIN, INPUT);
  pinMode(LM35_PIN, INPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, LOW);

  startAccessPoint();
  delay(1000); // Gives the Mac time to finish joining after a reset.
  loadConfig();
}

void loop() {
  const unsigned long now = millis();

  if (now - lastConfig >= CONFIG_EVERY_MS) {
    lastConfig = now;
    loadConfig();
  }

  const float fsr = readFSR();
  const float temperature = readTemperatureC();
  const bool pressureDetected = calibrated && fsr >= fsrThreshold;

  if (pressureDetected && pressureStarted == 0) {
    pressureStarted = now;
    Serial.println("Pressure detected.");
  } else if (!pressureDetected) {
    pressureStarted = 0;
  }

  const bool sustainedPressure =
      pressureStarted != 0 && now - pressureStarted >= HIGH_RISK_AFTER_MS;
  // Keep the buzzer off while the 30-second pressure timer is running.
  // It turns on only when pressure has remained high for the full duration.
  const bool highRisk = calibrated && sustainedPressure;
  digitalWrite(BUZZER_PIN, highRisk ? HIGH : LOW);

  if (now - lastSend >= SEND_EVERY_MS) {
    lastSend = now;
    Serial.print("FSR: ");
    Serial.print(fsr);
    Serial.print(" | Temperature: ");
    Serial.print(temperature);
    Serial.print(" C | Calibrated: ");
    Serial.print(calibrated ? "YES" : "NO");
    Serial.print(" | High risk: ");
    Serial.println(highRisk ? "YES" : "NO");
    sendReading(fsr, temperature);
  }

  delay(50);
}
