#!/usr/bin/env bash
# Experimental images for WE1626. NEVER flash from Keenetic NDMS.
set -Eeuo pipefail
VERSION=25.12.6
PROFILE=zbtlink_zbt-we2026
ARCHIVE="openwrt-imagebuilder-$VERSION-ramips-mt7620.Linux-x86_64.tar.zst"
CHECKSUM=bc43bf44422882a95381e1e2607074e650bcb53c93459d4bd6c573aa1e410729
ROOT="$(pwd)"
mkdir -p "$ROOT/work" "$ROOT/output"
cd "$ROOT/work"
curl -fL --retry 3 --connect-timeout 25 -o "$ARCHIVE" "https://downloads.openwrt.org/releases/$VERSION/targets/ramips/mt7620/$ARCHIVE"
echo "$CHECKSUM  $ARCHIVE" | sha256sum -c -
tar --zstd -xf "$ARCHIVE"
cd "$(echo "$ARCHIVE" | sed 's/\.tar\.zst$//')"
make info > "$ROOT/output/profiles.txt"
grep -Fq "$PROFILE" "$ROOT/output/profiles.txt" || { echo 'Device profile absent'; exit 1; }
echo 'DO NOT FLASH: WE1626 hardware revision, bootloader, and initial install format unverified.' > "$ROOT/output/DO_NOT_FLASH.txt"
echo 'Heat Bridge UI stores targets; real BALLU MQTT actuation DISABLED.' >> "$ROOT/output/DO_NOT_FLASH.txt"
echo 'ZTE 19d2:1225 needs mode switching to 19d2:1405. usb-modeswitch and usb-storage added, but actual function untested.' >> "$ROOT/output/DO_NOT_FLASH.txt"
echo 'Open first-boot Wi-Fi DachaHeat-Setup, no password; ZTE WAN and Heat Bridge disabled until owner secures LuCI.' >> "$ROOT/output/DO_NOT_FLASH.txt"
# No password generation, storage or publication in public GitHub Actions.
# First boot uses an open temporary Wi-Fi setup AP; no credentials embedded.
chmod +x "$ROOT/overlay/etc/uci-defaults/98-heatbridge" "$ROOT/overlay/etc/uci-defaults/99-heatbridge-open-setup" "$ROOT/overlay/www/heatbridge/cgi-bin/api" "$ROOT/overlay/usr/sbin/heatbridge-enable"
ok=0
for variant in heat-lite heat-webui; do
  packages='kmod-usb2 kmod-usb-net-cdc-ether kmod-usb-storage usb-modeswitch uhttpd jsonfilter mosquitto-nossl mosquitto-client-nossl'
  if [[ "$variant" == heat-webui ]]; then packages="$packages luci"; fi
  mkdir -p "$ROOT/output/$variant"
  echo "Building $variant with $packages"
  if make image PROFILE="$PROFILE" PACKAGES="$packages" FILES="$ROOT/overlay" DISABLED_SERVICES="mosquitto" BIN_DIR="$ROOT/output/$variant" > "$ROOT/output/$variant.log" 2>&1; then
    found=0
    for bin in "$ROOT/output/$variant/"*sysupgrade.bin; do
      [[ -f "$bin" ]] || continue
      found=1
      bytes="$(stat -c '%s' "$bin")"
      echo "$variant: $bytes bytes (screening ceiling 7,333,248 bytes)" | tee -a "$ROOT/output/sizes.txt"
      if (( bytes > 7333248 )); then
        echo "Oversized $variant. Removing unsafe candidate." >&2
        rm -f "$bin"
      else
        sha256sum "$bin" >> "$ROOT/output/SHA256SUMS"
        ok=$((ok+1))
      fi
    done
    if (( found==0 )); then echo "$variant: no image generated" >> "$ROOT/output/sizes.txt"; fi
  else
    echo "$variant: FAILED, see $variant.log" | tee -a "$ROOT/output/sizes.txt"
    tail -n 25 "$ROOT/output/$variant.log" || true
  fi
done
# Experimental factory format for the OEM identification in the installed NDMS:
# ZyXEL Keenetic Omni II, not the physical ZBT board revision.
VENDOR_PROFILE=zyxel_keenetic-omni-ii
VENDOR_DIR="$ROOT/output/omni2-firstflash"
mkdir -p "$VENDOR_DIR"
vendor_ok=0
echo 'Attempting OEM-compatible FACTORY format (board identity NOT verified)'
if make image PROFILE="$VENDOR_PROFILE" PACKAGES='kmod-usb2 kmod-usb-net-cdc-ether kmod-usb-storage usb-modeswitch uhttpd jsonfilter mosquitto-nossl mosquitto-client-nossl' FILES="$ROOT/overlay" DISABLED_SERVICES="mosquitto" BIN_DIR="$VENDOR_DIR" > "$ROOT/output/omni2-firstflash.log" 2>&1; then
  for bin in "$VENDOR_DIR/"*squashfs-factory.bin; do
    [[ -f "$bin" ]] || continue
    bytes="$(stat -c '%s' "$bin")"
    echo "omni2-firstflash: $bytes bytes; OEM factory format, unverified on ZBT" | tee -a "$ROOT/output/sizes.txt"
    if (( bytes <= 7333248 )); then
      sha256sum "$bin" >> "$ROOT/output/SHA256SUMS"
      vendor_ok=1
    else
      echo 'Factory image exceeds conservative 7,333,248-byte ceiling; removing candidate' >&2
      rm -f "$bin"
    fi
  done
else
  echo 'OEM factory variant failed; see omni2-firstflash.log' | tee -a "$ROOT/output/sizes.txt"
  tail -n 35 "$ROOT/output/omni2-firstflash.log" || true
fi
# Optional Omni II FACTORY build with LuCI for Android-only post-install WAN setup.
VENDOR_WEB_DIR="$ROOT/output/omni2-firstflash-webui"
mkdir -p "$VENDOR_WEB_DIR"
if make image PROFILE="$VENDOR_PROFILE" PACKAGES='kmod-usb2 kmod-usb-net-cdc-ether kmod-usb-storage usb-modeswitch uhttpd jsonfilter mosquitto-nossl mosquitto-client-nossl luci' FILES="$ROOT/overlay" DISABLED_SERVICES="mosquitto" BIN_DIR="$VENDOR_WEB_DIR" > "$ROOT/output/omni2-firstflash-webui.log" 2>&1; then
  for bin in "$VENDOR_WEB_DIR/"*squashfs-factory.bin; do
    [[ -f "$bin" ]] || continue
    bytes="$(stat -c '%s' "$bin")"
    echo "omni2-firstflash-webui: $bytes bytes; OEM factory format with LuCI, unverified on ZBT" | tee -a "$ROOT/output/sizes.txt"
    if (( bytes <= 7405568 )); then
      sha256sum "$bin" >> "$ROOT/output/SHA256SUMS"
    else
      echo 'Omni II webui image exceeds 7,405,568-byte ceiling (current NDMS 0x720000 minus 64 KiB), omitting' >&2
      rm -f "$bin"
    fi
  done
else
  echo 'Omni II factory+LuCI build failed; lite factory remains' >> "$ROOT/output/sizes.txt"
fi
if ((vendor_ok==0)); then echo 'No viable OEM-format factory image generated' >&2; exit 1; fi
if ((ok==0)); then echo 'No size-compliant candidates built' >&2; exit 1; fi
echo 'Images include Heat Bridge local server and MQTT binaries. NOT approved for first flashing.'
