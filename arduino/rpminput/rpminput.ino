#include <Arduino.h>

const int PWM_PIN    = 9;   // ESCON DI1
const int ENABLE_PIN = 8;   // ESCON DI2
const int STOP_PIN   = 7;   // ESCON DI4
const long MIN_RPM = 0;
const long MAX_RPM = 12000;
const long STOP_RPM_MAX = 20;
const int PWM_RESOLUTION_BITS = 12;
const int PWM_MAX_VALUE = (1 << PWM_RESOLUTION_BITS) - 1;
const int MIN_DUTY_VALUE = (PWM_MAX_VALUE * 10 + 50) / 100;
const int MAX_DUTY_VALUE = (PWM_MAX_VALUE * 90 + 50) / 100;

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
  // Preserve the established ESCON calibration: 10% duty is zero speed and
  // 90% duty is 12000 RPM. Twelve-bit command resolution reduces setpoint
  // quantization from roughly 59 RPM to roughly 3.7 RPM per PWM level.
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


void printStatus(const char *prefix) {
  Serial.print(prefix);
  Serial.print(" RPM ");
  Serial.print(commandedRpm);
  Serial.print(" DUTY ");
  Serial.print(commandedDuty);
  Serial.print("/");
  Serial.println(PWM_MAX_VALUE);
}


void setup() {
  pinMode(PWM_PIN, OUTPUT);
  pinMode(ENABLE_PIN, OUTPUT);
  pinMode(STOP_PIN, OUTPUT);

  digitalWrite(STOP_PIN, LOW);
  digitalWrite(ENABLE_PIN, HIGH);

  analogWriteResolution(PWM_RESOLUTION_BITS);
  applyRpm(0);

  Serial.begin(115200);
  Serial.setTimeout(100);
  delay(2000);
  Serial.println("RDE RPM controller ready");
  Serial.println("Commands: 0-12000, PING, STATUS");
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
  if (!parseIntegerLine(line, rpm)) {
    Serial.println("ERR RPM command must be an integer from 0 to 12000");
    return;
  }

  if (rpm < MIN_RPM || rpm > MAX_RPM) {
    Serial.println("ERR RPM command is outside 0 to 12000");
    return;
  }

  applyRpm(rpm);
  printStatus("ACK");
}
