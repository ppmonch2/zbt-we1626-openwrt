#!/usr/bin/env bash
# Small static MIPS32r2 / soft-float client: no OpenWrt dynamic loader on Keenetic.
set -Eeuo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
work="$root/.build-entware"
out="$root/output-entware"
mkdir -p "$work/mbedtls" "$out"
zig_bin="$(command -v zig)"
if [[ ! -f "$work/mbedtls.tar.bz2" ]]; then
 curl -fL --retry 3 https://github.com/Mbed-TLS/mbedtls/releases/download/mbedtls-3.6.7/mbedtls-3.6.7.tar.bz2 -o "$work/mbedtls.tar.bz2"
fi
printf '%s  %s\n' a7e8bcbec0e6f761b4af24f25677626b35f762f68eef79c08677a363212d11f6 "$work/mbedtls.tar.bz2" | sha256sum -c -
tar -xjf "$work/mbedtls.tar.bz2" -C "$work/mbedtls" --strip-components=1
cp "$root/entware/mbedtls_config.h" "$work/mbedtls/include/mbedtls/mbedtls_config.h"
cat > "$work/cc" <<WRAPPER
#!/bin/sh
exec "$zig_bin" cc -target mipsel-linux-musl -mcpu=mips32r2 -msoft-float "\$@"
WRAPPER
cat > "$work/ar" <<WRAPPER
#!/bin/sh
exec "$zig_bin" ar "\$@"
WRAPPER
chmod +x "$work/cc" "$work/ar"
cflags="-Os -ffunction-sections -fdata-sections"
make -C "$work/mbedtls/library" -j2 CC="$work/cc" AR="$work/ar" CFLAGS="$cflags" libmbedcrypto.a
bundle="$out/usb-root"
mkdir -p "$bundle/install" "$bundle/heatbridge-entware/bin" "$bundle/heatbridge-entware/licenses"
# The application uses the same copied minimal configuration as the crypto library.
"$work/cc" -static -Os -ffunction-sections -fdata-sections -Wl,--gc-sections \
 -DHEATBRIDGE_MBEDTLS \
 -I"$work/mbedtls/include" "$root/entware/client.c" \
 "$work/mbedtls/library/libmbedcrypto.a" -o "$bundle/heatbridge-entware/bin/heatbridge-syncleo"
file "$bundle/heatbridge-entware/bin/heatbridge-syncleo"
readelf -h "$bundle/heatbridge-entware/bin/heatbridge-syncleo" > "$out/native-elf.txt"
readelf -l "$bundle/heatbridge-entware/bin/heatbridge-syncleo" > "$out/native-segments.txt"
! grep -q INTERP "$out/native-segments.txt"
[[ $(stat -c %s "$bundle/heatbridge-entware/bin/heatbridge-syncleo") -lt 1048576 ]]
cp "$root/entware/bin/heatbridge-device" "$root/entware/bin/heatbridge-public-permissions" "$bundle/heatbridge-entware/bin/"
cp "$root/entware/install.sh" "$bundle/heatbridge-entware/"
cp -R "$root/entware/www" "$root/entware/init.d" "$bundle/heatbridge-entware/"
cp "$root/entware/README_RU.md" "$bundle/README_RU.md"
cp "$work/mbedtls/LICENSE" "$bundle/heatbridge-entware/licenses/mbedtls-LICENSE"
cp "$root/native/syncleo/LICENSE.pysyncleo" "$bundle/heatbridge-entware/licenses/"
# Include corresponding sources for the static executable and reproducible build inputs.
mkdir -p "$bundle/heatbridge-entware/source/native/syncleo" "$bundle/heatbridge-entware/source/entware"
cp "$root/native/syncleo/"*.c "$root/native/syncleo/"*.h "$root/native/syncleo/THIRD_PARTY.md" "$bundle/heatbridge-entware/source/native/syncleo/"
cp "$root/entware/client.c" "$root/entware/mdns.h" "$root/entware/mbedtls_config.h" "$root/entware/build.sh" "$bundle/heatbridge-entware/source/entware/"
curl -fL --retry 3 https://bin.entware.net/mipselsf-k3.4/installer/mipsel-installer.tar.gz -o "$bundle/install/mipsel-installer.tar.gz"
(cd "$bundle/heatbridge-entware"; find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)
cp "$root/entware/VERSION" "$bundle/heatbridge-entware/"
# Add VERSION to the installed payload's integrity check as well.
(cd "$bundle/heatbridge-entware"; sha256sum VERSION >> SHA256SUMS)
(cd "$bundle"; find . -type f -print0 | sort -z | xargs -0 sha256sum) > "$out/USB_SHA256SUMS"
cp "$out/USB_SHA256SUMS" "$bundle/USB_SHA256SUMS"
python3 - "$bundle" "$out/Ballu-Entware-USB.zip" <<'PY'
from pathlib import Path
import sys,zipfile
root=Path(sys.argv[1])
with zipfile.ZipFile(sys.argv[2],'w',zipfile.ZIP_DEFLATED) as z:
 for path in sorted(root.rglob('*')):
  if path.is_file():z.write(path,path.relative_to(root))
PY
(cd "$out"; sha256sum Ballu-Entware-USB.zip > Ballu-Entware-USB.zip.sha256)
