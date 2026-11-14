include $(TOPDIR)/rules.mk

PKG_NAME:=heatbridge-syncleo
PKG_VERSION:=0.1
PKG_RELEASE:=1
PKG_LICENSE:=MIT
PKG_BUILD_DIR:=$(BUILD_DIR)/$(PKG_NAME)

include $(INCLUDE_DIR)/package.mk

define Package/heatbridge-syncleo
  SECTION:=utils
  CATEGORY:=Utilities
  TITLE:=Minimal Syncleo v3 client for Heat Bridge
  DEPENDS:=+libmbedtls
endef

define Build/Prepare
	mkdir -p $(PKG_BUILD_DIR)
	$(CP) ./main.c ./crypto_mbedtls.h $(PKG_BUILD_DIR)/
endef

define Build/Configure
endef

define Build/Compile
	$(TARGET_CC) $(TARGET_CFLAGS) $(TARGET_CPPFLAGS) -std=c99 -Wall -Wextra -DHEATBRIDGE_MBEDTLS \
		-o $(PKG_BUILD_DIR)/heatbridge-syncleo $(PKG_BUILD_DIR)/main.c $(TARGET_LDFLAGS) -lmbedcrypto
endef

define Package/heatbridge-syncleo/install
	$(INSTALL_DIR) $(1)/usr/bin
	$(INSTALL_BIN) $(PKG_BUILD_DIR)/heatbridge-syncleo $(1)/usr/bin/
endef

$(eval $(call BuildPackage,heatbridge-syncleo))
