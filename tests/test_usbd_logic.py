import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "br2-external" / "package" / "magpie-usbd" / "src"
TEST_C = ROOT / "tests" / "c" / "test_usbd_logic.c"

pytestmark = pytest.mark.skipif(shutil.which("gcc") is None, reason="host gcc not installed")


def test_logic_builds_and_passes(tmp_path):
    exe = tmp_path / "test_usbd_logic"
    subprocess.run(
        ["gcc", "-Wall", "-Wextra", "-Werror", "-O2", f"-I{SRC}", "-o", str(exe),
         str(TEST_C), str(SRC / "usbd_logic.c")],
        check=True, capture_output=True, text=True)
    res = subprocess.run([str(exe)], capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr


def test_tool_builds_with_werror(tmp_path):
    """magpie_usbd.c itself (needs the Linux UAPI headers, present on any Linux host)."""
    exe = tmp_path / "magpie-usbd"
    subprocess.run(
        ["gcc", "-Wall", "-Wextra", "-Werror", "-O2", "-pthread", "-o", str(exe),
         str(SRC / "magpie_usbd.c"), str(SRC / "usbd_logic.c")],
        check=True, capture_output=True, text=True)
    res = subprocess.run([str(exe)], capture_output=True, text=True)
    assert res.returncode == 1  # usage error: no FunctionFS directory
    assert "usage" in res.stderr
    assert subprocess.run([str(exe), "-x"], capture_output=True).returncode == 1
    assert subprocess.run([str(exe), "-h"], capture_output=True).returncode == 0
