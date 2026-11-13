#!/usr/bin/env bash
# Builds unverified OpenWrt candidates only. NEVER flash from NDMS.
set -Eeuo pipefail
VERSION=25.12.6
PROFILE=zbtlink_zbt-we2026
ARCHIVE="openwrt-imagebuilder-${VERSION}-ramips-mt7620.Linux-x86_64.tar.zst"
CHECKSUM=bc43bf44422882a95381e1e2607074e650bcb53c93459d4bd6c573aa1e410729
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$ROOT/work" "$ROOT/output"
cd "$ROOT/work"
curl -fL --retry 3 --connect-timeout 25 -o "$ARCHIVE" "https://downloads.openwrt.org/releases/${VERSION}/targets/ramips/mt7620/$ARCHIVE"
echo "$CHECKSUM  $ARCHIVE" | sha256sum -c -
tar --zstd -xf "$ARCHIVE"
cd "${ARCHIVE%.tar.zst}"
make info > "$ROOT/output/profiles.txt"
grep -Fq "$PROFILE" "$ROOT/output/profiles.txt" || { echo 'Expected device profile absent'; exit 1; }
echo 'Unverified candidates only. NOT APPROVED TO FLASH. No credentials or first-boot WiFi configured.' > "$ROOT/output/DO_NOT_FLASH.txt"
ok=0
for variant in minimal webui; do
  packages='kmod-usb2 kmod-usb-net-cdc-ether'
  if [[ "$variant" == webui ]]; then packages="$packages luci"; fi
  mkdir -p "$ROOT/output/$variant"
  if make image PROFILE="$PROFILE" PACKAGES="$packages" BIN_DIR="$ROOT/output/$variant" > "$ROOT/output/${variant}.log" 2>&1; then
    find "$ROOT/output/$variant" -type f -name '*sysupgrade.bin' -print0 | while IFS= read -r -d '' bin; do
      sha256sum "$bin" >> "$ROOT/output/SHA256SUMS"
      size="$(stat -c '%s' "$bin")"
      echo "$variant size: $size bytes" >> "$ROOT/output/sizes.txt"
      if (( size > 7340032 )); then echo "WARNING: $variant exceeds conservative screening limit" >> "$ROOT/output/sizes.txt"; fi
    done
    ok=$((ok+1))
  else
    echo "$variant: BUILD FAILED, see log" >> "$ROOT/output/sizes.txt"
    tail -n 50 "$ROOT/output/${variant}.log"
  fi
done
if ((ok==0)); then echo 'No candidate images built' >&2; exit 1; fi
echo 'Candidates produced; NOT validated for first flashing or recovery.'
