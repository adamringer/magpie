import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "br2-external" / "package" / "gpsnap" / "src"
TEST_C = ROOT / "tests" / "c" / "test_gpsnap_logic.c"

pytestmark = pytest.mark.skipif(shutil.which("gcc") is None, reason="host gcc not installed")


def test_logic_builds_and_passes(tmp_path):
    exe = tmp_path / "test_gpsnap_logic"
    subprocess.run(
        ["gcc", "-Wall", "-Wextra", "-Werror", "-O2", f"-I{SRC}", "-o", str(exe),
         str(TEST_C), str(SRC / "gpsnap_logic.c")],
        check=True, capture_output=True, text=True)
    res = subprocess.run([str(exe)], capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr


def test_tool_builds_with_werror(tmp_path):
    """gpsnap.c itself (needs the Linux UAPI headers, present on any Linux host)."""
    exe = tmp_path / "gpsnap"
    subprocess.run(
        ["gcc", "-Wall", "-Wextra", "-Werror", "-O2", "-o", str(exe),
         str(SRC / "gpsnap.c"), str(SRC / "gpsnap_logic.c")],
        check=True, capture_output=True, text=True)
    res = subprocess.run([str(exe), "-s", "5"], capture_output=True, text=True)
    assert res.returncode == 1  # usage error: -s out of range
    assert subprocess.run([str(exe), "-h"], capture_output=True).returncode == 0
