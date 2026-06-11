const int PWM_PIN    = 9;   // ESCON DI1
const int ENABLE_PIN = 8;   // ESCON DI2
const int STOP_PIN   = 7;   // ESCON DI4

void setup() {
  pinMode(PWM_PIN, OUTPUT);
  pinMode(ENABLE_PIN, OUTPUT);
  pinMode(STOP_PIN, OUTPUT);

  digitalWrite(STOP_PIN, LOW);
  digitalWrite(ENABLE_PIN, HIGH);

  analogWriteResolution(8);
  analogWrite(PWM_PIN, 25);

  Serial.begin(115200);
  delay(2000);   // give Serial monitor time
  Serial.println("Start");
  Serial.println("Enter RPM (0-12000):");
}

void loop() {
  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    line.trim();

    if (line.length() == 0) {
      return;
    }

    int rpm = line.toInt();
    // Ignore non-numeric garbage lines.
    if (rpm == 0 && line != "0") {
      return;
    }

    // limit rpm
    rpm = constrain(rpm, 0, 12000);

    // convert rpm → duty (0–1)
    float duty_f = 0.10 + (rpm / 12000.0) * 0.80;

    // convert to 0–255
    int duty = duty_f * 255.0;

    analogWrite(PWM_PIN, duty);

    Serial.print("RPM: ");
    Serial.print(rpm);
    Serial.print(" → Duty: ");
    Serial.println(duty);
  }
}
