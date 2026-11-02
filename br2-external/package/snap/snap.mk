################################################################################
#
# snap
#
################################################################################

SNAP_VERSION = 1.0
SNAP_SITE = $(BR2_EXTERNAL_MAGPIE_PATH)/package/snap/src
SNAP_SITE_METHOD = local
SNAP_LICENSE = MIT

define SNAP_BUILD_CMDS
	$(TARGET_CC) $(TARGET_CFLAGS) -Wall -Wextra -Werror -O2 $(TARGET_LDFLAGS) \
		-o $(@D)/snap $(@D)/snap.c
endef

define SNAP_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/snap $(TARGET_DIR)/usr/bin/snap
endef

$(eval $(generic-package))
