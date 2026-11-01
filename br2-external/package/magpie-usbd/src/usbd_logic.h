/*
 * magpie-usbd: pure logic shared by the daemon and the host tests (no device I/O, no kernel headers).
 * Protocol: docs/PHONE_USB.md.
 */
#ifndef USBD_LOGIC_H
#define USBD_LOGIC_H

#include <stddef.h>
#include <stdint.h>

/* -------------------------------------------------------------- framing */

#define USBD_MAGIC 0x3150474Du /* "MGP1" little-endian */
#define USBD_HDR_LEN 16u
#define USBD_MAX_PAYLOAD 65536u
#define USBD_RESP_FLAG 0x8000u

#define USBD_CMD_STATUS 0x0001
#define USBD_CMD_LIST 0x0002
#define USBD_CMD_READ 0x0003
#define USBD_CMD_HASH 0x0004
#define USBD_CMD_DELETE 0x0005
#define USBD_CMD_TIME 0x0006
#define USBD_CMD_SETTIME 0x0007

struct usbd_hdr {
	uint32_t magic;
	uint16_t cmd;
	uint16_t flags;
	uint32_t seq;
	uint32_t len;
};

#define USBD_HDR_BAD_MAGIC (-1) /* header not parsable: reply with cmd 0x8000, seq 0 */
#define USBD_HDR_BAD_CMD (-2)   /* parsable, cmd unknown */
#define USBD_HDR_BAD_LEN (-3)   /* parsable, len > USBD_MAX_PAYLOAD */

void usbd_hdr_pack(const struct usbd_hdr *h, uint8_t out[USBD_HDR_LEN]);
/* Always fills *h (when the buffer has 16 bytes). Returns 0 or one of the USBD_HDR_BAD_* codes. */
int usbd_hdr_unpack(const uint8_t in[USBD_HDR_LEN], struct usbd_hdr *h);

/* -------------------------------------------------------------- file names */

#define USBD_NAME_MAX 32 /* buffer size for a valid name incl. NUL (longest is 25 + 1) */

#define USBD_KIND_NONE 0
#define USBD_KIND_JPG 1
#define USBD_KIND_SNAP 2
#define USBD_KIND_SNAP_TIME 3

/* ^[0-9]{8}-[0-9]{6}\.(jpg|snap|snap\.time)$ -> USBD_KIND_*, or USBD_KIND_NONE when s does not match */
int usbd_name_kind(const char *s);

/* -------------------------------------------------------------- JSON reader */

/*
 * Minimal reader for the request objects in the protocol. The whole document must be well-formed JSON
 * (a single object, nesting depth <= 8, nothing after it); only top-level keys are looked up, the first
 * occurrence wins. Return values: 0 ok, -ENOENT key missing, -EINVAL malformed or wrong type,
 * -ENAMETOOLONG string does not fit the output buffer. Strings are unescaped and must not contain NUL.
 */
int usbd_json_get_str(const char *js, size_t len, const char *key, char *out, size_t outsz);
int usbd_json_get_int(const char *js, size_t len, const char *key, int64_t *out);

#define USBD_DELETE_MAX 500
#define USBD_SHA_HEX 64

struct usbd_del_entry {
	char name[USBD_NAME_MAX];
	char sha[USBD_SHA_HEX + 1]; /* lower-cased */
};

/*
 * {"files":[{"name":..,"sha256":..},..]}: every name must be valid and every sha256 64 hex digits.
 * Returns 0 and the entry count, or -EINVAL (malformed, bad name, bad hash, more than max entries).
 */
int usbd_json_parse_delete(const char *js, size_t len, struct usbd_del_entry *out, size_t max, size_t *count);

#define USBD_LIST_MAX 500
#define USBD_LIST_DEFAULT 200

/* {"after":"<name>"|"","max":N}: absent keys give "" and 200; an empty payload also gives the defaults. */
int usbd_parse_list_req(const char *js, size_t len, char after[USBD_NAME_MAX], unsigned *max);

/* -------------------------------------------------------------- JSON writer */

/* Appends into a fixed buffer; once it is full, err is set and nothing more is written. Always NUL-terminated. */
struct usbd_jw {
	char *buf;
	size_t cap;
	size_t len;
	int err;
};

void usbd_jw_init(struct usbd_jw *w, char *buf, size_t cap);
void usbd_jw_raw(struct usbd_jw *w, const char *s);
void usbd_jw_fmt(struct usbd_jw *w, const char *fmt, ...) __attribute__((format(printf, 2, 3)));
/* "..." with escapes for quote, backslash and control characters; other bytes pass through (UTF-8) */
void usbd_jw_str(struct usbd_jw *w, const char *s);
void usbd_jw_int(struct usbd_jw *w, int64_t v);

/* -------------------------------------------------------------- SHA-256 */

struct usbd_sha256 {
	uint32_t h[8];
	uint8_t buf[64];
	uint64_t total;
};

void usbd_sha256_init(struct usbd_sha256 *c);
void usbd_sha256_update(struct usbd_sha256 *c, const void *data, size_t n);
void usbd_sha256_final(struct usbd_sha256 *c, uint8_t out[32]);
void usbd_hex(const uint8_t *d, size_t n, char *out); /* lower-case, writes 2n + 1 bytes */

/* -------------------------------------------------------------- LIST paging */

struct usbd_fent {
	char name[USBD_NAME_MAX];
	uint64_t size;
	int64_t mtime;
};

/*
 * Sorts e[0..n) ascending by name, then selects the entries strictly after `after` ("" = from the start),
 * at most `max`. Returns the number selected; *start is the index of the first one, *more tells whether
 * further entries follow the selection.
 */
size_t usbd_list_page(struct usbd_fent *e, size_t n, const char *after, size_t max, size_t *start, int *more);

/* -------------------------------------------------------------- RTT calibration */

/*
 * One line for /data/time/rtt_cal (no newline):
 *   cal=<ISO UTC>Z epoch_fit=<s> diff_ms=<ms> fit_roundtrip_ms=0.0
 * epoch_fit = t_after - count * pres / 32768, diff_ms = (epoch_fit - epoch_utc) * 1000.
 * t_after is the system clock (seconds since the epoch) right after it was set. Returns the line length,
 * or -1 if it does not fit.
 */
int usbd_rtt_cal_line(char *out, size_t cap, double t_after, uint32_t count, uint32_t pres, double epoch_utc);

/* last line of /data/time/rtt_epoch containing "epoch_utc=<s> pres=<n>": 0 ok, -1 none/malformed */
int usbd_rtt_epoch_parse(const char *text, double *epoch_utc, unsigned *pres);

/* -------------------------------------------------------------- helper tool output */

/* magpie-battery: the "raw: N" line. 0 ok, -1 not found */
int usbd_parse_battery(const char *text, int64_t *raw);
/* magpie-charge: "charging (..." -> 1, "not charging (..." -> 0, anything else -1 */
int usbd_parse_charge(const char *text);

#endif
