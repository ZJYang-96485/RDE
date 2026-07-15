const uint8_t PUL_PIN = 2;  // TB6600 PUL
const uint8_t DIR_PIN = 3;  // TB6600 DIR
const uint8_t ENA_PIN = 4;  // TB6600 ENA

const bool CW_LEVEL = HIGH;
const bool CCW_LEVEL = LOW;
const unsigned int STEP_PULSE_US = 2000;
const unsigned int MOTOR_FULL_STEPS_PER_REV = 200;
const unsigned int MICROSTEP = 8;  // Match this to TB6600 DIP microstep setting.
const unsigned int HALF_TURN_STEPS = (MOTOR_FULL_STEPS_PER_REV * MICROSTEP) / 2;

bool atHome = true;

void pulseOnce() {
  digitalWrite(PUL_PIN, HIGH);
  delayMicroseconds(STEP_PULSE_US);
  digitalWrite(PUL_PIN, LOW);
  delayMicroseconds(STEP_PULSE_US);
}

void stepMany(unsigned int count, bool dirLevel) {
  digitalWrite(DIR_PIN, dirLevel);
  for (unsigned int i = 0; i < count; i++) {
    pulseOnce();
  }
}

void discardPendingCommands() {
  // Drop extra button presses so they cannot execute much later. This is
  // called both before and after the intentionally blocking move.
  while (Serial.available()) {
    Serial.read();
  }
}

void setup() {
  pinMode(PUL_PIN, OUTPUT);
  pinMode(DIR_PIN, OUTPUT);
  pinMode(ENA_PIN, OUTPUT);

  digitalWrite(PUL_PIN, LOW);
  digitalWrite(DIR_PIN, CW_LEVEL);
  digitalWrite(ENA_PIN, LOW);  // TB6600 enable is commonly active LOW.

  Serial.begin(115200);
  delay(500);
  Serial.println("rotation ready");
  Serial.println("Send 1 -> 180 deg CCW");
  Serial.println("Send 0 -> return to home");
  Serial.print("Configured HALF_TURN_STEPS: ");
  Serial.println(HALF_TURN_STEPS);
}

void loop() {
  if (!Serial.available()) {
    return;
  }

  String line = Serial.readStringUntil('\n');
  line.trim();
  discardPendingCommands();

  if (line.equalsIgnoreCase("PING")) {
    Serial.println("ACK PONG Rotation");
  } else if (line.equalsIgnoreCase("STATUS")) {
    Serial.print("STATUS ");
    Serial.println(atHome ? "HOME" : "CCW");
  } else if (line.equalsIgnoreCase("HELP") || line == "?") {
    Serial.println("Rotation commands: 1, 0, PING, STATUS, HELP");
  } else if (line == "1") {
    if (atHome) {
      stepMany(HALF_TURN_STEPS, CCW_LEVEL);
      discardPendingCommands();
      atHome = false;
      Serial.println("Moved 180 deg CCW");
    } else {
      Serial.println("Already at 180 deg CCW position");
    }
  } else if (line == "0") {
    if (!atHome) {
      stepMany(HALF_TURN_STEPS, CW_LEVEL);
      discardPendingCommands();
      atHome = true;
      Serial.println("Returned to home");
    } else {
      Serial.println("Already at home");
    }
  } else if (line.length() > 0) {
    Serial.println("ERR rotation command must be 1, 0, PING, STATUS, or HELP");
  }
}
