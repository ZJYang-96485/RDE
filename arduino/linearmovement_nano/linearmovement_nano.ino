/*
  linearmovement_nano.ino
  For Arduino Nano / Nano 33 family + TB6600 stepper driver.

  Serial command:
    120   -> move CW 120 steps
   -120   -> move CCW 120 steps
     0    -> no move
    help  -> print help

  Default pins:
    D2 -> TB6600 PUL
    D3 -> TB6600 DIR
    D4 -> TB6600 ENA
*/

const uint8_t PUL_PIN = 2;   // Nano D2 -> TB6600 PUL
const uint8_t DIR_PIN = 3;   // Nano D3 -> TB6600 DIR
const uint8_t ENA_PIN = 4;   // Nano D4 -> TB6600 ENA

// Set this to true only if you wire TB6600 as common-anode:
// TB6600 PUL+, DIR+, ENA+ tied to +5V, and Nano pins connected to PUL-, DIR-, ENA-.
// For the same direct wiring as the old UNO code, leave it false.
const bool COMMON_ANODE_WIRING = false;

const uint8_t STEP_ACTIVE_LEVEL = COMMON_ANODE_WIRING ? LOW : HIGH;
const uint8_t STEP_IDLE_LEVEL   = COMMON_ANODE_WIRING ? HIGH : LOW;

// If the motor moves in the opposite direction, swap these two values.
const uint8_t CW_LEVEL  = COMMON_ANODE_WIRING ? LOW : HIGH;
const uint8_t CCW_LEVEL = COMMON_ANODE_WIRING ? HIGH : LOW;

// Many TB6600 modules use active-low enable when ENA+ is tied to +5V.
// If your driver is disabled instead of enabled, change LOW to HIGH.
const uint8_t ENABLE_LEVEL = LOW;

const unsigned int BASE_STEP_PULSE_US = 2000;
const unsigned int MIN_STEP_PULSE_US  = 50;

uint8_t speedMultiplierFor(unsigned long stepsAbs) {
  if (stepsAbs <= 100UL) return 1;
  if (stepsAbs <= 1000UL) return 2;
  if (stepsAbs <= 10000UL) return 5;
  return 10;
}

void pulseOnce(unsigned int pulseUs) {
  digitalWrite(PUL_PIN, STEP_ACTIVE_LEVEL);
  delayMicroseconds(pulseUs);
  digitalWrite(PUL_PIN, STEP_IDLE_LEVEL);
  delayMicroseconds(pulseUs);
}

void stepMany(uint8_t dirLevel, unsigned long count, unsigned int pulseUs) {
  digitalWrite(DIR_PIN, dirLevel);
  delayMicroseconds(50);  // let DIR settle before first pulse

  for (unsigned long i = 0; i < count; i++) {
    pulseOnce(pulseUs);
  }
}

bool parseIntegerLine(const String& line, long& value) {
  char buf[32];

  if (line.length() == 0 || line.length() >= sizeof(buf)) {
    return false;
  }

  line.toCharArray(buf, sizeof(buf));

  char* endptr = nullptr;
  value = strtol(buf, &endptr, 10);

  while (*endptr == ' ' || *endptr == '\t') {
    endptr++;
  }

  return *endptr == '\0';
}

void printHelp() {
  Serial.println("linearmovement nano ready");
  Serial.println("Send signed step count:");
  Serial.println("  120   -> CW by 120 steps");
  Serial.println(" -120   -> CCW by 120 steps");
  Serial.println("  0     -> no move");
  Serial.println("  help  -> show this message");
}

void setup() {
  pinMode(PUL_PIN, OUTPUT);
  pinMode(DIR_PIN, OUTPUT);
  pinMode(ENA_PIN, OUTPUT);

  digitalWrite(PUL_PIN, STEP_IDLE_LEVEL);
  digitalWrite(DIR_PIN, CW_LEVEL);
  digitalWrite(ENA_PIN, ENABLE_LEVEL);

  Serial.begin(115200);
  Serial.setTimeout(100);
  delay(500);

  printHelp();
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

  if (line.equalsIgnoreCase("help") || line == "?") {
    printHelp();
    return;
  }

  long steps = 0;
  if (!parseIntegerLine(line, steps)) {
    Serial.println("ERR: send integer like 120 or -120");
    return;
  }

  if (steps > 0) {
    unsigned long count = (unsigned long)steps;
    uint8_t mult = speedMultiplierFor(count);
    unsigned int pulseUs = BASE_STEP_PULSE_US / mult;
    if (pulseUs < MIN_STEP_PULSE_US) pulseUs = MIN_STEP_PULSE_US;

    stepMany(CW_LEVEL, count, pulseUs);
    Serial.print("ACK CW ");
    Serial.println(count);
  } else if (steps < 0) {
    unsigned long count = (unsigned long)(-steps);
    uint8_t mult = speedMultiplierFor(count);
    unsigned int pulseUs = BASE_STEP_PULSE_US / mult;
    if (pulseUs < MIN_STEP_PULSE_US) pulseUs = MIN_STEP_PULSE_US;

    stepMany(CCW_LEVEL, count, pulseUs);
    Serial.print("ACK CCW ");
    Serial.println(count);
  } else {
    Serial.println("ACK 0");
  }
}
