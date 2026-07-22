from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from workflow.data_manager import export_run_dta_to_csv, save_manifest, write_run_summary
from workflow.dta_csv import DtaCsvError, convert_dta_directory, convert_dta_to_csv


REAL_OCP_DTA = (
    "TOOLKITPY\n"
    "TAG\tCORPOT\n"
    "DATE\tLABEL\t07/22/2026\tDate\n"
    "TIME\tLABEL\t17:55:16\tTime\n"
    "CURVE\tTABLE\t2\n"
    "\tPt\tT\tVf\tVm\tAch\tOver\tTemp\t\n"
    "\t#\ts\tV vs. Ref.\tV\tA\tbits\tdeg C\t\n"
    "\t0\t0.5\t0.007746052\t0.007746052\t0\t...........\t2.765213\t\n"
    "\t1\t1\t0.007746144\t0.007750903\t0\t...........\t2.7651\t\n"
)


class DtaCsvTest(unittest.TestCase):
    def test_real_gamry_curve_exports_every_column_and_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            dta = Path(tmpdir) / "005_01_OCP.DTA"
            dta.write_text(REAL_OCP_DTA, encoding="utf-8")

            result = convert_dta_to_csv(dta)
            csv_path = dta.with_suffix(".csv")
            self.assertEqual(result["row_count"], 2)
            self.assertEqual(result["column_count"], 7)
            self.assertTrue(csv_path.is_file())
            with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.reader(handle))

            self.assertEqual(
                rows[0],
                ["Pt (#)", "T (s)", "Vf (V vs. Ref.)", "Vm (V)", "Ach (A)", "Over (bits)", "Temp (deg C)"],
            )
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[2][2], "0.007746144")
            self.assertEqual(rows[2][5], "...........")

    def test_mock_dta_exports_without_units_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            dta = Path(tmpdir) / "CV.DTA"
            dta.write_text(
                "TECHNIQUE\tcv\ntime_s\tpotential_v\tcurrent_a\n0\t0.2\t3D-6\n1\t0.3\t4e-6\n",
                encoding="utf-8",
            )
            convert_dta_to_csv(dta)
            text = dta.with_suffix(".csv").read_text(encoding="utf-8-sig")
            self.assertIn("time_s,potential_v,current_a", text)
            self.assertIn("0,0.2,3E-6", text)

    def test_declared_incomplete_curve_is_not_silently_exported(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            dta = Path(tmpdir) / "OCP.DTA"
            dta.write_text(REAL_OCP_DTA.replace("TABLE\t2", "TABLE\t3"), encoding="utf-8")
            with self.assertRaisesRegex(DtaCsvError, "declared 3 rows"):
                convert_dta_to_csv(dta)
            self.assertFalse(dta.with_suffix(".csv").exists())

    def test_directory_conversion_continues_and_reports_bad_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "good.DTA").write_text(REAL_OCP_DTA, encoding="utf-8")
            (root / "bad.DTA").write_text("not a DTA table\n", encoding="utf-8")
            report = convert_dta_directory(root)
            self.assertEqual(report["dta_count"], 2)
            self.assertEqual(report["converted_count"], 1)
            self.assertEqual(report["error_count"], 1)

    def test_finished_run_registers_csv_outputs_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "run"
            sample_dir = run_dir / "01_Sample"
            sample_dir.mkdir(parents=True)
            dta = sample_dir / "OCP.DTA"
            dta.write_text(REAL_OCP_DTA, encoding="utf-8")
            save_manifest(
                run_dir,
                {
                    "run_id": "run",
                    "status": "complete",
                    "samples": [],
                    "analysis_results": [],
                    "trials": [{"outputs": [str(dta)]}],
                    "outputs": [{"outputs": [str(dta)]}],
                    "errors": [],
                },
            )

            report = export_run_dta_to_csv(run_dir)
            write_run_summary(run_dir)
            self.assertEqual(report["converted_count"], 1)
            manifest = json.loads((run_dir / "_system" / "manifest.json").read_text(encoding="utf-8"))
            summary = json.loads((run_dir / "run_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["dta_csv_exports"][0]["csv_file"], "01_Sample/OCP.csv")
            self.assertEqual(manifest["outputs"][0]["csv_outputs"], [str(dta.with_suffix(".csv"))])
            self.assertEqual(summary["csv_files"], [str(Path("01_Sample") / "OCP.csv")])


if __name__ == "__main__":
    unittest.main()
