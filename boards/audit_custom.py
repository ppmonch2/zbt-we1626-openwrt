#!/usr/bin/env python3
"""Fail-closed preflight for a *source-built* WE1626 custom-DTS image.
Passing is NOT proof of bootloader compatibility or permission to flash it.
"""
from pathlib import Path
import hashlib, json, re, struct, subprocess, sys, zlib
root=Path(__file__).resolve().parents[1]
folder=root/"output-custom"
dts=(root/"boards/mt7620n_zbtlink_zbt-we1626-ndms.dts").read_text()
prof=(root/"boards/mt7620-we1626.mk").read_text()
assert 'reg = <0x50000 0x720000>;' in dts
assert 'reg = <0x770000 0x10000>;' in dts
assert 'reg = <0x780000 0x80000>;' in dts
assert re.search(r'reg = <0x0 0x30000>;\s*read-only',dts)
assert re.search(r'reg = <0x40000 0x10000>;\s*read-only',dts)
assert '&ehci {' in dts and '&ohci {' in dts
assert 'SOC := mt7620n' in prof
assert 'zyimage -d 2102034 -v "ZyXEL Keenetic Omni II"' in prof
images=list(folder.glob('*zbt-we1626-ndms*squashfs-factory.bin'))
assert len(images)==1, f"Expected exactly 1 source-built factory.bin, got {len(images)}"
f=images[0]
data=f.read_bytes()
max_factory=0x720000-0x40000  # keep >= 256KiB aside for future writable overlay
assert len(data)<=max_factory, f"Too large: {len(data)} > {max_factory} bytes"
assert data[:4]==bytes.fromhex('27051956'), "Missing uImage magic"
h=bytearray(data[:64]); stored_hcrc=struct.unpack('>I',h[4:8])[0];h[4:8]=b'\x00'*4
assert zlib.crc32(h)&0xffffffff==stored_hcrc, "Bad uImage header CRC"
ksize=struct.unpack('>I',data[12:16])[0]
assert zlib.crc32(data[64:64+ksize])&0xffffffff==struct.unpack('>I',data[24:28])[0], "Bad kernel CRC"
offset=64+ksize
assert data[offset:offset+4]==b'hsqs', "Missing rootfs SquashFS"
footer=data[-60:]
assert footer.startswith(b'ZNBG'), "ZyXEL factory footer missing"
assert b'ZyXEL Keenetic Omni II' in footer, "Vendor footer target mismatched"
proc=subprocess.run(['unsquashfs','-l','-o',str(offset),str(f)],text=True,capture_output=True)
assert proc.returncode==0, proc.stderr[-500:]
required=(
    'etc/uci-defaults/99-heatbridge-open-setup',
    'etc/uci-defaults/98-heatbridge',
    'etc/config/heatbridge',
    'www/heatbridge/index.html',
    'www/heatbridge/cgi-bin/api',
    'etc/hotplug.d/net/90-dh-zte-wan',
)
assert all(p in proc.stdout for p in required), "Custom files absent from SquashFS"
manifest=next(folder.glob('*zbt-we1626-ndms*.manifest')).read_text()
for pkg in ('luci','kmod-usb2','kmod-usb-net-cdc-ether','kmod-usb-storage','usb-modeswitch'):
    assert re.search(rf'^{re.escape(pkg)} ',manifest,re.M), f"Required package missing: {pkg}"
sha=hashlib.sha256(data).hexdigest()
lines=[
  f"FILE: {f.name}",
  f"SIZE: {len(data)} bytes",
  f"MAX SIZE (retains >=256KiB space): {max_factory} bytes",
  f"SHA256: {sha}",
  "CUSTOM DTS: MT7620N hypothesis; firmware [0x50000,0x770000), Config/Storage read-only",
  "VENDOR FACTORY FOOTER: present",
  "UBOOT/FACTORY: declared read-only, not overwritten by image builder",
  "EMBEDDED UI, USB MODE SWITCH, LAN/WIFI BOOTSTRAP: present",
  "NOT SAFE TO FLASH YET: bootloader and actual GPIO/SoC remain unverified",
]
(folder/"CUSTOM_PRE_FLIGHT.txt").write_text('\n'.join(lines)+'\n')
print('\n'.join(lines))
