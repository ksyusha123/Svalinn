"""Scientific protocol invariants and rejection of misleading measurements."""

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plotting"))
import run_experiment as runner
from analyze_profiles import analyze, outcome, recovery, validate_run
from load_profiles import phases
from run_adaptive_suite import case_arguments, plan, summarize


class ProfileTests(unittest.TestCase):
    def parse(self, *args):
        with patch.object(runner, "check_masks"):
            return runner.validate(runner.parser().parse_args(args))

    def test_overload_recovery_is_one_contiguous_schedule(self):
        a = self.parse(
            "--scenario",
            "SuccessDisaster",
            "--profile",
            "OverloadRecovery",
            "--rates",
            "60",
        )
        p = phases(a, 60)
        self.assertEqual([x["startSeconds"] for x in p], [0, 30, 150])
        self.assertEqual([sum(x["rates"].values()) for x in p], [20, 60, 20])
        self.assertEqual(p[0]["rates"], p[2]["rates"])

    def test_low_flood_spike_does_not_raise_critical(self):
        a = self.parse(
            "--profile", "Spike", "--low-rates", "60", "--critical-rate", "3"
        )
        p = phases(a, 60)
        self.assertEqual([x["rates"]["critical"] for x in p], [3, 3, 3])
        self.assertEqual([x["durationSeconds"] for x in p], [30, 10, 90])

    def test_dependency_changes_only_service_cost(self):
        a = self.parse(
            "--workload",
            "Io",
            "--scenario",
            "SuccessDisaster",
            "--profile",
            "DependencySlowdown",
            "--rates",
            "28",
        )
        p = phases(a, 28)
        self.assertEqual([x["ioServiceMs"] for x in p], [150, 200, 150])
        self.assertTrue(all(x["rates"] == p[0]["rates"] for x in p))

    def test_composition_changes_priorities_not_total_work(self):
        a = self.parse("--scenario", "SuccessDisaster", "--profile", "CompositionShift")
        p = phases(a, 80)
        self.assertEqual([sum(x["rates"].values()) for x in p], [80, 80, 80])
        self.assertEqual([x["rates"]["critical"] for x in p], [20, 48, 20])

    def test_rejects_confounding_or_invalid_protocols(self):
        for args in [
            ["--profile", "Spike", "--low-rates", "20"],
            ["--profile", "DependencySlowdown"],
            ["--profile", "CompositionShift"],
            [
                "--profile",
                "DependencySlowdown",
                "--workload",
                "Io",
                "--degraded-io-service-ms",
                "1000",
            ],
            [
                "--profile",
                "DependencySlowdown",
                "--workload",
                "Io",
                "--degraded-io-service-ms",
                "50",
            ],
        ]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.parse(*args)

    def test_reduced_matrix_has_shared_controls_and_agreed_scope(self):
        matrix = plan(32)
        for case in matrix["cases"]:
            a = self.parse(*case_arguments(case, matrix, Path("results/test")))
            self.assertEqual(a.modes, [case["mode"]])
            for load in case["levels"]:
                self.assertTrue(phases(a, load))
        self.assertEqual({c["ablation"] for c in matrix["cases"]}, {None})
        self.assertEqual({c["scenario"] for c in matrix["cases"]}, {"SuccessDisaster"})
        self.assertEqual((matrix["runCount"], matrix["loadSeconds"]), (144, 18960))
        self.assertEqual(
            (
                matrix["baselineSeconds"],
                matrix["durationSeconds"],
                matrix["recoverySeconds"],
            ),
            (30, 120, 90),
        )
        controls = [c for c in matrix["cases"] if c["mode"] == "off"]
        self.assertEqual(len(controls), 8)
        for case in matrix["cases"]:
            control = next(
                c for c in controls if c["controlCase"] == case["controlCase"]
            )
            for key in ("workload", "profile", "levels", "baseline"):
                self.assertEqual(case[key], control[key])
        self.assertTrue(
            all(
                c["workload"] == "Io"
                for c in matrix["cases"]
                if c["profile"] == "CompositionShift"
            )
        )
        self.assertTrue(
            all(
                len(c["levels"]) == 3
                for c in matrix["cases"]
                if c["profile"] == "Plateau"
            )
        )
        self.assertTrue(all(c["workload"] in ("Cpu", "Io") for c in matrix["cases"]))

    def test_single_modes_keep_counterbalancing_for_paired_runs(self):
        self.assertEqual(runner.ordered_modes(["off", "on"], 2), ["on", "off"])
        self.assertEqual(runner.ordered_modes(["off"], 2), ["off"])
        self.assertEqual(runner.ordered_modes(["on"], 1), ["on"])

    def test_adaptive_metrics_missing_is_not_zero(self):
        values = runner.parse_adaptive_metrics(
            "svalinn_adaptive_limit 7\nsvalinn_adaptive_inflight 0\n"
        )
        self.assertEqual(values["adaptiveLimit"], 7)
        self.assertEqual(values["adaptiveInflight"], 0)
        self.assertIsNone(values["adaptiveReferenceLatencyMs"])

    def test_cinnamon_experiment_starts_at_io_capacity_with_bounded_queue(self):
        a = self.parse("--strategy", "Cinnamon", "--workload", "Io")
        values = runner.cinnamon_settings(a)
        self.assertEqual(values["InitialLimit"], a.io_slots)
        self.assertEqual(values["QueueCapacity"], 16)
        self.assertEqual(values["QueueTimeoutMs"], 500)
        self.assertTrue(values["EnablePi"] and values["EnableAutoTuner"])
        self.assertEqual((a.slo_ms, a.high_slo_ms), (1000, 1000))
        self.assertEqual(a.slo_definition, "SuccessfulLatency")
        self.assertIsNone(a.critical_success_target_percent)

    def test_success_target_is_explicit_and_saved_in_matrix_policy(self):
        a = self.parse(
            "--critical-success-target-percent",
            "99",
            "--slo-definition",
            "TimelySuccessRatio",
        )
        self.assertEqual(a.critical_success_target_percent, 99)
        self.assertEqual(plan(32)["evaluation"]["latencyPercentile"], 90)
        self.assertEqual(plan(32)["cinnamonStart"]["QueueTimeoutMs"], 500)


class AnalysisTests(unittest.TestCase):
    def test_missing_paired_run_invalidates_series(self):
        exp, metrics = self.fixture()
        exp.update(
            schemaVersion=2,
            mode="off",
            strategy="Cinnamon",
            repetition=1,
            profile="Plateau",
            workload="io",
            scenario="SuccessDisaster",
        )
        exp["phases"][0].update(
            name="plateau", startSeconds=0, durationSeconds=10, ioServiceMs=100
        )
        for i, window in enumerate(exp["windows"]):
            window.update(startSeconds=i * 5, durationSeconds=5)
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / "manifest.json").write_text(
                json.dumps(
                    {
                        "scenario": "SuccessDisaster",
                        "repetitions": 1,
                        "loadLevels": [20],
                    }
                )
            )
            (directory / "r1-rate20-off.json").write_text(
                json.dumps({"experiment": exp, "metrics": metrics})
            )
            (directory / "r1-rate20-off.run.json").write_text("{}")
            (directory / "r1-rate20-off.cpu.csv").write_text("elapsedSeconds\n")
            with (
                patch("analyze_profiles.plot_run"),
                patch("analyze_profiles.plot_controllers"),
            ):
                self.assertEqual(analyze(directory), 1)
            self.assertIn(
                "Missing run: r1-rate20-on", (directory / "report.txt").read_text()
            )
            manifest = directory / "manifest.json"
            settings = json.loads(manifest.read_text())
            settings["modes"] = ["off"]
            manifest.write_text(json.dumps(settings))
            with (
                patch("analyze_profiles.plot_run"),
                patch("analyze_profiles.plot_controllers"),
            ):
                self.assertEqual(analyze(directory), 0)

    def test_matrix_summary_preserves_case_and_rejects_empty_results(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            (directory / "case-one").mkdir()
            for filename in ("comparison.csv", "runs.csv"):
                (directory / "case-one" / filename).write_text("mode,valid\non,True\n")
            summarize(directory, ["case-one"])
            self.assertIn(
                "case-one,,on,True",
                (directory / "suite-runs.csv").read_text(encoding="utf-8-sig"),
            )
            (directory / "case-one" / "runs.csv").write_text("")
            with self.assertRaises(ValueError):
                summarize(directory, ["case-one"])

    def fixture(self):
        exp = {
            "streams": [
                {
                    "name": "critical_io_p0",
                    "phase": 0,
                    "priority": "critical",
                    "rate": 5,
                    "durationSeconds": 10,
                }
            ],
            "phases": [{"index": 0, "rates": {"critical": 5}}],
            "rates": {"critical": 5},
            "windows": [{"index": 0, "phase": 0}, {"index": 1, "phase": 0}],
        }
        metrics = {
            key: {"values": {"count": count}}
            for key, count in [
                ("critical_io_p0_requests", 50),
                ("phase_0_critical_requests", 50),
                ("window_0_critical_requests", 25),
                ("window_1_critical_requests", 25),
                ("critical_requests", 50),
            ]
        }
        return exp, metrics

    def test_validates_each_stream_and_window_attribution(self):
        exp, metrics = self.fixture()
        self.assertEqual(validate_run(exp, metrics), [])
        metrics["critical_io_p0_requests"]["values"]["count"] = 40
        self.assertTrue(any("arrival count" in r for r in validate_run(exp, metrics)))

    def test_missing_window_or_dropped_iterations_invalidates_run(self):
        for key in ("window_0_critical_requests", "dropped_iterations"):
            exp, metrics = self.fixture()
            metrics[key] = {"values": {"count": 1}}
            self.assertTrue(validate_run(exp, metrics))

    def test_all_boundary_requests_must_still_be_attributed(self):
        exp, metrics = self.fixture()
        for key in (
            "critical_io_p0_requests",
            "phase_0_critical_requests",
            "critical_requests",
        ):
            metrics[key]["values"]["count"] += 1
        self.assertTrue(validate_run(exp, metrics))
        metrics["window_1_critical_requests"]["values"]["count"] += 1
        self.assertEqual(validate_run(exp, metrics), [])

    def test_goodput_keeps_errors_and_slo_misses_in_denominator(self):
        metrics = {
            "x_requests": {"values": {"count": 20}},
            "x_good": {"values": {"count": 5}},
        }
        result = outcome(metrics, "x", 5)
        self.assertEqual((result["goodput_rps"], result["slo_misses"]), (1, 15))
        self.assertTrue(math.isnan(result["p95_ms"]))
        self.assertTrue(math.isnan(result["p90_ms"]))

    def test_p90_is_read_directly_and_never_inferred_from_p95(self):
        metrics = {"x_success_latency_ms": {"values": {"p(90)": 800, "p(95)": 1200}}}
        self.assertEqual(outcome(metrics, "x", 5)["p90_ms"], 800)
        del metrics["x_success_latency_ms"]["values"]["p(90)"]
        self.assertTrue(math.isnan(outcome(metrics, "x", 5)["p90_ms"]))

    def test_recovery_requires_three_full_consecutive_populated_windows(self):
        rows = [
            {
                "phase": "recovery",
                "priority": "critical",
                "start_seconds": 60 + 5 * i,
                "duration_seconds": 5,
                "requests": 25,
                "success_pct": 100,
                "slo_pct": 100,
            }
            for i in range(5)
        ]
        self.assertEqual(recovery(rows, "critical"), 15)
        rows[1]["slo_pct"] = 80
        self.assertEqual(recovery(rows, "critical"), 25)
        rows[3]["requests"] = 0
        self.assertIsNone(recovery(rows, "critical"))
        self.assertIsNone(recovery(rows, "high"))

    def test_new_recovery_separates_latency_from_success_and_joint_goal(self):
        rows = [
            {
                "phase": "recovery",
                "priority": "critical",
                "start_seconds": 5 * i,
                "duration_seconds": 5,
                "requests": 100,
                "success_pct": 20,
                "slo_pct": 20,
                "p90_ms": 500,
            }
            for i in range(3)
        ]
        policy = {
            "latencyPercentile": 90,
            "sloMs": 1000,
            "highSloMs": 1000,
            "sloDefinition": "SuccessfulLatency",
            "criticalSuccessTargetPercent": None,
        }
        self.assertEqual(recovery(rows, "critical", policy=policy), 15)
        policy["criticalSuccessTargetPercent"] = 99
        self.assertIsNone(recovery(rows, "critical", policy=policy))
        policy.update(
            criticalSuccessTargetPercent=None, sloDefinition="TimelySuccessRatio"
        )
        self.assertIsNone(recovery(rows, "critical", policy=policy))
        policy["sloDefinition"] = "SuccessfulLatency"
        rows[1]["p90_ms"] = 1000
        self.assertIsNone(recovery(rows, "critical", policy=policy))


if __name__ == "__main__":
    unittest.main()
