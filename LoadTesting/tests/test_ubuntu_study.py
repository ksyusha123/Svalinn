"""Ubuntu protocol planning and capacity selection without a remote server."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('ubuntu_study', Path(__file__).resolve().parents[1] / 'ubuntu/study.py')
study = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(study)


class UbuntuStudyTests(unittest.TestCase):
    def args(self, *extra):
        return study.parser().parse_args(['matrix', '--output-directory', 'unused', *extra])

    def test_matrix_covers_resources_profiles_and_queue_pair(self):
        a = self.args()
        cases = study.matrix_plan(a, dict(Cpu=20, Io=25, Mixed=30))
        self.assertEqual(len(cases), 360)
        self.assertEqual(cases, study.matrix_plan(a, dict(Cpu=20, Io=25, Mixed=30)))
        self.assertEqual({c['workload'] for c in cases}, {'Cpu', 'Io', 'Mixed'})
        for c in cases:
            self.assertIn(c['variant'], study.VARIANTS)
            self.assertLessEqual(c['rate'], 60)
            if c['profile'] == 'DependencySlowdown': self.assertEqual(c['workload'], 'Io')

    def test_adaptive_pair_differs_only_by_queue(self):
        a = self.args()
        base = dict(workload='Io', profile='Plateau', rate=25, repetition=1)
        p = [study.options(a, dict(base, variant=v), Path('same'))
             for v in ('AdaptiveNoQueue', 'AdaptiveQueue')]
        different = [(x,y) for x,y in zip(*p) if x != y]
        self.assertEqual(different, [('0', '4')])
        self.assertIn('--generator-affinity-mask', p[0])
        self.assertEqual(p[0][p[0].index('--generator-affinity-mask')+1], '2')

    def test_two_gib_matrix_requires_lower_rate_if_capacity_too_high(self):
        with self.assertRaises(ValueError):
            study.matrix_plan(self.args(), dict(Cpu=40, Io=25, Mixed=30))

    def test_capacity_requires_all_repeats_and_upper_failure(self):
        cases = [dict(workload=w, rate=rate) for w in ('Cpu','Io','Mixed')
                 for rate in (10,20,30) for _ in range(3)]
        def results(folder):
            case = cases[int(folder.name.split('-')[1])]
            return [dict(requests=100, success_pct=100 if case['rate']<=20 else 80,
                         timeout_pct=0, p90_ms=200)]
        with patch.object(study, 'outcomes', side_effect=results):
            self.assertEqual(study.baseline_capacity(Path('data'), cases)['capacities'], dict(Cpu=20,Io=20,Mixed=20))
        with patch.object(study, 'outcomes', return_value=[dict(requests=100,success_pct=100,timeout_pct=0,p90_ms=200)]):
            with self.assertRaisesRegex(ValueError, 'верхняя граница'):
                study.baseline_capacity(Path('data'), cases)

    def test_invalid_generator_is_rejected_before_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            (folder/'r1-rate10-off.json').write_text(json.dumps(dict(experiment={},metrics={})))
            with patch.object(study, 'validate_run', return_value=['generator dropped iterations']):
                with self.assertRaisesRegex(ValueError, 'Невалидная нагрузка'):
                    study.outcomes(folder)

    def test_transferred_archive_without_git(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(study.runner, 'ROOT', Path(tmp)):
            self.assertEqual(study.runner.source_revision(), 'source-archive')

    def test_complete_run_moves_artifacts_and_resume_skips_accepted_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'study'
            binary = Path(tmp) / 'TestApp.dll'
            binary.write_bytes(b'test assembly')
            a = study.parser().parse_args(['smoke', '--output-directory', str(root)])
            case = study.smoke_plan()[0]
            def fake_run(args, folder, min_memory):
                path = folder / 'run'
                path.mkdir()
                (path / 'result.json').write_text('{}')
            with (patch.object(study.sys, 'platform', 'linux'),
                  patch.object(study, 'DLL', binary),
                  patch.object(study.runner, 'check_masks'),
                  patch.object(study.psutil, 'virtual_memory') as memory,
                  patch.object(study.shutil, 'disk_usage') as disk,
                  patch.object(study, 'source_hashes', return_value={'a':'b'}),
                  patch.object(study, 'host_identifier', return_value='test-host'),
                  patch.object(study, 'environment', return_value={}),
                  patch.object(study, 'smoke_plan', return_value=[case]),
                  patch.object(study, 'outcomes', return_value=[dict(requests=10, success_pct=100, p90_ms=200)]),
                  patch.object(study, 'run_checked', side_effect=fake_run) as run):
                memory.return_value.available = 1024 * study.MIB
                disk.return_value.free = 4 * 1024**3
                study.execute(a)
                self.assertEqual(json.loads((root/'status.json').read_text())['state'], 'complete')
                self.assertTrue((root/'case-0000/accepted.json').exists())
                self.assertTrue((root/'case-0000/result.json').exists())
                self.assertFalse((root/'case-0000/run').exists())
                a.resume = True
                study.execute(a)
                self.assertEqual(run.call_count, 1)
                a.iterations += 1
                with self.assertRaisesRegex(ValueError, 'Изменены'):
                    study.execute(a)

    def test_smoke_rejects_fast_errors_and_missing_successes(self):
        for rows in ([], [dict(requests=10, success_pct=0, p90_ms=float('nan'))],
                     [dict(requests=10, success_pct=90, p90_ms=100)]):
            with self.assertRaises(ValueError): study.verify_smoke(rows)


if __name__ == '__main__': unittest.main()
