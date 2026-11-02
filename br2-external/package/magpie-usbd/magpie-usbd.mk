################################################################################
#
# magpie-usbd
#
################################################################################

MAGPIE_USBD_VERSION = 1.0
MAGPIE_USBD_SITE = $(BR2_EXTERNAL_MAGPIE_PATH)/package/magpie-usbd/src
MAGPIE_USBD_SITE_METHOD = local
MAGPIE_USBD_LICENSE = MIT

define MAGPIE_USBD_BUILD_CMDS
	$(TARGET_CC) $(TARGET_CFLAGS) -Wall -Wextra -Werror -O2 -pthread $(TARGET_LDFLAGS) \
		-o $(@D)/magpie-usbd $(@D)/magpie_usbd.c $(@D)/usbd_logic.c
endef

define MAGPIE_USBD_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/magpie-usbd $(TARGET_DIR)/usr/sbin/magpie-usbd
endef

$(eval $(generic-package))
