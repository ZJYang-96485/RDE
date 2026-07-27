#include <Arduino.h>

const int PWM_PIN    = 9;   // ESCON DI1
const int ENABLE_PIN = 8;   // ESCON DI2
const int STOP_PIN   = 7;   // ESCON DI4
const long MIN_RPM = 0;
const long MAX_RPM = 12000;
const long STOP_RPM_MAX = 20;
// Keep the ESCON signal identical to the original, proven controller sketch.
// The July 17 change to a 12-bit command path added an unnecessary variable
// while diagnosing the station. The robust parser and command ACKs below do
// not require changing the physical PWM calibration.
const int PWM_RESOLUTION_BITS = 8;
const int PWM_MAX_VALUE = 255;
const int MIN_DUTY_VALUE = 25;
const int MAX_DUTY_VALUE = 229;
const unsigned long PWM_BEFORE_ENABLE_SETTLE_MS = 250;
const unsigned long ENABLE_SETTLE_MS = 25;

long commandedRpm = 0;
int commandedDuty = MIN_DUTY_VALUE;


bool parseIntegerLine(const String &line, long &value) {
  char buffer[24];

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


int dutyForRpm(long rpm) {
  // Preserve the established ESCON calibration: 25/255 is zero speed and
  // 229/255 is 12000 RPM.
  // The application deliberately uses 20 RPM as its stop command, so keep
  // 0-20 RPM at the calibrated zero-speed duty instead of allowing creep.
  if (rpm <= STOP_RPM_MAX) {
    return MIN_DUTY_VALUE;
  }

  const long dutySpan = (long)MAX_DUTY_VALUE - MIN_DUTY_VALUE;
  return MIN_DUTY_VALUE
      + (int)((rpm * dutySpan + (MAX_RPM / 2L)) / MAX_RPM);
}


void applyRpm(long rpm) {
  commandedRpm = rpm;
  commandedDuty = dutyForRpm(rpm);
  analogWrite(PWM_PIN, commandedDuty);
}


void disableDriveAtRpm(long rpm) {
  digitalWrite(ENABLE_PIN, LOW);
  digitalWrite(STOP_PIN, LOW);
  applyRpm(rpm);
}


void startDriveAtRpm(long rpm) {
  // ESCON requires a valid 10-90% PWM signal before its drive-enable input
  // becomes active. The calibrated 25/255 stop duty is slightly below 10%,
  // so establish the requested running PWM while disabled, then enable.
  digitalWrite(ENABLE_PIN, LOW);
  digitalWrite(STOP_PIN, LOW);
  applyRpm(rpm);
  delay(PWM_BEFORE_ENABLE_SETTLE_MS);
  digitalWrite(ENABLE_PIN, HIGH);
  delay(ENABLE_SETTLE_MS);
}


void printStatus(const char *prefix) {
  Serial.print(prefix);
  Serial.print(" RPM ");
  Serial.print(commandedRpm);
  Serial.print(" DUTY ");
  Serial.print(commandedDuty);
  Serial.print("/");
  Serial.print(PWM_MAX_VALUE);
  Serial.print(" ENABLE ");
  Serial.print(digitalRead(ENABLE_PIN));
  Serial.print(" STOP ");
  Serial.println(digitalRead(STOP_PIN));
}


void setup() {
  pinMode(PWM_PIN, OUTPUT);
  pinMode(ENABLE_PIN, OUTPUT);
  pinMode(STOP_PIN, OUTPUT);

  // Keep the ESCON disabled until its PWM set-value input is valid. Enabling
  // it first can leave the controller unable to run after a power/reset cycle.
  digitalWrite(ENABLE_PIN, LOW);
  digitalWrite(STOP_PIN, LOW);
  analogWriteResolution(PWM_RESOLUTION_BITS);
  disableDriveAtRpm(0);

  Serial.begin(115200);
  Serial.setTimeout(100);
  delay(2000);
  Serial.println("RDE RPM controller ready");
  Serial.println("Commands: 0-12000, START <rpm>, PING, STATUS");
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

  if (line.equalsIgnoreCase("PING")) {
    Serial.println("ACK PONG RDE");
    return;
  }

  if (line.equalsIgnoreCase("STATUS")) {
    printStatus("STATUS");
    return;
  }

  long rpm = 0;
  bool startCommand = false;
  String rpmText = line;
  if (line.length() > 6 && line.substring(0, 6).equalsIgnoreCase("START ")) {
    startCommand = true;
    rpmText = line.substring(6);
    rpmText.trim();
  }

  if (!parseIntegerLine(rpmText, rpm)) {
    Serial.println("ERR RPM command must be an integer from 0 to 12000");
    return;
  }

  if (rpm < MIN_RPM || rpm > MAX_RPM) {
    Serial.println("ERR RPM command is outside 0 to 12000");
    return;
  }

  if (startCommand) {
    if (rpm <= STOP_RPM_MAX) {
      Serial.println("ERR START rpm must be greater than 20");
      return;
    }
    startDriveAtRpm(rpm);
    printStatus("ACK STARTED");
    return;
  }

  if (rpm <= STOP_RPM_MAX) {
    // A software stop also disables the ESCON power stage. The next run must
    // pass through START's valid-PWM-before-enable sequence.
    disableDriveAtRpm(rpm);
  } else if (digitalRead(ENABLE_PIN) == LOW) {
    startDriveAtRpm(rpm);
  } else {
    applyRpm(rpm);
  }
  printStatus("ACK");
}
