#!/usr/bin/env python3
"""Package app-only files so future Heat Bridge changes need no sysupgrade."""
from pathlib import Path
import hashlib
import tarfile

root = Path(__file__).resolve().parents[1]
output = root / 'output-custom'
output.mkdir(exist_ok=True)
archive = output / 'heatbridge-app.tar.gz'
paths = [root / 'overlay/www/heatbridge', root / 'overlay/usr/sbin/heatbridge-mqtt',
         root / 'overlay/usr/sbin/heatbridge-discover']
with tarfile.open(archive, 'w:gz') as tar:
    for path in paths:
        for item in ([path] if path.is_file() else sorted(path.rglob('*'))):
            if not item.is_file():
                continue
            info = tar.gettarinfo(str(item), arcname=str(item.relative_to(root / 'overlay')))
            info.uid = info.gid = 0
            info.uname = info.gname = 'root'
            info.mode = 0o755 if '/cgi-bin/' in str(item) or '/sbin/' in str(item) else 0o644
            with item.open('rb') as stream:
                tar.addfile(info, stream)
(output / 'heatbridge-app.sha256').write_text(
    hashlib.sha256(archive.read_bytes()).hexdigest() + '  ' + archive.name + '\n')
print(archive)
