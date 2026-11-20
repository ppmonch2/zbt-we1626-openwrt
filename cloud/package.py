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
        for name in ('skill.py', 'yandex_services.py', 'diagnostics.py', 'device_state.py', 'disk_services.py', 'notify.py', 'fast_push.py'):
            archive.write(ROOT/'alice'/name, name)
    # The installed native client previously refused temperatures above 30 C.
    # Refuse to ship an incomplete router update that would still reject 31-35 C.
    native = ROOT.parent/'output-entware/usb-root/heatbridge-entware/bin/heatbridge-syncleo'
    if not native.is_file():
        raise SystemExit('Missing cross-built MIPS heatbridge-syncleo; run entware/build.sh first.')
    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp)/'heatbridge-alice'
        bundle.mkdir()
        hashes = []
        for name in ('heatbridge-alice', 'heatbridge-alice-worker', 'heatbridge-alice-state',
                     'S85heatbridge-alice', 'install.sh', 'heatbridge-device',
                     'heatbridge-disk-lib', 'heatbridge-disk-agent', 'heatbridge-disk-batch',
                     'heatbridge-disk-control'):
            source = ROOT.parent/'entware/bin'/name if name == 'heatbridge-device' else ROOT/'router'/name
            data = source.read_bytes()
            path = bundle/name
            path.write_bytes(data);path.chmod(0o755)
            hashes.append(hashlib.sha256(data).hexdigest()+'  '+name)
        for name in ('index.html', 'app.js', 'disk.html', 'disk.js', 'cgi-bin/api', 'cgi-bin/disk-api'):
            data = (ROOT.parent/'entware/www'/name).read_bytes()
            path = bundle/'www'/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o755 if name.startswith('cgi-bin/') else 0o644)
            hashes.append(hashlib.sha256(data).hexdigest()+'  www/'+name)
        binary = native.read_bytes()
        (bundle/'heatbridge-syncleo').write_bytes(binary)
        (bundle/'heatbridge-syncleo').chmod(0o755)
        hashes.append(hashlib.sha256(binary).hexdigest()+'  heatbridge-syncleo')
        (bundle/'SHA256SUMS').write_text('\n'.join(hashes)+'\n')
        with tarfile.open(OUT/'Ballu-Alice-Router.tar.gz', 'w:gz') as archive:
            archive.add(bundle, arcname=bundle.name)
        with zipfile.ZipFile(OUT/'Ballu-Alice-Router.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(bundle.rglob('*')):
                if path.is_file():
                    archive.write(path, str(path.relative_to(Path(tmp))))
    lines = [hashlib.sha256(path.read_bytes()).hexdigest()+'  '+path.name
             for path in sorted(OUT.iterdir()) if path.suffix in ('.zip', '.gz')]
    (OUT/'SHA256SUMS').write_text('\n'.join(lines)+'\n')
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
