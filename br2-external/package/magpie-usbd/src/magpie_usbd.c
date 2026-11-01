/*
 * magpie-usbd: FunctionFS daemon that serves the photos and captures of the Clip to a phone over a
 * vendor-specific bulk interface (WebUSB). Protocol and rules: docs/PHONE_USB.md.
 *
 * Usage: magpie-usbd [-d] /dev/ffs-magpie
 *
 * The daemon writes the FunctionFS descriptors and strings to <dir>/ep0, opens <dir>/ep1 (OUT, requests) and
 * <dir>/ep2 (IN, responses) and then serves one request at a time. It never exits because the host went away:
 * endpoint errors end the current request and the daemon waits for the next ENABLE (or BIND/RESUME).
 *
 * FunctionFS notes (checked against drivers/usb/gadget/function/f_fs.c of the 6.18 kernel):
 *  - Endpoint files cannot be polled, only ep0 can. A read on ep1 therefore blocks; to keep serving ep0
 *    (SETUP events must be answered or the host's control transfer times out) while idle, the wait for the
 *    first bytes of a request is interrupted by a 250 ms interval timer. The timer is only active during
 *    that wait (SIGALRM is blocked otherwise), so a transfer in progress is never interrupted.
 *  - Reads and writes are done in 16 KiB pieces (multiples of the 512-byte high-speed packet size): the
 *    kernel allocates one contiguous buffer per call and 64 KiB may not be available on a small system.
 *    Pieces of full packets are invisible on the wire; only the last one ends the USB transfer.
 *  - A read is rounded up to a whole number of packets by the kernel. We always ask for a whole number of
 *    packets as well, so a host that sends a little too much cannot make the controller report an overflow.
 *  - To stall a SETUP request, do I/O in the wrong direction on ep0: read for an IN request, write for OUT.
 *
 * Exit codes: 0 ok (-h), 1 usage, 2 setup error.
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include <linux/usb/ch9.h>
#include <linux/usb/functionfs.h>

#include "usbd_logic.h"

#define USBD_VERSION "1.0"

#define EXIT_USAGE 1
#define EXIT_SETUP 2

/* overridable only so that the protocol can be exercised on a development host */
#ifndef PHOTOS_DIR
#define PHOTOS_DIR "/data/photos"
#endif
#ifndef DATA_DIR
#define DATA_DIR "/data"
#endif
#define TIME_DIR "/data/time"
#define RTT_EPOCH_FILE TIME_DIR "/rtt_epoch"
#define RTT_CAL_FILE TIME_DIR "/rtt_cal"
#define RTT_PRES_MAGPIE 32

#define PIECE 16384u               /* one read()/write() on an endpoint */
#define PKT 512u                   /* high-speed bulk packet size; also a multiple of the full-speed 64 */
#define RXCAP (USBD_HDR_LEN + USBD_MAX_PAYLOAD + PKT)
#define TXCAP (USBD_HDR_LEN + USBD_MAX_PAYLOAD)
#define RES_OFF (USBD_HDR_LEN + 4) /* the result follows header and status in tx */
#define RES_CAP (TXCAP - RES_OFF)
#define IDLE_TICK_US 250000        /* ep0 service interval while waiting for a request */

static int g_debug;
static const char *g_dir;
static int g_ep0 = -1, g_ep1 = -1, g_ep2 = -1;
static int g_up;     /* gadget bound/enabled (BIND, ENABLE) */
static int g_susp;   /* bus suspended */
static int g_dirty;  /* endpoint files must be reopened before the next request (after UNBIND/DISABLE/errors) */
static int g_discard; /* after a bad header: drop input until a chunk starts with a valid header */
static volatile uint32_t *g_sysc;
static sigset_t g_alrm;

static uint8_t g_rx[RXCAP];
static uint8_t g_tx[TXCAP];

/* -------------------------------------------------------------- logging */

static void dlog(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void dlog(const char *fmt, ...)
{
	va_list ap;

	if (!g_debug)
		return;
	fputs("magpie-usbd: ", stderr);
	va_start(ap, fmt);
	vfprintf(stderr, fmt, ap);
	va_end(ap);
	fputc('\n', stderr);
}

/* one line to the kernel log, like the init scripts do (one write() = one record) */
static void klog(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void klog(const char *fmt, ...)
{
	char line[900];
	int n, fd;
	va_list ap;

	n = snprintf(line, sizeof(line), "magpie-usbd: ");
	va_start(ap, fmt);
	vsnprintf(line + n, sizeof(line) - (size_t)n, fmt, ap);
	va_end(ap);
	dlog("%s", line + n);
	fd = open("/dev/kmsg", O_WRONLY | O_CLOEXEC);
	if (fd >= 0) {
		if (write(fd, line, strlen(line)) < 0)
			dlog("kmsg write failed: %s", strerror(errno));
		close(fd);
	}
}

/* -------------------------------------------------------------- RTT */

#define SYSC_PAGE 0xFFFFF000u
#define RTT_MR_OFF 0xE20
#define RTT_VR_OFF 0xE28

static void rtt_map(void)
{
	int fd = open("/dev/mem", O_RDONLY | O_SYNC | O_CLOEXEC);
	void *p;

	if (fd < 0)
		return;
	p = mmap(NULL, 4096, PROT_READ, MAP_SHARED, fd, (off_t)SYSC_PAGE);
	close(fd);
	if (p != MAP_FAILED)
		g_sysc = p;
}

/* RTT_VR; the counter is asynchronous to the CPU clock: read until two reads agree. -1 if unavailable. */
static int rtt_read(uint32_t *count, uint32_t *mr)
{
	uint32_t a, b;
	int tries = 0;

	if (!g_sysc)
		return -1;
	do {
		a = g_sysc[RTT_VR_OFF / 4];
		b = g_sysc[RTT_VR_OFF / 4];
	} while (a != b && ++tries < 1000);
	if (a != b)
		return -1;
	*count = a;
	*mr = g_sysc[RTT_MR_OFF / 4];
	return 0;
}

/* -------------------------------------------------------------- FunctionFS setup */

static void le16(uint8_t *p, unsigned v)
{
	p[0] = (uint8_t)v;
	p[1] = (uint8_t)(v >> 8);
}

static void le32(uint8_t *p, uint32_t v)
{
	le16(p, v & 0xFFFF);
	le16(p + 2, v >> 16);
}

/* interface 0, vendor class, two bulk endpoints; returns the new write position */
static uint8_t *put_speed(uint8_t *p, unsigned maxpkt)
{
	/* interface descriptor */
	p[0] = 9; p[1] = USB_DT_INTERFACE; p[2] = 0; p[3] = 0; p[4] = 2;
	p[5] = 0xFF; p[6] = 0; p[7] = 0; p[8] = 1; /* iInterface = string 1 */
	p += 9;
	/* ep1 OUT, ep2 IN */
	p[0] = 7; p[1] = USB_DT_ENDPOINT; p[2] = 0x01; p[3] = USB_ENDPOINT_XFER_BULK; le16(p + 4, maxpkt); p[6] = 0;
	p += 7;
	p[0] = 7; p[1] = USB_DT_ENDPOINT; p[2] = 0x82; p[3] = USB_ENDPOINT_XFER_BULK; le16(p + 4, maxpkt); p[6] = 0;
	p += 7;
	return p;
}

static int write_all_ep0(const uint8_t *buf, size_t n)
{
	ssize_t r = write(g_ep0, buf, n);

	if (r < 0 || (size_t)r != n) {
		fprintf(stderr, "magpie-usbd: writing ep0: %s\n", r < 0 ? strerror(errno) : "short write");
		return -1;
	}
	return 0;
}

static int write_descriptors(void)
{
	static const char name[] = "Magpie data";
	uint8_t buf[256], *p = buf + 12;
	uint8_t *fs_end, *hs_end;

	le32(p, 3); /* FS descriptor count */
	le32(p + 4, 3); /* HS descriptor count */
	p += 8;
	fs_end = put_speed(p, 64);
	hs_end = put_speed(fs_end, 512);
	le32(buf, FUNCTIONFS_DESCRIPTORS_MAGIC_V2);
	le32(buf + 4, (uint32_t)(hs_end - buf));
	le32(buf + 8, FUNCTIONFS_HAS_FS_DESC | FUNCTIONFS_HAS_HS_DESC);
	if (write_all_ep0(buf, (size_t)(hs_end - buf)))
		return -1;

	/* strings: 1 string in 1 language (0x0409) */
	p = buf + 16;
	le16(p, 0x0409);
	memcpy(p + 2, name, sizeof(name));
	p += 2 + sizeof(name);
	le32(buf, FUNCTIONFS_STRINGS_MAGIC);
	le32(buf + 4, (uint32_t)(p - buf));
	le32(buf + 8, 1);
	le32(buf + 12, 1);
	return write_all_ep0(buf, (size_t)(p - buf));
}

static int open_eps(void)
{
	char path[256];
	int a, b;

	snprintf(path, sizeof(path), "%s/ep1", g_dir);
	a = open(path, O_RDONLY | O_CLOEXEC);
	if (a < 0)
		return -1;
	snprintf(path, sizeof(path), "%s/ep2", g_dir);
	b = open(path, O_WRONLY | O_CLOEXEC);
	if (b < 0) {
		close(a);
		return -1;
	}
	if (g_ep1 >= 0)
		close(g_ep1);
	if (g_ep2 >= 0)
		close(g_ep2);
	g_ep1 = a;
	g_ep2 = b;
	return 0;
}

/* -------------------------------------------------------------- ep0 events */

static void stall_setup(const struct usb_ctrlrequest *s)
{
	char c = 0;
	ssize_t r;

	/* the "wrong" direction stalls the request; the kernel answers -EL2HLT */
	if (s->bRequestType & USB_DIR_IN)
		r = read(g_ep0, &c, 0);
	else
		r = write(g_ep0, &c, 0);
	dlog("SETUP type=%02x req=%02x val=%04x idx=%04x len=%u: stalled (%zd)", s->bRequestType, s->bRequest,
	     (unsigned)s->wValue, (unsigned)s->wIndex, (unsigned)s->wLength, r);
}

static void handle_event(const struct usb_functionfs_event *ev)
{
	switch (ev->type) {
	case FUNCTIONFS_BIND:
		dlog("event BIND");
		g_up = 1;
		break;
	case FUNCTIONFS_UNBIND:
		dlog("event UNBIND");
		g_up = 0;
		g_dirty = 1;
		break;
	case FUNCTIONFS_ENABLE:
		dlog("event ENABLE");
		g_up = 1;
		break;
	case FUNCTIONFS_DISABLE:
		dlog("event DISABLE");
		g_up = 0;
		g_dirty = 1;
		break;
	case FUNCTIONFS_SETUP:
		stall_setup(&ev->u.setup);
		break;
	case FUNCTIONFS_SUSPEND:
		dlog("event SUSPEND");
		g_susp = 1;
		break;
	case FUNCTIONFS_RESUME:
		dlog("event RESUME");
		g_susp = 0;
		break;
	default:
		dlog("event %u", ev->type);
	}
}

/* Waits up to timeout_ms for ep0 events (0 = just look) and handles all queued ones. Returns their count. */
static int service_ep0(int timeout_ms)
{
	struct pollfd p = { .fd = g_ep0, .events = POLLIN };
	int count = 0;

	if (poll(&p, 1, timeout_ms) <= 0 || !(p.revents & POLLIN))
		return 0;
	for (;;) {
		struct usb_functionfs_event ev[8];
		ssize_t n = read(g_ep0, ev, sizeof(ev));
		ssize_t i;

		if (n < 0 && errno == EINTR)
			continue;
		if (n <= 0)
			break;
		for (i = 0; i < n / (ssize_t)sizeof(ev[0]); i++) {
			handle_event(&ev[i]);
			count++;
		}
	}
	return count;
}

static int serving(void)
{
	return g_up && !g_susp;
}

/* An endpoint call failed (ESHUTDOWN, EIO, ECONNRESET, EAGAIN, ...): the request is over, never the daemon. */
static void io_failed(const char *what)
{
	int e = errno;

	dlog("%s failed: %s", what, strerror(e));
	/* the DISABLE/UNBIND event may follow the error by a moment; if none is queued, assume the worst */
	if (service_ep0(0) == 0) {
		g_up = 0;
		g_dirty = 1;
	}
	g_discard = 0;
}

/* Called when not serving: wait for events. A silent second means events were lost; then just try again
 * (a read on a disabled endpoint blocks inside the kernel until it is enabled, so trying is harmless). */
static void wait_serving(void)
{
	if (service_ep0(1000) == 0 && !serving()) {
		dlog("no event for 1 s, trying anyway");
		g_up = 1;
		g_susp = 0;
	}
}

/* -------------------------------------------------------------- endpoint I/O */

static void on_alarm(int sig)
{
	(void)sig;
}

/*
 * First bytes of a request. Returns the byte count, or -1 (errno set) on an endpoint error, or -2 if the
 * state changed while waiting (go back to the main loop).
 */
static ssize_t read_idle(void)
{
	static const struct itimerval on = { { 0, IDLE_TICK_US }, { 0, IDLE_TICK_US } };
	static const struct itimerval off;

	for (;;) {
		ssize_t n;
		int e;

		service_ep0(0);
		if (!serving() || g_dirty)
			return -2;
		setitimer(ITIMER_REAL, &on, NULL);
		sigprocmask(SIG_UNBLOCK, &g_alrm, NULL);
		n = read(g_ep1, g_rx, PKT);
		e = errno;
		sigprocmask(SIG_BLOCK, &g_alrm, NULL);
		setitimer(ITIMER_REAL, &off, NULL);
		if (n >= 0)
			return n;
		if (e == EINTR)
			continue;
		errno = e;
		return -1;
	}
}

/* read more of the current request, in whole packets and at most one piece */
static ssize_t read_more(size_t have, size_t need)
{
	size_t ask = (need - have + PKT - 1) / PKT * PKT;

	if (ask > PIECE)
		ask = PIECE;
	if (ask > RXCAP - have)
		ask = RXCAP - have;
	for (;;) {
		ssize_t n = read(g_ep1, g_rx + have, ask);

		if (n < 0 && errno == EINTR)
			continue;
		return n;
	}
}

static int send_all(const uint8_t *buf, size_t n)
{
	size_t off = 0;

	while (off < n) {
		size_t piece = n - off > PIECE ? PIECE : n - off;
		ssize_t w = write(g_ep2, buf + off, piece);

		if (w < 0 && errno == EINTR)
			continue;
		if (w <= 0) {
			if (w == 0)
				errno = EIO;
			return -1;
		}
		off += (size_t)w;
	}
	return 0;
}

/* result_len bytes of result are already in g_tx + RES_OFF */
static int send_response(const struct usbd_hdr *rq, int32_t status, size_t result_len)
{
	struct usbd_hdr h = { USBD_MAGIC, (uint16_t)(rq->cmd | USBD_RESP_FLAG), 0, rq->seq, (uint32_t)(4 + result_len) };

	usbd_hdr_pack(&h, g_tx);
	le32(g_tx + USBD_HDR_LEN, (uint32_t)status);
	dlog("resp cmd=%04x seq=%u status=%d len=%u", h.cmd, h.seq, status, h.len);
	return send_all(g_tx, RES_OFF + result_len);
}

/* -------------------------------------------------------------- files */

static int open_photo(const char *name, int *fd, struct stat *st)
{
	char path[sizeof(PHOTOS_DIR) + USBD_NAME_MAX + 2];

	snprintf(path, sizeof(path), "%s/%s", PHOTOS_DIR, name);
	*fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
	if (*fd < 0)
		return -errno;
	if (fstat(*fd, st) || !S_ISREG(st->st_mode)) {
		close(*fd);
		*fd = -1;
		return -EINVAL;
	}
	return 0;
}

/* sha256 of an open file, using scratch (the request buffer: nothing in it is needed any more). Returns bytes or -errno. */
static int64_t hash_fd(int fd, char hex[USBD_SHA_HEX + 1])
{
	struct usbd_sha256 c;
	uint8_t digest[32];
	int64_t total = 0;

	usbd_sha256_init(&c);
	for (;;) {
		ssize_t n = read(fd, g_rx, USBD_MAX_PAYLOAD);

		if (n < 0 && errno == EINTR)
			continue;
		if (n < 0)
			return -errno;
		if (n == 0)
			break;
		usbd_sha256_update(&c, g_rx, (size_t)n);
		total += n;
	}
	usbd_sha256_final(&c, digest);
	usbd_hex(digest, sizeof(digest), hex);
	return total;
}

static int fstat_photo(DIR *d, const char *name, struct stat *st)
{
	return fstatat(dirfd(d), name, st, AT_SYMLINK_NOFOLLOW) == 0 && S_ISREG(st->st_mode);
}

/* -------------------------------------------------------------- commands */

/* each handler fills g_tx + RES_OFF (at most RES_CAP bytes), sets *rl and returns 0 or -errno */

static int run_tool(const char *cmd, char *out, size_t cap)
{
	FILE *f = popen(cmd, "r");
	size_t n;
	int st;

	if (!f)
		return -1;
	n = fread(out, 1, cap - 1, f);
	out[n] = 0;
	st = pclose(f);
	return st != -1 && WIFEXITED(st) && WEXITSTATUS(st) == 0 ? 0 : -1;
}

static int64_t now_ms(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_REALTIME, &ts);
	return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static double uptime_s(void)
{
	FILE *f = fopen("/proc/uptime", "re");
	double u = 0;

	if (f) {
		if (fscanf(f, "%lf", &u) != 1)
			u = 0;
		fclose(f);
	}
	return u;
}

static int cmd_status(size_t *rl)
{
	struct usbd_jw w;
	char *res = (char *)g_tx + RES_OFF;
	char text[256];
	int64_t photos = 0, snaps = 0, snap_times = 0, bytes = 0, free_kb = 0, battery = 0;
	int have_battery, charge = -1;
	uint32_t rtt, mr;
	struct statvfs sv;
	struct dirent *de;
	DIR *d = opendir(PHOTOS_DIR);

	if (d) {
		while ((de = readdir(d))) {
			struct stat st;
			int kind = usbd_name_kind(de->d_name);

			if (kind == USBD_KIND_NONE || !fstat_photo(d, de->d_name, &st))
				continue;
			bytes += st.st_size;
			if (kind == USBD_KIND_JPG)
				photos++;
			else if (kind == USBD_KIND_SNAP)
				snaps++;
			else
				snap_times++;
		}
		closedir(d);
	} else if (errno != ENOENT) {
		return -errno;
	}
	if (statvfs(DATA_DIR, &sv) == 0)
		free_kb = (int64_t)((uint64_t)sv.f_bavail * sv.f_frsize / 1024);
	have_battery = !run_tool("magpie-battery 2>/dev/null", text, sizeof(text)) && !usbd_parse_battery(text, &battery);
	if (!run_tool("magpie-charge 2>/dev/null", text, sizeof(text)))
		charge = usbd_parse_charge(text);

	usbd_jw_init(&w, res, RES_CAP);
	usbd_jw_raw(&w, "{\"proto\":1,\"version\":");
	usbd_jw_str(&w, USBD_VERSION);
	usbd_jw_raw(&w, ",\"clip_utc_ms\":");
	usbd_jw_int(&w, now_ms());
	usbd_jw_fmt(&w, ",\"uptime_s\":%.2f,\"rtt\":", uptime_s());
	if (rtt_read(&rtt, &mr) == 0)
		usbd_jw_int(&w, rtt);
	else
		usbd_jw_raw(&w, "null");
	usbd_jw_fmt(&w, ",\"photos\":%lld,\"snaps\":%lld,\"snap_times\":%lld,\"bytes\":%lld,\"free_kb\":%lld,",
		    (long long)photos, (long long)snaps, (long long)snap_times, (long long)bytes, (long long)free_kb);
	usbd_jw_raw(&w, "\"battery_raw\":");
	if (have_battery)
		usbd_jw_int(&w, battery);
	else
		usbd_jw_raw(&w, "null");
	usbd_jw_raw(&w, ",\"charging\":");
	usbd_jw_raw(&w, charge < 0 ? "null" : charge ? "true" : "false");
	usbd_jw_raw(&w, "}");
	if (w.err)
		return -EMSGSIZE;
	*rl = w.len;
	return 0;
}

static int cmd_list(const uint8_t *pl, size_t plen, size_t *rl)
{
	char after[USBD_NAME_MAX];
	unsigned max;
	struct usbd_fent *ents = NULL;
	size_t n = 0, cap = 0, start, cnt, i;
	int more, first = 1, ret = 0;
	struct usbd_jw w;
	struct dirent *de;
	DIR *d;

	if (usbd_parse_list_req((const char *)pl, plen, after, &max))
		return -EINVAL;
	d = opendir(PHOTOS_DIR);
	if (!d && errno != ENOENT)
		return -errno;
	/* names only here; size and mtime are looked up for the page we return, not for every file */
	while (d && (de = readdir(d))) {
		if (usbd_name_kind(de->d_name) == USBD_KIND_NONE)
			continue;
		if (n == cap) {
			struct usbd_fent *bigger;

			cap = cap ? cap * 2 : 256;
			bigger = realloc(ents, cap * sizeof(*ents));
			if (!bigger) {
				free(ents);
				closedir(d);
				return -ENOMEM;
			}
			ents = bigger;
		}
		memset(&ents[n], 0, sizeof(ents[n]));
		memcpy(ents[n].name, de->d_name, strlen(de->d_name) + 1); /* a valid name is at most 25 bytes */
		n++;
	}
	cnt = usbd_list_page(ents, n, after, max, &start, &more);
	usbd_jw_init(&w, (char *)g_tx + RES_OFF, RES_CAP);
	usbd_jw_raw(&w, "{\"files\":[");
	for (i = 0; i < cnt; i++) {
		struct stat st;

		if (!fstat_photo(d, ents[start + i].name, &st))
			continue; /* deleted since readdir */
		if (!first)
			usbd_jw_raw(&w, ",");
		first = 0;
		usbd_jw_raw(&w, "{\"name\":");
		usbd_jw_str(&w, ents[start + i].name);
		usbd_jw_fmt(&w, ",\"size\":%lld,\"mtime\":%lld}", (long long)st.st_size, (long long)st.st_mtime);
	}
	usbd_jw_fmt(&w, "],\"more\":%s}", more ? "true" : "false");
	if (w.err)
		ret = -EMSGSIZE;
	else
		*rl = w.len;
	free(ents);
	if (d)
		closedir(d);
	return ret;
}

static int cmd_read(const uint8_t *pl, size_t plen, size_t *rl)
{
	char name[USBD_NAME_MAX];
	int64_t off, len;
	struct stat st;
	size_t got = 0;
	int fd, r;

	if (usbd_json_get_str((const char *)pl, plen, "name", name, sizeof(name)) || usbd_name_kind(name) == USBD_KIND_NONE ||
	    usbd_json_get_int((const char *)pl, plen, "offset", &off) ||
	    usbd_json_get_int((const char *)pl, plen, "len", &len) || off < 0 || len < 0 || len > (int64_t)RES_CAP)
		return -EINVAL;
	r = open_photo(name, &fd, &st);
	if (r)
		return r;
	while (got < (size_t)len) {
		ssize_t n = pread(fd, g_tx + RES_OFF + got, (size_t)len - got, (off_t)off + (off_t)got);

		if (n < 0 && errno == EINTR)
			continue;
		if (n < 0) {
			r = -errno;
			close(fd);
			return r;
		}
		if (n == 0)
			break; /* EOF, also when offset is at or past the end */
		got += (size_t)n;
	}
	close(fd);
	*rl = got;
	return 0;
}

static int cmd_hash(const uint8_t *pl, size_t plen, size_t *rl)
{
	char name[USBD_NAME_MAX], hex[USBD_SHA_HEX + 1];
	struct usbd_jw w;
	struct stat st;
	int64_t size;
	int fd, r;

	if (usbd_json_get_str((const char *)pl, plen, "name", name, sizeof(name)) || usbd_name_kind(name) == USBD_KIND_NONE)
		return -EINVAL;
	r = open_photo(name, &fd, &st);
	if (r)
		return r;
	size = hash_fd(fd, hex);
	close(fd);
	if (size < 0)
		return (int)size;
	usbd_jw_init(&w, (char *)g_tx + RES_OFF, RES_CAP);
	usbd_jw_raw(&w, "{\"name\":");
	usbd_jw_str(&w, name);
	usbd_jw_fmt(&w, ",\"size\":%lld,\"sha256\":\"%s\"}", (long long)size, hex);
	if (w.err)
		return -EMSGSIZE;
	*rl = w.len;
	return 0;
}

static int cmd_delete(const uint8_t *pl, size_t plen, size_t *rl)
{
	struct usbd_del_entry *e = malloc(USBD_DELETE_MAX * sizeof(*e));
	size_t n = 0, i, deleted = 0, logn = 0;
	int trunc = 0;
	char logbuf[700];
	struct usbd_jw w;
	int ret = 0;

	if (!e)
		return -ENOMEM;
	if (usbd_json_parse_delete((const char *)pl, plen, e, USBD_DELETE_MAX, &n)) {
		free(e);
		return -EINVAL;
	}
	logbuf[0] = 0;
	usbd_jw_init(&w, (char *)g_tx + RES_OFF, RES_CAP);
	usbd_jw_raw(&w, "{\"results\":[");
	for (i = 0; i < n; i++) {
		char hex[USBD_SHA_HEX + 1], path[sizeof(PHOTOS_DIR) + USBD_NAME_MAX + 2];
		const char *err = NULL;
		struct stat st;
		int fd;
		int64_t size;

		/* the request buffer is free scratch now (hash_fd), the entries were copied out of it */
		if (open_photo(e[i].name, &fd, &st)) {
			err = "not found";
		} else {
			size = hash_fd(fd, hex);
			close(fd);
			if (size < 0) {
				err = "read error";
			} else if (strcmp(hex, e[i].sha)) {
				err = "sha256 mismatch";
			} else {
				snprintf(path, sizeof(path), "%s/%s", PHOTOS_DIR, e[i].name);
				if (unlink(path))
					err = "unlink failed";
			}
		}
		if (i)
			usbd_jw_raw(&w, ",");
		usbd_jw_raw(&w, "{\"name\":");
		usbd_jw_str(&w, e[i].name);
		if (err) {
			usbd_jw_raw(&w, ",\"ok\":false,\"err\":");
			usbd_jw_str(&w, err);
			usbd_jw_raw(&w, "}");
		} else {
			usbd_jw_raw(&w, ",\"ok\":true}");
			deleted++;
			/* kmsg records are short: log as many names as fit */
			if (logn + strlen(e[i].name) + 2 < sizeof(logbuf))
				logn += (size_t)snprintf(logbuf + logn, sizeof(logbuf) - logn, "%s%s", logn ? "," : "", e[i].name);
			else
				trunc = 1;
		}
	}
	usbd_jw_raw(&w, "]}");
	klog("delete requested=%zu deleted=%zu names=%s%s", n, deleted, logbuf, trunc ? ",..." : "");
	if (w.err)
		ret = -EMSGSIZE;
	else
		*rl = w.len;
	free(e);
	return ret;
}

static int cmd_time(size_t *rl)
{
	struct timespec rt, mono;
	struct usbd_jw w;
	uint32_t rtt, mr;
	int have;

	clock_gettime(CLOCK_REALTIME, &rt);
	clock_gettime(CLOCK_MONOTONIC, &mono);
	have = rtt_read(&rtt, &mr) == 0;
	usbd_jw_init(&w, (char *)g_tx + RES_OFF, RES_CAP);
	usbd_jw_fmt(&w, "{\"clip_utc_ms\":%lld,\"mono_ns\":%lld,\"rtt\":", (long long)rt.tv_sec * 1000 + rt.tv_nsec / 1000000,
		    (long long)mono.tv_sec * 1000000000LL + mono.tv_nsec);
	if (have)
		usbd_jw_int(&w, rtt);
	else
		usbd_jw_raw(&w, "null");
	usbd_jw_raw(&w, "}");
	if (w.err)
		return -EMSGSIZE;
	*rl = w.len;
	return 0;
}

/* last 4 KiB of the RTT epoch file are enough to find its last line */
static int read_rtt_epoch(double *epoch, unsigned *pres)
{
	char text[4096];
	off_t size;
	ssize_t n;
	int fd = open(RTT_EPOCH_FILE, O_RDONLY | O_CLOEXEC);

	if (fd < 0)
		return -1;
	size = lseek(fd, 0, SEEK_END);
	if (size < 0 || (n = pread(fd, text, sizeof(text) - 1, size > (off_t)sizeof(text) - 1 ? size - (off_t)sizeof(text) + 1 : 0)) <= 0) {
		close(fd);
		return -1;
	}
	close(fd);
	text[n] = 0;
	return usbd_rtt_epoch_parse(text, epoch, pres);
}

static int append_rtt_cal(const char *line)
{
	char buf[256];
	int n = snprintf(buf, sizeof(buf), "%s\n", line);
	int fd = open(RTT_CAL_FILE, O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC, 0644);
	int ok;

	if (fd < 0)
		return -1;
	ok = write(fd, buf, (size_t)n) == n && fsync(fd) == 0;
	close(fd);
	return ok ? 0 : -1;
}

static int cmd_settime(const uint8_t *pl, size_t plen, size_t *rl)
{
	int64_t utc_ms, before_ms, after_ms;
	struct timespec ts, after;
	struct usbd_jw w;
	uint32_t count = 0, mr = 0;
	int have_rtt, rc, rtc_ok;
	char cal[200];
	int have_cal = 0;
	double epoch;
	unsigned epoch_pres;

	/* 2020-01-01 .. 2100-01-01: a buggy client must not set the clock to 1970 */
	if (usbd_json_get_int((const char *)pl, plen, "utc_ms", &utc_ms) || utc_ms < 1577836800000LL ||
	    utc_ms >= 4102444800000LL)
		return -EINVAL;
	before_ms = now_ms();
	ts.tv_sec = (time_t)(utc_ms / 1000);
	ts.tv_nsec = (long)(utc_ms % 1000) * 1000000L;
	if (clock_settime(CLOCK_REALTIME, &ts))
		return -errno;
	/* the RTT count belongs to the clock reading right after the set: take both before anything slow */
	clock_gettime(CLOCK_REALTIME, &after);
	have_rtt = rtt_read(&count, &mr) == 0;
	after_ms = (int64_t)after.tv_sec * 1000 + after.tv_nsec / 1000000;

	rc = system("hwclock -w -u");
	rtc_ok = rc != -1 && WIFEXITED(rc) && WEXITSTATUS(rc) == 0;

	/* keep the RTT running; only record a calibration point if it runs at our prescaler and has an epoch */
	if (have_rtt && (mr & 0xFFFF) == RTT_PRES_MAGPIE && !read_rtt_epoch(&epoch, &epoch_pres) &&
	    epoch_pres == RTT_PRES_MAGPIE &&
	    usbd_rtt_cal_line(cal, sizeof(cal), (double)after.tv_sec + after.tv_nsec / 1e9, count, mr & 0xFFFF, epoch) > 0)
		have_cal = append_rtt_cal(cal) == 0;

	klog("settime offset_ms=%lld rtc=%s rtt_cal=%s", (long long)(utc_ms - before_ms), rtc_ok ? "ok" : "err",
	     have_cal ? "yes" : "no");

	usbd_jw_init(&w, (char *)g_tx + RES_OFF, RES_CAP);
	usbd_jw_fmt(&w, "{\"before_ms\":%lld,\"after_ms\":%lld,\"rtc\":\"%s\",\"rtt_cal\":", (long long)before_ms,
		    (long long)after_ms, rtc_ok ? "ok" : "err");
	if (have_cal)
		usbd_jw_str(&w, cal);
	else
		usbd_jw_raw(&w, "null");
	usbd_jw_raw(&w, "}");
	if (w.err)
		return -EMSGSIZE;
	*rl = w.len;
	return 0;
}

static int dispatch(const struct usbd_hdr *h, const uint8_t *pl, size_t *rl)
{
	*rl = 0;
	switch (h->cmd) {
	case USBD_CMD_STATUS: return cmd_status(rl);
	case USBD_CMD_LIST: return cmd_list(pl, h->len, rl);
	case USBD_CMD_READ: return cmd_read(pl, h->len, rl);
	case USBD_CMD_HASH: return cmd_hash(pl, h->len, rl);
	case USBD_CMD_DELETE: return cmd_delete(pl, h->len, rl);
	case USBD_CMD_TIME: return cmd_time(rl);
	case USBD_CMD_SETTIME: return cmd_settime(pl, h->len, rl);
	}
	return -EINVAL;
}

/* -------------------------------------------------------------- main loop */

enum { RX_OK, RX_BADHDR, RX_RETRY, RX_IOERR };

/* Receives one request into g_rx. For RX_BADHDR *h holds what the error reply must echo. */
static int recv_request(struct usbd_hdr *h)
{
	size_t have, need;
	ssize_t n = read_idle();
	int r;

	if (n == -2)
		return RX_RETRY;
	if (n < 0)
		return RX_IOERR;
	if (n == 0)
		return RX_RETRY; /* a zero-length packet: nothing to do */
	have = (size_t)n;
	if (g_discard) {
		/* after a bad header the rest of that transfer is garbage; resume at the next valid header */
		uint8_t magic[4];

		le32(magic, USBD_MAGIC);
		if (have < USBD_HDR_LEN || memcmp(g_rx, magic, 4)) {
			dlog("discarding %zu bytes", have);
			return RX_RETRY;
		}
		g_discard = 0;
	}
	while (have < USBD_HDR_LEN) {
		n = read_more(have, USBD_HDR_LEN);
		if (n < 0)
			return RX_IOERR;
		have += (size_t)n;
	}
	r = usbd_hdr_unpack(g_rx, h);
	if (r == USBD_HDR_BAD_MAGIC) {
		h->cmd = 0; /* the reply uses cmd 0x8000, seq 0 */
		h->seq = 0;
		g_discard = 1;
		return RX_BADHDR;
	}
	if (r == USBD_HDR_BAD_LEN) {
		g_discard = 1;
		return RX_BADHDR;
	}
	need = USBD_HDR_LEN + h->len;
	while (have < need) {
		n = read_more(have, need);
		if (n < 0)
			return RX_IOERR;
		have += (size_t)n;
	}
	return r == 0 ? RX_OK : RX_BADHDR; /* unknown cmd: payload consumed, nothing to discard */
}

static void serve(void)
{
	for (;;) {
		struct usbd_hdr h;
		size_t rl = 0;
		int32_t status;
		int r;

		service_ep0(0);
		if (!serving()) {
			wait_serving();
			continue;
		}
		if (g_dirty || g_ep1 < 0 || g_ep2 < 0) {
			if (open_eps()) {
				dlog("reopening the endpoints failed: %s", strerror(errno));
				usleep(200000);
			} else {
				g_dirty = 0;
			}
			continue;
		}
		r = recv_request(&h);
		if (r == RX_RETRY)
			continue;
		if (r == RX_IOERR) {
			io_failed("read ep1");
			continue;
		}
		if (r == RX_BADHDR) {
			dlog("bad request header (cmd %04x len %u)", h.cmd, h.len);
			status = -EINVAL;
		} else {
			dlog("req cmd=%04x seq=%u len=%u", h.cmd, h.seq, h.len);
			status = dispatch(&h, g_rx + USBD_HDR_LEN, &rl);
			if (status)
				rl = 0;
		}
		if (send_response(&h, status, rl))
			io_failed("write ep2");
	}
}

static void usage(FILE *f)
{
	fputs("usage: magpie-usbd [-d] /dev/ffs-magpie\n"
	      "  -d  debug log to stderr\n",
	      f);
}

int main(int argc, char **argv)
{
	struct sigaction sa;
	char path[256];
	int i, tries;

	g_dir = NULL;
	for (i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "-d")) {
			g_debug = 1;
		} else if (!strcmp(argv[i], "-h")) {
			usage(stdout);
			return 0;
		} else if (argv[i][0] != '-' && !g_dir) {
			g_dir = argv[i];
		} else {
			usage(stderr);
			return EXIT_USAGE;
		}
	}
	if (!g_dir || strlen(g_dir) > sizeof(path) - 8) {
		usage(stderr);
		return EXIT_USAGE;
	}

	/* the helper tools are looked up through PATH, which an init script may not have set */
	setenv("PATH", "/usr/sbin:/usr/bin:/sbin:/bin", 1);
	signal(SIGPIPE, SIG_IGN);
	sigemptyset(&g_alrm);
	sigaddset(&g_alrm, SIGALRM);
	sigprocmask(SIG_BLOCK, &g_alrm, NULL);
	memset(&sa, 0, sizeof(sa));
	sa.sa_handler = on_alarm; /* no SA_RESTART: the signal must interrupt the idle read */
	sigaction(SIGALRM, &sa, NULL);

	rtt_map();

	snprintf(path, sizeof(path), "%s/ep0", g_dir);
	g_ep0 = open(path, O_RDWR | O_CLOEXEC);
	if (g_ep0 < 0) {
		fprintf(stderr, "magpie-usbd: %s: %s\n", path, strerror(errno));
		return EXIT_SETUP;
	}
	if (write_descriptors())
		return EXIT_SETUP;
	if (fcntl(g_ep0, F_SETFL, fcntl(g_ep0, F_GETFL) | O_NONBLOCK))
		return EXIT_SETUP;
	/* writing the strings makes FunctionFS create ep1 and ep2 */
	for (tries = 0; open_eps(); tries++) {
		if (tries >= 50) {
			fprintf(stderr, "magpie-usbd: cannot open ep1/ep2 in %s: %s\n", g_dir, strerror(errno));
			return EXIT_SETUP;
		}
		usleep(100000);
	}
	klog("started, version " USBD_VERSION);
	serve();
	return 0;
}
