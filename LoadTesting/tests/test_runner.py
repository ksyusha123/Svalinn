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

    def test_adaptive_options(self):
        a = self.parse("--strategy", "Adaptive", "--adaptive-initial-limit", "8", "--adaptive-max-limit", "32")
        self.assertEqual(runner.adaptive_settings(a)["InitialLimit"], 8)
        self.assertEqual(runner.adaptive_settings(a)["MaxLimit"], 32)
        for args in [("--adaptive-min-limit", "10"), ("--adaptive-max-limit", "2"),
                     ("--adaptive-update-interval-ms", "6000")]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.parse("--strategy", "Adaptive", *args)

    def test_adaptive_resume_rejects_changed_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            previous = {"runner": "python-v1", "strategy": "Adaptive", "adaptive": {"MaxLimit": 32}}
            write_json(directory / "manifest.json", previous)
            with self.assertRaisesRegex(ValueError, "adaptive"):
                runner.check_resume(directory, dict(previous, adaptive={"MaxLimit": 64}))

    def test_cinnamon_options_and_ablation(self):
        a = self.parse("--strategy", "Cinnamon", "--cinnamon-initial-limit", "8", "--cinnamon-disable-pi")
        values = runner.cinnamon_settings(a)
        self.assertEqual(values["InitialLimit"], 8)
        self.assertFalse(values["EnablePi"])
        self.assertTrue(values["EnableAutoTuner"])
        for args in [("--cinnamon-min-limit", "20"), ("--cinnamon-max-limit", "2"),
                     ("--cinnamon-pi-interval-ms", "3000"), ("--cinnamon-latency-min-samples", "5000"),
                     ("--cinnamon-reference-reset-interval-ms", "10")]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.parse("--strategy", "Cinnamon", *args)

    def test_cinnamon_resume_rejects_changed_controller(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            previous = {"runner": "python-v1", "strategy": "Cinnamon", "cinnamon": {"EnablePi": True}}
            write_json(directory / "manifest.json", previous)
            with self.assertRaisesRegex(ValueError, "cinnamon"):
                runner.check_resume(directory, dict(previous, cinnamon={"EnablePi": False}))

    def test_cinnamon_metrics_use_default_group_and_preserve_missing_values(self):
        text = '# TYPE svalinn_cinnamon_limit gauge\n'
        text += 'svalinn_cinnamon_limit{group="other"} 99\n'
        text += 'svalinn_cinnamon_limit{group="default"} 8\n'
        text += 'svalinn_cinnamon_pi_error{group="default"} -0.25\n'
        values = runner.parse_cinnamon_metrics(text)
        self.assertEqual(values["cinnamonLimit"], 8)
        self.assertEqual(values["cinnamonPiError"], -.25)
        self.assertIsNone(values["cinnamonQueued"])

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
