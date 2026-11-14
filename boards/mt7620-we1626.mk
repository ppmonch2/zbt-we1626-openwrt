# EXPERIMENTAL ZBT WE1626 NDMS-flash-map variant.
# Software reports kn_rf / Omni II, board itself is ZBT.
# Firmware region is 0x720000 bytes. Budget leaves >=256 KiB + for overlay.
define Device/zbtlink_zbt-we1626-ndms
  SOC := mt7620n
  IMAGE_SIZE := 7036k
  DEVICE_VENDOR := ZBT
  DEVICE_MODEL := WE1626
  DEVICE_VARIANT := NDMS partition map (EXPERIMENTAL)
  DEVICE_PACKAGES := kmod-usb2 kmod-usb-ohci kmod-usb-net-cdc-ether kmod-usb-storage usb-modeswitch
  IMAGES += factory.bin
  IMAGE/factory.bin := $$(sysupgrade_bin) | pad-to 64k | check-size | \
	zyimage -d 2102034 -v "ZyXEL Keenetic Omni II"
endef
TARGET_DEVICES += zbtlink_zbt-we1626-ndms
