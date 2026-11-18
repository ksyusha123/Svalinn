import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'LoadTesting'))
import run_experiment as runner
from runner_common import stamp, read_json, write_json

started = time.monotonic()
output = ROOT / 'LoadTesting/results' / ('short-cpu-' + stamp())
cal = output / 'calibration'
cal.mkdir(parents=True)
config = runner.validate(runner.parser().parse_args([
    '--output-directory', str(cal), '--warmup-requests', '20']))
runner.calibrate(config)
capacity = read_json(cal / 'calibration.json')['estimatedSingleCpuCapacityRps']
levels = sorted({max(1, round(capacity * x)) for x in (.6, 2)})
write_json(output / 'plan.json', dict(capacityRps=capacity, rates=levels,
    durationSeconds=60, repetitions=1, serverCpu=0, generatorCpu=2,
    cases=['off', 'Adaptive', 'Cinnamon']))
print('PLAN', output, levels, flush=True)
for name, strategy, mode in [('off', 'Adaptive', 'off'),
                             ('Adaptive', 'Adaptive', 'on'),
                             ('Cinnamon', 'Cinnamon', 'on')]:
    print('CASE', name, flush=True)
    runner.run(runner.parser().parse_args([
        '--scenario', 'SuccessDisaster', '--workload', 'Cpu',
        '--strategy', strategy, '--modes', mode, '--rates', ','.join(map(str, levels)),
        '--duration-seconds', '60', '--repetitions', '1',
        '--warmup-requests', '20', '--skip-build', '--analyze',
        '--output-directory', str(output / name)]))
write_json(output / 'completed.json', dict(elapsedSeconds=time.monotonic()-started))
print('COMPLETE', output, 'seconds', time.monotonic()-started, flush=True)
