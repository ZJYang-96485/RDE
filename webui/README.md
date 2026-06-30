# RDE EChem Automation Web UI

## What it does

This app controls an automated RDE electrochemistry workflow from a Flask web interface.

The web UI is organized around three main functions:

1. **Motor Control**
   - Manual RDE RPM control
   - Manual rotation control
   - Manual X/Y/Z motion control
   - Home axes command

2. **Sample Run Plan**
   - Create and save reusable sample run plans
   - Define sample name, X/Y/Z position, RPM, stabilization time, rotation command, EChem protocol, and rinse behavior
   - Start, monitor, and abort automated runs

3. **EChem Protocol Builder**
   - Create reusable electrochemistry protocols directly from the web app
   - Start from a blank protocol
   - Add, remove, reorder, duplicate, and edit EChem steps
   - Supports OCP, CA, CA range, CA staircase, CV, LSV, and EIS
   - Save protocols to `webui/protocols/`
   - Saved protocols appear in each sample's EChem Protocol dropdown

The app supports both mock testing and real Gamry/ToolkitPy execution.

## Current hardware mapping

The COM ports are configured in `webui/config.json`, not hardcoded in `app.py`.

Default configuration:

| Device | Port |
|---|---|
| RDE RPM controller | `COM10` |
| Rotation controller | `COM7` |
| Linear / Z axis | `COM9` |
| Horizontal / X axis | `COM4` |
| Vertical / Y axis | `COM5` |

Serial baud rate:

```text
115200
```

RPM range:

| Setting | Value |
|---|---:|
| Minimum RPM | `30` |
| Maximum RPM | `12000` |
| Stop RPM | `20` |

For software-only development, `config.json` can enable mock serial mode:

```json
"hardware": {
  "mock_serial": true
}
```

When enabled, serial commands return mock ACK responses instead of opening real COM ports.

## Project structure

```text
RDE/
├── arduino/
│   ├── linearmovement/
│   ├── rotation/
│   └── rpminput/
└── webui/
    ├── app.py
    ├── config.json
    ├── requirements.txt
    ├── README.md
    ├── templates/
    │   └── index.html
    ├── static/
    │   └── styles.css
    ├── hardware/
    │   ├── gamry_client.py
    │   ├── motion_controller.py
    │   ├── rde_controller.py
    │   ├── rinse_controller.py
    │   ├── rotation_controller.py
    │   └── serial_base.py
    ├── workflow/
    │   ├── config_loader.py
    │   ├── data_manager.py
    │   ├── protocol_loader.py
    │   ├── recipe_runner.py
    │   ├── run_plan_loader.py
    │   ├── safety.py
    │   └── state.py
    ├── gamry_worker/
    │   ├── worker.py
    │   ├── run_ocp.py
    │   ├── run_ca.py
    │   ├── run_cv.py
    │   ├── run_lsv.py
    │   └── run_eis.py
    ├── protocols/
    ├── run_plans/
    └── output/runs/
```

## Run

Open terminal in `webui`:

```powershell
cd "C:\Users\zyang\Downloads\RDE data\RDE\webui"
```

Install dependencies:

```powershell
& "$env:LOCALAPPDATA\miniforge3\python.exe" -m pip install -r requirements.txt
```

Start the server:

```powershell
& "$env:LOCALAPPDATA\miniforge3\python.exe" .\app.py
```

Open browser:

```text
http://127.0.0.1:5055
```

The default port is `5055`. You can override it by setting the `PORT` environment variable before starting the app.

Compile-check the main files:

```powershell
& "$env:LOCALAPPDATA\miniforge3\python.exe" -m py_compile .\app.py
& "$env:LOCALAPPDATA\miniforge3\python.exe" -m py_compile .\workflow\protocol_loader.py
& "$env:LOCALAPPDATA\miniforge3\python.exe" -m py_compile .\workflow\recipe_runner.py
```

Compile-check the real Gamry worker files with the Gamry 32-bit Python:

```powershell
& "C:\Program Files (x86)\Gamry Instruments\Python\Python37-32\python.exe" -m py_compile .\gamry_worker\worker.py
& "C:\Program Files (x86)\Gamry Instruments\Python\Python37-32\python.exe" -m py_compile .\gamry_worker\run_ocp.py
& "C:\Program Files (x86)\Gamry Instruments\Python\Python37-32\python.exe" -m py_compile .\gamry_worker\run_ca.py
& "C:\Program Files (x86)\Gamry Instruments\Python\Python37-32\python.exe" -m py_compile .\gamry_worker\run_cv.py
& "C:\Program Files (x86)\Gamry Instruments\Python\Python37-32\python.exe" -m py_compile .\gamry_worker\run_lsv.py
& "C:\Program Files (x86)\Gamry Instruments\Python\Python37-32\python.exe" -m py_compile .\gamry_worker\run_eis.py
```

## How the workflow works

A full automated experiment uses two separate JSON layers.

### 1. Sample run plan

Run plans are stored in:

```text
webui/run_plans/
```

A run plan defines physical/sample information:

- sample name
- X/Y/Z position
- RPM
- stabilization time
- rotation command
- selected EChem protocol
- post-EChem wait time
- whether to rinse after the sample

Example files:

```text
single_sample_test.json
multi_sample_test.json
```

Each sample in a run plan selects one EChem protocol by name.

### 2. EChem protocol

Protocols are stored in:

```text
webui/protocols/
```

A protocol defines the electrochemical sequence. Supported real techniques are:

```text
ocp
ca
ca_staircase
cv
lsv
eis
```

The web UI also supports compact protocol-builder blocks such as `ca_range`. These are expanded by `workflow/protocol_loader.py` into normal executable CA steps before the run starts.

Example protocol files:

```text
ocp_only.json
lsv_orr.json
ca_steps_backward.json
bulk_electrode_ca_steps_with_backward_compact.json
```

## EChem Protocol Builder

The EChem Protocol Builder is a flexible `.GSequence`-like builder inside the web app.

It starts blank. Users can add steps such as:

- OCP
- EIS
- CA
- CA Range
- LSV
- CV

For each step, users can adjust parameters such as:

- duration
- sample period
- output file name
- voltage
- voltage range
- scan rate
- EIS frequency range
- EIS AC voltage
- estimated impedance
- points per decade

Users can also:

- move steps up/down
- duplicate steps
- delete steps
- preview the generated protocol
- save the protocol
- load/edit saved protocols

A compact CA range block such as:

```json
{
  "name": "ca_forward",
  "type": "ca_range",
  "direction_label": "forward",
  "start_voltage_v": -0.1,
  "end_voltage_v": -1.6,
  "step_voltage_v": -0.1,
  "duration_s": 300,
  "sample_period_s": 1,
  "output_prefix": "CA_forward",
  "area_cm2": 1
}
```

is expanded into individual CA steps such as:

```text
CA_forward_m0p1V.DTA
CA_forward_m0p2V.DTA
...
CA_forward_m1p6V.DTA
```

This allows users to create protocols similar to Gamry `.GSequence` files without manually writing dozens of JSON steps.

## Example GSequence-like protocol

A typical bulk-electrode CA steps protocol can be built as:

```text
1. OCP before
2. EIS before
3. OCP between
4. CA forward range: -0.1 V to -1.6 V
5. CA backward range: -1.6 V to -0.1 V
6. OCP after
7. EIS after
```

For initial testing, use short durations first:

```text
OCP: 5 s
CA range step duration: 3 s
EIS: 10 kHz to 1 kHz
```

Do not start a full 300-second-per-step protocol until the saved protocol has been previewed and the output paths look correct.

## Rinse behavior

Rinse is done by moving the electrode to a water beaker position and spinning.

The rinse position is configured in `config.json`:

```json
"rinse": {
  "enabled": true,
  "position": {
    "x": 120000,
    "y": 60000,
    "z": 50000
  },
  "rpm": 1000,
  "duration_s": 10,
  "rotation_command": "",
  "return_to_safe_z_after": true
}
```

The current rinse sequence is:

```text
move to safe Z
move to rinse beaker X/Y
lower to rinse Z
spin at rinse RPM
stop RDE
lift back to safe Z
```

## Gamry mode

The app supports two Gamry modes:

```json
"gamry": {
  "mode": "mock"
}
```

or:

```json
"gamry": {
  "mode": "real"
}
```

### Mock mode

In mock mode, the app generates fake `.DTA` files for testing the workflow without the real potentiostat.

Use mock mode for:

- UI development
- run-plan testing
- file-output testing
- motion/RDE workflow tests without electrochemistry

### Real mode

In real mode, the Flask app launches the worker script:

```text
gamry_worker/worker.py
```

The worker should be run using the Gamry-provided 32-bit Python with ToolkitPy installed.

Example `config.json` Gamry section:

```json
"gamry": {
  "mode": "real",
  "worker_python": "C:\\Program Files (x86)\\Gamry Instruments\\Python\\Python37-32\\python.exe",
  "worker_script": "gamry_worker/worker.py",
  "instrument_index": 0,
  "instrument_label": "IFC1010-36030",
  "default_file_extension": ".DTA"
}
```

The worker is launched with:

```text
--job <job.json> --result <result.json>
```

The worker reads the job JSON, runs the requested technique, creates every file listed in `outputs`, and writes a result JSON.

Successful result example:

```json
{
  "ok": true,
  "mode": "real",
  "technique": "ocp",
  "result": {
    "ok": true,
    "technique": "ocp",
    "points": 20,
    "pstat": "IFC1010-36030"
  }
}
```

If the worker writes `{"ok": false, "error": "..."}` or exits non-zero, the app stops automation and records the error in the run manifest.

## Real Gamry support status

The real Gamry backend has been tested with ToolkitPy for:

| Technique | Status |
|---|---|
| OCP | Working |
| CA | Working |
| CA staircase / CA range | Working through expanded CA steps |
| LSV | Working |
| CV | Working |
| EIS | Working |

For real EChem testing, close Gamry Framework or Instrument Manager before running direct ToolkitPy worker tests if the instrument is locked.

## Output files

Experiment outputs are saved in:

```text
webui/output/runs/
```

Each run creates a timestamped folder containing:

```text
run_plan.json
manifest.json
log.txt
protocol_snapshots/
_jobs/
samples/
```

Mock or real Gamry `.DTA` files are saved inside the corresponding sample folder.

Example sample output folder:

```text
webui/output/runs/20260630T191016Z_default/
└── samples/
    └── 001_sample_001_Sample_1/
        ├── OCP_before.DTA
        ├── EIS_before.DTA
        ├── CA_forward_m0p1V.DTA
        ├── CA_forward_m0p2V.DTA
        └── ...
```

## Recommended protocol cleanup

Keep reusable protocols in:

```text
webui/protocols/
```

Archive one-off test protocols in:

```text
webui/archived_protocols/
```

Suggested reusable protocols to keep:

```text
ocp_only.json
lsv_orr.json
ca_steps_backward.json
bulk_electrode_ca_steps_with_backward_compact.json
full_real_gamry_smoke_test.json
```

Temporary smoke-test protocols such as single-technique OCP/CA/CV/LSV/EIS tests can be moved to `archived_protocols/` after validation.

## Safety notes

- Keep Arduino sketches flashed before using real hardware.
- Check and update COM ports in `config.json` before running with hardware.
- Use `mock_serial: true` for software-only testing.
- Do not start full automation without confirming X/Y/Z positions are safe.
- Do not run a full 300-second-per-step EChem sequence as the first test.
- Use short OCP/CA/EIS timings for smoke tests.
- Empty JSON files should not be kept inside `protocols/` or `run_plans/`.
- `__init__.py` files can be empty.
- Stop or abort the automation before manually moving hardware.
