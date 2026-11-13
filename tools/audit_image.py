#!/usr/bin/env python3
"""Validate test images and embedded Heat Bridge; NEVER authorize flashing."""
import hashlib, json, struct, subprocess, sys, zlib
from pathlib import Path
root=Path(sys.argv[1] if len(sys.argv)>1 else "output")
errors=[]; lines=["NOT APPROVED TO FLASH: board / bootloader / recovery UNVERIFIED."]
valid=0
for variant in ("heat-lite","heat-webui"):
    folder=root/variant
    images=list(folder.glob("*-squashfs-sysupgrade.bin"))
    if not images:
        lines.append(f"{variant}: absent (failed or exceeded maximum size)")
        continue
    if len(images)!=1:
        errors.append(f"{variant}: unexpected number of images")
        continue
    b=images[0].read_bytes()
    meta=json.loads((folder/"profiles.json").read_text())
    prof=meta["profiles"]["zbtlink_zbt-we2026"]
    if b[:4]!=bytes.fromhex("27051956"):
        errors.append(f"{variant}: no uImage magic")
        continue
    header=bytearray(b[:64]); crc=struct.unpack(">I",header[4:8])[0]; header[4:8]=b"\x00"*4
    hc=(zlib.crc32(header)&0xffffffff)==crc
    kernel_len=struct.unpack(">I",b[12:16])[0]
    kc=(zlib.crc32(b[64:64+kernel_len])&0xffffffff)==struct.unpack(">I",b[24:28])[0]
    offset=64+kernel_len
    sq=b[offset:offset+4]==b"hsqs"
    digest=hashlib.sha256(b).hexdigest()
    metadata=any(x["sha256"]==digest and x["size"]==len(b) for x in prof["images"])
    manifest=next(folder.glob("*.manifest")).read_text()
    pkgs=all((f"{p} " in manifest) for p in ("uhttpd","jsonfilter","mosquitto-nossl","mosquitto-client-nossl","kmod-usb-net-cdc-ether","kmod-usb-storage","kmod-fs-vfat"))
    limit=prof["file_size_limits"]["image"]
    safe_size=len(b)<=min(limit,7333248)
    listing=subprocess.run(["unsquashfs","-l","-o",str(offset),str(images[0])],capture_output=True,text=True)
    embedded=(listing.returncode==0 and all(x in listing.stdout for x in (
      "www/heatbridge/index.html","www/heatbridge/cgi-bin/api",
      "etc/config/heatbridge","usr/sbin/heatbridge-enable",
      "etc/uci-defaults/98-heatbridge",
      "etc/uci-defaults/99-heatbridge-usb-setup",
      "etc/hotplug.d/net/90-dh-zte-wan",
    )))
    ok=all((hc,kc,sq,metadata,pkgs,safe_size,embedded))
    lines.append(f"{variant}: {len(b)} bytes, profile limit {limit}, unverified headroom {limit-len(b)}, SHA256 {digest}")
    lines.append(f"  headerCRC={hc}, kernelCRC={kc}, squashfs={sq}, metadata={metadata}, packages={pkgs}, embedded_server={embedded}, size={safe_size}")
    if ok: valid+=1
    else: errors.append(f"{variant}: integrity/size/package/overlay FAILED")
if not valid: errors.append("No compliant images")
lines.append("Note: remaining image bytes are NOT guaranteed writable space; actual WE1626 flash map is not proven.")
(root/"IMAGE_AUDIT.txt").write_text("\n".join(lines)+"\n",encoding="utf-8")
print("\n".join(lines))
if errors: raise SystemExit("; ".join(errors))
