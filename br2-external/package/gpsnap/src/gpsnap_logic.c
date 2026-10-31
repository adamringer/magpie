#include "gpsnap_logic.h"

unsigned gps_pages_from_ms(int ms)
{
	if (ms < GPS_MS_MIN || ms > GPS_MS_MAX)
		return 0;
	return (unsigned)ms >> 4;
}

void gps_cmd_read(uint8_t reg, uint8_t out[4])
{
	out[0] = 0xE5;
	out[1] = 0x1A;
	out[2] = 0x00;
	out[3] = reg;
}

void gps_cmd_write(uint8_t reg, uint32_t val, uint8_t out[8])
{
	out[0] = 0xE5;
	out[1] = 0x1A;
	out[2] = 0x80;
	out[3] = reg;
	out[4] = (uint8_t)(val >> 24);
	out[5] = (uint8_t)(val >> 16);
	out[6] = (uint8_t)(val >> 8);
	out[7] = (uint8_t)val;
}

uint32_t gps_decode_reg(const uint8_t in[4])
{
	return (uint32_t)in[0] << 24 | (uint32_t)in[1] << 16 | (uint32_t)in[2] << 8 | in[3];
}

size_t gps_cmd_write_table(const struct gps_regval *t, size_t n, uint8_t *out)
{
	size_t i;

	for (i = 0; i < n; i++)
		gps_cmd_write(t[i].reg, t[i].val, out + 8 * i);
	return 8 * n;
}

void gps_rf_bit_cmds(int bit, uint8_t out[24])
{
	/* register 0xC8: bit 20 = clock, bit 21 = data */
	const uint32_t base = 0x00C15258u;
	const uint32_t data = bit ? 0x200000u : 0;
	const uint32_t clk = 0x100000u;

	gps_cmd_write(0xC8, base | data, out);
	gps_cmd_write(0xC8, base | data | clk, out + 8);
	gps_cmd_write(0xC8, base | data, out + 16);
}

void gps_pattern_fill_page(uint8_t page[GPS_PAGE_BYTES], uint16_t *counter)
{
	unsigned i;

	for (i = 0; i < GPS_PAGE_HALFWORDS; i++) {
		unsigned pos = 2 * (i ^ 1u);

		page[pos] = (uint8_t)(*counter >> 8);
		page[pos + 1] = (uint8_t)*counter;
		if (++*counter == GPS_PATTERN_MOD)
			*counter = 0;
	}
}

void gps_verify_init(struct gps_verify *v)
{
	*v = (struct gps_verify){ .last_ok = 1, .first_delta = -1 };
}

void gps_verify_page(struct gps_verify *v, const uint8_t page[GPS_PAGE_BYTES], unsigned page_no)
{
	unsigned i;

	for (i = 0; i < GPS_PAGE_HALFWORDS; i++) {
		unsigned pos = 2 * (i ^ 1u);
		uint16_t got = (uint16_t)(page[pos] << 8 | page[pos + 1]);

		v->checked++;
		if (got != v->expect) {
			v->mismatches++;
			if (v->last_ok)
				v->breaks++;
			v->last_ok = 0;
			if (!v->have_first) {
				v->have_first = 1;
				v->first_page = page_no;
				v->first_index = i;
				v->first_expected = v->expect;
				v->first_got = got;
				v->first_delta = got < GPS_PATTERN_MOD ?
					(long)((got + GPS_PATTERN_MOD - v->expect) % GPS_PATTERN_MOD) : -1;
			}
		} else {
			v->last_ok = 1;
		}
		/* like stock: advance regardless of a mismatch */
		if (++v->expect == GPS_PATTERN_MOD)
			v->expect = 0;
	}
}
