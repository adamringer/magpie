# SAM-BA restore-path pieces. Build in the container:
#   scripts/xrun.sh make -C restore
CROSS ?= arm-linux-
LOAD_ADDR = 0x300000

all: sdram_init.bin

sdram_init.elf: sdram_init.S
	$(CROSS)gcc -mcpu=arm926ej-s -marm -nostdlib -static -Wl,-Ttext=$(LOAD_ADDR) -Wl,--build-id=none -o $@ $<

sdram_init.bin: sdram_init.elf
	$(CROSS)objcopy -O binary $< $@

clean:
	rm -f sdram_init.elf sdram_init.bin

.PHONY: all clean
