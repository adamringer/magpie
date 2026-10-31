/*
 * gpsnap: raw-sample snapshot capture from the CellGuide ACLYS GPS chip of the Narrative Clip.
 *
 * Reproduces the stock aclys_snap (non -k path) on mainline Linux: SPI through spidev,
 * DATA READY / RESET / LNA through the GPIO character device (v2 uAPI). The protocol, every
 * register value and the ordering come from analysis/gps/snap_format.md and the stock binary;
 * the "stock 0x...." comments give the address in analysis/p2_root/bin/aclys_snap.
 *
 * Exit codes: 0 ok, 1 usage, 2 device error, 3 DATA READY timeout, 4 write error, 5 verify mismatch.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <poll.h>
#include <sched.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include <linux/gpio.h>
#include <linux/spi/spidev.h>

#include "gpsnap_logic.h"

#define GPSNAP_VERSION "1.0"

#define EXIT_OK 0
#define EXIT_USAGE 1
#define EXIT_DEVICE 2
#define EXIT_TIMEOUT 3
#define EXIT_WRITE 4
#define EXIT_VERIFY 5

#define SPI_DEV "/dev/spidev1.1"
#define DEFAULT_DIR "/data/gps"
/* PIOA is the gpio controller at 0xfffff400; the at91 pinctrl driver labels the chip with the device name */
#define PIOA_LABEL "fffff400.gpio"
#define LINE_DR 1     /* PA1 DATA READY, input, active low (raw level used: 0 = asserted) */
#define LINE_RESET 6  /* PA6 */
#define LINE_LNA 28   /* PA28 */
#define OUT_RESET 0x1 /* bit in the output request: offsets[0] = RESET, offsets[1] = LNA */
#define OUT_LNA 0x2

#define SPI_MODE SPI_MODE_2 /* CPOL=1, CPHA=0 (stock board table) */
#define SPI_BITS 8
#define SPI_SPEED_HZ 18300000u
#define DR_TIMEOUT_MS 100 /* stock select() timeout (0x9928 callers) */

struct opts {
	int ms;
	int lna;
	int test;
	int verify;
	int verbose;
	int version_only;
	int pins_only;
	int warmup_ms;
	const char *out;
};

static struct {
	struct opts o;
	int chip_fd;
	int out_fd; /* RESET + LNA line request */
	int dr_fd;  /* DATA READY line request with falling-edge events */
	int spi_fd;
	int chip_on;       /* SPI is up: power the chip down on exit */
	uint32_t rfreg;    /* stock global 0x13674: shadow of register 0xC8 */
} g = { .chip_fd = -1, .out_fd = -1, .dr_fd = -1, .spi_fd = -1 };

static volatile sig_atomic_t g_stop;

static void on_signal(int sig)
{
	(void)sig;
	g_stop = 1;
}

static void vlog(const char *fmt, ...)
{
	va_list ap;

	if (!g.o.verbose)
		return;
	va_start(ap, fmt);
	fputs("gpsnap: ", stderr);
	vfprintf(stderr, fmt, ap);
	fputc('\n', stderr);
	va_end(ap);
}

static void err(const char *fmt, ...)
{
	va_list ap;

	va_start(ap, fmt);
	fputs("gpsnap: ", stderr);
	vfprintf(stderr, fmt, ap);
	fputc('\n', stderr);
	va_end(ap);
}

static int64_t now_ns(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (int64_t)ts.tv_sec * 1000000000 + ts.tv_nsec;
}

/* ---------------------------------------------------------------- GPIO */

/* Find the PIOA gpiochip by its label (not by number: the numbering depends on probe order) */
static int gpio_open_chip(void)
{
	int i;

	for (i = 0; i < 32; i++) {
		char path[32];
		struct gpiochip_info info;
		int fd;

		snprintf(path, sizeof(path), "/dev/gpiochip%d", i);
		fd = open(path, O_RDWR | O_CLOEXEC);
		if (fd < 0)
			continue;
		memset(&info, 0, sizeof(info));
		if (ioctl(fd, GPIO_GET_CHIPINFO_IOCTL, &info) == 0 && strcmp(info.label, PIOA_LABEL) == 0) {
			vlog("%s: label %s, %u lines", path, info.label, info.lines);
			return fd;
		}
		close(fd);
	}
	err("no gpiochip labelled %s (is the device tree up to date?)", PIOA_LABEL);
	return -1;
}

static int gpio_request(int chip_fd, struct gpio_v2_line_request *req)
{
	if (ioctl(chip_fd, GPIO_V2_GET_LINE_IOCTL, req) < 0)
		return -1;
	return req->fd;
}

static int gpio_setup(int lna)
{
	struct gpio_v2_line_request req;

	g.chip_fd = gpio_open_chip();
	if (g.chip_fd < 0)
		return -1;

	/* RESET (initially high = not in reset) and LNA (value from -a): outputs; stock 0x96f0 sets the LNA first */
	memset(&req, 0, sizeof(req));
	req.offsets[0] = LINE_RESET;
	req.offsets[1] = LINE_LNA;
	req.num_lines = 2;
	strncpy(req.consumer, "gpsnap", sizeof(req.consumer) - 1);
	req.config.flags = GPIO_V2_LINE_FLAG_OUTPUT;
	req.config.num_attrs = 1;
	req.config.attrs[0].mask = OUT_RESET | OUT_LNA;
	req.config.attrs[0].attr.id = GPIO_V2_LINE_ATTR_ID_OUTPUT_VALUES;
	req.config.attrs[0].attr.values = OUT_RESET | (lna ? OUT_LNA : 0);
	g.out_fd = gpio_request(g.chip_fd, &req);
	if (g.out_fd < 0) {
		err("cannot request RESET (PA%d) / LNA (PA%d): %s", LINE_RESET, LINE_LNA, strerror(errno));
		return -1;
	}
	vlog("LNA = %d", lna ? 1 : 0);

	/* DATA READY: input, one falling edge per page */
	memset(&req, 0, sizeof(req));
	req.offsets[0] = LINE_DR;
	req.num_lines = 1;
	strncpy(req.consumer, "gpsnap-dr", sizeof(req.consumer) - 1);
	req.config.flags = GPIO_V2_LINE_FLAG_INPUT | GPIO_V2_LINE_FLAG_EDGE_FALLING;
	req.event_buffer_size = 64;
	g.dr_fd = gpio_request(g.chip_fd, &req);
	if (g.dr_fd < 0) {
		err("cannot request DATA READY (PA%d): %s", LINE_DR, strerror(errno));
		return -1;
	}
	fcntl(g.dr_fd, F_SETFL, fcntl(g.dr_fd, F_GETFL) | O_NONBLOCK);
	return 0;
}

static int out_set(unsigned bit, int val)
{
	struct gpio_v2_line_values v = { .bits = val ? bit : 0, .mask = bit };

	if (ioctl(g.out_fd, GPIO_V2_LINE_SET_VALUES_IOCTL, &v) < 0) {
		err("gpio set: %s", strerror(errno));
		return -1;
	}
	return 0;
}

/* stock fn 0x8930: CS=1 (done by the SPI core here), RESET = 1, 0, 1 with no delays */
static int reset_pulse(void)
{
	if (out_set(OUT_RESET, 1) || out_set(OUT_RESET, 0) || out_set(OUT_RESET, 1))
		return -1;
	return 0;
}

static int dr_level(void)
{
	struct gpio_v2_line_values v = { .mask = 1 };

	if (ioctl(g.dr_fd, GPIO_V2_LINE_GET_VALUES_IOCTL, &v) < 0)
		return -1;
	return (int)(v.bits & 1);
}

/* Read all queued edge events. *n += count, *last_ts = timestamp of the newest one. */
static void dr_drain(unsigned *n, uint64_t *last_ts)
{
	struct gpio_v2_line_event ev[8];

	for (;;) {
		ssize_t r = read(g.dr_fd, ev, sizeof(ev));
		size_t k;

		if (r <= 0)
			return;
		k = (size_t)r / sizeof(ev[0]);
		if (k == 0)
			return;
		*n += (unsigned)k;
		*last_ts = ev[k - 1].timestamp_ns;
	}
}

/*
 * Wait for DATA READY (stock fn 0x9928: select() on the gpio fd, 100 ms; ready when the line is low).
 * Go immediately if the line is already low, else wait for the falling edge. Events seen are
 * returned in *n (callers use more than one as an overrun indicator).
 * Returns 0, -ETIMEDOUT, or -EINTR (signal).
 */
static int wait_dr(int timeout_ms, unsigned *n, uint64_t *last_ts)
{
	int64_t end = now_ns() + (int64_t)timeout_ms * 1000000;

	*n = 0;
	*last_ts = 0;
	for (;;) {
		struct pollfd p = { .fd = g.dr_fd, .events = POLLIN };
		int64_t left;
		int r;

		dr_drain(n, last_ts);
		if (dr_level() == 0 || *n > 0)
			return 0;
		if (g_stop)
			return -EINTR;
		left = (end - now_ns()) / 1000000;
		if (left <= 0)
			return -ETIMEDOUT;
		r = poll(&p, 1, (int)left);
		if (r < 0 && errno != EINTR)
			return -errno;
	}
}

static void dr_flush(void)
{
	unsigned n = 0;
	uint64_t ts = 0;

	dr_drain(&n, &ts);
}

/* ----------------------------------------------------------------- SPI */

static int spi_open(void)
{
	uint8_t mode = SPI_MODE, bits = SPI_BITS, lsb = 0;
	uint32_t speed = SPI_SPEED_HZ;

	g.spi_fd = open(SPI_DEV, O_RDWR | O_CLOEXEC);
	if (g.spi_fd < 0) {
		err("open %s: %s", SPI_DEV, strerror(errno));
		return -1;
	}
	/* stock relies on the board table; set everything explicitly here */
	if (ioctl(g.spi_fd, SPI_IOC_WR_MODE, &mode) < 0 || ioctl(g.spi_fd, SPI_IOC_WR_LSB_FIRST, &lsb) < 0 ||
	    ioctl(g.spi_fd, SPI_IOC_WR_BITS_PER_WORD, &bits) < 0 ||
	    ioctl(g.spi_fd, SPI_IOC_WR_MAX_SPEED_HZ, &speed) < 0) {
		err("SPI configuration: %s", strerror(errno));
		return -1;
	}
	mode = bits = 0xff;
	speed = 0;
	if (ioctl(g.spi_fd, SPI_IOC_RD_MODE, &mode) == 0 && ioctl(g.spi_fd, SPI_IOC_RD_BITS_PER_WORD, &bits) == 0 &&
	    ioctl(g.spi_fd, SPI_IOC_RD_MAX_SPEED_HZ, &speed) == 0)
		vlog("%s: mode %u, %u bits, max %u Hz", SPI_DEV, mode, bits, speed);
	return 0;
}

/* One write()/read() is one SPI message: chip select stays asserted for all of it */
static int spi_wr(const void *buf, size_t len)
{
	ssize_t r = write(g.spi_fd, buf, len);

	if (r != (ssize_t)len) {
		err("SPI write of %zu bytes: %s", len, r < 0 ? strerror(errno) : "short write");
		return -1;
	}
	return 0;
}

static int spi_rd(void *buf, size_t len)
{
	ssize_t r = read(g.spi_fd, buf, len);

	if (r != (ssize_t)len) {
		err("SPI read of %zu bytes: %s", len, r < 0 ? strerror(errno) : "short read");
		return -1;
	}
	return 0;
}

/* stock fn 0x8fe4 */
static int reg_write(uint8_t reg, uint32_t val)
{
	uint8_t cmd[8];

	gps_cmd_write(reg, val, cmd);
	return spi_wr(cmd, sizeof(cmd));
}

/* stock fn 0x9048: up to 32 words (16 registers) in one write() */
static int reg_write_table(const struct gps_regval *t, size_t n)
{
	uint8_t buf[8 * 16];

	if (n > 16)
		return -1;
	return spi_wr(buf, gps_cmd_write_table(t, n, buf));
}

/* stock fn 0x8f08: 4-byte command, wait for DATA READY (100 ms), 4-byte read */
static int reg_read(uint8_t reg, uint32_t *val)
{
	uint8_t cmd[4], rx[4];
	unsigned n;
	uint64_t ts;
	int r;

	dr_flush();
	gps_cmd_read(reg, cmd);
	if (spi_wr(cmd, sizeof(cmd)))
		return -1;
	r = wait_dr(DR_TIMEOUT_MS, &n, &ts);
	if (r) {
		err("register 0x%02x read: %s", reg, r == -ETIMEDOUT ? "DATA READY timeout" : "interrupted");
		return -1;
	}
	if (spi_rd(rx, sizeof(rx)))
		return -1;
	*val = gps_decode_reg(rx);
	return 0;
}

/* stock fn 0x922c: read-modify-write */
static int reg_rmw(uint8_t reg, uint32_t clear, uint32_t set)
{
	uint32_t v;

	if (reg_read(reg, &v))
		return -1;
	return reg_write(reg, (v & ~clear) | set);
}

/* --------------------------------------------------------- chip set-up */

#define RF_CLK 0x100000u  /* register 0xC8 bit 20 */
#define RF_DATA 0x200000u /* register 0xC8 bit 21 */

static int rf_write(void)
{
	return reg_write(0xC8, g.rfreg);
}

/* RF programming data, stock table 0xaf24: sequence count, then (length, bytes...) */
static const uint8_t rf_table[] = {
	9,
	18, 0x88, 0x00, 0x18, 0x1f, 0x00, 0x40, 0x02, 0x00, 0x00, 0x0b, 0x00, 0x90, 0xff, 0x13, 0x08, 0x31, 0x97, 0xff,
	4, 0x88, 0x0e, 0xa2, 0x40,
	4, 0x88, 0x0e, 0xa2, 0x00,
	4, 0x88, 0x0e, 0x97, 0x5c,
	4, 0x88, 0x0e, 0x80, 0x20,
	4, 0x88, 0x0e, 0x81, 0x03,
	4, 0x88, 0x0e, 0x84, 0x10,
	4, 0x88, 0x0e, 0x85, 0x38,
	4, 0x88, 0x0e, 0x86, 0x60,
};

/* stock fn 0x9290: clock one sequence out through register 0xC8 (3-wire port: bit 20 clock, bit 21 data) */
static int rf_send_sequence(const uint8_t *seq)
{
	uint8_t len = seq[0];
	unsigned i;
	int b;

	g.rfreg &= ~RF_DATA;
	if (rf_write())
		return -1;
	g.rfreg &= ~RF_CLK;
	if (rf_write())
		return -1;
	for (i = 0; i < len; i++) {
		uint8_t byte = seq[1 + i];

		for (b = 0; b < 8; b++) { /* MSB first */
			uint8_t cmds[24];

			gps_rf_bit_cmds(byte & 0x80, cmds);
			if (spi_wr(cmds, sizeof(cmds)))
				return -1;
			byte <<= 1;
		}
		g.rfreg |= RF_CLK;
		if (rf_write())
			return -1;
		/*
		 * Stock pulses chip select here (ioctl 0x2205: 1 then 0). Chip select belongs to the SPI core
		 * now (cs-gpios), so it cannot be toggled on its own; the next write() starts with a falling
		 * edge of chip select anyway. NOT reproduced exactly.
		 */
		g.rfreg &= ~RF_CLK;
		if (rf_write())
			return -1;
	}
	g.rfreg |= RF_CLK;
	if (rf_write())
		return -1;
	g.rfreg |= RF_DATA;
	return rf_write();
}

/* stock fn 0x9518 */
static int config_rf(void)
{
	const uint8_t *p = rf_table + 1;
	unsigned i;

	if (reg_rmw(0x88, 0x00C00100u, 0))
		return -1;
	if (reg_read(0xC8, &g.rfreg))
		return -1;
	g.rfreg |= RF_DATA;
	g.rfreg |= RF_CLK;
	if (rf_write() || rf_write())
		return -1;
	for (i = 0; i < rf_table[0]; i++) {
		if (rf_send_sequence(p))
			return -1;
		p += p[0] + 1;
	}
	g.rfreg |= RF_DATA;
	g.rfreg |= RF_CLK;
	return rf_write();
}

/* stock table 0xaea4 (written in one burst, fn 0x9048) */
static const struct gps_regval cfg_table[] = {
	{ 0xc8, 0x00f15258 }, { 0xc0, 0x05f03c8d }, { 0x00, 0x20100221 }, { 0x04, 0x0000017b },
	{ 0x88, 0x00000010 }, { 0x08, 0x00400063 }, { 0x20, 0x09c18f0a }, { 0x0c, 0x001bea6f },
	{ 0x2c, 0x00000000 }, { 0x50, 0x00000000 }, { 0x54, 0x00000000 }, { 0x00, 0x20100221 },
	{ 0x04, 0x0000017b }, { 0x38, 0x00000055 }, { 0x3c, 0x000000aa }, { 0x88, 0x00000016 },
};

/* stock fn 0x8b64 mode 3 + fn 0x8988: 5 ms, register 0x44 = 0, read the version. The pin 3/4/5 power calls are no-ops in stock. */
static int power_mode3(uint32_t *version)
{
	usleep(5000);
	if (reg_write(GPS_REG_VERSION, 0))
		return -1;
	if (reg_read(GPS_REG_VERSION, version))
		return -1;
	if (*version != GPS_VERSION_EXPECTED) {
		err("unexpected version register 0x%08x (expected 0x%08x)", *version, GPS_VERSION_EXPECTED);
		return -1;
	}
	vlog("version register 0x%08x", *version);
	return 0;
}

/* stock fn 0x9680 (CgAclysConfig) */
static int cg_config(void)
{
	uint32_t v;

	if (reg_write_table(cfg_table, sizeof(cfg_table) / sizeof(cfg_table[0])))
		return -1;
	if (reg_read(GPS_REG_VERSION, &v))
		return -1;
	vlog("version register after config 0x%08x", v);
	return config_rf();
}

/* stock fn 0x8eb8 (InitComm): reset pulse, open SPI, reset pulse */
static int init_comm(void)
{
	if (reset_pulse())
		return -1;
	if (spi_open())
		return -1;
	g.chip_on = 1;
	return reset_pulse();
}

/* stock fn 0x910c (SnapPrepare): register 0x30, then the table whose last entry starts the capture */
static int snap_prepare(unsigned pages, int test)
{
	struct gps_regval t[] = {
		{ 0x8c, 0x00000fff }, { 0x88, 0x00000016 }, { 0x34, 0x00000003 },
		{ 0x00, 0x20100223 }, { 0x04, test ? 0x00002370u : 0x00000370u },
	};

	if (reg_write(0x30, 0xFF000000u + 4 * pages))
		return -1;
	dr_flush(); /* only edges from now on count as pages */
	return reg_write_table(t, sizeof(t) / sizeof(t[0]));
}

/* stock fn 0x8b64 mode 1: register 0xCC = 0xFDB96420 (pin 4/5 off are no-ops) */
static void power_down(void)
{
	if (!g.chip_on)
		return;
	g.chip_on = 0;
	if (reg_write(0xCC, 0xFDB96420u))
		err("power-down write failed");
}

/* stock fn 0x9198, run after power-down by the stock main: reg 0x38 = 0x00AA00AA, read 0x38 and 0x3C */
static void status_regs(void)
{
	uint32_t a = 0, b = 0;
	int ra, rb;

	reg_write(0x38, 0x00AA00AA);
	ra = reg_read(0x38, &a);
	rb = reg_read(0x3C, &b);
	vlog("status registers: 0x38 = %s0x%08x, 0x3c = %s0x%08x", ra ? "(failed) " : "", a, rb ? "(failed) " : "", b);
}

/* -------------------------------------------------------------- RTT */

/*
 * The SAM9X5 Real-time Timer counts the 32.768 kHz slow clock in the backup power domain, so it keeps running
 * through the fast-resume power cuts and cold boots, unlike the kernel's clock, which is re-read from the RTC
 * (whole seconds) after every resume. Its count at each capture puts all captures on one continuous timebase.
 */
#define SYSC_PAGE 0xFFFFF000u
#define RTT_MR_OFF 0xE20
#define RTT_VR_OFF 0xE28
static volatile uint32_t *g_sysc;

static void rtt_map(void)
{
	int fd = open("/dev/mem", O_RDONLY | O_SYNC | O_CLOEXEC);
	void *p;

	if (fd < 0)
		return;
	p = mmap(NULL, 4096, PROT_READ, MAP_SHARED, fd, SYSC_PAGE);
	close(fd);
	if (p != MAP_FAILED)
		g_sysc = p;
}

static uint32_t rtt_value(void)
{
	uint32_t a, b;

	/* the counter is asynchronous to the CPU clock: read until two reads agree */
	do {
		a = g_sysc[RTT_VR_OFF / 4];
		b = g_sysc[RTT_VR_OFF / 4];
	} while (a != b);
	return a;
}

/* -------------------------------------------------------------- capture */

struct stats {
	unsigned pages_done;
	unsigned overruns;
	int64_t first_rt_ns; /* CLOCK_REALTIME of page 0's DATA READY edge (page 0 complete) */
	int first_from_edge; /* 1: from the kernel edge timestamp, 0: line was already low (read time) */
	int have_rtt;        /* RTT counter read right after page 0 */
	uint32_t rtt;        /* RTT_VR then */
	uint32_t rtt_pres;   /* RTT_MR prescaler (SLCK cycles per count; 0 means 65536) */
	int64_t rtt_after_edge_ns; /* time between the page-0 edge and the RTT read */
	int64_t *t;       /* clock_gettime at each page start */
	uint64_t *edge;   /* newest kernel edge timestamp seen before each page (0 = none) */
};

static int capture(uint8_t *buf, unsigned pages, struct stats *st)
{
	unsigned p;

	for (p = 0; p < pages; p++) {
		unsigned n;
		uint64_t ts;
		int r = wait_dr(DR_TIMEOUT_MS, &n, &ts);

		if (r == -EINTR || g_stop) {
			err("interrupted after %u of %u pages", p, pages);
			return EXIT_DEVICE;
		}
		if (r) {
			err("page %u: DATA READY timeout (%d ms)", p, DR_TIMEOUT_MS);
			return r == -ETIMEDOUT ? EXIT_TIMEOUT : EXIT_DEVICE;
		}
		st->t[p] = now_ns();
		st->edge[p] = ts;
		if (p == 0) {
			/* the edge timestamp is CLOCK_MONOTONIC: move it to CLOCK_REALTIME */
			struct timespec rt;
			int64_t rt_ns, mono_ns;

			clock_gettime(CLOCK_REALTIME, &rt);
			mono_ns = now_ns();
			rt_ns = (int64_t)rt.tv_sec * 1000000000 + rt.tv_nsec;
			st->first_from_edge = ts != 0;
			st->first_rt_ns = ts ? rt_ns - (mono_ns - (int64_t)ts) : rt_ns;
			if (g_sysc) {
				st->rtt = rtt_value();
				st->rtt_pres = g_sysc[RTT_MR_OFF / 4] & 0xFFFF;
				st->rtt_after_edge_ns = ts ? now_ns() - (int64_t)ts : 0;
				st->have_rtt = 1;
			}
		}
		if (n > 1)
			st->overruns += n - 1;
		/* stock 0x9e64: chip select low, read 4096 bytes, chip select high (here: one SPI message) */
		if (spi_rd(buf + (size_t)p * GPS_PAGE_BYTES, GPS_PAGE_BYTES))
			return EXIT_DEVICE;
		st->pages_done = p + 1;
	}
	return EXIT_OK;
}

static void report_intervals(const char *what, const double *iv, unsigned n)
{
	double mn = iv[0], mx = iv[0], sum = 0;
	unsigned i;

	for (i = 0; i < n; i++) {
		if (iv[i] < mn)
			mn = iv[i];
		if (iv[i] > mx)
			mx = iv[i];
		sum += iv[i];
	}
	fprintf(stderr, "gpsnap: %s interval min %.3f max %.3f mean %.3f ms (%u intervals)\n", what, mn, mx, sum / n, n);
}

static void report_stats(const struct stats *st)
{
	unsigned i, n = 0;
	double *iv;

	if (st->pages_done < 2)
		return;
	iv = malloc(sizeof(double) * st->pages_done);
	if (!iv)
		return;
	for (i = 1; i < st->pages_done; i++)
		iv[n++] = (double)(st->t[i] - st->t[i - 1]) / 1e6;
	report_intervals("page", iv, n);
	n = 0;
	for (i = 1; i < st->pages_done; i++)
		if (st->edge[i] && st->edge[i - 1])
			iv[n++] = (double)(st->edge[i] - st->edge[i - 1]) / 1e6;
	if (n)
		report_intervals("DATA READY edge", iv, n);
	free(iv);
}

/* -------------------------------------------------------------- output */

static int mkdir_p(const char *path)
{
	char tmp[512];
	char *p;

	if (strlen(path) >= sizeof(tmp))
		return -1;
	strcpy(tmp, path);
	for (p = tmp + 1; *p; p++) {
		if (*p != '/')
			continue;
		*p = 0;
		if (mkdir(tmp, 0755) && errno != EEXIST)
			return -1;
		*p = '/';
	}
	return (mkdir(tmp, 0755) && errno != EEXIST) ? -1 : 0;
}

static int write_file(const char *path, const uint8_t *buf, size_t len)
{
	char dir[512];
	char *slash;
	int fd;
	size_t off = 0;

	if (strlen(path) >= sizeof(dir)) {
		err("path too long");
		return -1;
	}
	strcpy(dir, path);
	slash = strrchr(dir, '/');
	if (slash && slash != dir) {
		*slash = 0;
		if (mkdir_p(dir)) {
			err("cannot create %s: %s", dir, strerror(errno));
			return -1;
		}
	}
	fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
	if (fd < 0) {
		err("cannot create %s: %s", path, strerror(errno));
		return -1;
	}
	while (off < len) {
		ssize_t r = write(fd, buf + off, len - off);

		if (r < 0) {
			if (errno == EINTR)
				continue;
			err("write %s: %s", path, strerror(errno));
			close(fd);
			return -1;
		}
		off += (size_t)r;
	}
	if (fsync(fd) || close(fd)) {
		err("write %s: %s", path, strerror(errno));
		return -1;
	}
	return 0;
}

/*
 * Sidecar PATH.time: the UTC time of page 0's DATA READY edge, i.e. the end of the first 16 ms page, so sample 0
 * of the file is about 16 ms earlier. Lets a position solver use the capture time to the millisecond.
 */
static void write_time_file(const char *path, const struct stats *st, unsigned pages)
{
	char tpath[600], when[32];
	time_t sec = (time_t)(st->first_rt_ns / 1000000000);
	long usec = (long)((st->first_rt_ns % 1000000000) / 1000);
	struct tm tmv;
	FILE *f;

	if (snprintf(tpath, sizeof(tpath), "%s.time", path) >= (int)sizeof(tpath))
		return;
	gmtime_r(&sec, &tmv);
	strftime(when, sizeof(when), "%Y-%m-%dT%H:%M:%S", &tmv);
	f = fopen(tpath, "w");
	if (!f) {
		err("cannot create %s: %s", tpath, strerror(errno));
		return;
	}
	fprintf(f, "first_page_utc=%s.%06ldZ\nsource=%s\npages=%u\n", when, usec,
		st->first_from_edge ? "edge" : "level", pages);
	if (st->have_rtt)
		fprintf(f, "rtt=%u\nrtt_pres=%u\nrtt_after_edge_us=%lld\n", st->rtt, st->rtt_pres ? st->rtt_pres : 65536,
			(long long)(st->rtt_after_edge_ns / 1000));
	if (fflush(f) || fsync(fileno(f)) || fclose(f))
		err("write %s: %s", tpath, strerror(errno));
	vlog("first page (DATA READY edge) at %s.%06ldZ (%s)", when, usec, st->first_from_edge ? "edge" : "level");
}

static int verify_buffer(const uint8_t *buf, unsigned pages)
{
	struct gps_verify v;
	unsigned p;

	gps_verify_init(&v);
	for (p = 0; p < pages; p++)
		gps_verify_page(&v, buf + (size_t)p * GPS_PAGE_BYTES, p);
	if (!v.mismatches) {
		printf("verify: OK, %lu halfwords over %u pages match the test pattern\n", v.checked, pages);
		return 0;
	}
	printf("verify: FAILED, %lu of %lu halfwords differ (%lu breaks); first at page %u halfword %u: expected 0x%04x got 0x%04x",
	       v.mismatches, v.checked, v.breaks, v.first_page, v.first_index, v.first_expected, v.first_got);
	if (v.first_delta >= 0)
		printf(" (ahead by %ld counts = %.2f pages)", v.first_delta, (double)v.first_delta / GPS_PAGE_HALFWORDS);
	putchar('\n');
	return -1;
}

/* ------------------------------------------------------------------ main */

static void cleanup(void)
{
	power_down();
	if (g.spi_fd >= 0)
		close(g.spi_fd);
	if (g.dr_fd >= 0)
		close(g.dr_fd);
	if (g.out_fd >= 0)
		close(g.out_fd);
	if (g.chip_fd >= 0)
		close(g.chip_fd);
	g.spi_fd = g.dr_fd = g.out_fd = g.chip_fd = -1;
}

static void rt_setup(void)
{
	struct sched_param sp = { .sched_priority = 10 };

	rtt_map(); /* optional: no RTT count in the .time file if /dev/mem can't be mapped */
	if (mlockall(MCL_CURRENT | MCL_FUTURE))
		vlog("mlockall: %s", strerror(errno));
	if (sched_setscheduler(0, SCHED_FIFO, &sp))
		vlog("SCHED_FIFO not available: %s", strerror(errno));
	else
		vlog("running SCHED_FIFO priority %d", sp.sched_priority);
}

static int run(void)
{
	unsigned pages = gps_pages_from_ms(g.o.ms);
	uint8_t *buf = NULL;
	struct stats st = { 0 };
	uint32_t version;
	char name[64], path[512];
	const char *out = g.o.out;
	int rc, tries, ok = 0;

	if (!g.o.version_only && !g.o.pins_only) {
		/* everything allocated and touched before the capture starts */
		if (posix_memalign((void **)&buf, 4096, (size_t)pages * GPS_PAGE_BYTES)) {
			err("out of memory");
			return EXIT_DEVICE;
		}
		memset(buf, 0, (size_t)pages * GPS_PAGE_BYTES);
		st.t = calloc(pages, sizeof(*st.t));
		st.edge = calloc(pages, sizeof(*st.edge));
		if (!st.t || !st.edge) {
			err("out of memory");
			rc = EXIT_DEVICE;
			goto out;
		}
		rt_setup();
	}

	if (gpio_setup(g.o.lna)) {
		rc = EXIT_DEVICE;
		goto out;
	}
	/*
	 * Stock's aclys_gpio module drove RESET (and CS, and the LNA) high when it loaded at boot and
	 * kept them there. Released lines keep their level on this SoC, so S14gps runs this once at boot
	 * to start every capture from the same pin state.
	 */
	if (g.o.pins_only) {
		vlog("pins set: RESET = 1, LNA = %d", g.o.lna ? 1 : 0);
		rc = EXIT_OK;
		goto out;
	}
	/* stock fn 0x8a48: up to 10 tries at opening and resetting */
	for (tries = 0; tries < 10 && !ok; tries++) {
		if (init_comm()) {
			if (g.spi_fd >= 0)
				close(g.spi_fd);
			g.spi_fd = -1;
			g.chip_on = 0;
		} else {
			ok = 1;
		}
	}
	if (!ok) {
		rc = EXIT_DEVICE;
		goto out;
	}

	if (power_mode3(&version)) {
		rc = EXIT_DEVICE;
		goto out;
	}
	if (g.o.version_only) {
		printf("version register 0x%08x\n", version);
		rc = EXIT_OK;
		goto out;
	}
	if (cg_config()) {
		rc = EXIT_DEVICE;
		goto out;
	}
	if (g.o.warmup_ms > 0) {
		/* chip and RF section configured and running; let them settle before the capture starts */
		vlog("warm-up %d ms", g.o.warmup_ms);
		usleep((useconds_t)g.o.warmup_ms * 1000);
	}
	if (snap_prepare(pages, g.o.test)) {
		rc = EXIT_DEVICE;
		goto out;
	}
	vlog("capturing %u pages (%d ms)%s", pages, pages * 16, g.o.test ? ", chip test pattern" : "");
	rc = capture(buf, pages, &st);

	/* chip down before any file I/O (stock writes the file first) */
	power_down();
	if (rc == EXIT_OK)
		status_regs();
	if (g.o.verbose) {
		report_stats(&st);
		fprintf(stderr, "gpsnap: DATA READY overruns: %u\n", st.overruns);
	}
	if (st.overruns)
		err("warning: %u DATA READY edge(s) arrived before the previous page was read (overrun, data may be missing)",
		    st.overruns);
	if (rc != EXIT_OK)
		goto out;

	if (!out) {
		time_t tt = time(NULL);
		struct tm tmv;

		localtime_r(&tt, &tmv);
		strftime(name, sizeof(name), "%Y%m%d-%H%M%S.snap", &tmv);
		snprintf(path, sizeof(path), "%s/%s", DEFAULT_DIR, name);
		out = path;
	}
	if (write_file(out, buf, (size_t)pages * GPS_PAGE_BYTES)) {
		rc = EXIT_WRITE;
		goto out;
	}
	write_time_file(out, &st, pages);
	printf("%s: %u pages, %u bytes\n", out, pages, pages * GPS_PAGE_BYTES);
	if (g.o.verify && verify_buffer(buf, pages))
		rc = EXIT_VERIFY;
out:
	free(buf);
	free(st.t);
	free(st.edge);
	return rc;
}

static void usage(FILE *f)
{
	fprintf(f,
		"gpsnap " GPSNAP_VERSION " - ACLYS GPS snapshot capture\nUsage: gpsnap [options]\n"
		"  -s MS    snapshot length in ms, 16..4096 (default %d; pages = ms/16)\n"
		"  -a 0|1   LNA GPIO value (default 0)\n"
		"  -w MS    warm-up: wait MS ms after configuring the chip before the capture starts (default 0)\n"
		"  -t       chip test pattern instead of GPS samples\n"
		"  -e       verify the test pattern (implies -t); exit 5 on mismatch\n"
		"  -o PATH  output file (default %s/YYYYMMDD-HHMMSS.snap)\n"
		"  -v       verbose (timing statistics, overrun count)\n"
		"  -h       this help\n"
		"  --version-only  reset, print the chip's version register, power down\n"
		"  --pins-only     drive RESET high and LNA to the -a value, then exit (boot-time pin state)\n"
		"Exit: 0 ok, 1 usage, 2 device error, 3 DATA READY timeout, 4 write error, 5 verify mismatch\n",
		GPS_MS_DEFAULT, DEFAULT_DIR);
}

int main(int argc, char **argv)
{
	static const struct option longopts[] = {
		{ "version-only", no_argument, NULL, 'V' },
		{ "pins-only", no_argument, NULL, 'P' },
		{ "help", no_argument, NULL, 'h' },
		{ NULL, 0, NULL, 0 },
	};
	struct sigaction sa;
	int c, rc;

	g.o.ms = GPS_MS_DEFAULT;
	while ((c = getopt_long(argc, argv, "s:a:w:teo:vh", longopts, NULL)) != -1) {
		char *end;

		switch (c) {
		case 's':
			g.o.ms = (int)strtol(optarg, &end, 10);
			if (*end || !optarg[0] || !gps_pages_from_ms(g.o.ms)) {
				fprintf(stderr, "gpsnap: -s needs %d..%d\n", GPS_MS_MIN, GPS_MS_MAX);
				return EXIT_USAGE;
			}
			break;
		case 'a':
			if (strcmp(optarg, "0") && strcmp(optarg, "1")) {
				fprintf(stderr, "gpsnap: -a needs 0 or 1\n");
				return EXIT_USAGE;
			}
			g.o.lna = optarg[0] - '0';
			break;
		case 'w':
			g.o.warmup_ms = (int)strtol(optarg, &end, 10);
			if (*end || !optarg[0] || g.o.warmup_ms < 0 || g.o.warmup_ms > 10000) {
				fprintf(stderr, "gpsnap: -w needs 0..10000\n");
				return EXIT_USAGE;
			}
			break;
		case 't':
			g.o.test = 1;
			break;
		case 'e':
			g.o.test = g.o.verify = 1;
			break;
		case 'o':
			g.o.out = optarg;
			break;
		case 'v':
			g.o.verbose = 1;
			break;
		case 'V':
			g.o.version_only = 1;
			break;
		case 'P':
			g.o.pins_only = 1;
			break;
		case 'h':
			usage(stdout);
			return EXIT_OK;
		default:
			usage(stderr);
			return EXIT_USAGE;
		}
	}
	if (optind != argc) {
		usage(stderr);
		return EXIT_USAGE;
	}

	memset(&sa, 0, sizeof(sa));
	sa.sa_handler = on_signal; /* no SA_RESTART: a blocking poll() must return */
	sigaction(SIGINT, &sa, NULL);
	sigaction(SIGTERM, &sa, NULL);
	sigaction(SIGHUP, &sa, NULL);
	signal(SIGPIPE, SIG_IGN);

	rc = run();
	cleanup(); /* power down (if still up) and release the lines on every path */
	return rc;
}
