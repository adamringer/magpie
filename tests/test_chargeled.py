"""Host dry run of magpie-chargeledd with the stubs in tests/lifelog_stubs (see test_lifelog.py)."""
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
OVERLAY = ROOT / "br2-external" / "board" / "magpie" / "rootfs-overlay"
DAEMON = OVERLAY / "usr" / "bin" / "magpie-chargeledd"
INIT = OVERLAY / "etc" / "init.d" / "S51chargeled"
STUBS = pathlib.Path(__file__).resolve().parent / "lifelog_stubs"

SHELLS = [s for s in (["sh"], ["dash"], ["busybox", "sh"]) if shutil.which(s[0])]


class Env:
    def __init__(self, tmp, shell):
        self.shell = shell
        self.s = tmp / "state"
        self.s.mkdir()
        for n, t in (("vbus.seq", "present\n"), ("battery", "240\n"), ("charge", "charging\n"), ("calls", "")):
            (self.s / n).write_text(t)
        self.env = dict(
            os.environ, STUB_STATE=str(self.s), DEVMEM=str(STUBS / "devmem"),
            BATTERY_CMD=str(STUBS / "magpie-battery"), CHARGE_CMD=str(STUBS / "magpie-charge"),
            LED_CMD=str(STUBS / "magpie-led"), SLEEP_CMD=str(STUBS / "tick"), CHARGELED_MAX_LOOPS="12",
        )

    def put(self, name, text):
        (self.s / name).write_text(text)

    def run(self, vbus="present\n", **kw):
        self.put("vbus.seq", vbus)
        self.env.update({k: str(v) for k, v in kw.items()})
        subprocess.run([*self.shell, str(DAEMON)], env=self.env, capture_output=True, text=True, timeout=60)
        return [c.split(" ", 1)[1] if c.startswith("magpie-led ") else c
                for c in (self.s / "calls").read_text().splitlines()]

    def reads(self):
        f = self.s / "charge_reads"
        return len(f.read_text().splitlines()) if f.exists() else 0


@pytest.fixture(params=SHELLS, ids=[" ".join(s) for s in SHELLS])
def env(request, tmp_path):
    return Env(tmp_path, request.param)


def replay(calls):
    """Apply led calls; return the lit LED set at the end of every loop iteration (tick)."""
    lit, out = set(), []
    for c in calls:
        if c == "tick":
            out.append(frozenset(lit))
            continue
        n, v = c.split()
        for i in ([1, 2, 3, 4] if n == "all" else [int(n)]):
            (lit.add if v == "on" else lit.discard)(i)
    return out


def leds(calls):
    return [c for c in calls if c != "tick"]


def test_scripts_parse():
    for f in (DAEMON, INIT):
        for sh in SHELLS:
            r = subprocess.run([*sh, "-n", str(f)], capture_output=True, text=True)
            assert r.returncode == 0, (sh, f, r.stderr)


def test_docked_full_all_on(env):
    env.put("charge", "not charging\n")
    calls = leds(env.run())
    assert calls[:4] == ["1 on", "2 on", "3 on", "4 on"]
    # alive heartbeat: the top LED is off for one second in four, nothing else changes
    assert set(calls[4:]) <= {"4 off", "4 on"} and "4 off" in calls[4:]
    assert calls[4:6] == ["4 off", "4 on"]


@pytest.mark.parametrize("raw,level", [(210, 1), (221, 2), (226, 3), (240, 4)])
def test_levels_and_blink(env, raw, level):
    env.put("battery", f"{raw}\n")
    states = replay(env.run())
    solid = frozenset(range(1, level))
    top = frozenset(range(1, level + 1))
    assert set(states) == {solid, top}
    # strictly alternating, 1 s each
    assert all(a != b for a, b in zip(states, states[1:]))


def test_hysteresis(env):
    # stock: 221 -> 2; 219 stays 2; 216 -> 1
    wrapper = env.s / "bat.sh"
    wrapper.write_text('#!/bin/sh\nf=$STUB_STATE/bseq\nv=$(head -n 1 $f)\n[ $(wc -l < $f) -gt 1 ] && sed -i 1d $f\necho "raw: $v"\n')
    wrapper.chmod(0o755)
    env.put("bseq", "221\n219\n216\n")
    states = replay(env.run(BATTERY_CMD=wrapper, CHARGELED_MAX_LOOPS="6", BATT_EVERY="2"))
    # ticks 0-1 read 221 -> level 2; ticks 2-3 read 219 -> still 2; ticks 4-5 read 216 -> level 1
    assert set(states[0:4]) == {frozenset({1}), frozenset({1, 2})}
    assert set(states[4:6]) <= {frozenset({1}), frozenset()}
    assert all(2 not in s for s in states[4:])


def test_unplugged_all_off_once_no_writes(env):
    assert leds(env.run(vbus="absent\n")) == []  # starts unplugged: nothing written


def test_unplug_transition(env):
    calls = leds(env.run(vbus="present\npresent\nabsent\n", CHARGELED_MAX_LOOPS="10"))
    assert calls[-1] == "all off"
    assert calls.count("all off") == 1  # and nothing is written after it


def test_plug_after_unplug(env):
    calls = leds(env.run(vbus="absent\nabsent\npresent\n", CHARGELED_MAX_LOOPS="6"))
    assert calls and "all off" not in calls
    assert "1 on" in calls


def test_plug_unplug_plug(env):
    env.put("charge", "not charging\n")
    calls = leds(env.run(vbus="present\nabsent\nabsent\npresent\n", CHARGELED_MAX_LOOPS="8"))
    # replugged on tick 3, a heartbeat tick: the top LED starts off for that second; tick 7 is the next one
    assert calls == ["1 on", "2 on", "3 on", "4 on", "all off", "1 on", "2 on", "3 on", "4 off", "4 on", "4 off"]


def test_poll_rates(env):
    env.env["CHARGELED_MAX_LOOPS"] = "25"
    env.run()
    assert env.reads() <= 3


def test_helper_failure_keeps_state(env):
    (env.s / "charge").unlink()  # magpie-charge fails: unknown, nothing written
    assert leds(env.run()) == []
    env.put("charge", "charging\n")
    (env.s / "battery").unlink()  # battery fails: nothing written
    assert leds(env.run()) == []
