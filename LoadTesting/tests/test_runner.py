"""Regression checks for validation, interruption, locking and artifact recovery."""

import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_experiment as runner
from runner_common import Child, directory_lock, read_json, sha256, write_json


class ValidationTests(unittest.TestCase):
    def parse(self, *args):
        with patch.object(runner, "check_masks"):
            return runner.validate(runner.parser().parse_args(args))

    def test_delay_defaults_and_override(self):
        a = self.parse("--workload", "Delay")
        self.assertEqual((a.slo_ms, a.high_slo_ms), (1200, 2500))
        a = self.parse("--workload", "Delay", "--slo-ms", "700", "--high-slo-ms", "900")
        self.assertEqual((a.slo_ms, a.high_slo_ms), (700, 900))

    def test_incompatible_scenario_arguments(self):
        for args in [
            ("--rates", "20"),
            ("--scenario", "SuccessDisaster", "--low-rates", "20"),
            ("--scenario", "SuccessDisaster", "--critical-rate", "5"),
        ]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.parse(*args)

    def test_rates_and_thresholds(self):
        self.assertEqual(runner.rates("0,10"), [0, 10])
        for value in ("1,1", "-1", "", "1,no"):
            with self.assertRaises(argparse.ArgumentTypeError):
                runner.rates(value)
        with self.assertRaises(ValueError):
            self.parse("--scenario", "SuccessDisaster", "--rates", "0")
        with self.assertRaises(ValueError):
            self.parse("--cpu-recovery-percent", "90")

    def test_resume_requires_directory(self):
        with self.assertRaises(ValueError):
            self.parse("--resume")


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runner test space ")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.current = {
            "runner": "python-v1",
            "strategy": "Inflight",
            "loadLevels": [20],
            "binarySha256": "ABC",
        }
        write_json(self.path / "manifest.json", self.current)

    def test_atomic_json_supports_bom_and_unicode(self):
        write_json(self.path / "data.json", {"путь": "тест"})
        self.assertEqual(read_json(self.path / "data.json"), {"путь": "тест"})
        self.assertFalse((self.path / "data.json.tmp").exists())
        (self.path / "bom.json").write_text('\ufeff{"a": 1}', encoding="utf-8")
        self.assertEqual(read_json(self.path / "bom.json"), {"a": 1})

    def test_preparation_can_resume_but_missing_provenance_cannot(self):
        runner.check_resume(self.path, self.current)
        write_json(self.path / "calibration.json", {})
        with self.assertRaisesRegex(ValueError, "source-hashes"):
            runner.check_resume(self.path, self.current)

    def test_old_runner_and_changed_parameters_rejected(self):
        write_json(self.path / "manifest.json", {"strategy": "Inflight"})
        with self.assertRaisesRegex(ValueError, "PowerShell"):
            runner.check_resume(self.path, self.current)
        write_json(self.path / "manifest.json", self.current)
        with self.assertRaisesRegex(ValueError, "strategy"):
            runner.check_resume(self.path, dict(self.current, strategy="Cpu"))

    def test_source_changes_rejected(self):
        source = self.path / "model.js"
        source.write_text("original", encoding="utf-8")
        write_json(
            self.path / "source-hashes.json",
            [{"Path": "model.js", "Sha256": sha256(source)}],
        )
        with patch.object(runner, "ROOT", self.path):
            runner.check_resume(self.path, self.current)
            source.write_text("changed", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "model.js"):
                runner.check_resume(self.path, self.current)

    def test_snapshot_recovers_incomplete_preparation(self):
        (self.path / "source.txt").write_text("data", encoding="utf-8")
        with (
            patch.object(runner, "ROOT", self.path),
            patch.object(runner, "source_files", return_value=["source.txt"]),
        ):
            prefix = runner.snapshot(self.path, self.current, True)
        self.assertTrue(prefix.startswith("resume-"))
        self.assertTrue((self.path / "experiment-source.zip").exists())
        self.assertEqual(read_json(self.path / "manifest.json"), self.current)
        self.assertEqual(
            read_json(self.path / "source-hashes.json")[0]["Path"], "source.txt"
        )

    @unittest.skipUnless(sys.platform == "win32", "Windows locking")
    def test_lock_prevents_concurrent_writers_and_releases(self):
        with (
            directory_lock(self.path),
            self.assertRaises(RuntimeError),
            directory_lock(self.path),
        ):
            self.fail("Second writer acquired lock")
        with directory_lock(self.path):
            pass

    def test_child_stops_on_keyboard_interrupt_and_paths_with_spaces(self):
        with (
            self.assertRaises(KeyboardInterrupt),
            Child(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                self.path,
                "child with space",
            ) as child,
        ):
            raise KeyboardInterrupt
        self.assertIsNotNone(child.process.poll())
        self.assertTrue(all(stream.closed for stream in child.logs))


if __name__ == "__main__":
    unittest.main()
