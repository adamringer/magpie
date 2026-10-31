/*
 * gpsnap: pure logic shared by the tool and the host tests (no I/O, no kernel headers).
 * Reverse-engineered from the stock aclys_snap, see analysis/gps/snap_format.md.
 */
#ifndef GPSNAP_LOGIC_H
#define GPSNAP_LOGIC_H

#include <stddef.h>
#include <stdint.h>

#define GPS_PAGE_BYTES 4096u
#define GPS_MS_MIN 16
#define GPS_MS_MAX 4096
#define GPS_MS_DEFAULT 512

/* Register addresses used by the stock tool */
#define GPS_REG_VERSION 0x44
#define GPS_VERSION_EXPECTED 0x01001A00u

/* -e pattern: 16-bit counter 0..GPS_PATTERN_MOD-1, then wraps to 0 */
#define GPS_PATTERN_MOD 0xFDE9u
#define GPS_PAGE_HALFWORDS (GPS_PAGE_BYTES / 2)

struct gps_regval {
	uint8_t reg;
	uint32_t val;
};

/* Pages for a snapshot length: ms >> 4 (one 4096-byte page = 16 ms). 0 if ms is out of range. */
unsigned gps_pages_from_ms(int ms);

/* Register read command, 4 bytes on the wire: e5 1a 00 <reg> (stock: LE u32 reg<<24 | 0x1AE5) */
void gps_cmd_read(uint8_t reg, uint8_t out[4]);

/* Register write, 8 bytes: e5 1a 80 <reg>, then the value big-endian (stock: LE u32 reg<<24 | 0x801AE5) */
void gps_cmd_write(uint8_t reg, uint32_t val, uint8_t out[8]);

/* Register read data: 4 bytes on the wire, big-endian */
uint32_t gps_decode_reg(const uint8_t in[4]);

/* Concatenated write commands (8 bytes each) for a table, as the stock burst writer sends them in one write(). Returns bytes. */
size_t gps_cmd_write_table(const struct gps_regval *t, size_t n, uint8_t *out);

/*
 * One bit of the bit-banged RF serial port: three writes (24 bytes) to register 0xC8:
 * clock low, clock high, clock low, with the data bit in bit 21 (clock is bit 20).
 * The constant part (0x00C15258) is the stock template.
 */
void gps_rf_bit_cmds(int bit, uint8_t out[24]);

/* ---- -e test pattern ---- */

/*
 * Fill one page with the pattern the chip's test mode sends, continuing at *counter (updated).
 * Layout: 32-bit word k holds counter value 2k+1 in its high half and 2k in its low half,
 * each half big-endian (stock verifier: halfword index i is read at position i^1, byte-swapped).
 */
void gps_pattern_fill_page(uint8_t page[GPS_PAGE_BYTES], uint16_t *counter);

struct gps_verify {
	uint16_t expect;           /* next expected counter value (does not resync, like stock) */
	unsigned long checked;     /* halfwords checked */
	unsigned long mismatches;  /* halfwords that differ */
	unsigned long breaks;      /* transitions from match to mismatch */
	int have_first;
	unsigned first_page;       /* page index of the first mismatch */
	unsigned first_index;      /* halfword index in that page (0..2047) */
	uint16_t first_expected;
	uint16_t first_got;
	long first_delta;          /* (got - expected) mod GPS_PATTERN_MOD, -1 if got is not a valid counter */
	int last_ok;
};

void gps_verify_init(struct gps_verify *v);
void gps_verify_page(struct gps_verify *v, const uint8_t page[GPS_PAGE_BYTES], unsigned page_no);

#endif
