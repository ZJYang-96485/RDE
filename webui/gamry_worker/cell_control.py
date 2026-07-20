"""Direct ToolkitPy worker for the Gamry potentiostat cell relay/output.

This module is intentionally isolated from the Flask process.  Run it with
the configured 32-bit Gamry Python runtime.
"""

from __future__ import print_function

import argparse
import json
import sys
import time
from datetime import datetime, timezone


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def normalize_actual_state(value):
    """Translate ToolkitPy CELLSTATE without inventing a hardware reading."""
    name = getattr(value, "name", None)
    text = str(name or value or "").strip().upper()

    if text.endswith("CELL_ON") or text == "ON":
        return "on"
    if text.endswith("CELL_OFF") or text == "OFF":
        return "off"
    if text.endswith("CELL_MON") or text == "MON":
        return "monitor"
    if text.endswith("CELL_RELAY") or text == "RELAY":
        return "relay"
    return "unknown"


def read_actual_state(pstat):
    cell_reader = getattr(pstat, "cell", None)
    if not callable(cell_reader):
        return "unknown"

    try:
        return normalize_actual_state(cell_reader())
    except Exception:
        return "unknown"


def choose_instrument(tkp, requested_instrument):
    sections = [str(section).strip() for section in tkp.enum_sections()]
    sections = [section for section in sections if section]
    requested = str(requested_instrument or "").strip()

    if requested:
        if requested not in sections:
            raise RuntimeError(
                "Requested Gamry instrument {!r} was not found. Detected: {}".format(
                    requested,
                    ", ".join(sections) if sections else "none",
                )
            )
        return requested

    if not sections:
        raise RuntimeError("No Gamry potentiostat was detected.")

    return sections[0]


def run_command(state, duration_s=None, instrument=None):
    import toolkitpy as tkp

    if state == "on" and duration_s is not None and duration_s <= 0:
        raise ValueError("duration must be greater than 0 seconds.")

    tkp.toolkitpy_init("cell_control.py")
    pstat = None
    selected = None
    timed_on = state == "on" and duration_s is not None
    primary_error = None

    try:
        selected = choose_instrument(tkp, instrument)
        pstat = tkp.Pstat(selected)

        opener = getattr(pstat, "open", None)
        if callable(opener):
            opener()

        if state == "status":
            actual_state = read_actual_state(pstat)
            return {
                "ok": True,
                "instrument": selected,
                "requested_state": "status",
                "duration_s": None,
                "final_state": actual_state,
                "actual_state": actual_state,
                "message": "Gamry cell state readback completed.",
                "time": utc_now(),
            }

        if state == "off":
            pstat.set_cell(False)
            actual_state = read_actual_state(pstat)
            return {
                "ok": True,
                "instrument": selected,
                "requested_state": "off",
                "duration_s": None,
                "final_state": "off",
                "actual_state": actual_state,
                "message": "Gamry cell relay/output was turned OFF.",
                "time": utc_now(),
            }

        pstat.set_cell(True)

        if timed_on:
            time.sleep(float(duration_s))
            pstat.set_cell(False)
            actual_state = read_actual_state(pstat)
            return {
                "ok": True,
                "instrument": selected,
                "requested_state": "on",
                "duration_s": float(duration_s),
                "final_state": "off",
                "actual_state": actual_state,
                "message": "Cell was turned ON for {:.1f} s and then OFF.".format(
                    float(duration_s)
                ),
                "time": utc_now(),
            }

        actual_state = read_actual_state(pstat)
        return {
            "ok": True,
            "instrument": selected,
            "requested_state": "on",
            "duration_s": None,
            "final_state": "on",
            "actual_state": actual_state,
            "message": "Gamry cell relay/output was turned ON until a later OFF command.",
            "time": utc_now(),
        }

    except Exception as exc:
        primary_error = exc
        raise

    finally:
        cleanup_error = None

        if timed_on and pstat is not None:
            try:
                pstat.set_cell(False)
            except Exception as exc:
                cleanup_error = exc

        if pstat is not None:
            closer = getattr(pstat, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass

        try:
            close_toolkit = getattr(tkp, "toolkitpy_close", None)
            if callable(close_toolkit):
                close_toolkit()
        except Exception:
            pass

        if cleanup_error is not None and primary_error is None:
            raise RuntimeError(
                "Timed Cell ON completed, but the final forced OFF failed: {}".format(
                    cleanup_error
                )
            )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Control the Gamry cell relay/output through ToolkitPy."
    )
    parser.add_argument("--state", required=True, choices=["status", "on", "off"])
    parser.add_argument("--duration", type=float, default=None)
    parser.add_argument("--instrument", default=None)
    args = parser.parse_args(argv)

    if args.state != "on" and args.duration is not None:
        parser.error("--duration is valid only with --state on")
    if args.duration is not None and args.duration <= 0:
        parser.error("--duration must be greater than 0")

    return args


def main(argv=None):
    args = parse_args(argv)

    try:
        result = run_command(args.state, args.duration, args.instrument)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "instrument": str(args.instrument or "") or None,
                    "requested_state": args.state,
                    "duration_s": args.duration,
                    "final_state": "unknown",
                    "actual_state": "unknown",
                    "error": str(exc),
                    "time": utc_now(),
                },
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
