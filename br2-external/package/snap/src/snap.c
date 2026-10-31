/*
 * snap - V4L2 still capture for the Narrative Clip (Atmel ISI + OV5640).
 *
 * Captures one frame via MMAP streaming, discarding the first N frames so
 * auto-exposure can settle, and writes it to a file. JPEG frames arrive in
 * the sensor's line framing (see jpeg_unpack) and are written as a .jpg;
 * YUYV frames are written raw.
 * Depends only on libc and the kernel UAPI headers.
 *
 * Exit codes: 0 ok, 1 usage, 2 device/driver error, 3 timeout, 4 write error.
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <linux/videodev2.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/select.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define ISI_DRIVER "atmel-isi"
#define PHOTO_DIR "/data/photos"
/* Two is the ISI's minimum (min_queued_buffers) and keeps the contiguous (CMA) need at 2 x 2 MiB for a
 * 5 MP JPEG; snap keeps only one frame after the skipped ones, so a third buffer gains nothing. */
#define MAX_BUFS 2
#define FRAME_TIMEOUT_S 5
/* OV5640 JPEG framing set by the magpie kernel patch: fixed lines, 2-byte length header */
#define JPEG_LINE 1024

enum { EX_OK = 0, EX_USAGE = 1, EX_DEV = 2, EX_TIMEOUT = 3, EX_WRITE = 4 };

struct buf {
	void *start;
	size_t len;
};

static int verbose;
static volatile sig_atomic_t got_signal;

static struct {
	int fd;
	int streaming;
	struct buf bufs[MAX_BUFS];
	unsigned int nbufs;
} cap = { .fd = -1 };

#define VLOG(...) do { if (verbose) fprintf(stderr, __VA_ARGS__); } while (0)

static void on_signal(int sig)
{
	got_signal = sig;
}

static int xioctl(int fd, unsigned long req, void *arg)
{
	int r;

	do {
		r = ioctl(fd, req, arg);
	} while (r < 0 && errno == EINTR && !got_signal);
	return r;
}

static void cleanup(void)
{
	unsigned int i;

	if (cap.streaming) {
		enum v4l2_buf_type t = V4L2_BUF_TYPE_VIDEO_CAPTURE;

		ioctl(cap.fd, VIDIOC_STREAMOFF, &t);
		cap.streaming = 0;
	}
	for (i = 0; i < cap.nbufs; i++)
		if (cap.bufs[i].start && cap.bufs[i].start != MAP_FAILED)
			munmap(cap.bufs[i].start, cap.bufs[i].len);
	cap.nbufs = 0;
	if (cap.fd >= 0) {
		close(cap.fd);
		cap.fd = -1;
	}
}

static void fourcc_str(unsigned int f, char out[5])
{
	int i;

	for (i = 0; i < 4; i++)
		out[i] = (f >> (8 * i)) & 0xff;
	out[4] = 0;
}

static void usage(FILE *o)
{
	fprintf(o,
		"usage: snap [options]\n"
		"  -d, --device DEV   video node (default: find the " ISI_DRIVER " node)\n"
		"      --size WxH     frame size (default 2592x1944)\n"
		"      --format F     jpeg, yuyv or uyvy (default jpeg)\n"
		"      --skip N       frames to discard first (default 10)\n"
		"  -o, --output PATH  output file (default " PHOTO_DIR "/YYYYMMDD-HHMMSS.jpg or .<fmt>)\n"
		"      --list         print formats and frame sizes, then exit\n"
		"  -v, --verbose      verbose\n"
		"  -h, --help         this help\n"
		"exit: 0 ok, 1 usage, 2 device error, 3 timeout, 4 write error\n");
}

static int open_node(const char *path, struct v4l2_capability *c)
{
	int fd = open(path, O_RDWR);

	if (fd < 0)
		return -1;
	if (ioctl(fd, VIDIOC_QUERYCAP, c) < 0) {
		close(fd);
		return -1;
	}
	return fd;
}

/* Returns an open fd of the first /dev/video* whose driver is atmel-isi, or -1. */
static int find_isi(char *path, size_t plen)
{
	struct dirent *de;
	DIR *d = opendir("/dev");
	int fd = -1;

	if (!d)
		return -1;
	while (fd < 0 && (de = readdir(d))) {
		struct v4l2_capability c;
		int f;

		if (strncmp(de->d_name, "video", 5))
			continue;
		if (snprintf(path, plen, "/dev/%s", de->d_name) >= (int)plen)
			continue;
		f = open_node(path, &c);
		if (f < 0)
			continue;
		VLOG("%s: driver %s card %s\n", path, c.driver, c.card);
		if (!strcmp((char *)c.driver, ISI_DRIVER) &&
		    (c.capabilities & V4L2_CAP_VIDEO_CAPTURE))
			fd = f;
		else
			close(f);
	}
	closedir(d);
	return fd;
}

static int do_list(int fd)
{
	struct v4l2_fmtdesc fd_;
	unsigned int i;

	for (i = 0;; i++) {
		struct v4l2_frmsizeenum fs;
		char cc[5];
		unsigned int j;

		memset(&fd_, 0, sizeof(fd_));
		fd_.index = i;
		fd_.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
		if (ioctl(fd, VIDIOC_ENUM_FMT, &fd_) < 0)
			break;
		fourcc_str(fd_.pixelformat, cc);
		printf("format %u: %s%s%s\n", i, cc,
		       fd_.description[0] ? " " : "", (char *)fd_.description);
		for (j = 0;; j++) {
			memset(&fs, 0, sizeof(fs));
			fs.index = j;
			fs.pixel_format = fd_.pixelformat;
			if (ioctl(fd, VIDIOC_ENUM_FRAMESIZES, &fs) < 0)
				break;
			if (fs.type == V4L2_FRMSIZE_TYPE_DISCRETE) {
				printf("  size %ux%u\n", fs.discrete.width,
				       fs.discrete.height);
			} else {
				printf("  size %ux%u .. %ux%u step %ux%u\n",
				       fs.stepwise.min_width, fs.stepwise.min_height,
				       fs.stepwise.max_width, fs.stepwise.max_height,
				       fs.stepwise.step_width, fs.stepwise.step_height);
				break;
			}
		}
	}
	if (i == 0) {
		fprintf(stderr, "snap: no formats reported: %s\n",
			errno ? strerror(errno) : "none");
		return EX_DEV;
	}
	return EX_OK;
}

static int format_supported(int fd, unsigned int fourcc)
{
	struct v4l2_fmtdesc f;
	unsigned int i;

	for (i = 0;; i++) {
		memset(&f, 0, sizeof(f));
		f.index = i;
		f.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
		if (ioctl(fd, VIDIOC_ENUM_FMT, &f) < 0)
			return 0;
		if (f.pixelformat == fourcc)
			return 1;
	}
}

static int mkdir_p(const char *dir)
{
	if (mkdir(dir, 0755) < 0 && errno != EEXIST) {
		/* parent may be missing (/data not mounted is an error) */
		return -1;
	}
	return 0;
}

static int write_all(int fd, const void *p, size_t n)
{
	const char *c = p;

	while (n) {
		ssize_t w = write(fd, c, n);

		if (w < 0) {
			if (errno == EINTR)
				continue;
			return -1;
		}
		c += w;
		n -= (size_t)w;
	}
	return 0;
}

static int save(const char *path, const void *data, size_t len)
{
	char tmp[256];
	int fd;

	if (snprintf(tmp, sizeof(tmp), "%s.tmp", path) >= (int)sizeof(tmp)) {
		fprintf(stderr, "snap: output path too long\n");
		return EX_WRITE;
	}
	fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC, 0644);
	if (fd < 0) {
		fprintf(stderr, "snap: %s: %s\n", tmp, strerror(errno));
		return EX_WRITE;
	}
	if (write_all(fd, data, len) < 0 || fsync(fd) < 0) {
		fprintf(stderr, "snap: write %s: %s\n", tmp, strerror(errno));
		close(fd);
		unlink(tmp);
		return EX_WRITE;
	}
	if (close(fd) < 0) {
		fprintf(stderr, "snap: close %s: %s\n", tmp, strerror(errno));
		unlink(tmp);
		return EX_WRITE;
	}
	if (rename(tmp, path) < 0) {
		fprintf(stderr, "snap: rename to %s: %s\n", path, strerror(errno));
		unlink(tmp);
		return EX_WRITE;
	}
	return EX_OK;
}

/*
 * Join the payloads of a JPEG frame: each JPEG_LINE-byte line starts with a
 * big-endian count of the JPEG bytes that follow (0 = empty line). Stops at
 * the first EOI (FF D9 cannot occur inside entropy-coded data), so stale
 * lines from an earlier frame after it are ignored. Returns the JPEG size,
 * or 0 if the data isn't a complete JPEG.
 */
static size_t jpeg_unpack(const unsigned char *src, size_t len, unsigned char *out)
{
	size_t o = 0, off;

	for (off = 0; off + JPEG_LINE <= len; off += JPEG_LINE) {
		const unsigned char *l = src + off;
		size_t n = ((size_t)l[0] << 8) | l[1], i;

		if (n > JPEG_LINE - 2) {
			VLOG("line %zu: bad length %zu\n", off / JPEG_LINE, n);
			return 0;
		}
		for (i = 0; i < n; i++) {
			out[o++] = l[2 + i];
			if (o == 2 && (out[0] != 0xff || out[1] != 0xd8)) {
				VLOG("no SOI marker at the start\n");
				return 0;
			}
			if (o > 2 && out[o - 2] == 0xff && out[o - 1] == 0xd9) {
				VLOG("EOI in line %zu, %zu bytes\n", off / JPEG_LINE, o);
				return o;
			}
		}
	}
	VLOG("no EOI marker (%zu bytes joined)\n", o);
	return 0;
}

static int parse_size(const char *s, unsigned int *w, unsigned int *h)
{
	char *e;
	unsigned long a, b;

	a = strtoul(s, &e, 10);
	if (e == s || (*e != 'x' && *e != 'X'))
		return -1;
	s = e + 1;
	b = strtoul(s, &e, 10);
	if (e == s || *e || !a || !b || a > 16384 || b > 16384)
		return -1;
	*w = a;
	*h = b;
	return 0;
}

static int capture(int fd, unsigned int w, unsigned int h, unsigned int fourcc,
		   const char *fmtname, int skip, const char *out, int out_default)
{
	struct v4l2_format fmt;
	struct v4l2_requestbuffers req;
	enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
	struct v4l2_buffer b;
	unsigned int i;
	int n, held = -1;
	char cc[5];

	memset(&fmt, 0, sizeof(fmt));
	fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
	fmt.fmt.pix.width = w;
	fmt.fmt.pix.height = h;
	fmt.fmt.pix.pixelformat = fourcc;
	fmt.fmt.pix.field = V4L2_FIELD_NONE;
	if (xioctl(fd, VIDIOC_S_FMT, &fmt) < 0) {
		fprintf(stderr, "snap: VIDIOC_S_FMT %ux%u %s: %s\n", w, h,
			fmtname, strerror(errno));
		return EX_DEV;
	}
	if (fmt.fmt.pix.width != w || fmt.fmt.pix.height != h ||
	    fmt.fmt.pix.pixelformat != fourcc) {
		fourcc_str(fmt.fmt.pix.pixelformat, cc);
		fprintf(stderr, "snap: driver chose %ux%u %s instead of %ux%u %s\n",
			fmt.fmt.pix.width, fmt.fmt.pix.height, cc, w, h, fmtname);
	}
	VLOG("format: %ux%u bytesperline %u sizeimage %u\n", fmt.fmt.pix.width,
	     fmt.fmt.pix.height, fmt.fmt.pix.bytesperline, fmt.fmt.pix.sizeimage);

	memset(&req, 0, sizeof(req));
	req.count = MAX_BUFS;
	req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
	req.memory = V4L2_MEMORY_MMAP;
	if (xioctl(fd, VIDIOC_REQBUFS, &req) < 0) {
		fprintf(stderr, "snap: VIDIOC_REQBUFS: %s\n", strerror(errno));
		return EX_DEV;
	}
	if (req.count == 0) {
		fprintf(stderr, "snap: driver granted no buffers\n");
		return EX_DEV;
	}
	if (req.count > MAX_BUFS)
		req.count = MAX_BUFS;
	if (req.count < MAX_BUFS)
		VLOG("only %u buffers granted\n", req.count);

	for (i = 0; i < req.count; i++) {
		memset(&b, 0, sizeof(b));
		b.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
		b.memory = V4L2_MEMORY_MMAP;
		b.index = i;
		if (xioctl(fd, VIDIOC_QUERYBUF, &b) < 0) {
			fprintf(stderr, "snap: VIDIOC_QUERYBUF: %s\n", strerror(errno));
			return EX_DEV;
		}
		cap.bufs[i].len = b.length;
		cap.bufs[i].start = mmap(NULL, b.length, PROT_READ | PROT_WRITE,
					 MAP_SHARED, fd, b.m.offset);
		if (cap.bufs[i].start == MAP_FAILED) {
			fprintf(stderr, "snap: mmap: %s\n", strerror(errno));
			return EX_DEV;
		}
		cap.nbufs = i + 1;
	}
	for (i = 0; i < cap.nbufs; i++) {
		memset(&b, 0, sizeof(b));
		b.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
		b.memory = V4L2_MEMORY_MMAP;
		b.index = i;
		if (xioctl(fd, VIDIOC_QBUF, &b) < 0) {
			fprintf(stderr, "snap: VIDIOC_QBUF: %s\n", strerror(errno));
			return EX_DEV;
		}
	}
	if (xioctl(fd, VIDIOC_STREAMON, &type) < 0) {
		fprintf(stderr, "snap: VIDIOC_STREAMON: %s\n", strerror(errno));
		return EX_DEV;
	}
	cap.streaming = 1;

	/* Frames 0..skip-1 are discarded; frame number skip is kept (not requeued). */
	for (n = 0; n <= skip; n++) {
		fd_set fds;
		struct timeval tv = { FRAME_TIMEOUT_S, 0 };
		int r;

		FD_ZERO(&fds);
		FD_SET(fd, &fds);
		r = select(fd + 1, &fds, NULL, NULL, &tv);
		if (got_signal) {
			fprintf(stderr, "snap: interrupted by signal %d\n", (int)got_signal);
			return EX_DEV;
		}
		if (r < 0) {
			if (errno == EINTR) {
				n--;
				continue;
			}
			fprintf(stderr, "snap: select: %s\n", strerror(errno));
			return EX_DEV;
		}
		if (r == 0) {
			fprintf(stderr, "snap: timeout waiting for frame %d (%d s); "
				"no sync from the sensor?\n", n, FRAME_TIMEOUT_S);
			return EX_TIMEOUT;
		}
		memset(&b, 0, sizeof(b));
		b.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
		b.memory = V4L2_MEMORY_MMAP;
		if (xioctl(fd, VIDIOC_DQBUF, &b) < 0) {
			fprintf(stderr, "snap: VIDIOC_DQBUF: %s\n", strerror(errno));
			return EX_DEV;
		}
		VLOG("frame %d: buf %u bytesused %u flags 0x%x\n", n, b.index,
		     b.bytesused, b.flags);
		if (n < skip) {
			if (xioctl(fd, VIDIOC_QBUF, &b) < 0) {
				fprintf(stderr, "snap: VIDIOC_QBUF: %s\n", strerror(errno));
				return EX_DEV;
			}
		} else {
			held = b.index;
		}
	}
	if (b.flags & V4L2_BUF_FLAG_ERROR)
		fprintf(stderr, "snap: warning: driver flagged the frame as erroneous\n");

	if (out_default && mkdir_p(PHOTO_DIR) < 0) {
		fprintf(stderr, "snap: mkdir %s: %s\n", PHOTO_DIR, strerror(errno));
		return EX_WRITE;
	}
	{
		size_t len = b.bytesused ? b.bytesused : cap.bufs[held].len;
		int rc;

		if (len > cap.bufs[held].len)
			len = cap.bufs[held].len;
		if (fourcc == V4L2_PIX_FMT_JPEG) {
			unsigned char *jpg = malloc(len);
			size_t n;

			if (!jpg) {
				fprintf(stderr, "snap: out of memory\n");
				return EX_DEV;
			}
			n = jpeg_unpack(cap.bufs[held].start, len, jpg);
			if (!n) {
				fprintf(stderr, "snap: frame is not a complete JPEG "
					"(%zu bytes captured; -v for details)\n", len);
				free(jpg);
				return EX_DEV;
			}
			rc = save(out, jpg, n);
			free(jpg);
		} else {
			rc = save(out, cap.bufs[held].start, len);
		}
		if (rc == EX_OK)
			printf("%s\n", out);
		return rc;
	}
}

int main(int argc, char **argv)
{
	static const struct option lo[] = {
		{ "device", required_argument, NULL, 'd' },
		{ "size", required_argument, NULL, 's' },
		{ "format", required_argument, NULL, 'f' },
		{ "skip", required_argument, NULL, 'k' },
		{ "output", required_argument, NULL, 'o' },
		{ "list", no_argument, NULL, 'l' },
		{ "verbose", no_argument, NULL, 'v' },
		{ "help", no_argument, NULL, 'h' },
		{ NULL, 0, NULL, 0 }
	};
	const char *dev = NULL, *fmtname = "jpeg", *outarg = NULL, *ext;
	unsigned int w = 2592, h = 1944, fourcc;
	int skip = 10, list = 0, c, rc;
	char devpath[64], outpath[128];
	struct v4l2_capability qc;
	struct sigaction sa;

	while ((c = getopt_long(argc, argv, "d:o:vh", lo, NULL)) != -1) {
		char *e;

		switch (c) {
		case 'd': dev = optarg; break;
		case 'o': outarg = optarg; break;
		case 'v': verbose = 1; break;
		case 'l': list = 1; break;
		case 'h': usage(stdout); return EX_OK;
		case 's':
			if (parse_size(optarg, &w, &h) < 0) {
				fprintf(stderr, "snap: bad --size '%s' (want WxH)\n", optarg);
				return EX_USAGE;
			}
			break;
		case 'f': fmtname = optarg; break;
		case 'k': {
			long v = strtol(optarg, &e, 10);

			if (e == optarg || *e || v < 0 || v > 1000) {
				fprintf(stderr, "snap: bad --skip '%s'\n", optarg);
				return EX_USAGE;
			}
			skip = (int)v;
			break;
		}
		default: usage(stderr); return EX_USAGE;
		}
	}
	if (optind != argc) {
		usage(stderr);
		return EX_USAGE;
	}
	ext = fmtname;
	if (!strcmp(fmtname, "jpeg")) {
		fourcc = V4L2_PIX_FMT_JPEG;
		ext = "jpg";
	} else if (!strcmp(fmtname, "yuyv")) {
		fourcc = V4L2_PIX_FMT_YUYV;
	} else if (!strcmp(fmtname, "uyvy")) {
		fourcc = V4L2_PIX_FMT_UYVY;
	} else {
		fprintf(stderr, "snap: unknown --format '%s' (jpeg, yuyv or uyvy)\n", fmtname);
		return EX_USAGE;
	}

	memset(&sa, 0, sizeof(sa));
	sa.sa_handler = on_signal;
	sigemptyset(&sa.sa_mask);
	sigaction(SIGINT, &sa, NULL);
	sigaction(SIGTERM, &sa, NULL);

	if (dev) {
		cap.fd = open_node(dev, &qc);
		if (cap.fd < 0) {
			fprintf(stderr, "snap: %s: %s\n", dev, strerror(errno));
			return EX_DEV;
		}
	} else {
		cap.fd = find_isi(devpath, sizeof(devpath));
		if (cap.fd < 0) {
			fprintf(stderr, "snap: no %s video node found in /dev "
				"(modules loaded? see docs/CAMERA.md)\n", ISI_DRIVER);
			return EX_DEV;
		}
		dev = devpath;
	}
	VLOG("using %s\n", dev);

	if (list) {
		rc = do_list(cap.fd);
		cleanup();
		return rc;
	}

	if (!format_supported(cap.fd, fourcc)) {
		fprintf(stderr, "snap: device does not offer %s (see snap --list)\n",
			fmtname);
		cleanup();
		return EX_DEV;
	}

	if (outarg) {
		snprintf(outpath, sizeof(outpath), "%s", outarg);
	} else {
		time_t t = time(NULL);
		struct tm tm;
		char ts[32];

		localtime_r(&t, &tm);
		strftime(ts, sizeof(ts), "%Y%m%d-%H%M%S", &tm);
		snprintf(outpath, sizeof(outpath), PHOTO_DIR "/%s.%s", ts, ext);
	}

	/* Lock our pages before streaming: a page-in from the eMMC while the ISI streams can fail after a
	 * fast resume (see the magpie status backlog). Not fatal if refused. */
	if (mlockall(MCL_CURRENT | MCL_FUTURE) < 0)
		VLOG("mlockall: %s\n", strerror(errno));
	rc = capture(cap.fd, w, h, fourcc, fmtname, skip, outpath, outarg == NULL);
	cleanup();
	return rc;
}
