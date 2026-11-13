#!/usr/bin/env python3
"""Read-only validation of experimental OpenWrt output. NO flashing."""
import hashlib
import json
from pathlib import Path
import struct
import sys
import zlib

root = Path(sys.argv[1] if len(sys.argv) > 1 else "output")
errors = []
lines = ["DO NOT FLASH. No proof of board or bootloader compatibility."]
for variant in ("minimal", "webui"):
    folder = root / variant
    meta = json.loads((folder / "profiles.json").read_text())
    profile = meta["profiles"]["zbtlink_zbt-we2026"]
    files = list(folder.glob("*-squashfs-sysupgrade.bin"))
    if len(files) != 1:
        errors.append(f"{variant}: found {len(files)} images")
        continue
    b = files[0].read_bytes()
    if b[:4] != bytes.fromhex("27051956"):
        errors.append(f"{variant}: missing U-Boot uImage header")
        continue
    header = bytearray(b[:64])
    expected_header_crc = struct.unpack(">I", header[4:8])[0]
    header[4:8] = b"\x00" * 4
    header_ok = (zlib.crc32(header) & 0xffffffff) == expected_header_crc
    kernel_len = struct.unpack(">I", b[12:16])[0]
    kernel_crc = struct.unpack(">I", b[24:28])[0]
    kernel_ok = (zlib.crc32(b[64:64 + kernel_len]) & 0xffffffff) == kernel_crc
    squashfs_ok = b[64 + kernel_len:68 + kernel_len] == b"hsqs"
    digest = hashlib.sha256(b).hexdigest()
    meta_ok = any(x["sha256"] == digest and x["size"] == len(b) for x in profile["images"])
    manifest = next(folder.glob("*.manifest")).read_text()
    driver_ok = "kmod-usb-net-cdc-ether " in manifest
    limit = profile["file_size_limits"]["image"]
    spare = limit - len(b)
    passed = all((header_ok, kernel_ok, squashfs_ok, meta_ok, driver_ok, spare > 0))
    if not passed:
        errors.append(f"{variant}: validation failed")
    lines.append(f"{variant}: {len(b)} bytes, image limit {limit}, unverified headroom {spare}, SHA256 {digest}")
    lines.append(f"  headerCRC={header_ok} kernelCRC={kernel_ok} squashfs={squashfs_ok} metadata={meta_ok} CDC-Ether={driver_ok}")
lines.append("Warning: headroom is NOT writable overlay capacity. Recovery is NOT verified.")
(root / "IMAGE_AUDIT.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
print("\n".join(lines))
if errors:
    raise SystemExit("; ".join(errors))
