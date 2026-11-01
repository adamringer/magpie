/* Host test of the pure logic in br2-external/package/magpie-usbd/src/usbd_logic.c (run by tests/test_usbd_logic.py) */
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "usbd_logic.h"

static int failures;

#define CHECK(cond)                                                                  \
	do {                                                                         \
		if (!(cond)) {                                                       \
			printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);       \
			failures++;                                                  \
		}                                                                    \
	} while (0)

#define JS(s) (s), strlen(s)

static void test_header(void)
{
	struct usbd_hdr h = { USBD_MAGIC, 0x0003, 0, 0xA1B2C3D4u, 1234 }, g;
	uint8_t b[USBD_HDR_LEN];
	uint16_t c;

	usbd_hdr_pack(&h, b);
	CHECK(!memcmp(b, "MGP1", 4)); /* little-endian 0x3150474D */
	CHECK(b[4] == 3 && b[5] == 0 && b[6] == 0 && b[7] == 0);
	CHECK(b[8] == 0xD4 && b[9] == 0xC3 && b[10] == 0xB2 && b[11] == 0xA1);
	CHECK(b[12] == 0xD2 && b[13] == 0x04 && b[14] == 0 && b[15] == 0);
	memset(&g, 0xEE, sizeof(g));
	CHECK(usbd_hdr_unpack(b, &g) == 0);
	CHECK(g.magic == USBD_MAGIC && g.cmd == 3 && g.flags == 0 && g.seq == 0xA1B2C3D4u && g.len == 1234);

	/* every command is accepted, those around the range are not */
	for (c = 1; c <= 7; c++) {
		h.cmd = c;
		usbd_hdr_pack(&h, b);
		CHECK(usbd_hdr_unpack(b, &g) == 0);
	}
	h.cmd = 0;
	usbd_hdr_pack(&h, b);
	CHECK(usbd_hdr_unpack(b, &g) == USBD_HDR_BAD_CMD && g.seq == 0xA1B2C3D4u);
	h.cmd = 8;
	usbd_hdr_pack(&h, b);
	CHECK(usbd_hdr_unpack(b, &g) == USBD_HDR_BAD_CMD);
	h.cmd = 0x8001; /* a response is not a request */
	usbd_hdr_pack(&h, b);
	CHECK(usbd_hdr_unpack(b, &g) == USBD_HDR_BAD_CMD);

	/* length limit is inclusive */
	h.cmd = 1;
	h.len = USBD_MAX_PAYLOAD;
	usbd_hdr_pack(&h, b);
	CHECK(usbd_hdr_unpack(b, &g) == 0);
	h.len = USBD_MAX_PAYLOAD + 1;
	usbd_hdr_pack(&h, b);
	CHECK(usbd_hdr_unpack(b, &g) == USBD_HDR_BAD_LEN);
	h.len = 0xFFFFFFFFu;
	usbd_hdr_pack(&h, b);
	CHECK(usbd_hdr_unpack(b, &g) == USBD_HDR_BAD_LEN);

	/* bad magic wins over everything else */
	h.len = 0;
	usbd_hdr_pack(&h, b);
	b[0] ^= 1;
	CHECK(usbd_hdr_unpack(b, &g) == USBD_HDR_BAD_MAGIC);
	b[0] ^= 1;
	b[3] = '2';
	CHECK(usbd_hdr_unpack(b, &g) == USBD_HDR_BAD_MAGIC);
	memset(b, 0, sizeof(b));
	CHECK(usbd_hdr_unpack(b, &g) == USBD_HDR_BAD_MAGIC);

	/* flags are reserved, not checked */
	h.magic = USBD_MAGIC;
	h.flags = 0xFFFF;
	usbd_hdr_pack(&h, b);
	CHECK(usbd_hdr_unpack(b, &g) == 0 && g.flags == 0xFFFF);
}

static void test_names(void)
{
	char big[300];

	CHECK(usbd_name_kind("20260101-123456.jpg") == USBD_KIND_JPG);
	CHECK(usbd_name_kind("20260101-123456.snap") == USBD_KIND_SNAP);
	CHECK(usbd_name_kind("20260101-123456.snap.time") == USBD_KIND_SNAP_TIME);
	CHECK(usbd_name_kind("00000000-000000.jpg") == USBD_KIND_JPG);

	CHECK(usbd_name_kind("") == USBD_KIND_NONE);
	CHECK(usbd_name_kind(NULL) == USBD_KIND_NONE);
	CHECK(usbd_name_kind("..") == USBD_KIND_NONE);
	CHECK(usbd_name_kind(".") == USBD_KIND_NONE);
	CHECK(usbd_name_kind("../20260101-123456.jpg") == USBD_KIND_NONE);
	CHECK(usbd_name_kind("/20260101-123456.jpg") == USBD_KIND_NONE);
	CHECK(usbd_name_kind("/data/photos/20260101-123456.jpg") == USBD_KIND_NONE);
	CHECK(usbd_name_kind("a/20260101-123456.jpg") == USBD_KIND_NONE);
	CHECK(usbd_name_kind("20260101-123456.jpg/") == USBD_KIND_NONE);
	CHECK(usbd_name_kind("20260101-123456.jpg/..") == USBD_KIND_NONE);
	CHECK(usbd_name_kind("20260101-123456.jpg\n") == USBD_KIND_NONE); /* regex $ would allow it; we do not */
	CHECK(usbd_name_kind(" 20260101-123456.jpg") == USBD_KIND_NONE);
	/* suffixes */
	CHECK(usbd_name_kind("20260101-123456.JPG") == USBD_KIND_NONE);
	CHECK(usbd_name_kind("20260101-123456.jpeg") == USBD_KIND_NONE);
	CHECK(usbd_name_kind("20260101-123456.png") == USBD_KIND_NONE);
	CHECK(usbd_name_kind("20260101-123456.snaps") == USBD_KIND_NONE);
	CHECK(usbd_name_kind("20260101-123456.time") == USBD_KIND_NONE);
	CHECK(usbd_name_kind("20260101-123456.snap.time2") == USBD_KIND_NONE);
	CHECK(usbd_name_kind("20260101-123456.snap.") == USBD_KIND_NONE);
	CHECK(usbd_name_kind("20260101-123456.jpg.time") == USBD_KIND_NONE);
	CHECK(usbd_name_kind("20260101-123456") == USBD_KIND_NONE);
	CHECK(usbd_name_kind("20260101-123456.") == USBD_KIND_NONE);
	/* digit counts and separators */
	CHECK(usbd_name_kind("2026010-123456.jpg") == USBD_KIND_NONE);
	CHECK(usbd_name_kind("202601011-123456.jpg") == USBD_KIND_NONE);
	CHECK(usbd_name_kind("20260101-12345.jpg") == USBD_KIND_NONE);
	CHECK(usbd_name_kind("20260101-1234567.jpg") == USBD_KIND_NONE);
	CHECK(usbd_name_kind("20260101_123456.jpg") == USBD_KIND_NONE);
	CHECK(usbd_name_kind("2026010a-123456.jpg") == USBD_KIND_NONE);
	CHECK(usbd_name_kind("20260101-12345x.jpg") == USBD_KIND_NONE);
	CHECK(usbd_name_kind("20260101123456.jpg") == USBD_KIND_NONE);
	memset(big, '1', sizeof(big) - 1);
	big[sizeof(big) - 1] = 0;
	CHECK(usbd_name_kind(big) == USBD_KIND_NONE);
}

static void test_json_reader(void)
{
	char s[64];
	int64_t v;
	size_t i, n = 0;
	char deep[256];

	CHECK(usbd_json_get_str(JS("{\"name\":\"abc\"}"), "name", s, sizeof(s)) == 0 && !strcmp(s, "abc"));
	CHECK(usbd_json_get_str(JS(" {\n\t\"a\" : \"x\" ,\r\n \"name\" : \"abc\" } \n"), "name", s, sizeof(s)) == 0 &&
	      !strcmp(s, "abc"));
	CHECK(usbd_json_get_str(JS("{\"name\":\"\"}"), "name", s, sizeof(s)) == 0 && s[0] == 0);
	CHECK(usbd_json_get_str(JS("{\"name\":\"abc\"}"), "other", s, sizeof(s)) == -ENOENT);
	CHECK(usbd_json_get_str(JS("{}"), "name", s, sizeof(s)) == -ENOENT);
	/* first occurrence wins; other keys with nested values are skipped */
	CHECK(usbd_json_get_str(JS("{\"x\":{\"name\":\"no\",\"y\":[1,2,{\"z\":null}]},\"name\":\"first\",\"name\":\"second\"}"),
				"name", s, sizeof(s)) == 0 && !strcmp(s, "first"));
	/* escapes */
	CHECK(usbd_json_get_str(JS("{\"k\":\"a\\\"b\\\\c\\/d\\n\\t\\u0041\\u00e9\\ud83d\\ude00\"}"), "k", s, sizeof(s)) == 0 &&
	      !strcmp(s, "a\"b\\c/d\n\tA\xc3\xa9\xf0\x9f\x98\x80"));
	CHECK(usbd_json_get_str(JS("{\"k\":\"\\u0000\"}"), "k", s, sizeof(s)) == -EINVAL);
	CHECK(usbd_json_get_str(JS("{\"k\":\"\\ud83d\"}"), "k", s, sizeof(s)) == -EINVAL);
	CHECK(usbd_json_get_str(JS("{\"k\":\"\\ude00\"}"), "k", s, sizeof(s)) == -EINVAL);
	CHECK(usbd_json_get_str(JS("{\"k\":\"\\u12g4\"}"), "k", s, sizeof(s)) == -EINVAL);
	CHECK(usbd_json_get_str(JS("{\"k\":\"\\x\"}"), "k", s, sizeof(s)) == -EINVAL);
	CHECK(usbd_json_get_str(JS("{\"k\":\"a\nb\"}"), "k", s, sizeof(s)) == -EINVAL); /* raw control char */

	/* buffer limits: exactly fitting works, one more does not, and the buffer stays terminated */
	CHECK(usbd_json_get_str(JS("{\"k\":\"1234567\"}"), "k", s, 8) == 0 && !strcmp(s, "1234567"));
	memset(s, 'X', sizeof(s));
	CHECK(usbd_json_get_str(JS("{\"k\":\"12345678\"}"), "k", s, 8) == -ENAMETOOLONG && s[0] == 0);
	CHECK(usbd_json_get_str(JS("{\"k\":\"1\"}"), "k", s, 0) == -ENAMETOOLONG);
	CHECK(usbd_json_get_str(JS("{\"k\":\"1\"}"), "k", s, 1) == -ENAMETOOLONG);
	CHECK(usbd_json_get_str(JS("{\"k\":\"\"}"), "k", s, 1) == 0);
	/* a too-long key in front does not confuse the walk */
	CHECK(usbd_json_get_str(JS("{\"0123456789012345678901234567890123456789\":1,\"k\":\"v\"}"), "k", s, sizeof(s)) == 0 &&
	      !strcmp(s, "v"));

	/* wrong type */
	CHECK(usbd_json_get_str(JS("{\"k\":5}"), "k", s, sizeof(s)) == -EINVAL);
	CHECK(usbd_json_get_str(JS("{\"k\":null}"), "k", s, sizeof(s)) == -EINVAL);
	CHECK(usbd_json_get_int(JS("{\"k\":\"5\"}"), "k", &v) == -EINVAL);
	CHECK(usbd_json_get_int(JS("{\"k\":true}"), "k", &v) == -EINVAL);

	/* integers */
	CHECK(usbd_json_get_int(JS("{\"k\":0}"), "k", &v) == 0 && v == 0);
	CHECK(usbd_json_get_int(JS("{\"k\":123456}"), "k", &v) == 0 && v == 123456);
	CHECK(usbd_json_get_int(JS("{\"k\":-42}"), "k", &v) == 0 && v == -42);
	CHECK(usbd_json_get_int(JS("{\"k\": 1767225600123 }"), "k", &v) == 0 && v == 1767225600123LL);
	CHECK(usbd_json_get_int(JS("{\"k\":9223372036854775807}"), "k", &v) == 0 && v == INT64_MAX);
	CHECK(usbd_json_get_int(JS("{\"k\":-9223372036854775808}"), "k", &v) == 0 && v == INT64_MIN);
	CHECK(usbd_json_get_int(JS("{\"k\":9223372036854775808}"), "k", &v) == -EINVAL);
	CHECK(usbd_json_get_int(JS("{\"k\":-9223372036854775809}"), "k", &v) == -EINVAL);
	CHECK(usbd_json_get_int(JS("{\"k\":99999999999999999999999999}"), "k", &v) == -EINVAL);
	CHECK(usbd_json_get_int(JS("{\"k\":1.5}"), "k", &v) == -EINVAL);
	CHECK(usbd_json_get_int(JS("{\"k\":1e3}"), "k", &v) == -EINVAL);
	CHECK(usbd_json_get_int(JS("{\"k\":01}"), "k", &v) == -EINVAL);
	CHECK(usbd_json_get_int(JS("{\"k\":-}"), "k", &v) == -EINVAL);
	CHECK(usbd_json_get_int(JS("{\"k\":+1}"), "k", &v) == -EINVAL);
	CHECK(usbd_json_get_int(JS("{\"k\":}"), "k", &v) == -EINVAL);
	CHECK(usbd_json_get_int(JS("{\"k\":1}"), "z", &v) == -ENOENT);

	/* malformed */
	CHECK(usbd_json_get_str(NULL, 0, "k", s, sizeof(s)) == -EINVAL);
	CHECK(usbd_json_get_str(JS(""), "k", s, sizeof(s)) == -EINVAL);
	CHECK(usbd_json_get_str(JS("   "), "k", s, sizeof(s)) == -EINVAL);
	CHECK(usbd_json_get_str(JS("[]"), "k", s, sizeof(s)) == -EINVAL);
	CHECK(usbd_json_get_str(JS("\"k\""), "k", s, sizeof(s)) == -EINVAL);
	CHECK(usbd_json_get_str(JS("{\"k\":\"v\"} x"), "k", s, sizeof(s)) == -EINVAL);
	CHECK(usbd_json_get_str(JS("{\"k\":\"v\"}{}"), "k", s, sizeof(s)) == -EINVAL);
	CHECK(usbd_json_get_str(JS("{\"k\":\"v\",}"), "k", s, sizeof(s)) == -EINVAL);
	CHECK(usbd_json_get_str(JS("{,\"k\":\"v\"}"), "k", s, sizeof(s)) == -EINVAL);
	CHECK(usbd_json_get_str(JS("{\"k\" \"v\"}"), "k", s, sizeof(s)) == -EINVAL);
	CHECK(usbd_json_get_str(JS("{k:\"v\"}"), "k", s, sizeof(s)) == -EINVAL);
	CHECK(usbd_json_get_str(JS("{\"k\":\"v\" \"j\":1}"), "k", s, sizeof(s)) == -EINVAL);
	CHECK(usbd_json_get_str(JS("{\"k\":tru}"), "k", s, sizeof(s)) == -EINVAL);
	CHECK(usbd_json_get_str(JS("{\"k\":\"v\",\"j\":[1,2}"), "k", s, sizeof(s)) == -EINVAL);
	/* every truncation of a valid document is rejected, none reads past the length it was given */
	{
		static const char doc[] = "{\"name\":\"20260101-123456.jpg\",\"x\":[1,{\"a\":\"b\"}],\"offset\":10}";
		char *copy;

		for (i = 0; i < sizeof(doc) - 1; i++) {
			copy = malloc(i ? i : 1); /* exact-size heap block: valgrind/ASan would catch an overread */
			memcpy(copy, doc, i);
			CHECK(usbd_json_get_str(copy, i, "name", s, sizeof(s)) != 0);
			CHECK(usbd_json_get_int(copy, i, "offset", &v) != 0);
			free(copy);
		}
		CHECK(usbd_json_get_int(doc, sizeof(doc) - 1, "offset", &v) == 0 && v == 10);
	}
	/* nesting limit */
	memset(deep, '[', 100);
	memcpy(deep + 100, "1", 1);
	memset(deep + 101, ']', 100);
	{
		char doc[400];
		int len = snprintf(doc, sizeof(doc), "{\"x\":%.*s,\"k\":\"v\"}", 201, deep);

		CHECK(len > 0 && usbd_json_get_str(doc, (size_t)len, "k", s, sizeof(s)) == -EINVAL);
		len = snprintf(doc, sizeof(doc), "{\"x\":[[[1]]],\"k\":\"v\"}");
		CHECK(usbd_json_get_str(doc, (size_t)len, "k", s, sizeof(s)) == 0);
	}
	/* oversized: a long string value and a huge document of valid padding */
	{
		size_t big = 70000;
		char *doc = malloc(big + 64);
		int len;

		len = snprintf(doc, 32, "{\"k\":\"");
		memset(doc + len, 'a', big);
		memcpy(doc + len + big, "\"}", 2);
		CHECK(usbd_json_get_str(doc, (size_t)len + big + 2, "k", s, sizeof(s)) == -ENAMETOOLONG);
		free(doc);
	}
	(void)n;

	/* DELETE */
	{
		struct usbd_del_entry e[4];
		static const char h1[] = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
		char doc[1024];
		int len;
		size_t cnt = 99;

		len = snprintf(doc, sizeof(doc),
			       "{\"files\":[{\"name\":\"20260101-123456.jpg\",\"sha256\":\"%s\"},"
			       "{\"sha256\":\"%s\",\"name\":\"20260101-123456.snap.time\",\"extra\":[1]}]}", h1, h1);
		CHECK(usbd_json_parse_delete(doc, (size_t)len, e, 4, &cnt) == 0 && cnt == 2);
		CHECK(!strcmp(e[0].name, "20260101-123456.jpg") && !strcmp(e[0].sha, h1));
		CHECK(!strcmp(e[1].name, "20260101-123456.snap.time") && !strcmp(e[1].sha, h1));
		CHECK(usbd_json_parse_delete(JS("{\"files\":[]}"), e, 4, &cnt) == 0 && cnt == 0);
		/* limit */
		CHECK(usbd_json_parse_delete(doc, (size_t)len, e, 1, &cnt) == -EINVAL);
		CHECK(usbd_json_parse_delete(doc, (size_t)len, e, 2, &cnt) == 0 && cnt == 2);
		/* upper-case hex is accepted and normalised */
		len = snprintf(doc, sizeof(doc), "{\"files\":[{\"name\":\"20260101-123456.jpg\",\"sha256\":\"E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855\"}]}");
		CHECK(usbd_json_parse_delete(doc, (size_t)len, e, 4, &cnt) == 0 && !strcmp(e[0].sha, h1));
		/* rejects */
		CHECK(usbd_json_parse_delete(JS("{}"), e, 4, &cnt) == -EINVAL);
		CHECK(usbd_json_parse_delete(JS("{\"files\":{}}"), e, 4, &cnt) == -EINVAL);
		CHECK(usbd_json_parse_delete(JS("{\"files\":[1]}"), e, 4, &cnt) == -EINVAL);
		CHECK(usbd_json_parse_delete(JS("{\"files\":[{}]}"), e, 4, &cnt) == -EINVAL);
		CHECK(usbd_json_parse_delete(JS("{\"files\":[{\"name\":\"20260101-123456.jpg\"}]}"), e, 4, &cnt) == -EINVAL);
		len = snprintf(doc, sizeof(doc), "{\"files\":[{\"name\":\"../20260101-123456.jpg\",\"sha256\":\"%s\"}]}", h1);
		CHECK(usbd_json_parse_delete(doc, (size_t)len, e, 4, &cnt) == -EINVAL);
		len = snprintf(doc, sizeof(doc), "{\"files\":[{\"name\":\"20260101-123456.txt\",\"sha256\":\"%s\"}]}", h1);
		CHECK(usbd_json_parse_delete(doc, (size_t)len, e, 4, &cnt) == -EINVAL);
		len = snprintf(doc, sizeof(doc), "{\"files\":[{\"name\":\"20260101-123456.jpg\",\"sha256\":\"%.63s\"}]}", h1);
		CHECK(usbd_json_parse_delete(doc, (size_t)len, e, 4, &cnt) == -EINVAL);
		len = snprintf(doc, sizeof(doc), "{\"files\":[{\"name\":\"20260101-123456.jpg\",\"sha256\":\"%sa\"}]}", h1);
		CHECK(usbd_json_parse_delete(doc, (size_t)len, e, 4, &cnt) == -EINVAL);
		len = snprintf(doc, sizeof(doc), "{\"files\":[{\"name\":\"20260101-123456.jpg\",\"sha256\":\"g%.63s\"}]}", h1);
		CHECK(usbd_json_parse_delete(doc, (size_t)len, e, 4, &cnt) == -EINVAL);
		/* truncated */
		len = snprintf(doc, sizeof(doc), "{\"files\":[{\"name\":\"20260101-123456.jpg\",\"sha256\":\"%s\"}]}", h1);
		for (i = 0; i < (size_t)len; i++)
			CHECK(usbd_json_parse_delete(doc, i, e, 4, &cnt) == -EINVAL);
		CHECK(usbd_json_parse_delete(doc, (size_t)len, e, 4, &cnt) == 0 && cnt == 1);
	}

	/* the full 500 entries fit */
	{
		struct usbd_del_entry *e = malloc(USBD_DELETE_MAX * sizeof(*e));
		char *doc = malloc(200000);
		size_t pos = 0, cnt = 0;
		static const char h1[] = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

		pos += (size_t)sprintf(doc, "{\"files\":[");
		for (i = 0; i < 501; i++)
			pos += (size_t)sprintf(doc + pos, "%s{\"name\":\"20260101-%06zu.jpg\",\"sha256\":\"%s\"}", i ? "," : "", i, h1);
		sprintf(doc + pos, "]}");
		CHECK(usbd_json_parse_delete(doc, strlen(doc), e, USBD_DELETE_MAX, &cnt) == -EINVAL); /* 501 */
		/* drop the last element */
		pos -= strlen(",") + strlen("{\"name\":\"20260101-000500.jpg\",\"sha256\":\"\"}") + 64;
		sprintf(doc + pos, "]}");
		CHECK(usbd_json_parse_delete(doc, strlen(doc), e, USBD_DELETE_MAX, &cnt) == 0 && cnt == 500);
		CHECK(!strcmp(e[499].name, "20260101-000499.jpg"));
		free(doc);
		free(e);
	}
}

static void test_list_request(void)
{
	char after[USBD_NAME_MAX];
	unsigned max;

	CHECK(usbd_parse_list_req(JS("{\"after\":\"\",\"max\":200}"), after, &max) == 0 && after[0] == 0 && max == 200);
	CHECK(usbd_parse_list_req(JS("{\"after\":\"20260101-123456.jpg\",\"max\":1}"), after, &max) == 0 &&
	      !strcmp(after, "20260101-123456.jpg") && max == 1);
	CHECK(usbd_parse_list_req(JS("{\"max\":500}"), after, &max) == 0 && after[0] == 0 && max == 500);
	CHECK(usbd_parse_list_req(JS("{}"), after, &max) == 0 && after[0] == 0 && max == USBD_LIST_DEFAULT);
	CHECK(usbd_parse_list_req("", 0, after, &max) == 0 && max == USBD_LIST_DEFAULT);
	CHECK(usbd_parse_list_req(JS("{\"max\":501}"), after, &max) == -EINVAL);
	CHECK(usbd_parse_list_req(JS("{\"max\":0}"), after, &max) == -EINVAL);
	CHECK(usbd_parse_list_req(JS("{\"max\":-3}"), after, &max) == -EINVAL);
	CHECK(usbd_parse_list_req(JS("{\"max\":\"5\"}"), after, &max) == -EINVAL);
	CHECK(usbd_parse_list_req(JS("{\"after\":\"../x\"}"), after, &max) == -EINVAL);
	CHECK(usbd_parse_list_req(JS("{\"after\":5}"), after, &max) == -EINVAL);
	CHECK(usbd_parse_list_req(JS("{\"after\":\"20260101-123456.jpg\""), after, &max) == -EINVAL);
	CHECK(usbd_parse_list_req(JS("garbage"), after, &max) == -EINVAL);
}

static void test_writer(void)
{
	char buf[64];
	struct usbd_jw w;

	usbd_jw_init(&w, buf, sizeof(buf));
	usbd_jw_str(&w, "plain");
	CHECK(!w.err && !strcmp(buf, "\"plain\""));

	usbd_jw_init(&w, buf, sizeof(buf));
	usbd_jw_str(&w, "a\"b\\c\n\r\t\b\f\x01\x1f/\x7f\xc3\xa9");
	CHECK(!w.err && !strcmp(buf, "\"a\\\"b\\\\c\\n\\r\\t\\b\\f\\u0001\\u001f/\x7f\xc3\xa9\""));

	usbd_jw_init(&w, buf, sizeof(buf));
	usbd_jw_raw(&w, "{\"n\":");
	usbd_jw_int(&w, -9223372036854775807LL - 1);
	usbd_jw_fmt(&w, ",\"f\":%.2f,\"t\":%s}", 1.5, "true");
	CHECK(!w.err && !strcmp(buf, "{\"n\":-9223372036854775808,\"f\":1.50,\"t\":true}"));

	/* a writer that runs out of room stays terminated and sticky */
	usbd_jw_init(&w, buf, 8);
	usbd_jw_raw(&w, "1234567");
	CHECK(!w.err && w.len == 7);
	usbd_jw_raw(&w, "8");
	CHECK(w.err && w.len == 7 && !strcmp(buf, "1234567"));
	usbd_jw_raw(&w, "");
	usbd_jw_int(&w, 1);
	CHECK(w.err && !strcmp(buf, "1234567"));
	usbd_jw_init(&w, buf, 4);
	usbd_jw_str(&w, "abc"); /* needs 5 */
	CHECK(w.err && strlen(buf) < 4);
	usbd_jw_init(&w, buf, 5);
	usbd_jw_fmt(&w, "%s", "abcdef");
	CHECK(w.err && strlen(buf) < 5);
	usbd_jw_init(&w, buf, 0);
	CHECK(w.err);
	usbd_jw_str(&w, "x");

	/* what we write, the reader reads back */
	{
		char out[128], back[64];
		const char *tricky = "q\"u\\o\nte\x01";

		usbd_jw_init(&w, out, sizeof(out));
		usbd_jw_raw(&w, "{\"k\":");
		usbd_jw_str(&w, tricky);
		usbd_jw_raw(&w, "}");
		CHECK(!w.err && usbd_json_get_str(out, w.len, "k", back, sizeof(back)) == 0 && !strcmp(back, tricky));
	}
}

static void sha_hex(const void *data, size_t n, char hex[65])
{
	struct usbd_sha256 c;
	uint8_t d[32];

	usbd_sha256_init(&c);
	usbd_sha256_update(&c, data, n);
	usbd_sha256_final(&c, d);
	usbd_hex(d, sizeof(d), hex);
}

static void test_sha256(void)
{
	char hex[65];
	struct usbd_sha256 c;
	uint8_t d[32], *buf;
	size_t i;

	sha_hex("", 0, hex);
	CHECK(!strcmp(hex, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"));
	sha_hex("abc", 3, hex);
	CHECK(!strcmp(hex, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"));
	sha_hex("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq", 56, hex);
	CHECK(!strcmp(hex, "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1"));

	/* one million 'a', fed in odd-sized pieces */
	usbd_sha256_init(&c);
	buf = malloc(1000000);
	memset(buf, 'a', 1000000);
	for (i = 0; i < 1000000;) {
		size_t piece = (i % 7) * 13 + 1;

		if (piece > 1000000 - i)
			piece = 1000000 - i;
		usbd_sha256_update(&c, buf + i, piece);
		i += piece;
	}
	usbd_sha256_final(&c, d);
	usbd_hex(d, sizeof(d), hex);
	CHECK(!strcmp(hex, "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0"));
	/* and in one call */
	sha_hex(buf, 1000000, hex);
	CHECK(!strcmp(hex, "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0"));

	/* block-boundary lengths agree between one call and byte-by-byte */
	for (i = 54; i <= 66; i++) {
		char a[65], b[65];

		sha_hex(buf, i, a);
		usbd_sha256_init(&c);
		{
			size_t j;

			for (j = 0; j < i; j++)
				usbd_sha256_update(&c, buf + j, 1);
		}
		usbd_sha256_final(&c, d);
		usbd_hex(d, sizeof(d), b);
		CHECK(!strcmp(a, b));
	}
	/* 55, 56 and 64 'a' (padding corner cases), values from a reference implementation */
	sha_hex(buf, 55, hex);
	CHECK(!strcmp(hex, "9f4390f8d30c2dd92ec9f095b65e2b9ae9b0a925a5258e241c9f1e910f734318"));
	sha_hex(buf, 56, hex);
	CHECK(!strcmp(hex, "b35439a4ac6f0948b6d6f9e3c6af0f5f590ce20f1bde7090ef7970686ec6738a"));
	sha_hex(buf, 64, hex);
	CHECK(!strcmp(hex, "ffe054fe7ae0cb6dc65c3af9b61d5209f439851db43d0ba5997337df154668eb"));
	free(buf);
}

static void fill(struct usbd_fent *e, size_t i, const char *name)
{
	memset(&e[i], 0, sizeof(e[i]));
	strcpy(e[i].name, name);
	e[i].size = 100 + i;
	e[i].mtime = 1000 + (int64_t)i;
}

static void test_paging(void)
{
	struct usbd_fent e[6];
	size_t start, cnt;
	int more;

	/* empty */
	cnt = usbd_list_page(e, 0, "", 10, &start, &more);
	CHECK(cnt == 0 && start == 0 && !more);
	cnt = usbd_list_page(e, 0, "20260101-000000.jpg", 10, &start, &more);
	CHECK(cnt == 0 && !more);

	/* unsorted input is sorted, sizes and mtimes travel with their names */
	fill(e, 0, "20260103-000000.jpg");
	fill(e, 1, "20260101-000000.jpg");
	fill(e, 2, "20260102-000000.snap");
	fill(e, 3, "20260102-000000.jpg");
	fill(e, 4, "20260101-000000.snap.time");
	cnt = usbd_list_page(e, 5, "", 10, &start, &more);
	CHECK(cnt == 5 && start == 0 && !more);
	CHECK(!strcmp(e[0].name, "20260101-000000.jpg") && e[0].size == 101 && e[0].mtime == 1001);
	CHECK(!strcmp(e[1].name, "20260101-000000.snap.time") && e[1].size == 104);
	CHECK(!strcmp(e[2].name, "20260102-000000.jpg"));
	CHECK(!strcmp(e[3].name, "20260102-000000.snap"));
	CHECK(!strcmp(e[4].name, "20260103-000000.jpg") && e[4].size == 100);

	/* max = 1 walks the list one at a time */
	cnt = usbd_list_page(e, 5, "", 1, &start, &more);
	CHECK(cnt == 1 && start == 0 && more);
	cnt = usbd_list_page(e, 5, e[0].name, 1, &start, &more);
	CHECK(cnt == 1 && start == 1 && more && !strcmp(e[start].name, "20260101-000000.snap.time"));
	cnt = usbd_list_page(e, 5, "20260102-000000.snap", 1, &start, &more);
	CHECK(cnt == 1 && start == 4 && !more && !strcmp(e[start].name, "20260103-000000.jpg"));

	/* after = last: nothing, no more */
	cnt = usbd_list_page(e, 5, "20260103-000000.jpg", 3, &start, &more);
	CHECK(cnt == 0 && !more);
	/* after beyond the last */
	cnt = usbd_list_page(e, 5, "20991231-235959.jpg", 3, &start, &more);
	CHECK(cnt == 0 && !more);
	/* after not in the list: the next greater name */
	cnt = usbd_list_page(e, 5, "20260102-000000.a", 2, &start, &more);
	CHECK(cnt == 2 && start == 2 && more && !strcmp(e[start].name, "20260102-000000.jpg"));
	/* after before the first */
	cnt = usbd_list_page(e, 5, "00000000-000000.jpg", 2, &start, &more);
	CHECK(cnt == 2 && start == 0 && more);
	/* max exactly the remainder: no more */
	cnt = usbd_list_page(e, 5, "20260101-000000.jpg", 4, &start, &more);
	CHECK(cnt == 4 && start == 1 && !more);
	/* max one less than the remainder: more */
	cnt = usbd_list_page(e, 5, "20260101-000000.jpg", 3, &start, &more);
	CHECK(cnt == 3 && start == 1 && more);
	/* max 0 selects nothing but still tells whether anything follows */
	cnt = usbd_list_page(e, 5, "", 0, &start, &more);
	CHECK(cnt == 0 && more);
	/* single entry */
	fill(e, 0, "20260101-000000.jpg");
	cnt = usbd_list_page(e, 1, "", 1, &start, &more);
	CHECK(cnt == 1 && start == 0 && !more);
	cnt = usbd_list_page(e, 1, "20260101-000000.jpg", 1, &start, &more);
	CHECK(cnt == 0 && !more);

	/* paging a longer list to the end visits every entry once, in order */
	{
		struct usbd_fent *big = malloc(1234 * sizeof(*big));
		char after[USBD_NAME_MAX] = "";
		size_t i, seen = 0;

		for (i = 0; i < 1234; i++) {
			char nm[USBD_NAME_MAX];

			snprintf(nm, sizeof(nm), "20260101-%06zu.jpg", (i * 7919) % 1234); /* a permutation of 0..1233 */
			fill(big, i, nm);
		}
		do {
			cnt = usbd_list_page(big, 1234, after, 500, &start, &more);
			for (i = 0; i < cnt; i++) {
				char want[USBD_NAME_MAX];

				snprintf(want, sizeof(want), "20260101-%06zu.jpg", seen + i);
				CHECK(!strcmp(big[start + i].name, want));
			}
			seen += cnt;
			if (cnt)
				strcpy(after, big[start + cnt - 1].name);
		} while (more);
		CHECK(seen == 1234);
		free(big);
	}
}

static void test_rtt(void)
{
	char line[200];
	double ep;
	unsigned pres;

	/*
	 * t_after = 1767225600.5 (2026-01-01T00:00:00Z + 0.5 s), count = 1000000, pres = 32:
	 * count * 32 / 32768 = 976.5625 s, epoch_fit = 1767225600.5 - 976.5625 = 1767224623.9375.
	 * With epoch_utc = 1767224623.9 the difference is 0.0375 s = +37.5 ms.
	 */
	CHECK(usbd_rtt_cal_line(line, sizeof(line), 1767225600.5, 1000000, 32, 1767224623.9) > 0);
	CHECK(!strcmp(line, "cal=2026-01-01T00:00:00Z epoch_fit=1767224623.9375 diff_ms=+37.5 fit_roundtrip_ms=0.0"));
	/* the other sign, and a later second (the ISO time is the integer second of t_after) */
	CHECK(usbd_rtt_cal_line(line, sizeof(line), 1767225661.999, 1000000, 32, 1767224685.0) > 0);
	CHECK(!strcmp(line, "cal=2026-01-01T00:01:01Z epoch_fit=1767224685.4365 diff_ms=+436.5 fit_roundtrip_ms=0.0"));
	CHECK(usbd_rtt_cal_line(line, sizeof(line), 1767225600.5, 1000000, 32, 1767224624.0) > 0);
	CHECK(strstr(line, "diff_ms=-62.5 "));
	/* count 0 */
	CHECK(usbd_rtt_cal_line(line, sizeof(line), 1767225600.0, 0, 32, 1767225600.0) > 0);
	CHECK(!strcmp(line, "cal=2026-01-01T00:00:00Z epoch_fit=1767225600.0000 diff_ms=+0.0 fit_roundtrip_ms=0.0"));
	/* a different prescaler scales the count */
	CHECK(usbd_rtt_cal_line(line, sizeof(line), 1000.0, 1024, 64, 998.0) > 0);
	CHECK(strstr(line, "epoch_fit=998.0000 diff_ms=+0.0"));
	/* too small a buffer */
	CHECK(usbd_rtt_cal_line(line, 20, 1767225600.5, 1000000, 32, 1767224623.9) == -1);

	CHECK(usbd_rtt_epoch_parse("epoch_utc=1767224623.9000 pres=32 set=2026-01-01T00:00:00Z fit_roundtrip_ms=3.1\n", &ep, &pres) == 0 &&
	      ep == 1767224623.9 && pres == 32);
	/* the last line counts */
	CHECK(usbd_rtt_epoch_parse("epoch_utc=1.0 pres=32 set=x\nepoch_utc=2.5 pres=64 set=y\n\n", &ep, &pres) == 0 &&
	      ep == 2.5 && pres == 64);
	CHECK(usbd_rtt_epoch_parse("epoch_utc=7 pres=32", &ep, &pres) == 0 && ep == 7.0);
	CHECK(usbd_rtt_epoch_parse("", &ep, &pres) == -1);
	CHECK(usbd_rtt_epoch_parse("\n\n", &ep, &pres) == -1);
	CHECK(usbd_rtt_epoch_parse("epoch_utc=5 pres=32\ngarbage\n", &ep, &pres) == -1);
	CHECK(usbd_rtt_epoch_parse("epoch_utc= pres=32\n", &ep, &pres) == -1);
	CHECK(usbd_rtt_epoch_parse("epoch_utc=5\n", &ep, &pres) == -1);
}

static void test_tools(void)
{
	int64_t raw;

	CHECK(usbd_parse_battery("raw: 249\nscale: 3.3 mV/LSB\nADC pin: 822 mV (divider ratio unknown)\n", &raw) == 0 && raw == 249);
	CHECK(usbd_parse_battery("scale: 3\nraw: 7", &raw) == 0 && raw == 7);
	CHECK(usbd_parse_battery("raw:12 x", &raw) == 0 && raw == 12);
	CHECK(usbd_parse_battery("", &raw) == -1);
	CHECK(usbd_parse_battery("magpie-battery: read failed\n", &raw) == -1);
	CHECK(usbd_parse_battery("raw: \n", &raw) == -1);
	CHECK(usbd_parse_battery("raw: abc\n", &raw) == -1);
	CHECK(usbd_parse_battery("raw: 12abc\n", &raw) == -1);

	CHECK(usbd_parse_charge("charging (raw PD15=0)\n") == 1);
	CHECK(usbd_parse_charge("not charging (raw PD15=1)\n") == 0);
	CHECK(usbd_parse_charge("magpie-charge: cannot export PD15\n") == -1);
	CHECK(usbd_parse_charge("") == -1);
}

int main(void)
{
	test_header();
	test_names();
	test_json_reader();
	test_list_request();
	test_writer();
	test_sha256();
	test_paging();
	test_rtt();
	test_tools();

	if (failures) {
		printf("%d check(s) failed\n", failures);
		return 1;
	}
	printf("all checks passed\n");
	return 0;
}
