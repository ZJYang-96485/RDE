/*
  linearmovement.ino
  Z-axis controller (original linear movement board) + TB6600

  Compatible with the RDE Flask app:
    1000    -> move +1000 steps (CW)
   -1000    -> move -1000 steps (CCW)
    0       -> no movement
    STOP    -> interrupt an active movement
    ABORT   -> same as STOP
    CANCEL  -> same as STOP
    help    -> print command help

  Emergency-stop acknowledgement:
    ACK STOP <executed>/<requested>

  Default wiring:
    D2 -> TB6600 PUL
    D3 -> TB6600 DIR
    D4 -> TB6600 ENA
*/

#include <Arduino.h>
#include <limits.h>

const uint8_t PUL_PIN = 2;
const uint8_t DIR_PIN = 3;
const uint8_t ENA_PIN = 4;

// Original controller wiring and direction convention.
const uint8_t STEP_ACTIVE_LEVEL = HIGH;
const uint8_t STEP_IDLE_LEVEL = LOW;
const uint8_t CW_LEVEL = HIGH;
const uint8_t CCW_LEVEL = LOW;

// Most TB6600 modules use active-low enable.
const uint8_t ENABLE_LEVEL = LOW;

// Start each move at the previously proven Z-axis speed, then ramp to a
// moderately faster cruise speed. Each value is the HIGH or LOW half-period;
// a complete step therefore takes twice this delay plus GPIO overhead.
const unsigned int START_BASE_STEP_PULSE_US = 800;
const unsigned int CRUISE_BASE_STEP_PULSE_US = 600;
const unsigned int MIN_STEP_PULSE_US = 50;
const unsigned long ACCELERATION_STEPS = 1000UL;

String emergencyBuffer;


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


unsigned int pulseWidthFor(
  unsigned long stepsAbs,
  unsigned int basePulseUs
) {
  const uint8_t multiplier = speedMultiplierFor(stepsAbs);
  unsigned int pulseUs = basePulseUs / multiplier;

  if (pulseUs < MIN_STEP_PULSE_US) {
    pulseUs = MIN_STEP_PULSE_US;
  }

  return pulseUs;
}


unsigned int rampedPulseWidthFor(
  unsigned long executedSteps,
  unsigned long requestedSteps,
  unsigned int startPulseUs,
  unsigned int cruisePulseUs
) {
  if (requestedSteps < 3UL || startPulseUs <= cruisePulseUs) {
    return startPulseUs;
  }

  unsigned long rampSteps = requestedSteps / 2UL;
  if (rampSteps > ACCELERATION_STEPS) {
    rampSteps = ACCELERATION_STEPS;
  }

  const unsigned long stepsFromStart = executedSteps;
  const unsigned long stepsFromEnd = requestedSteps - 1UL - executedSteps;
  const unsigned long stepsFromEdge = stepsFromStart < stepsFromEnd
      ? stepsFromStart
      : stepsFromEnd;

  if (stepsFromEdge >= rampSteps) {
    return cruisePulseUs;
  }

  const unsigned long pulseReduction =
      (unsigned long)(startPulseUs - cruisePulseUs)
      * stepsFromEdge
      / rampSteps;

  return startPulseUs - (unsigned int)pulseReduction;
}


void pulseOnce(unsigned int pulseUs) {
  digitalWrite(PUL_PIN, STEP_ACTIVE_LEVEL);
  delayMicroseconds(pulseUs);

  digitalWrite(PUL_PIN, STEP_IDLE_LEVEL);
  delayMicroseconds(pulseUs);
}


bool isStopCommand(String command) {
  command.trim();

  return command.equalsIgnoreCase("STOP")
      || command.equalsIgnoreCase("ABORT")
      || command.equalsIgnoreCase("CANCEL");
}


/*
  Read emergency input without blocking.

  While the motor is moving, only STOP / ABORT / CANCEL is accepted.
  Any other complete command received during movement is rejected.
*/
bool emergencyStopRequested() {
  while (Serial.available() > 0) {
    const char c = (char)Serial.read();

    if (c == '\r') {
      continue;
    }

    if (c == '\n') {
      emergencyBuffer.trim();

      if (isStopCommand(emergencyBuffer)) {
        emergencyBuffer = "";
        return true;
      }

      if (emergencyBuffer.length() > 0) {
        Serial.print("ERR BUSY; ignored command: ");
        Serial.println(emergencyBuffer);
      }

      emergencyBuffer = "";
      continue;
    }

    if (emergencyBuffer.length() < 31) {
      emergencyBuffer += c;
    } else {
      emergencyBuffer = "";
      Serial.println("ERR emergency command too long");
    }
  }

  return false;
}


unsigned long stepManyInterruptible(
  uint8_t directionLevel,
  unsigned long requestedSteps,
  unsigned int startPulseUs,
  unsigned int cruisePulseUs,
  bool &stopped
) {
  digitalWrite(DIR_PIN, directionLevel);
  delayMicroseconds(50);

  stopped = false;
  unsigned long executedSteps = 0;

  while (executedSteps < requestedSteps) {
    if (emergencyStopRequested()) {
      stopped = true;
      break;
    }

    const unsigned int pulseUs = rampedPulseWidthFor(
        executedSteps,
        requestedSteps,
        startPulseUs,
        cruisePulseUs
    );
    pulseOnce(pulseUs);
    executedSteps++;
  }

  digitalWrite(PUL_PIN, STEP_IDLE_LEVEL);
  return executedSteps;
}


bool parseIntegerLine(const String &line, long &value) {
  char buffer[32];

  if (line.length() == 0 || line.length() >= sizeof(buffer)) {
    return false;
  }

  line.toCharArray(buffer, sizeof(buffer));

  char *endPointer = nullptr;
  value = strtol(buffer, &endPointer, 10);

  while (*endPointer == ' ' || *endPointer == '\t') {
    endPointer++;
  }

  return *endPointer == '\0';
}


void printHelp() {
  Serial.println("Z linear movement ready");
  Serial.println("Commands:");
  Serial.println("  1000   -> CW by 1000 relative steps");
  Serial.println(" -1000   -> CCW by 1000 relative steps");
  Serial.println("  0      -> no movement");
  Serial.println("  STOP   -> interrupt active movement");
  Serial.println("  ABORT  -> interrupt active movement");
  Serial.println("  help   -> show this message");
}


void runMovement(long signedSteps) {
  if (signedSteps == 0) {
    Serial.println("ACK 0");
    return;
  }

  // Avoid overflow when converting LONG_MIN to an absolute value.
  if (signedSteps == LONG_MIN) {
    Serial.println("ERR step value is too negative");
    return;
  }

  const bool clockwise = signedSteps > 0;
  const unsigned long requestedSteps = clockwise
      ? (unsigned long)signedSteps
      : (unsigned long)(-signedSteps);

  const unsigned int startPulseUs = pulseWidthFor(
      requestedSteps,
      START_BASE_STEP_PULSE_US
  );
  const unsigned int cruisePulseUs = pulseWidthFor(
      requestedSteps,
      CRUISE_BASE_STEP_PULSE_US
  );

  bool stopped = false;
  const unsigned long executedSteps = stepManyInterruptible(
      clockwise ? CW_LEVEL : CCW_LEVEL,
      requestedSteps,
      startPulseUs,
      cruisePulseUs,
      stopped
  );

  if (stopped) {
    Serial.print("ACK STOP ");
    Serial.print(executedSteps);
    Serial.print("/");
    Serial.println(requestedSteps);
    return;
  }

  Serial.print(clockwise ? "ACK CW " : "ACK CCW ");
  Serial.println(executedSteps);
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

  if (isStopCommand(line)) {
    Serial.println("ACK STOP IDLE");
    return;
  }

  long steps = 0;

  if (!parseIntegerLine(line, steps)) {
    Serial.println("ERR: send signed integer, STOP, ABORT, or help");
    return;
  }

  runMovement(steps);
}
