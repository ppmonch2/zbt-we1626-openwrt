#!/usr/bin/env python3
"""Validate experimental ZyXEL OEM factory image; not proof of ZBT board compatibility."""
import hashlib, json, struct, subprocess, sys, zlib
from pathlib import Path
root=Path(sys.argv[1] if len(sys.argv)>1 else "output")
folder=root/"omni2-firstflash"
image=next(folder.glob("*squashfs-factory.bin"),None)
if image is None: raise SystemExit("Expected Omni II factory image absent")
data=image.read_bytes()
profile=json.loads((folder/"profiles.json").read_text())["profiles"]["zyxel_keenetic-omni-ii"]
h=bytearray(data[:64]); hdr_crc=struct.unpack(">I",h[4:8])[0];h[4:8]=b"\x00"*4
header_crc=(zlib.crc32(h)&0xffffffff)==hdr_crc
kernel_size=struct.unpack(">I",data[12:16])[0]
kernel_crc=(zlib.crc32(data[64:64+kernel_size])&0xffffffff)==struct.unpack(">I",data[24:28])[0]
sq_start=64+kernel_size
squashfs=data[sq_start:sq_start+4]==b"hsqs"
prefix=data[:4]==b"\x27\x05\x19\x56"
footer=data[-60:]
zyxel_footer=(footer[:8]==b"ZNBG\x12\x13\x20\x00" and b"ZyXEL Keenetic Omni II" in footer
              and (len(data)-60)%65536==0)
digest=hashlib.sha256(data).hexdigest()
metadata=any(img["name"]==image.name and img["sha256"]==digest
             and img["size"]==len(data) and img["type"]=="factory" for img in profile["images"])
flash_limit=profile["file_size_limits"]["image"]
size_ok=len(data)<=min(flash_limit,7333248)
manifest=next(folder.glob("*.manifest")).read_text()
packages=all(f"{p} " in manifest for p in
             ("uhttpd","jsonfilter","kmod-usb-storage","kmod-fs-vfat",
              "kmod-usb-net-cdc-ether","wpad-basic-mbedtls"))
unpacked=subprocess.run(["unsquashfs","-l","-o",str(sq_start),str(image)],capture_output=True,text=True)
overlay_ok=unpacked.returncode==0 and all(x in unpacked.stdout for x in
           ("www/heatbridge/index.html","etc/uci-defaults/99-heatbridge-usb-setup",
            "www/heatbridge/cgi-bin/api","etc/config/heatbridge",
            "etc/hotplug.d/net/90-dh-zte-wan"))
results={
    "uimage_header":prefix and header_crc,
    "kernel_crc":kernel_crc,
    "squashfs":squashfs,
    "ZyXEL_Omni_II_zn_bg_footer":zyxel_footer,
    "official_openwrt_metadata":metadata,
    "flash_image_size":size_ok,
    "USB_FAT32_WiFi_packages":packages,
    "embedded_DachaHeat_server":overlay_ok,
}
report=[
    "Experimental first-install image. NOT proof of ZBT WE1626 hardware compatibility.",
    "A matching factory format is necessary but NOT sufficient for successful initial flashing.",
    f"file={image.name}",
    f"size={len(data)} bytes; target image limit={flash_limit} bytes",
    f"SHA256={digest}",
]
report.extend(f"{key}={value}" for key,value in results.items())
(root/"FACTORY_AUDIT.txt").write_text("\n".join(report)+"\n",encoding="utf-8")
print("\n".join(report))
if not all(results.values()): raise SystemExit("OEM factory image failed audit")
