from __future__ import annotations

import argparse
from pathlib import Path

from workflow.data_manager import export_run_dta_to_csv, output_root, write_run_summary


def run_directories(root: Path) -> list[Path]:
    if any(path.is_file() and path.suffix.lower() == ".dta" for path in root.rglob("*")):
        # A named run directory contains its DTA files below sample folders.
        if (root / "_system" / "manifest.json").is_file():
            return [root]
    return sorted(
        (
            path
            for path in root.iterdir()
            if path.is_dir()
            and any(item.is_file() and item.suffix.lower() == ".dta" for item in path.rglob("*"))
        ),
        key=lambda path: path.name.casefold(),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert every final Gamry DTA table to a same-named CSV file."
    )
    parser.add_argument(
        "path",
        nargs="?",
        help="Run directory or runs root (default: configured output/runs directory).",
    )
    args = parser.parse_args()

    root = Path(args.path).expanduser().resolve() if args.path else output_root().resolve()
    if not root.is_dir():
        parser.error(f"directory does not exist: {root}")

    converted = 0
    failures = 0
    runs = run_directories(root)
    for run_dir in runs:
        report = export_run_dta_to_csv(run_dir)
        write_run_summary(run_dir)
        converted += int(report["converted_count"])
        failures += int(report["error_count"])
        print(
            f"{run_dir.name}: {report['converted_count']}/{report['dta_count']} converted; "
            f"{report['error_count']} failed"
        )

    print(f"Finished: {converted} CSV file(s) created across {len(runs)} run(s); {failures} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
