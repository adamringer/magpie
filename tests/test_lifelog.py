"""Host-side dry run of magpie-lifelogd and magpie-accel with stubbed helpers (tests/lifelog_stubs).

Every path and command the scripts use is overridable through the environment (see
rootfs-overlay/usr/lib/magpie-lifelog.sh), so the decision logic runs unchanged on the host.
"""
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
OVERLAY = ROOT / "br2-external" / "board" / "magpie" / "rootfs-overlay"
DAEMON = OVERLAY / "usr" / "bin" / "magpie-lifelogd"
ACCEL = OVERLAY / "usr" / "bin" / "magpie-accel"
LIB = OVERLAY / "usr" / "lib" / "magpie-lifelog.sh"
INIT = OVERLAY / "etc" / "init.d" / "S50lifelog"
STUBS = pathlib.Path(__file__).resolve().parent / "lifelog_stubs"

SHELLS = [s for s in (["sh"], ["dash"], ["busybox", "sh"]) if shutil.which(s[0])]


class Env:
    def __init__(self, tmp, shell):
        self.shell = shell
        self.s = tmp / "state"
        self.data = tmp / "data"
        (self.s / "rtc").mkdir(parents=True)
        self.data.mkdir()
        (self.s / "rtc" / "wakealarm").write_text("")
        (self.s / "uptime").write_text("10.42 20.00\n")
        (self.s / "vbus.seq").write_text("absent\n")
        (self.s / "battery").write_text("240\n")
        (self.s / "video0").write_text("")
        (self.s / "spidev").write_text("")
        (self.s / "calls").write_text("")
        (self.s / "UDC").write_text("udc.0\n")
        self.env = dict(
            os.environ,
            STUB_STATE=str(self.s),
            LIFELOG_LIB=str(LIB),
            LL_DATA=str(self.data),
            RTC_DIR=str(self.s / "rtc"),
            KMSG="/dev/null",
            PROC_UPTIME=str(self.s / "uptime"),
            VIDEO_NODE=str(self.s / "video0"),
            SPI_NODE=str(self.s / "spidev"),
            DEVMEM=str(STUBS / "devmem"),
            BATTERY_CMD=str(STUBS / "magpie-battery"),
            SNAP_CMD=str(STUBS / "snap"),
            GPSNAP_CMD=str(STUBS / "gpsnap"),
            LED_CMD=str(STUBS / "magpie-led"),
            ACCEL_CMD=str(STUBS / "magpie-accel"),
            DMESG_CMD=str(STUBS / "dmesg"),
            MOUNTPOINT_CMD=str(STUBS / "mountpoint"),
            POWEROFF_CMD=str(STUBS / "poweroff") + " -f",
            POWEROFF_FALLBACK="",
            SLEEP_MODE="poweroff",  # the old tests assume one cycle, then power off; the resume tests override this
            PM_STATE=str(self.s / "pm_state"),
            UDC_FILE=str(self.s / "UDC"),
            TIMEOUT_CMD="",
            SLEEP_CMD="true",
            LIFELOG_MAX_LOOPS="12",
            I2CGET=str(STUBS / "i2cget"),
            I2CSET=str(STUBS / "i2cset"),
            RMMOD_CMD=str(STUBS / "rmmod"),
            MODPROBE_CMD=str(STUBS / "modprobe"),
            I2C_DEV_DIR=str(self.s / "i2cdev"),
            I2C_NODE_DIR=str(self.s / "devnodes"),
        )
        (self.s / "i2cdev" / "3-0018").mkdir(parents=True)
        (self.s / "devnodes").mkdir()
        (self.s / "devnodes" / "i2c-3").write_text("")

    def put(self, name, text):
        (self.s / name).write_text(text)

    def conf(self, text):
        (self.data / "lifelog.conf").write_text(text)

    def state(self, fd=0, dark=0):
        d = self.data / "lifelog"
        d.mkdir(exist_ok=True)
        (d / "state").write_text(f"fd_count={fd}\ndark_count={dark}\n")

    def run(self, script=DAEMON, *args):
        return subprocess.run([*self.shell, str(script), *args], env=self.env,
                              capture_output=True, text=True, timeout=60)

    @property
    def calls(self):
        return (self.s / "calls").read_text().splitlines()

    def called(self, prefix):
        return [c for c in self.calls if c.startswith(prefix)]

    def csv_rows(self):
        f = self.data / "lifelog" / "log.csv"
        if not f.exists():
            return []
        lines = f.read_text().splitlines()
        hdr = lines[0].split(",")
        return [dict(zip(hdr, ln.split(","))) for ln in lines[1:]]

    def daemon_log(self):
        f = self.data / "lifelog" / "daemon.log"
        return f.read_text() if f.exists() else ""

    def state_file(self):
        return dict(ln.split("=") for ln in (self.data / "lifelog" / "state").read_text().split())


@pytest.fixture(params=SHELLS, ids=[" ".join(s) for s in SHELLS])
def env(request, tmp_path):
    return Env(tmp_path, request.param)


def poweroff_alarm(env):
    p = env.called("poweroff")
    assert len(p) == 1, env.calls
    assert p[0].startswith("poweroff -f alarm=")
    return p[0].split("alarm=")[1]


def test_scripts_parse():
    for f in (DAEMON, ACCEL, LIB, INIT):
        for sh in SHELLS:
            r = subprocess.run([*sh, "-n", str(f)], capture_output=True, text=True)
            assert r.returncode == 0, (sh, f, r.stderr)


def test_docked_does_nothing(env):
    env.put("vbus.seq", "present\n")
    env.run()
    assert env.calls == []
    assert not (env.data / "photos").exists()


def test_needs_three_unplugged_checks(env):
    # present, then two absent, then present again: never three in a row
    env.put("vbus.seq", "absent\nabsent\npresent\n")
    env.run()
    assert env.calls == []


def test_normal_cycle(env):
    env.put("vbus.seq", "present\nabsent\nabsent\nabsent\nabsent\n")  # the last one is the re-check before power-off
    env.put("dmesg", "[ 0.0] foo\n")
    r = env.run()
    assert poweroff_alarm(env) == "+15"  # 30 - 10 (uptime) - 5 (preamble)
    calls = env.calls
    snap = [c for c in calls if c.startswith("snap -o ")]
    gps = [c for c in calls if c.startswith("gpsnap ")]
    assert len(snap) == 1 and len(gps) == 1
    stem = snap[0][len("snap -o "):-len(".jpg")]
    assert stem.startswith(str(env.data / "photos") + "/")
    assert gps[0] == f"gpsnap -a 1 -o {stem}.snap"
    assert calls.index(snap[0]) < calls.index(gps[0])  # photo first
    # battery 240: the blink shows level 4 (LEDs 1-4)
    assert all(f"magpie-led {n} on" in calls and f"magpie-led {n} off" in calls for n in (1, 2, 3, 4))
    assert "magpie-accel wake none" in calls  # tap wake off by default; the chip is powered down
    assert "devmem write 0xfffffe68 0" in calls  # boot counter cleared (stub starts at 1)
    assert "devmem write 0xfffffe64 0x4D500020" in calls
    assert calls[-1].startswith("poweroff -f")
    assert calls.index("magpie-accel wake none") < calls.index(calls[-1])
    rows = env.csv_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["photo_ok"] == "1" and row["photo_bytes"] == "120000" and row["gps_ok"] == "1"
    assert row["battery"] == "240" and row["x_mg"] == "-940" and row["x_raw"] == "-15040"
    assert row["alarm_s"] == "15" and row["end"] == "poweroff" and row["facedown"] == "0" and row["dark"] == "0"
    assert (env.data / "lifelog" / "log.csv").read_text().startswith("date,wake,")
    assert env.state_file() == {"fd_count": "0", "dark_count": "0"}
    assert r.returncode in (0, 1)


def test_rtc_wake_starts_at_once(env):
    env.put("dmesg", "[ 1.0] at91-poweroff fffffe10.poweroff: Wake-Up source: RTC\n")
    env.put("vbus.seq", "absent\n")
    env.run(DAEMON)  # MAX_LOOPS would stop it after 12 loops; one is enough for a cycle
    assert poweroff_alarm(env) == "+15"
    assert env.csv_rows()[0]["wake"] == "RTC"


def test_rtc_wake_but_docked(env):
    env.put("dmesg", "Wake-Up source: unknown\n")
    env.put("vbus.seq", "present\n")
    env.run()
    assert env.calls == []


def test_vbus_unreadable_is_docked(env):
    env.env["DEVMEM"] = "/nonexistent"
    env.run()
    assert env.calls == []


def test_custom_interval_and_gps_blink_off(env):
    env.conf("INTERVAL=60  # a minute\nGPS=0\nBLINK=0\nBOGUS=1\nINTERVAL_X=2\n")
    env.run()
    assert poweroff_alarm(env) == "+45"
    assert not env.called("gpsnap") and not env.called("magpie-led")
    assert "unknown key BOGUS" in env.daemon_log()


def test_bad_number_falls_back(env):
    env.conf('INTERVAL="abc"\n')
    env.run()
    assert poweroff_alarm(env) == "+15"
    assert "INTERVAL='abc' is not a number" in env.daemon_log()


def test_min_alarm(env):
    env.put("uptime", "29.9 1\n")
    env.run()
    assert poweroff_alarm(env) == "+8"
    assert env.csv_rows()[0]["alarm_s"] == "8"


def test_low_battery_powers_off_without_alarm(env):
    env.put("battery", "200\n")
    env.run()
    assert poweroff_alarm(env) == "0"
    assert "magpie-accel wake none" in env.calls
    assert "magpie-accel wake tap" not in env.calls
    assert not env.called("snap") and not env.called("gpsnap")
    assert env.csv_rows()[0]["end"] == "lowbat"


# ---- RTT backup wake ----

def rtt_alarm_writes(env):
    return [c.split()[-1] for c in env.calls if c.startswith("devmem write 0xfffffe24 ")]


def test_rtt_backup_alarm_follows_rtc_alarm(env):
    env.put("dmesg", "[ 0.0] foo\n")
    env.run()
    assert poweroff_alarm(env) == "+15"
    # prescaler 32 = 1024 counts/s; (15 + 5) s after count 0x100000; the status is read (cleared) after the write
    assert rtt_alarm_writes(env) == ["0x00105000"]


def test_rtt_backup_alarm_wraps_at_32_bits(env):
    env.put("rtt_vr", "0xFFFFF000\n")
    env.put("rtt_mr", "0x00000000\n")  # RTPRES 0 = 65536: 0.5 count/s, 20 s = 10 counts
    env.run()
    assert rtt_alarm_writes(env) == ["0xFFFFF00A"]
    env.put("rtt_vr", "0xFFFFFFFE\n")
    env.put("calls", "")
    env.put("rtt_mr", "0x00000020\n")
    env.run()
    assert rtt_alarm_writes(env)[-1] == "0x00004FFE"


def test_rtt_backup_off(env):
    env.env["RTT_BACKUP"] = "0"
    env.run()
    assert rtt_alarm_writes(env) == []


def test_rtt_unreadable_still_powers_off_with_rtc_alarm(env):
    env.put("rtt_vr", "garbage\n")
    env.run()
    assert poweroff_alarm(env) == "+15"
    assert rtt_alarm_writes(env) == []
    assert "RTT backup alarm not set" in env.daemon_log()


def test_low_battery_disarms_rtt(env):
    env.put("battery", "200\n")
    env.run()
    assert rtt_alarm_writes(env) == ["0xFFFFFFFF"]


def test_warning_level_continues(env):
    env.put("battery", "206\n")
    env.run()
    assert poweroff_alarm(env) == "+15"
    assert "WARNING battery 206" in env.daemon_log()
    assert env.called("snap -o ")


def test_battery_read_failure_continues(env):
    (env.s / "battery").unlink()
    env.run()
    assert poweroff_alarm(env) == "+15"
    assert "battery read failed" in env.daemon_log()


FACEDOWN_CONF = "FACEDOWN_AXIS=z\nFACEDOWN_SIGN=-1\n"


def test_facedown_on_by_default(env):
    # measured on the Clip: camera down = Z -1008 mg
    env.put("accel", "0 0 -16000 0 0 -1000\n")
    env.run()
    assert not env.called("snap")
    assert env.csv_rows()[0]["facedown"] == "1"


def test_facedown_can_be_disabled(env):
    env.conf("FACEDOWN_AXIS=\n")
    env.put("accel", "0 0 -16000 0 0 -1000\n")
    env.run()
    assert env.called("snap -o ")
    assert env.csv_rows()[0]["facedown"] == "0"


@pytest.mark.parametrize("count,alarm,new_count", [(0, "+105", "1"), (10, "+105", "11"), (11, "+225", "12")])
def test_facedown_streak(env, count, alarm, new_count):
    env.conf(FACEDOWN_CONF)
    env.put("accel", "0 0 -16000 0 0 -1000\n")
    env.state(fd=count, dark=5)
    env.run()
    assert not env.called("snap") and not env.called("gpsnap")
    assert poweroff_alarm(env) == alarm
    assert env.state_file() == {"fd_count": new_count, "dark_count": "0"}
    assert env.csv_rows()[0]["facedown"] == "1"


def test_upright_is_not_facedown_and_resets_streak(env):
    env.conf(FACEDOWN_CONF)
    env.put("accel", "-15000 0 0 -940 0 0\n")
    env.state(fd=7)
    env.run()
    assert env.called("snap -o ")
    assert env.state_file()["fd_count"] == "0"


def test_facedown_needs_other_axes_flat(env):
    env.conf(FACEDOWN_CONF)
    env.put("accel", "8000 0 -16000 500 0 -1000\n")
    env.run()
    assert env.called("snap -o ")


@pytest.mark.parametrize("count,alarm,new_count", [(0, "+15", "1"), (2, "+15", "3"), (3, "+105", "4"),
                                                   (10, "+105", "11"), (11, "+465", "12")])
def test_dark_streak(env, count, alarm, new_count):
    env.put("snap_size", "1000")
    env.state(dark=count)
    env.run()
    assert env.called("snap -o ")
    assert poweroff_alarm(env) == alarm
    assert env.state_file()["dark_count"] == new_count
    assert env.csv_rows()[0]["dark"] == "1"


def test_bright_photo_resets_dark_streak(env):
    env.state(dark=9)
    env.run()
    assert env.state_file()["dark_count"] == "0"
    assert env.csv_rows()[0]["dark"] == "0"


def test_photo_failure_still_powers_off_and_keeps_dark_streak(env):
    env.put("snap_fail", "")
    env.put("gps_fail", "")
    env.state(dark=4)
    env.run()
    assert poweroff_alarm(env) == "+15"
    row = env.csv_rows()[0]
    assert row["photo_ok"] == "0" and row["gps_ok"] == "0"
    assert env.state_file()["dark_count"] == "4"
    assert "photo failed" in env.daemon_log()


def test_missing_camera_and_spidev(env):
    (env.s / "video0").unlink()
    (env.s / "spidev").unlink()
    env.run()
    assert poweroff_alarm(env) == "+15"
    assert not env.called("snap") and not env.called("gpsnap")
    log = env.daemon_log()
    assert "no " in log and "no photo" in log and "no GPS snapshot" in log


def test_vbus_returns_before_poweroff(env):
    # three absent checks start the cycle, the last check inside the cycle sees VBUS
    env.put("vbus.seq", "absent\nabsent\nabsent\npresent\n")
    env.run()
    assert not env.called("poweroff")
    assert (env.s / "rtc" / "wakealarm").read_text().strip() == "0"
    assert env.csv_rows()[0]["end"] == "docked"
    assert "VBUS is back" in env.daemon_log()


def test_data_missing_still_powers_off_with_alarm(env):
    env.put("nodata", "")
    env.env["DATA_WAIT"] = "3"
    env.run()
    assert poweroff_alarm(env) == "+15"
    assert not env.called("snap") and not env.called("gpsnap")
    assert not (env.data / "lifelog").exists() and not (env.data / "photos").exists()


def test_lifelog_off_file(env):
    (env.data / "lifelog.off").write_text("")
    env.run()
    assert not env.called("poweroff") and not env.called("snap")
    assert env.daemon_log().count("lifelog.off exists") == 1


def test_alarm_cannot_be_set_stays_awake(env):
    (env.s / "rtc" / "wakealarm").unlink()
    (env.s / "rtc" / "wakealarm").mkdir()  # writes fail
    env.env["LIFELOG_MAX_LOOPS"] = "3"
    env.run()
    assert not env.called("poweroff")
    assert env.csv_rows()[0]["end"] == "noalarm"


def test_poweroff_failure_is_reported(env):
    env.env["POWEROFF_CMD"] = "false"
    r = env.run()
    assert r.returncode == 1


# ---- magpie-accel ----

def accel(env, *args):
    r = env.run(ACCEL, *args)
    regs = {int(p.name): p.read_text().strip() for p in (env.s / "regs").glob("*") if p.name.isdigit()}
    return r, regs


def test_accel_wake_tap_writes_and_verifies(env):
    r, regs = accel(env, "wake", "tap")
    assert r.returncode == 0, r.stderr
    assert regs[0x22] == "0x80" and regs[0x38] == "0x20" and regs[0x3A] == "0x98" and regs[0x3B] == "0x0c"
    assert regs[0x3C] == "0x28" and regs[0x3D] == "0x50" and regs[0x20] == "0x77"
    assert regs[0x21] == "0x84" and regs[0x23] == "0x28"
    # cleared first: tilt config (INT1_CFG 0x30) is off
    assert regs[0x30] == "0x00"


def test_accel_wake_none_disables_everything(env):
    r, regs = accel(env, "wake", "none")
    assert r.returncode == 0
    assert regs[0x22] == "0x00" and regs[0x38] == "0x00" and regs[0x30] == "0x00" and regs[0x20] == "0x00"


def test_accel_wake_tilt(env):
    r, regs = accel(env, "wake", "tilt")
    assert r.returncode == 0
    assert regs[0x22] == "0x40" and regs[0x30] == "0x20" and regs[0x32] == "0x20" and regs[0x33] == "0x05"


def test_accel_mismatch_is_logged(env):
    (env.s / "regs").mkdir()
    (env.s / "regs" / "ro-58").write_text("")  # 0x3A ignores writes
    r, _ = accel(env, "wake", "tap")
    assert r.returncode == 1
    assert "MISMATCH reg 0x3A: wrote 0x98" in env.daemon_log()


def test_accel_uses_config_block(env):
    env.conf("ACCEL_TAP_REGS=0x3A=0x40 0x20=0x47\n")
    r, regs = accel(env, "wake", "tap")
    assert r.returncode == 0
    assert regs[0x3A] == "0x40" and regs[0x20] == "0x47"


def test_accel_sample(env):
    (env.s / "regs").mkdir()
    for reg, val in {0x28: "0x40", 0x29: "0xc5", 0x2A: "0x10", 0x2B: "0x00", 0x2C: "0x00", 0x2D: "0x3e"}.items():
        (env.s / "regs" / str(reg)).write_text(val + "\n")
    env.env["ACCEL_SETTLE"] = "0"
    r = env.run(ACCEL, "sample")
    assert r.returncode == 0, r.stderr
    # x = 0xc540 = -15040 -> -940 mg; y = 16 -> 1 mg; z = 0x3e00 = 15872 -> 992 mg
    assert r.stdout.split() == ["-15040", "16", "15872", "-940", "1", "992"]


def test_accel_prep_unloads_driver(env):
    r = env.run(ACCEL, "prep")
    assert r.returncode == 0, r.stderr
    assert "rmmod st_accel_i2c" in env.calls and "modprobe i2c-dev" in env.calls


def test_accel_no_device(env):
    shutil.rmtree(env.s / "i2cdev" / "3-0018")
    r = env.run(ACCEL, "wake", "tap")
    assert r.returncode == 1


@pytest.mark.parametrize("raw,leds", [("210", "1"), ("221", "12"), ("226", "123"), ("240", "1234")])
def test_blink_shows_battery_level(env, raw, leds):
    env.put("battery", raw + "\n")
    env.run()
    on = "".join(c.split()[1] for c in env.calls if c.startswith("magpie-led") and c.endswith(" on"))
    assert on == leds


def test_blink_single_led_when_level_off(env):
    env.conf("BLINK_LEVEL=0\nBLINK_LED=3\n")
    env.run()
    on = [c for c in env.calls if c.startswith("magpie-led") and c.endswith(" on")]
    assert on == ["magpie-led 3 on"]


# ---- fast-resume sleep (SLEEP_MODE=resume) ----

RESUMED_RTC = "[ 5.0] at91-backup: resumed, SHDW_SR=0x00020000\n"


def resume_env(env, conf="", dmesg=RESUMED_RTC, vbus="absent\n", loops=5):
    env.env["SLEEP_MODE"] = "resume"
    env.env["STUB_DMESG_LOG"] = "1"
    env.env["LIFELOG_MAX_LOOPS"] = str(loops)
    env.put("dmesg", dmesg)
    env.put("vbus.seq", vbus)
    if conf:
        env.conf(conf)


def udc(env):
    return (env.s / "UDC").read_text().strip()


def resume_file(env):
    return dict(ln.split("=") for ln in (env.data / "lifelog" / "resume").read_text().split())


def test_defaults_are_resume():
    env = {k: v for k, v in os.environ.items() if k not in ("SLEEP_MODE", "COLD_EVERY", "RESUME_MAX_FAIL")}
    r = subprocess.run(["sh", "-c", f". {LIB}; echo $SLEEP_MODE $COLD_EVERY $RESUME_MAX_FAIL"], env=env,
                       capture_output=True, text=True)
    assert r.stdout.split() == ["resume", "70", "3"]


def test_bad_sleep_mode_falls_back(env):
    env.env["SLEEP_MODE"] = "nap"
    env.env["LIFELOG_MAX_LOOPS"] = "1"
    env.run()
    assert "SLEEP_MODE='nap' must be resume or poweroff" in env.daemon_log()


def test_poweroff_mode_never_sleeps(env):
    env.env["STUB_DMESG_LOG"] = "1"
    env.run()
    assert poweroff_alarm(env) == "+15"
    assert not (env.s / "pm_state").exists()
    assert udc(env) == "udc.0"
    assert env.csv_rows()[0]["end"] == "poweroff"


def test_resume_loops_between_cycles(env):
    resume_env(env)
    env.run()
    assert not env.called("poweroff")
    assert (env.s / "pm_state").read_text().strip() == "mem"
    rows = env.csv_rows()
    assert [r["end"] for r in rows] == ["sleep"] * 3
    assert [r["wake"] for r in rows] == ["none", "RTC", "RTC"]
    # first cycle: alarm as after a cold boot; later ones count from the resume, with no bootloader time
    assert [r["alarm_s"] for r in rows] == ["15", "30", "30"]
    assert [r["up_start_s"] for r in rows] == ["10", "0", "0"]
    assert len(env.called("snap -o ")) == 3
    # GPBR3 cleared before each sleep; the gadget was unbound when the system woke and is not rebound (not docked)
    assert env.calls.count("devmem write 0xfffffe6c 0") == 3
    after = [c for c in env.calls if c.startswith("dmesg ")]
    assert after[1:] == ["dmesg udc= gpbr3=0"] * 3  # [0] is the start-up wake-source lookup
    assert udc(env) == ""
    assert resume_file(env) == {"resume_fail": "0", "sleep_count": "3"}



def test_two_failed_photos_force_a_cold_boot(env):
    resume_env(env, loops=9)
    env.put("snap_fail", "")
    env.run()
    rows = env.csv_rows()
    assert [r["photo_ok"] for r in rows] == ["0", "0"]
    assert [r["end"] for r in rows] == ["sleep", "poweroff"]
    assert len(env.called("poweroff")) == 1
    assert "photo failed twice in a row: cold boot" in env.daemon_log()

def test_resume_wake_source_from_shdw_sr(env):
    resume_env(env, dmesg="at91-backup: resumed, SHDW_SR=0x00000001\n", loops=4)
    env.run()
    assert [r["wake"] for r in env.csv_rows()] == ["none", "unknown"]


def test_resume_wake_source_rtt(env):
    resume_env(env, dmesg="at91-backup: resumed, SHDW_SR=0x00010000\n", loops=4)
    env.run()
    assert [r["wake"] for r in env.csv_rows()] == ["none", "RTT"]


def test_every_cold_every_th_sleep_powers_off(env):
    resume_env(env, "COLD_EVERY=3\n", loops=9)
    env.run()
    assert [r["end"] for r in env.csv_rows()] == ["sleep", "sleep", "poweroff"]
    assert len(env.called("poweroff")) == 1
    assert env.calls.count("devmem write 0xfffffe6c 0") == 2
    assert resume_file(env)["sleep_count"] == "0"
    assert "cold boot" in env.daemon_log()


def test_cold_every_zero_never_powers_off(env):
    resume_env(env, "COLD_EVERY=0\n", loops=12)
    env.run()
    assert not env.called("poweroff")
    assert len(env.csv_rows()) == 10


def test_sleep_count_survives_cold_boot(env):
    resume_env(env, "COLD_EVERY=3\n", loops=3)
    env.state()
    (env.data / "lifelog" / "resume").write_text("resume_fail=0\nsleep_count=2\n")
    env.run()
    assert len(env.called("poweroff")) == 1
    assert env.calls.count("devmem write 0xfffffe6c 0") == 0


def test_echo_mem_failure_powers_off_and_rebinds(env):
    resume_env(env, loops=3)
    (env.s / "pm_state").mkdir()  # writing fails
    env.run()
    assert poweroff_alarm(env) == "+15"
    assert udc(env) == "udc.0"
    assert "echo mem" in env.daemon_log() and "failed" in env.daemon_log()


def test_vbus_after_resume_rebinds_and_docks(env):
    # 3 debounce checks, the check inside the cycle, then present right after the resume
    resume_env(env, vbus="absent\nabsent\nabsent\nabsent\npresent\n", loops=7)
    env.run()
    assert not env.called("poweroff")
    assert len(env.csv_rows()) == 1
    assert udc(env) == "udc.0"
    assert "VBUS present after resume" in env.daemon_log()
    assert len(env.called("snap -o ")) == 1  # docked: no more photos


def test_docked_during_awake_period_rebinds_gadget(env):
    # cycle 1 sleeps; the plug-in is noticed by the main loop later
    resume_env(env, vbus="absent\nabsent\nabsent\nabsent\nabsent\npresent\n", loops=7)
    env.run()
    assert udc(env) == "udc.0"
    assert len(env.called("snap -o ")) == 1


def test_gpbr3_marker_counts_failure(env):
    resume_env(env, loops=1)
    env.put("gpbr3", "0x4D505703\n")
    env.run()
    assert "fast resume failed (GPBR3=0x4D505703)" in env.daemon_log()
    assert env.calls.count("devmem write 0xfffffe6c 0") == 1
    assert resume_file(env)["resume_fail"] == "1"


def test_clear_gpbr3_is_no_failure(env):
    resume_env(env, loops=1)
    env.put("gpbr3", "0x00000000\n")
    env.run()
    assert "fast resume failed" not in env.daemon_log()
    assert not (env.data / "lifelog" / "resume").exists()


def test_failures_disable_resume_until_docked(env):
    resume_env(env, loops=3)
    env.state()
    (env.data / "lifelog" / "resume").write_text("resume_fail=2\nsleep_count=5\n")
    env.put("gpbr3", "0x4D505701\n")
    env.run()
    assert resume_file(env)["resume_fail"] == "3"
    assert poweroff_alarm(env) == "+15"
    assert not (env.s / "pm_state").exists()
    assert "fast resume disabled" in env.daemon_log()
    assert env.csv_rows()[0]["end"] == "poweroff"


def test_below_max_fail_still_resumes(env):
    resume_env(env, loops=3)
    env.state()
    (env.data / "lifelog" / "resume").write_text("resume_fail=1\nsleep_count=0\n")
    env.put("gpbr3", "0x4D505700\n")
    env.run()
    assert resume_file(env)["resume_fail"] == "2"
    assert (env.s / "pm_state").read_text().strip() == "mem"


def test_docking_resets_failure_count(env):
    resume_env(env, vbus="present\n", loops=2)
    env.state()
    (env.data / "lifelog" / "resume").write_text("resume_fail=3\nsleep_count=5\n")
    env.run()
    assert resume_file(env) == {"resume_fail": "0", "sleep_count": "5"}


def test_clean_resume_does_not_reset_failures(env):
    resume_env(env, loops=4)
    env.state()
    (env.data / "lifelog" / "resume").write_text("resume_fail=2\nsleep_count=0\n")
    env.run()
    assert resume_file(env)["resume_fail"] == "2"
    assert len(env.csv_rows()) == 2

