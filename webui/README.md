# RDE EChem Automation Web UI

## Future work: validate real RPM

- Implement an independent RPM validation device so the software can record and compare the RDE's real measured RPM against the commanded RPM.
- Until that device is implemented, RPM values and Levich outputs must continue to be identified as **commanded**, not measured.

## What it does

This app controls an automated RDE electrochemistry workflow from a Flask web interface.

## Automatic Ru and iR preparation

Every enabled EChem measurement now performs a fresh per-trial preparation: verify the configured Gamry instrument/channel, stabilize OCP, acquire and validate at least two uncompensated-resistance points, configure a fixed current range, and apply the configured conservative positive-feedback fraction to supported potentiostatic DC techniques. EIS/GEIS and current-controlled techniques never enable positive feedback. The setting is disabled and the cell is returned to a safe/off state after every success, skip, or error.

Normal Ru repeatability failures skip only that measurement and preserve diagnostics in the run manifest. Communication loss, unverifiable channel/relay state, near-rail OCP (possible reference-electrode failure), compliance/overload, and failed cleanup abort the run. Defaults are under `gamry.ru_preparation` in `config.json`.

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
   - Supports OCP, CA, CA range, CA staircase, CV, LSV, EIS, CP, constant-current charge/discharge, and GEIS
   - Save protocols to `webui/protocols/`
   - Saved protocols appear in each sample's EChem Protocol dropdown

The app supports both mock testing and real Gamry/ToolkitPy execution.

## Current hardware mapping

The COM ports are configured in `webui/config.json`, not hardcoded in `app.py`.

Default configuration:

| Device | Port |
|---|---|
| RDE RPM controller | `COM_` |
| Rotation controller | `COM_` |
| Linear / Z axis | `COM_` |
| Horizontal / X axis | `COM_` |
| Vertical / Y axis | `COM_` |

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

```json
"hardware": {
  "mock_serial": false
}
```

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
    │   ├── run_eis.py
    │   ├── run_cp.py
    │   ├── run_cc.py
    │   └── run_geis.py
    ├── protocols/
    ├── run_plans/
    └── output/runs/
```

## Run

Open terminal in `webui`:

```powershell
cd "C:\YOUR FOLDER\RDE data\RDE\webui"
```

Install dependencies: (Install Conda through BAM)

```powershell
& "$env:LOCALAPPDATA\miniforge3\python.exe" -m pip install -r requirements.txt
```

Start the server:

```powershell
& "$env:LOCALAPPDATA\miniforge3\python.exe" .\app.py
```

Start exactly one server. Do not run `app.py` while
`start_rde_automation.bat`/`server_awake.py` is already open. Duplicate servers
can compete for COM3/COM6. A listener check plus process lock now rejects the
second launch, including an older server that predates the lock.

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

Compile-check the real Gamry worker files with the Gamry 32-bit Python: (Might be different)

```powershell
& "C:\Program Files (x86)\Gamry Instruments\Python\Python37-32\python.exe" -m py_compile .\gamry_worker\worker.py
& "C:\Program Files (x86)\Gamry Instruments\Python\Python37-32\python.exe" -m py_compile .\gamry_worker\run_ocp.py
& "C:\Program Files (x86)\Gamry Instruments\Python\Python37-32\python.exe" -m py_compile .\gamry_worker\run_ca.py
& "C:\Program Files (x86)\Gamry Instruments\Python\Python37-32\python.exe" -m py_compile .\gamry_worker\run_cv.py
& "C:\Program Files (x86)\Gamry Instruments\Python\Python37-32\python.exe" -m py_compile .\gamry_worker\run_lsv.py
& "C:\Program Files (x86)\Gamry Instruments\Python\Python37-32\python.exe" -m py_compile .\gamry_worker\run_eis.py
& "C:\Program Files (x86)\Gamry Instruments\Python\Python37-32\python.exe" -m py_compile .\gamry_worker\run_cp.py
& "C:\Program Files (x86)\Gamry Instruments\Python\Python37-32\python.exe" -m py_compile .\gamry_worker\run_cc.py
& "C:\Program Files (x86)\Gamry Instruments\Python\Python37-32\python.exe" -m py_compile .\gamry_worker\run_geis.py
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
cp
cc_charge
cc_discharge
geis
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
- CP (chronopotentiometry)
- Constant-current charge
- Constant-current discharge
- GEIS (galvanostatic EIS)

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

The web screen also has a **Check Device** button. It runs a read-only ToolkitPy
probe through the configured 32-bit Python, lists attached potentiostats, and
confirms that the configured `instrument_label` or `instrument_index` is
available. The probe does not turn on the cell or start an acquisition.

The equivalent command-line probe is:

```powershell
& "C:\Program Files (x86)\Gamry Instruments\Python\Python37-32\python.exe" .\gamry_worker\worker.py --probe
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
| Levich CA RPM sweep | One continuous CA with commanded-RPM staircase and post-run analysis; connected-device validation pending |
| LSV | Working |
| CV | Working |
| EIS | Working |
| CP | Implemented from installed `Chronopotentiometry.exp`; connected-device cell test pending |
| Constant-current charge/discharge | Implemented from installed `pwr_charge.py` / `pwr_discharge.py`; connected-device cell test pending |
| GEIS | Implemented from installed `galvanostatic_eis.py`; connected-device cell test pending |

The web screen streams temporary live points for every listed technique. Real
curves are mapped from the exact acquisition fields in the installed ToolkitPy
`OcvCurve`, `ChronoCurve`, `RcvCurve`, `PwrCurve`, and `ZCurve` classes. Final
Gamry `.DTA` files remain the authoritative stored data.

`levich_rpm_sweep_ca` deliberately reuses the CA current-versus-time live
display and does not perform a live Levich fit. It commands each configured RPM,
keeps one CA/cell acquisition active across the full staircase, and runs the
Levich/Koutecky-Levich analysis only after the DTA and commanded-RPM schedule
are complete. The schedule, CSV/JSON results, and three PNG plots are registered
as one result in Current Trial History & Analysis. These outputs always identify
the RPM source as `commanded` and stabilization as `fixed delay`.

For CP, `current_a` is signed under the anodic current convention. For
`cc_charge` and `cc_discharge`, `current_a` is always a positive magnitude;
the selected technique and `working_positive` electrode wiring determine the
physical direction. Do not reuse a current or voltage cutoff from a different
cell chemistry without checking it first.

For real EChem testing, close Gamry Framework or Instrument Manager before running direct ToolkitPy worker tests if the instrument is locked.

## Output files

Experiment outputs are saved in:

```text
webui/output/runs/
```

Each run creates a timestamped folder containing:

```text
README_DATA.txt
run.log
run_summary.json
01_Sample_Name/
_system/
  manifest.json
  run_plan.json
  jobs/
  protocols/
  samples/
```

Mock or real Gamry `.DTA` files and registered post-run analysis artifacts are
saved directly inside the corresponding sample/group folder at the run root.
Worker jobs, protocol snapshots, detailed metadata, and per-sample JSON are kept
under `_system/`. Repeated runs use prefixes such as `R01_` and `R02_` to prevent
overwriting.

Example sample output folder:

```text
webui/output/runs/20260715-143000_default/
├── README_DATA.txt
├── run.log
├── run_summary.json
├── 01_Sample_1/
│   ├── 01_OCP_before.DTA
│   ├── 02_EIS_before.DTA
│   ├── 03_CA_forward_m0p1V.DTA
│   └── ...
└── _system/
    ├── manifest.json
    ├── run_plan.json
    ├── jobs/
    ├── protocols/
    └── samples/
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
