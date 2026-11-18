"""Repeat Adaptive plateaus, then a 15 -> 40 -> 15 RPS step in one server."""
import argparse
import subprocess
import sys
from pathlib import Path

from runner_common import LOAD_TESTING, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-directory', type=Path, required=True)
    a = parser.parse_args()
    output = a.output_directory.resolve()
    output.mkdir(parents=True, exist_ok=False)
    phases = [
        ('plateaus', []),
        ('spike', ['--profile', 'Spike', '--rates', '40']),
    ]
    try:
        for name, args in phases:
            write_json(output / 'status.json', dict(state='running', current=name))
            print(f'START {name}', flush=True)
            subprocess.run([sys.executable, str(LOAD_TESTING / 'run_io_baseline.py'),
                            '--strategy', 'Adaptive', '--skip-build',
                            '--output-directory', str(output / name)] + args, check=True)
        write_json(output / 'status.json', dict(state='complete'))
        print(f'COMPLETE {output}', flush=True)
    except BaseException as error:
        write_json(output / 'status.json', dict(state='failed', error=str(error)))
        raise


if __name__ == '__main__':
    main()
