#!/usr/bin/env bash
# Compile the first true custom-DTS ZBT-WE1626-NDMS candidate from OpenWrt source.
# This is NOT a safe-to-flash image; U-Boot acceptance and actual board GPIO unverified.
set -Eeuo pipefail
WORK="$(cd "$(dirname "$0")/.." && pwd)"
export FORCE_UNSAFE_CONFIGURE=1
cd "$WORK"
git clone --filter=blob:none --branch v25.12.6 --depth 1 https://github.com/openwrt/openwrt.git openwrt-src
python3 boards/patch_openwrt.py openwrt-src
cd openwrt-src
./scripts/feeds update -a
./scripts/feeds install -a
mkdir -p files
cp -a "$WORK/overlay/." files/
chmod +x files/etc/uci-defaults/98-heatbridge files/etc/uci-defaults/99-heatbridge-open-setup
chmod +x files/etc/hotplug.d/net/90-dh-zte-wan files/usr/sbin/heatbridge-* files/www/heatbridge/cgi-bin/*
cat > .config <<'EOF'
CONFIG_TARGET_ramips=y
CONFIG_TARGET_ramips_mt7620=y
CONFIG_TARGET_ramips_mt7620_DEVICE_zbtlink_zbt-we1626-ndms=y
CONFIG_PACKAGE_luci=y
CONFIG_PACKAGE_uhttpd=y
CONFIG_PACKAGE_jsonfilter=y
CONFIG_PACKAGE_mosquitto-ssl=y
CONFIG_PACKAGE_mosquitto-client-ssl=y
CONFIG_PACKAGE_ca-bundle=y
CONFIG_PACKAGE_umdns=y
CONFIG_PACKAGE_tcpdump-mini=y
CONFIG_PACKAGE_kmod-usb2=y
CONFIG_PACKAGE_kmod-usb-ohci=y
CONFIG_PACKAGE_kmod-usb-net-cdc-ether=y
CONFIG_PACKAGE_kmod-usb-storage=y
CONFIG_PACKAGE_usb-modeswitch=y
EOF
make defconfig
grep -q '^CONFIG_TARGET_ramips_mt7620_DEVICE_zbtlink_zbt-we1626-ndms=y$' .config || {
  echo 'Custom hardware target was NOT selected by OpenWrt, aborting' >&2; exit 1;
}
echo 'Building for preliminary MT7620N / NDMS 0x720000 flash map. Nothing approved for flashing.'
make download -j4
make -j4 V=s
mkdir -p "$WORK/output-custom"
cp -av bin/targets/ramips/mt7620/*zbt-we1626-ndms* "$WORK/output-custom/" || true
cp -av bin/targets/ramips/mt7620/profiles.json "$WORK/output-custom/" || true
cp -av bin/targets/ramips/mt7620/*.manifest "$WORK/output-custom/" || true
if ! find "$WORK/output-custom" -name '*squashfs-factory.bin' -print -quit | grep -q .; then
  echo 'No custom factory image was produced. Check build log / size constraint.' >&2
  exit 1
fi
cp "$WORK/docs/BALLU_SETUP_RU.md" "$WORK/output-custom/"
python3 "$WORK/boards/make_heatbridge_update.py"
echo 'DO NOT FLASH: factory acceptance, MT7620N and Ethernet GPIO remain unverified.' > "$WORK/output-custom/DO_NOT_FLASH.txt"
echo 'Flash 0x50000..0x770000 only; reserve NDMS Config and Storage until factory backup.' >> "$WORK/output-custom/DO_NOT_FLASH.txt"

