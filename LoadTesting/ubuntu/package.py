"""Create a source-only archive for transfer from Windows or Linux."""
import argparse
import hashlib
import os
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXCLUDED = {'bin', 'obj', '.venv', '__pycache__', 'results', '.ruff_cache'}
SUFFIXES = {'.cs', '.csproj', '.json', '.md', '.py', '.sh', '.js', '.toml', '.lock', '.config', '.http'}


def package(output):
    files = [ROOT / 'Svalinn.slnx']
    for directory in ('Svalinn', 'Svalinn.Tests', 'TestApp', 'LoadTesting'):
        for folder, directories, names in os.walk(ROOT / directory):
            directories[:] = [d for d in directories if d not in EXCLUDED]
            files += [Path(folder) / name for name in names
                      if Path(name).suffix.lower() in SUFFIXES or name == '.gitattributes']
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(set(files)):
            data = path.read_bytes()
            if path.suffix == '.sh': data = data.replace(b'\r\n', b'\n')
            archive.writestr('Svalinn/' + path.relative_to(ROOT).as_posix(), data)
    with output.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    print(f'{output.resolve()}\nSHA256: {digest}\nFiles: {len(set(files))}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'output/Svalinn-Ubuntu.zip')
    package(parser.parse_args().output)
