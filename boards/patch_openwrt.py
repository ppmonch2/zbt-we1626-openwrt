#!/usr/bin/env python3
"""Patch a pinned OpenWrt source checkout with the experimental ZBT-WE1626 board.
No firmware is known to be flash-safe; never patch U-Boot or calibration."""
from pathlib import Path
from sys import argv

root=Path(argv[1] if len(argv)>1 else "openwrt")
files=Path(__file__).resolve().parent
ramips=root/"target/linux/ramips"
assert ramips.exists(), f"Source tree missing: {ramips}"
new_dts=ramips/"dts/mt7620n_zbtlink_zbt-we1626-ndms.dts"
assert not new_dts.exists(), "Refusing to overwrite existing upstream board DTS"
new_dts.write_bytes((files/"mt7620n_zbtlink_zbt-we1626-ndms.dts").read_bytes())
images=ramips/"image/mt7620.mk"
orig=images.read_text()
assert "TARGET_DEVICES += zyxel_keenetic-omni-ii" in orig, "Unexpected upstream image layout"
images.write_text(orig+"\n"+(files/"mt7620-we1626.mk").read_text())

network=ramips/"mt7620/base-files/etc/board.d/02_network"
orig=network.read_text()
marker="zbtlink,zbt-we2026)"
assert marker in orig and "zbtlink,zbt-we1626-ndms" not in orig
network.write_text(orig.replace(marker,"zbtlink,zbt-we1626-ndms|\\\n\tzbtlink,zbt-we2026)",1))

# Board-specific WAN MAC offset matches the ZBT reference profiles (may need correction).
patched=network.read_text()
key="zbtlink,zbt-we2026|\\\n"
assert key in patched
# The portmap is provisional. No board-specific PHY GPIO writes are performed.
assert "zbtlink,zbt-we1626-ndms" in patched

print("Patched",new_dts)
print("Firmware MTD range: 0x50000 + 0x720000; end 0x770000")
print("NDMS reserved: 0x770000..0x800000; u-boot/factory read-only")
print("WARNING: U-Boot OEM factory acceptance / GPIO / radio remain unverified")
