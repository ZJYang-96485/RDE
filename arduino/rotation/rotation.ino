#include <Arduino.h>

const uint8_t PUL_PIN = 2;  // TB6600 PUL
const uint8_t DIR_PIN = 3;  // TB6600 DIR
const uint8_t ENA_PIN = 4;  // TB6600 ENA

const bool CW_LEVEL = HIGH;
const bool CCW_LEVEL = LOW;

// Preserve the previous speed at launch, then accelerate moderately.
const unsigned int START_STEP_PULSE_US = 2000;
const unsigned int CRUISE_STEP_PULSE_US = 1500;
const unsigned int ACCELERATION_STEPS = 200;

const unsigned int MOTOR_FULL_STEPS_PER_REV = 200;
const unsigned int MICROSTEP = 8;  // Match this to TB6600 DIP microstep setting.
const unsigned int HALF_TURN_STEPS = (MOTOR_FULL_STEPS_PER_REV * MICROSTEP) / 2;

unsigned int positionStepsFromHome = 0;
String emergencyBuffer;


void pulseOnce(unsigned int pulseUs) {
  digitalWrite(PUL_PIN, HIGH);
  delayMicroseconds(pulseUs);
  digitalWrite(PUL_PIN, LOW);
  delayMicroseconds(pulseUs);
}


bool isStopCommand(String command) {
  command.trim();
  return command.equalsIgnoreCase("STOP")
      || command.equalsIgnoreCase("ABORT")
      || command.equalsIgnoreCase("CANCEL");
}


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


unsigned int rampedPulseWidthFor(
  unsigned int executedSteps,
  unsigned int requestedSteps
) {
  if (requestedSteps < 3U) {
    return START_STEP_PULSE_US;
  }

  unsigned int rampSteps = requestedSteps / 2U;
  if (rampSteps > ACCELERATION_STEPS) {
    rampSteps = ACCELERATION_STEPS;
  }

  const unsigned int stepsFromEnd = requestedSteps - 1U - executedSteps;
  const unsigned int stepsFromEdge = executedSteps < stepsFromEnd
      ? executedSteps
      : stepsFromEnd;

  if (stepsFromEdge >= rampSteps) {
    return CRUISE_STEP_PULSE_US;
  }

  const unsigned long pulseReduction =
      (unsigned long)(START_STEP_PULSE_US - CRUISE_STEP_PULSE_US)
      * stepsFromEdge
      / rampSteps;

  return START_STEP_PULSE_US - (unsigned int)pulseReduction;
}


unsigned int stepManyInterruptible(
  unsigned int requestedSteps,
  bool directionLevel,
  bool &stopped
) {
  digitalWrite(DIR_PIN, directionLevel);
  delayMicroseconds(50);

  stopped = false;
  unsigned int executedSteps = 0;

  while (executedSteps < requestedSteps) {
    if (emergencyStopRequested()) {
      stopped = true;
      break;
    }

    pulseOnce(rampedPulseWidthFor(executedSteps, requestedSteps));
    executedSteps++;
  }

  digitalWrite(PUL_PIN, LOW);
  return executedSteps;
}


void discardPendingCommands() {
  while (Serial.available()) {
    Serial.read();
  }
}


const char *positionLabel() {
  if (positionStepsFromHome == 0U) {
    return "HOME";
  }
  if (positionStepsFromHome == HALF_TURN_STEPS) {
    return "CCW";
  }
  return "PARTIAL";
}


void printStatus() {
  Serial.print("STATUS ");
  Serial.print(positionLabel());
  Serial.print(" POSITION ");
  Serial.print(positionStepsFromHome);
  Serial.print("/");
  Serial.println(HALF_TURN_STEPS);
}


void runToCcw() {
  if (positionStepsFromHome >= HALF_TURN_STEPS) {
    Serial.println("Already at 180 deg CCW position");
    return;
  }

  const unsigned int requestedSteps = HALF_TURN_STEPS - positionStepsFromHome;
  bool stopped = false;
  const unsigned int executedSteps = stepManyInterruptible(
      requestedSteps,
      CCW_LEVEL,
      stopped
  );
  positionStepsFromHome += executedSteps;
  discardPendingCommands();

  if (stopped) {
    Serial.print("ACK STOP ");
    Serial.print(executedSteps);
    Serial.print("/");
    Serial.print(requestedSteps);
    Serial.print(" POSITION ");
    Serial.println(positionStepsFromHome);
    return;
  }

  Serial.println("Moved 180 deg CCW");
}


void runToHome() {
  if (positionStepsFromHome == 0U) {
    Serial.println("Already at home");
    return;
  }

  const unsigned int requestedSteps = positionStepsFromHome;
  bool stopped = false;
  const unsigned int executedSteps = stepManyInterruptible(
      requestedSteps,
      CW_LEVEL,
      stopped
  );
  positionStepsFromHome -= executedSteps;
  discardPendingCommands();

  if (stopped) {
    Serial.print("ACK STOP ");
    Serial.print(executedSteps);
    Serial.print("/");
    Serial.print(requestedSteps);
    Serial.print(" POSITION ");
    Serial.println(positionStepsFromHome);
    return;
  }

  Serial.println("Returned to home");
}


void setup() {
  pinMode(PUL_PIN, OUTPUT);
  pinMode(DIR_PIN, OUTPUT);
  pinMode(ENA_PIN, OUTPUT);

  digitalWrite(PUL_PIN, LOW);
  digitalWrite(DIR_PIN, CW_LEVEL);
  digitalWrite(ENA_PIN, LOW);  // TB6600 enable is commonly active LOW.

  Serial.begin(115200);
  Serial.setTimeout(100);
  delay(500);
  Serial.println("rotation ready");
  Serial.println("Commands: 1, 0, STOP, PING, STATUS, HELP");
  Serial.print("Configured HALF_TURN_STEPS: ");
  Serial.println(HALF_TURN_STEPS);
}


void loop() {
  if (!Serial.available()) {
    return;
  }

  String line = Serial.readStringUntil('\n');
  line.trim();

  if (line.equalsIgnoreCase("PING")) {
    Serial.println("ACK PONG Rotation");
  } else if (line.equalsIgnoreCase("STATUS")) {
    printStatus();
  } else if (line.equalsIgnoreCase("HELP") || line == "?") {
    Serial.println("Rotation commands: 1, 0, STOP, PING, STATUS, HELP");
  } else if (isStopCommand(line)) {
    Serial.println("ACK STOP IDLE");
  } else if (line == "1") {
    runToCcw();
  } else if (line == "0") {
    runToHome();
  } else if (line.length() > 0) {
    Serial.println("ERR rotation command must be 1, 0, STOP, PING, STATUS, or HELP");
  }
}
