# Motor Web UI (COM14)

## What it does
- RPM input range: `30` to `12000`
- Duration input in seconds
- `Start` sends selected RPM to Arduino (`rpminput.ino`) via `COM14`
- Button switches to `Stop` while running
- `Stop` sends RPM `20` immediately
- After countdown ends, app also sends RPM `20`

## Run
1. Open terminal in `webui`
2. Install dependencies:
   `pip install -r requirements.txt`
3. Start server:
   `python app.py`
4. Open browser:
   `http://127.0.0.1:5000`

## Notes
- Serial settings are fixed to `COM14` and `115200` in `app.py`.
- Keep Arduino sketch flashed and board connected before pressing Start.
