/* Host test of the pure logic in br2-external/package/gpsnap/src/gpsnap_logic.c (run by tests/test_gpsnap_logic.py) */
#include <stdio.h>
#include <string.h>

#include "gpsnap_logic.h"

static int failures;

#define CHECK(cond)                                                                  \
	do {                                                                         \
		if (!(cond)) {                                                       \
			printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);       \
			failures++;                                                  \
		}                                                                    \
	} while (0)

#define MAXPAGES 40

static uint8_t pages[MAXPAGES][GPS_PAGE_BYTES];

static void make(unsigned n)
{
	uint16_t counter = 0;
	unsigned i;

	for (i = 0; i < n; i++)
		gps_pattern_fill_page(pages[i], &counter);
}

static void verify(struct gps_verify *v, unsigned n)
{
	unsigned i;

	gps_verify_init(v);
	for (i = 0; i < n; i++)
		gps_verify_page(v, pages[i], i);
}

int main(void)
{
	struct gps_verify v;
	uint8_t b[24];
	unsigned i;

	/* pages from ms */
	CHECK(gps_pages_from_ms(16) == 1);
	CHECK(gps_pages_from_ms(20) == 1);
	CHECK(gps_pages_from_ms(512) == 32);
	CHECK(gps_pages_from_ms(4096) == 256);
	CHECK(gps_pages_from_ms(15) == 0);
	CHECK(gps_pages_from_ms(4097) == 0);
	CHECK(gps_pages_from_ms(0) == 0);
	CHECK(gps_pages_from_ms(-16) == 0);

	/* commands, bytes taken from the stock binary (config table at 0xaea4, RF templates at 0xafb0/0xafc8) */
	gps_cmd_read(0x44, b);
	CHECK(!memcmp(b, "\xe5\x1a\x00\x44", 4));
	gps_cmd_write(0xc8, 0x00f15258, b);
	CHECK(!memcmp(b, "\xe5\x1a\x80\xc8\x00\xf1\x52\x58", 8));
	gps_cmd_write(0xcc, 0xfdb96420u, b);
	CHECK(!memcmp(b, "\xe5\x1a\x80\xcc\xfd\xb9\x64\x20", 8));
	CHECK(gps_decode_reg((const uint8_t *)"\x01\x00\x1a\x00") == GPS_VERSION_EXPECTED);
	{
		const struct gps_regval t[2] = { { 0xc8, 0x00f15258 }, { 0xc0, 0x05f03c8d } };
		uint8_t out[16];

		CHECK(gps_cmd_write_table(t, 2, out) == 16);
		CHECK(!memcmp(out, "\xe5\x1a\x80\xc8\x00\xf1\x52\x58\xe5\x1a\x80\xc0\x05\xf0\x3c\x8d", 16));
	}
	gps_rf_bit_cmds(0, b);
	CHECK(!memcmp(b, "\xe5\x1a\x80\xc8\x00\xc1\x52\x58\xe5\x1a\x80\xc8\x00\xd1\x52\x58\xe5\x1a\x80\xc8\x00\xc1\x52\x58", 24));
	gps_rf_bit_cmds(1, b);
	CHECK(!memcmp(b, "\xe5\x1a\x80\xc8\x00\xe1\x52\x58\xe5\x1a\x80\xc8\x00\xf1\x52\x58\xe5\x1a\x80\xc8\x00\xe1\x52\x58", 24));
	gps_rf_bit_cmds(0x80, b); /* any non-zero value is a 1 bit */
	CHECK(b[5] == 0xe1);

	/* pattern layout: word 0 is counter 1 (high half) then 0 (low half), each big-endian */
	make(1);
	CHECK(!memcmp(pages[0], "\x00\x01\x00\x00\x00\x03\x00\x02", 8));

	/* a correct stream (40 pages = 81920 halfwords, wraps the 65001 counter) is accepted */
	make(MAXPAGES);
	verify(&v, MAXPAGES);
	CHECK(v.mismatches == 0 && v.checked == MAXPAGES * GPS_PAGE_HALFWORDS && !v.have_first);

	/* dropped page 5 */
	make(MAXPAGES);
	memmove(pages[5], pages[6], (MAXPAGES - 6) * GPS_PAGE_BYTES);
	verify(&v, MAXPAGES - 1);
	CHECK(v.mismatches > 0);
	CHECK(v.have_first && v.first_page == 5 && v.first_index == 0);
	CHECK(v.first_delta == GPS_PAGE_HALFWORDS);
	CHECK(v.breaks == 1);

	/* word swap (the two halfwords of word 100 in page 2 exchanged) */
	make(MAXPAGES);
	{
		uint8_t *w = pages[2] + 4 * 100;
		uint8_t t0 = w[0], t1 = w[1];

		w[0] = w[2];
		w[1] = w[3];
		w[2] = t0;
		w[3] = t1;
	}
	verify(&v, MAXPAGES);
	CHECK(v.mismatches == 2 && v.breaks == 1);
	CHECK(v.first_page == 2 && v.first_index == 200);

	/* swapped bytes inside a halfword, and a single flipped bit */
	make(4);
	{
		uint8_t t0 = pages[1][10];

		pages[1][10] = pages[1][11];
		pages[1][11] = t0;
	}
	verify(&v, 4);
	CHECK(v.mismatches == 1 && v.first_page == 1);
	make(4);
	pages[3][4095] ^= 0x01;
	verify(&v, 4);
	CHECK(v.mismatches == 1 && v.first_page == 3 && v.first_index == 2046);

	/* two pages exchanged */
	make(6);
	{
		static uint8_t tmp[GPS_PAGE_BYTES];

		memcpy(tmp, pages[2], sizeof(tmp));
		memcpy(pages[2], pages[3], sizeof(tmp));
		memcpy(pages[3], tmp, sizeof(tmp));
	}
	verify(&v, 6);
	CHECK(v.mismatches == 2 * GPS_PAGE_HALFWORDS && v.first_page == 2);

	/* the whole stream stays valid when checked page by page with the same state */
	make(3);
	gps_verify_init(&v);
	for (i = 0; i < 3; i++)
		gps_verify_page(&v, pages[i], i);
	CHECK(v.mismatches == 0);

	if (failures) {
		printf("%d check(s) failed\n", failures);
		return 1;
	}
	printf("all checks passed\n");
	return 0;
}
