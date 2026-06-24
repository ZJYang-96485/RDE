# RDE EChem Automation Web UI

## What it does

This app controls an automated RDE electrochemistry workflow from a Flask web interface.

Main functions:

* Manual RDE RPM control
* Manual rotation control
* Manual X/Y/Z motion control
* Saved sample run plans
* Selectable EChem protocols for each sample
* Optional rinse after each sample by moving to a water beaker position and spinning
* Mock Gamry mode for testing EChem output without the real potentiostat connection

## Current hardware mapping

The COM ports are configured in `webui/config.json`, not hardcoded in `app.py`.

Default configuration:

* RDE RPM controller: `COM10`
* Rotation controller: `COM7`
* Linear / Z axis: `COM9`
* Horizontal / X axis: `COM4`
* Vertical / Y axis: `COM5`
* Baud rate: `115200`

RPM range:

* Minimum RPM: `30`
* Maximum RPM: `12000`
* Stop RPM: `20`

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
    ├── templates/
    │   └── index.html
    ├── static/
    │   └── styles.css
    ├── hardware/
    ├── workflow/
    ├── gamry_worker/
    ├── protocols/
    ├── run_plans/
    └── output/runs/
```

## Run

Open terminal in `webui`:

```bash
cd webui
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Start the server:

```bash
python app.py
```

Open browser:

```text
http://127.0.0.1:5000
```

## How the workflow works

A full automated experiment uses two separate JSON layers.

### 1. Sample run plan

Run plans are stored in:

```text
webui/run_plans/
```

A run plan defines physical/sample information:

* sample name
* X/Y/Z position
* RPM
* stabilization time
* selected EChem protocol
* whether to rinse after the sample

Example:

```text
single_sample_test.json
multi_sample_test.json
```

### 2. EChem protocol

Protocols are stored in:

```text
webui/protocols/
```

A protocol defines the electrochemical sequence:

* OCP
* CA
* CA staircase
* CV
* LSV
* EIS

Example:

```text
ca_steps_backward.json
ocp_only.json
lsv_orr.json
```

Each sample in a run plan selects one protocol.

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

The app currently supports mock Gamry mode:

```json
"gamry": {
  "mode": "mock"
}
```

In mock mode, the app generates fake `.DTA` output files for testing the workflow.

Real Gamry control is not implemented yet. Later, real ToolkitPy/Gamry code can be added inside:

```text
webui/gamry_worker/run_ocp.py
webui/gamry_worker/run_ca.py
webui/gamry_worker/run_cv.py
webui/gamry_worker/run_lsv.py
webui/gamry_worker/run_eis.py
```

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

## Notes

* Keep Arduino sketches flashed before using real hardware.
* Check and update COM ports in `config.json` before running with hardware.
* Do not start full automation without confirming the X/Y/Z positions are safe.
* Empty JSON files should not be kept inside `protocols/` or `run_plans/`.
* `__init__.py` files can be empty.
