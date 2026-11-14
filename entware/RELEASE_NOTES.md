Package for the owner's ZBT WE1626 / MT7620 with NDMS, OPKG and an EXT USB drive.

Local Syncleo v3 control for Ballu devtype 46 and the existing HOMMYN/Alice cloud
integration for remote commands. Includes authenticated LAN web controls,
fresh mDNS discovery, temperature/power commands and Entware autostart.

Built as static MIPS32r2 little-endian soft-float; no OpenWrt dynamic loader is
required. The source ran 27 host tests and the target executable passed five
encrypted-UDP tests in QEMU. Physical router/heater compatibility still requires
testing. No owner credentials, tokens or network configuration are included.

`Ballu-Entware-App-0.1.0.tar.gz` is for an already installed Entware system.
`Ballu-Entware-USB-0.1.0.zip` also contains the official Entware bootstrap.
Verify downloads with SHA256SUMS before installing. See README_RU.md in the USB
archive for setup and limitations. The package does not flash the router or
modify the working HOMMYN/Alice binding.
