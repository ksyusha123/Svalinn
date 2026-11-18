import sys
import time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'LoadTesting'))
import run_experiment as runner
from runner_common import stamp, write_json

output = ROOT / 'LoadTesting/results' / ('cinnamon-diagnosis-' + stamp())
print('OUTPUT', output, flush=True)
started = time.monotonic()
cases = [
    ('off', ['--modes', 'off']),
    ('default', ['--modes', 'on']),
    ('fixed4', ['--modes', 'on', '--cinnamon-disable-pi', '--cinnamon-disable-auto-tuner',
                '--cinnamon-initial-limit', '4', '--cinnamon-max-limit', '4']),
    ('fixed1', ['--modes', 'on', '--cinnamon-disable-pi', '--cinnamon-disable-auto-tuner',
                '--cinnamon-initial-limit', '1', '--cinnamon-max-limit', '1']),
]
for name, extra in cases:
    print('CASE', name, flush=True)
    runner.run(runner.parser().parse_args([
        '--scenario', 'SuccessDisaster', '--workload', 'Cpu', '--strategy', 'Cinnamon',
        '--rates', '22', '--duration-seconds', '30', '--repetitions', '1',
        '--skip-build', '--analyze', '--output-directory', str(output / name),
        *extra]))
write_json(output / 'completed.json', {'elapsedSeconds': time.monotonic()-started})
print('COMPLETE', output, flush=True)
