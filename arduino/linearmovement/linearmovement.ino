const uint8_t PUL_PIN = 2;  // TB6600 PUL
const uint8_t DIR_PIN = 3;  // TB6600 DIR
const uint8_t ENA_PIN = 4;  // TB6600 ENA

const bool CW_LEVEL = HIGH;
const bool CCW_LEVEL = LOW;
const unsigned int BASE_STEP_PULSE_US = 800;

uint8_t speedMultiplierFor(unsigned long stepsAbs) {
  if (stepsAbs <= 100UL) {
    return 1;
  }
  if (stepsAbs <= 1000UL) {
    return 2;
  }
  if (stepsAbs <= 10000UL) {
    return 5;
  }
  return 10;
}

void pulseOnce(unsigned int pulseUs) {
  digitalWrite(PUL_PIN, HIGH);
  delayMicroseconds(pulseUs);
  digitalWrite(PUL_PIN, LOW);
  delayMicroseconds(pulseUs);
}

void stepMany(bool dirLevel, unsigned long count, unsigned int pulseUs) {
  digitalWrite(DIR_PIN, dirLevel);
  for (unsigned long i = 0; i < count; i++) {
    pulseOnce(pulseUs);
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
  Serial.println("linearmovement ready");
  Serial.println("Send signed step count:");
  Serial.println("  x  -> CW by x steps");
  Serial.println(" -x  -> CCW by x steps");
  Serial.println("  0  -> no move");
}

void loop() {
  if (!Serial.available()) {
    return;
  }

  String line = Serial.readStringUntil('\n');
  line.trim();

  if (line.length() == 0) {
    return;
  }

  long steps = line.toInt();
  // Reject non-numeric input while allowing 0, +0, and -0.
  if (steps == 0 && line != "0" && line != "+0" && line != "-0") {
    Serial.println("ERR: send integer like 120 or -120");
    return;
  }

  if (steps > 0) {
    unsigned long stepsAbs = (unsigned long)steps;
    uint8_t mult = speedMultiplierFor(stepsAbs);
    unsigned int pulseUs = BASE_STEP_PULSE_US / mult;
    if (pulseUs < 50U) {
      pulseUs = 50U;
    }

    stepMany(CW_LEVEL, stepsAbs, pulseUs);
    Serial.print("ACK CW ");
    Serial.println(steps);
  } else if (steps < 0) {
    unsigned long count = (unsigned long)(-steps);
    uint8_t mult = speedMultiplierFor(count);
    unsigned int pulseUs = BASE_STEP_PULSE_US / mult;
    if (pulseUs < 50U) {
      pulseUs = 50U;
    }

    stepMany(CCW_LEVEL, count, pulseUs);
    Serial.print("ACK CCW ");
    Serial.println(count);
  } else {
    Serial.println("ACK 0");
  }
}
