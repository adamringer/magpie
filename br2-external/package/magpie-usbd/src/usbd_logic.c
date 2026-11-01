/*
 * magpie-usbd: pure logic, see usbd_logic.h. Plain C99 plus gmtime_r; nothing here touches a device.
 *
 * The JSON reader is hand-rolled on purpose: the requests are small, fixed shapes, and every input comes
 * from a USB host we do not trust, so it validates the whole document and never writes past the caller's
 * buffers.
 */
#include <errno.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "usbd_logic.h"

/* -------------------------------------------------------------- framing */

static void put_le16(uint8_t *p, uint16_t v)
{
	p[0] = (uint8_t)v;
	p[1] = (uint8_t)(v >> 8);
}

static void put_le32(uint8_t *p, uint32_t v)
{
	p[0] = (uint8_t)v;
	p[1] = (uint8_t)(v >> 8);
	p[2] = (uint8_t)(v >> 16);
	p[3] = (uint8_t)(v >> 24);
}

static uint16_t get_le16(const uint8_t *p)
{
	return (uint16_t)(p[0] | p[1] << 8);
}

static uint32_t get_le32(const uint8_t *p)
{
	return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

void usbd_hdr_pack(const struct usbd_hdr *h, uint8_t out[USBD_HDR_LEN])
{
	put_le32(out, h->magic);
	put_le16(out + 4, h->cmd);
	put_le16(out + 6, h->flags);
	put_le32(out + 8, h->seq);
	put_le32(out + 12, h->len);
}

int usbd_hdr_unpack(const uint8_t in[USBD_HDR_LEN], struct usbd_hdr *h)
{
	h->magic = get_le32(in);
	h->cmd = get_le16(in + 4);
	h->flags = get_le16(in + 6);
	h->seq = get_le32(in + 8);
	h->len = get_le32(in + 12);
	if (h->magic != USBD_MAGIC)
		return USBD_HDR_BAD_MAGIC;
	if (h->len > USBD_MAX_PAYLOAD)
		return USBD_HDR_BAD_LEN;
	if (h->cmd < USBD_CMD_STATUS || h->cmd > USBD_CMD_SETTIME)
		return USBD_HDR_BAD_CMD;
	return 0;
}

/* -------------------------------------------------------------- file names */

static int is_digits(const char *s, int n)
{
	int i;

	for (i = 0; i < n; i++)
		if (s[i] < '0' || s[i] > '9')
			return 0;
	return 1;
}

int usbd_name_kind(const char *s)
{
	if (!s || !is_digits(s, 8) || s[8] != '-' || !is_digits(s + 9, 6) || s[15] != '.')
		return USBD_KIND_NONE;
	s += 16;
	if (!strcmp(s, "jpg"))
		return USBD_KIND_JPG;
	if (!strcmp(s, "snap"))
		return USBD_KIND_SNAP;
	if (!strcmp(s, "snap.time"))
		return USBD_KIND_SNAP_TIME;
	return USBD_KIND_NONE;
}

/* -------------------------------------------------------------- JSON reader */

#define JS_MAXDEPTH 8
#define JS_KEYMAX 32

struct jp {
	const char *p;
	const char *e;
};

static void js_ws(struct jp *j)
{
	while (j->p < j->e && (*j->p == ' ' || *j->p == '\t' || *j->p == '\n' || *j->p == '\r'))
		j->p++;
}

static int hexval(int c)
{
	if (c >= '0' && c <= '9')
		return c - '0';
	if (c >= 'a' && c <= 'f')
		return c - 'a' + 10;
	if (c >= 'A' && c <= 'F')
		return c - 'A' + 10;
	return -1;
}

struct sink {
	char *out;
	size_t cap;
	size_t n;
	int over;
};

static void sink_put(struct sink *s, unsigned c)
{
	if (!s->out)
		return;
	if (s->n + 1 < s->cap)
		s->out[s->n++] = (char)c;
	else
		s->over = 1;
}

static void sink_utf8(struct sink *s, uint32_t cp)
{
	if (cp < 0x80) {
		sink_put(s, cp);
	} else if (cp < 0x800) {
		sink_put(s, 0xC0 | (cp >> 6));
		sink_put(s, 0x80 | (cp & 0x3F));
	} else if (cp < 0x10000) {
		sink_put(s, 0xE0 | (cp >> 12));
		sink_put(s, 0x80 | ((cp >> 6) & 0x3F));
		sink_put(s, 0x80 | (cp & 0x3F));
	} else {
		sink_put(s, 0xF0 | (cp >> 18));
		sink_put(s, 0x80 | ((cp >> 12) & 0x3F));
		sink_put(s, 0x80 | ((cp >> 6) & 0x3F));
		sink_put(s, 0x80 | (cp & 0x3F));
	}
}

static int js_hex4(struct jp *j, uint32_t *v)
{
	int i;

	if (j->e - j->p < 4)
		return -EINVAL;
	*v = 0;
	for (i = 0; i < 4; i++) {
		int d = hexval((unsigned char)j->p[i]);

		if (d < 0)
			return -EINVAL;
		*v = *v << 4 | (uint32_t)d;
	}
	j->p += 4;
	return 0;
}

/* j->p at the opening quote. out may be NULL (validate only). */
static int js_string(struct jp *j, char *out, size_t outsz)
{
	struct sink s = { out, outsz, 0, 0 };

	if (j->p >= j->e || *j->p != '"')
		return -EINVAL;
	j->p++;
	for (;;) {
		unsigned c;

		if (j->p >= j->e)
			return -EINVAL; /* truncated */
		c = (unsigned char)*j->p++;
		if (c == '"')
			break;
		if (c < 0x20)
			return -EINVAL;
		if (c != '\\') {
			sink_put(&s, c);
			continue;
		}
		if (j->p >= j->e)
			return -EINVAL;
		c = (unsigned char)*j->p++;
		switch (c) {
		case '"': case '\\': case '/':
			sink_put(&s, c);
			break;
		case 'b': sink_put(&s, '\b'); break;
		case 'f': sink_put(&s, '\f'); break;
		case 'n': sink_put(&s, '\n'); break;
		case 'r': sink_put(&s, '\r'); break;
		case 't': sink_put(&s, '\t'); break;
		case 'u': {
			uint32_t cp, lo;

			if (js_hex4(j, &cp))
				return -EINVAL;
			if (cp >= 0xDC00 && cp <= 0xDFFF)
				return -EINVAL; /* lone low surrogate */
			if (cp >= 0xD800 && cp <= 0xDBFF) {
				if (j->e - j->p < 2 || j->p[0] != '\\' || j->p[1] != 'u')
					return -EINVAL;
				j->p += 2;
				if (js_hex4(j, &lo) || lo < 0xDC00 || lo > 0xDFFF)
					return -EINVAL;
				cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
			}
			if (cp == 0)
				return -EINVAL; /* an embedded NUL would truncate names */
			sink_utf8(&s, cp);
			break;
		}
		default:
			return -EINVAL;
		}
	}
	if (out) {
		if (s.over || outsz == 0) {
			if (outsz)
				out[0] = 0;
			return -ENAMETOOLONG;
		}
		out[s.n] = 0;
	}
	return 0;
}

/* JSON number grammar; *is_int tells whether it has neither fraction nor exponent */
static int js_number(struct jp *j, int *is_int, int *neg, uint64_t *mag, int *overflow)
{
	const char *p = j->p;
	const char *e = j->e;
	uint64_t m = 0;
	int ovf = 0, ng = 0, ndig = 0;

	if (p < e && *p == '-') {
		ng = 1;
		p++;
	}
	if (p >= e || *p < '0' || *p > '9')
		return -EINVAL;
	if (*p == '0') {
		p++;
		ndig = 1;
	} else {
		while (p < e && *p >= '0' && *p <= '9') {
			unsigned d = (unsigned)(*p - '0');

			if (m > (UINT64_MAX - d) / 10)
				ovf = 1;
			else
				m = m * 10 + d;
			p++;
			ndig++;
		}
	}
	*is_int = 1;
	if (p < e && *p == '.') {
		*is_int = 0;
		p++;
		if (p >= e || *p < '0' || *p > '9')
			return -EINVAL;
		while (p < e && *p >= '0' && *p <= '9')
			p++;
	}
	if (p < e && (*p == 'e' || *p == 'E')) {
		*is_int = 0;
		p++;
		if (p < e && (*p == '+' || *p == '-'))
			p++;
		if (p >= e || *p < '0' || *p > '9')
			return -EINVAL;
		while (p < e && *p >= '0' && *p <= '9')
			p++;
	}
	/* "01" is not JSON: after a leading zero no digit may follow */
	if (ndig == 1 && p < e && *p >= '0' && *p <= '9')
		return -EINVAL;
	j->p = p;
	if (neg)
		*neg = ng;
	if (mag)
		*mag = m;
	if (overflow)
		*overflow = ovf;
	return 0;
}

static int js_literal(struct jp *j, const char *lit)
{
	size_t n = strlen(lit);

	if ((size_t)(j->e - j->p) < n || memcmp(j->p, lit, n))
		return -EINVAL;
	j->p += n;
	return 0;
}

static int js_skip_value(struct jp *j, int depth);

/*
 * Walks the object at j->p. If key is non-NULL, *val receives the position of the first member of that name
 * (before ws), else val is untouched. Returns 0, -ENOENT when the key was not there, -EINVAL if malformed.
 */
static int js_object(struct jp *j, int depth, const char *key, struct jp *val)
{
	int found = 0;

	if (depth > JS_MAXDEPTH || j->p >= j->e || *j->p != '{')
		return -EINVAL;
	j->p++;
	js_ws(j);
	if (j->p < j->e && *j->p == '}') {
		j->p++;
		return key ? -ENOENT : 0;
	}
	for (;;) {
		char k[JS_KEYMAX];
		int r, match;
		struct jp v;

		js_ws(j);
		r = js_string(j, k, sizeof(k));
		if (r && r != -ENAMETOOLONG)
			return -EINVAL;
		match = key && !r && !strcmp(k, key); /* a key too long for k cannot match any of ours */
		js_ws(j);
		if (j->p >= j->e || *j->p != ':')
			return -EINVAL;
		j->p++;
		js_ws(j);
		v = *j;
		if (js_skip_value(j, depth + 1))
			return -EINVAL;
		if (match && !found) {
			found = 1;
			*val = v;
		}
		js_ws(j);
		if (j->p >= j->e)
			return -EINVAL;
		if (*j->p == ',') {
			j->p++;
			continue;
		}
		if (*j->p == '}') {
			j->p++;
			break;
		}
		return -EINVAL;
	}
	return key && !found ? -ENOENT : 0;
}

static int js_array(struct jp *j, int depth)
{
	if (depth > JS_MAXDEPTH || j->p >= j->e || *j->p != '[')
		return -EINVAL;
	j->p++;
	js_ws(j);
	if (j->p < j->e && *j->p == ']') {
		j->p++;
		return 0;
	}
	for (;;) {
		js_ws(j);
		if (js_skip_value(j, depth + 1))
			return -EINVAL;
		js_ws(j);
		if (j->p >= j->e)
			return -EINVAL;
		if (*j->p == ',') {
			j->p++;
			continue;
		}
		if (*j->p == ']') {
			j->p++;
			return 0;
		}
		return -EINVAL;
	}
}

static int js_skip_value(struct jp *j, int depth)
{
	int is_int;

	if (j->p >= j->e)
		return -EINVAL;
	switch (*j->p) {
	case '"':
		return js_string(j, NULL, 0) ? -EINVAL : 0;
	case '{':
		return js_object(j, depth, NULL, NULL);
	case '[':
		return js_array(j, depth);
	case 't':
		return js_literal(j, "true");
	case 'f':
		return js_literal(j, "false");
	case 'n':
		return js_literal(j, "null");
	default:
		return js_number(j, &is_int, NULL, NULL, NULL);
	}
}

/* whole-document lookup of a top-level key; *val = position of its value */
static int js_find(const char *js, size_t len, const char *key, struct jp *val)
{
	struct jp j = { js, js + len };
	int r;

	if (!js)
		return -EINVAL;
	js_ws(&j);
	r = js_object(&j, 1, key, val);
	if (r && r != -ENOENT)
		return -EINVAL;
	js_ws(&j);
	if (j.p != j.e)
		return -EINVAL; /* trailing garbage */
	return r;
}

int usbd_json_get_str(const char *js, size_t len, const char *key, char *out, size_t outsz)
{
	struct jp v;
	int r = js_find(js, len, key, &v);

	if (r)
		return r;
	return js_string(&v, out, outsz); /* 0, -EINVAL (also: value is not a string) or -ENAMETOOLONG */
}

int usbd_json_get_int(const char *js, size_t len, const char *key, int64_t *out)
{
	struct jp v;
	int r = js_find(js, len, key, &v);
	int is_int, neg, ovf;
	uint64_t mag;

	if (r)
		return r;
	if (v.p >= v.e || js_number(&v, &is_int, &neg, &mag, &ovf) || !is_int || ovf)
		return -EINVAL;
	if (neg ? mag > (uint64_t)INT64_MAX + 1 : mag > (uint64_t)INT64_MAX)
		return -EINVAL;
	*out = neg ? (int64_t)(0 - mag) : (int64_t)mag;
	return 0;
}

static int get_name(const char *js, size_t len, const char *key, char name[USBD_NAME_MAX])
{
	if (usbd_json_get_str(js, len, key, name, USBD_NAME_MAX) || usbd_name_kind(name) == USBD_KIND_NONE)
		return -EINVAL;
	return 0;
}

int usbd_json_parse_delete(const char *js, size_t len, struct usbd_del_entry *out, size_t max, size_t *count)
{
	struct jp arr, j;
	size_t n = 0;
	int r = js_find(js, len, "files", &arr);

	if (r)
		return -EINVAL;
	j = arr;
	if (j.p >= j.e || *j.p != '[')
		return -EINVAL;
	j.p++;
	js_ws(&j);
	if (j.p < j.e && *j.p == ']') {
		*count = 0;
		return 0;
	}
	for (;;) {
		struct jp obj, end;
		size_t i;

		js_ws(&j);
		obj = j;
		end = j;
		if (js_object(&end, 3, NULL, NULL)) /* an element must be an object (depth: doc 1, array 2, element 3) */
			return -EINVAL;
		if (n >= max)
			return -EINVAL;
		/* look the two keys up inside this element only */
		if (get_name(obj.p, (size_t)(end.p - obj.p), "name", out[n].name))
			return -EINVAL;
		if (usbd_json_get_str(obj.p, (size_t)(end.p - obj.p), "sha256", out[n].sha, sizeof(out[n].sha)))
			return -EINVAL;
		if (strlen(out[n].sha) != USBD_SHA_HEX)
			return -EINVAL;
		for (i = 0; i < USBD_SHA_HEX; i++) {
			char c = out[n].sha[i];

			if (hexval((unsigned char)c) < 0)
				return -EINVAL;
			if (c >= 'A' && c <= 'F')
				out[n].sha[i] = (char)(c - 'A' + 'a');
		}
		n++;
		j = end;
		js_ws(&j);
		if (j.p >= j.e)
			return -EINVAL;
		if (*j.p == ',') {
			j.p++;
			continue;
		}
		if (*j.p == ']')
			break;
		return -EINVAL;
	}
	*count = n;
	return 0;
}

int usbd_parse_list_req(const char *js, size_t len, char after[USBD_NAME_MAX], unsigned *max)
{
	int64_t m;
	int r;

	after[0] = 0;
	*max = USBD_LIST_DEFAULT;
	if (len == 0)
		return 0;
	r = usbd_json_get_str(js, len, "after", after, USBD_NAME_MAX);
	if (r == -ENOENT)
		after[0] = 0;
	else if (r)
		return -EINVAL;
	if (after[0] && usbd_name_kind(after) == USBD_KIND_NONE)
		return -EINVAL;
	r = usbd_json_get_int(js, len, "max", &m);
	if (r == -ENOENT)
		return 0;
	if (r || m < 1 || m > USBD_LIST_MAX)
		return -EINVAL;
	*max = (unsigned)m;
	return 0;
}

/* -------------------------------------------------------------- JSON writer */

void usbd_jw_init(struct usbd_jw *w, char *buf, size_t cap)
{
	w->buf = buf;
	w->cap = cap;
	w->len = 0;
	w->err = cap == 0;
	if (cap)
		buf[0] = 0;
}

static void jw_put(struct usbd_jw *w, const char *s, size_t n)
{
	if (w->err)
		return;
	if (n >= w->cap - w->len) {
		w->err = 1;
		return;
	}
	memcpy(w->buf + w->len, s, n);
	w->len += n;
	w->buf[w->len] = 0;
}

void usbd_jw_raw(struct usbd_jw *w, const char *s)
{
	jw_put(w, s, strlen(s));
}

void usbd_jw_fmt(struct usbd_jw *w, const char *fmt, ...)
{
	va_list ap;
	int n;

	if (w->err)
		return;
	va_start(ap, fmt);
	n = vsnprintf(w->buf + w->len, w->cap - w->len, fmt, ap);
	va_end(ap);
	if (n < 0 || (size_t)n >= w->cap - w->len) {
		w->err = 1;
		w->buf[w->len] = 0;
		return;
	}
	w->len += (size_t)n;
}

void usbd_jw_str(struct usbd_jw *w, const char *s)
{
	jw_put(w, "\"", 1);
	for (; *s; s++) {
		unsigned char c = (unsigned char)*s;
		char esc[8];

		switch (c) {
		case '"': jw_put(w, "\\\"", 2); break;
		case '\\': jw_put(w, "\\\\", 2); break;
		case '\b': jw_put(w, "\\b", 2); break;
		case '\f': jw_put(w, "\\f", 2); break;
		case '\n': jw_put(w, "\\n", 2); break;
		case '\r': jw_put(w, "\\r", 2); break;
		case '\t': jw_put(w, "\\t", 2); break;
		default:
			if (c < 0x20) {
				snprintf(esc, sizeof(esc), "\\u%04x", c);
				jw_put(w, esc, 6);
			} else {
				jw_put(w, (const char *)&c, 1);
			}
		}
	}
	jw_put(w, "\"", 1);
}

void usbd_jw_int(struct usbd_jw *w, int64_t v)
{
	usbd_jw_fmt(w, "%lld", (long long)v);
}

/* -------------------------------------------------------------- SHA-256 (FIPS 180-4) */

static const uint32_t K256[64] = {
	0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
	0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
	0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
	0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
	0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
	0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
	0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
	0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
};

#define ROR(x, n) (((x) >> (n)) | ((x) << (32 - (n))))

static void sha_block(struct usbd_sha256 *c, const uint8_t *p)
{
	uint32_t w[64], a, b, cc, d, e, f, g, h;
	int i;

	for (i = 0; i < 16; i++)
		w[i] = (uint32_t)p[4 * i] << 24 | (uint32_t)p[4 * i + 1] << 16 | (uint32_t)p[4 * i + 2] << 8 | p[4 * i + 3];
	for (i = 16; i < 64; i++) {
		uint32_t s0 = ROR(w[i - 15], 7) ^ ROR(w[i - 15], 18) ^ (w[i - 15] >> 3);
		uint32_t s1 = ROR(w[i - 2], 17) ^ ROR(w[i - 2], 19) ^ (w[i - 2] >> 10);

		w[i] = w[i - 16] + s0 + w[i - 7] + s1;
	}
	a = c->h[0]; b = c->h[1]; cc = c->h[2]; d = c->h[3];
	e = c->h[4]; f = c->h[5]; g = c->h[6]; h = c->h[7];
	for (i = 0; i < 64; i++) {
		uint32_t t1 = h + (ROR(e, 6) ^ ROR(e, 11) ^ ROR(e, 25)) + ((e & f) ^ (~e & g)) + K256[i] + w[i];
		uint32_t t2 = (ROR(a, 2) ^ ROR(a, 13) ^ ROR(a, 22)) + ((a & b) ^ (a & cc) ^ (b & cc));

		h = g; g = f; f = e; e = d + t1;
		d = cc; cc = b; b = a; a = t1 + t2;
	}
	c->h[0] += a; c->h[1] += b; c->h[2] += cc; c->h[3] += d;
	c->h[4] += e; c->h[5] += f; c->h[6] += g; c->h[7] += h;
}

void usbd_sha256_init(struct usbd_sha256 *c)
{
	static const uint32_t init[8] = {
		0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
	};

	memcpy(c->h, init, sizeof(init));
	c->total = 0;
}

void usbd_sha256_update(struct usbd_sha256 *c, const void *data, size_t n)
{
	const uint8_t *p = data;
	size_t fill = (size_t)(c->total & 63);

	c->total += n;
	if (fill) {
		size_t take = 64 - fill;

		if (take > n)
			take = n;
		memcpy(c->buf + fill, p, take);
		p += take;
		n -= take;
		if (fill + take < 64)
			return;
		sha_block(c, c->buf);
	}
	while (n >= 64) {
		sha_block(c, p);
		p += 64;
		n -= 64;
	}
	if (n)
		memcpy(c->buf, p, n);
}

void usbd_sha256_final(struct usbd_sha256 *c, uint8_t out[32])
{
	uint64_t bits = c->total * 8;
	uint8_t pad[72];
	size_t fill = (size_t)(c->total & 63);
	size_t padlen = (fill < 56 ? 56 : 120) - fill;
	int i;

	memset(pad, 0, sizeof(pad));
	pad[0] = 0x80;
	for (i = 0; i < 8; i++)
		pad[padlen + (size_t)i] = (uint8_t)(bits >> (56 - 8 * i));
	usbd_sha256_update(c, pad, padlen + 8);
	for (i = 0; i < 8; i++) {
		out[4 * i] = (uint8_t)(c->h[i] >> 24);
		out[4 * i + 1] = (uint8_t)(c->h[i] >> 16);
		out[4 * i + 2] = (uint8_t)(c->h[i] >> 8);
		out[4 * i + 3] = (uint8_t)c->h[i];
	}
}

void usbd_hex(const uint8_t *d, size_t n, char *out)
{
	static const char digits[] = "0123456789abcdef";
	size_t i;

	for (i = 0; i < n; i++) {
		out[2 * i] = digits[d[i] >> 4];
		out[2 * i + 1] = digits[d[i] & 15];
	}
	out[2 * n] = 0;
}

/* -------------------------------------------------------------- LIST paging */

static int fent_cmp(const void *a, const void *b)
{
	return strcmp(((const struct usbd_fent *)a)->name, ((const struct usbd_fent *)b)->name);
}

size_t usbd_list_page(struct usbd_fent *e, size_t n, const char *after, size_t max, size_t *start, int *more)
{
	size_t s = 0, cnt;

	if (n > 1)
		qsort(e, n, sizeof(*e), fent_cmp);
	while (s < n && strcmp(e[s].name, after) <= 0)
		s++;
	cnt = n - s;
	if (cnt > max)
		cnt = max;
	*start = s;
	*more = s + cnt < n;
	return cnt;
}

/* -------------------------------------------------------------- RTT calibration */

int usbd_rtt_cal_line(char *out, size_t cap, double t_after, uint32_t count, uint32_t pres, double epoch_utc)
{
	time_t sec = (time_t)t_after; /* truncates toward zero; times before 1970 are not expected */
	struct tm tm;
	char iso[32];
	double fit = t_after - (double)count * (double)pres / 32768.0;
	int n;

	if (!gmtime_r(&sec, &tm) || !strftime(iso, sizeof(iso), "%Y-%m-%dT%H:%M:%SZ", &tm))
		return -1;
	n = snprintf(out, cap, "cal=%s epoch_fit=%.4f diff_ms=%+.1f fit_roundtrip_ms=%.1f", iso, fit,
		     (fit - epoch_utc) * 1000.0, 0.0);
	return n < 0 || (size_t)n >= cap ? -1 : n;
}

int usbd_rtt_epoch_parse(const char *text, double *epoch_utc, unsigned *pres)
{
	size_t len = strlen(text), end = len, start;
	char line[256];
	const char *p;
	char *q;
	double ep;
	unsigned long pr;

	while (end && (text[end - 1] == '\n' || text[end - 1] == '\r' || text[end - 1] == ' '))
		end--;
	start = end;
	while (start && text[start - 1] != '\n')
		start--;
	if (end == start || end - start >= sizeof(line))
		return -1;
	memcpy(line, text + start, end - start);
	line[end - start] = 0;
	p = strstr(line, "epoch_utc=");
	if (!p)
		return -1;
	p += strlen("epoch_utc=");
	ep = strtod(p, &q);
	if (q == p || *q != ' ')
		return -1;
	p = strstr(q, "pres=");
	if (!p)
		return -1;
	p += strlen("pres=");
	pr = strtoul(p, &q, 10);
	if (q == p)
		return -1;
	*epoch_utc = ep;
	*pres = (unsigned)pr;
	return 0;
}

/* -------------------------------------------------------------- helper tool output */

int usbd_parse_battery(const char *text, int64_t *raw)
{
	const char *p = text;

	while (*p) {
		if (!strncmp(p, "raw:", 4)) {
			const char *q = p + 4;
			char *end;
			long long v;

			while (*q == ' ')
				q++;
			if (*q < '0' || *q > '9')
				return -1;
			v = strtoll(q, &end, 10);
			if (*end && *end != '\n' && *end != '\r' && *end != ' ')
				return -1;
			*raw = v;
			return 0;
		}
		p = strchr(p, '\n');
		if (!p)
			break;
		p++;
	}
	return -1;
}

int usbd_parse_charge(const char *text)
{
	while (*text == ' ' || *text == '\n' || *text == '\t')
		text++;
	if (!strncmp(text, "not charging", 12))
		return 0;
	if (!strncmp(text, "charging", 8))
		return 1;
	return -1;
}
