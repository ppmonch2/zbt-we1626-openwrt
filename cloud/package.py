"""Create separate cloud and router archives; never reads private config or keys."""
import hashlib
from pathlib import Path
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent
OUT = ROOT.parent/'output-alice'


def main():
    OUT.mkdir(exist_ok=True)
    with zipfile.ZipFile(OUT/'Ballu-Alice-Function.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in ('skill.py', 'yandex_services.py', 'diagnostics.py'):
            archive.write(ROOT/'alice'/name, name)
    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp)/'heatbridge-alice'
        bundle.mkdir()
        hashes = []
        for name in ('heatbridge-alice', 'heatbridge-alice-worker', 'S85heatbridge-alice', 'install.sh'):
            data = (ROOT/'router'/name).read_bytes()
            path = bundle/name
            path.write_bytes(data);path.chmod(0o755)
            hashes.append(hashlib.sha256(data).hexdigest()+'  '+name)
        (bundle/'SHA256SUMS').write_text('\n'.join(hashes)+'\n')
        with tarfile.open(OUT/'Ballu-Alice-Router.tar.gz', 'w:gz') as archive:
            archive.add(bundle, arcname=bundle.name)
    lines = [hashlib.sha256(path.read_bytes()).hexdigest()+'  '+path.name
             for path in sorted(OUT.iterdir()) if path.suffix in ('.zip', '.gz')]
    (OUT/'SHA256SUMS').write_text('\n'.join(lines)+'\n')
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
