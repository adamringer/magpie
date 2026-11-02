################################################################################
#
# gpsnap
#
################################################################################

GPSNAP_VERSION = 1.0
GPSNAP_SITE = $(BR2_EXTERNAL_MAGPIE_PATH)/package/gpsnap/src
GPSNAP_SITE_METHOD = local
GPSNAP_LICENSE = MIT

define GPSNAP_BUILD_CMDS
	$(TARGET_CC) $(TARGET_CFLAGS) -Wall -Wextra -Werror -O2 $(TARGET_LDFLAGS) \
		-o $(@D)/gpsnap $(@D)/gpsnap.c $(@D)/gpsnap_logic.c
endef

define GPSNAP_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/gpsnap $(TARGET_DIR)/usr/bin/gpsnap
endef

$(eval $(generic-package))
