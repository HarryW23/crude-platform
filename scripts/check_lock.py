"""核對 constraints.txt：

1. pyproject.toml 裡的每個直接依賴都鎖了版本
2. 在每個要支持的平台 × Python 版本上，鎖定的版本都有現成的安裝包（wheel），用戶安裝時不需要編譯器
   （只下載包的元數據文件做檢查，需要聯網，CI 的 lock 任務運行）

用法：python scripts/check_lock.py            兩項都查
      python scripts/check_lock.py --offline  只查第 1 項
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
# (平台標籤, sys_platform, platform_machine, os_name)
PLATFORMS = [("macosx_11_0_arm64", "darwin", "arm64", "posix"),
             ("macosx_10_13_x86_64", "darwin", "x86_64", "posix"),
             ("win_amd64", "win32", "AMD64", "nt"),
             ("manylinux_2_34_x86_64", "linux", "x86_64", "posix")]   # glibc 2.34+：Ubuntu 22.04、RHEL 9 起
PYTHONS = ["3.11", "3.12", "3.13"]
# 已知沒有現成安裝包、會在安裝時編譯的組合：寫明原因。新出現的一律算錯
KNOWN_SOURCE_BUILDS = {
    ("greenlet", "macosx_10_13_x86_64"): "greenlet 不再發佈 Intel Mac 的安裝包；在 Intel Mac 上安裝時會自動編譯"
                                         "（需要 Xcode 命令行工具：xcode-select --install）",
    ("httptools", "macosx_10_13_x86_64"): "uvicorn 的加速組件，0.9.0 起沒有 Intel Mac 的安裝包；同上，安裝時自動編譯",
}


def _compatible(tag: str) -> list[str]:
    """pip 的 --platform 不會自動包含更舊但兼容的 Linux 標籤（manylinux_2_34 的機器也能裝 manylinux_2_17 的包）：
    手動列出。"""
    match = re.match(r"manylinux_2_(\d+)_(\w+)", tag)
    if not match:
        return [tag]
    newest, arch = int(match.group(1)), match.group(2)
    return [f"manylinux_2_{v}_{arch}" for v in range(newest, 4, -1)] + \
        [f"manylinux2014_{arch}", f"manylinux2010_{arch}", f"manylinux1_{arch}"]


def pins() -> list[Requirement]:
    lines = [ln.strip() for ln in (ROOT / "constraints.txt").read_text(encoding="utf-8").splitlines()]
    return [Requirement(ln) for ln in lines if ln and not ln.startswith("#")]


def direct_dependencies() -> set[str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    names = [*project["dependencies"], *(d for extra in project["optional-dependencies"].values() for d in extra)]
    return {canonicalize_name(Requirement(re.sub(r"\s*#.*$", "", n)).name) for n in names}


def check_offline() -> list[str]:
    pinned = {canonicalize_name(r.name) for r in pins()}
    return [f"{name} 是直接依賴，但 constraints.txt 裡沒有鎖定版本" for name in sorted(direct_dependencies() - pinned)]


def check_wheels() -> list[str]:
    problems = []
    for tag, sys_platform, machine, os_name in PLATFORMS:
        for python in PYTHONS:
            env = {"sys_platform": sys_platform, "platform_machine": machine, "os_name": os_name,
                   "python_version": python, "python_full_version": f"{python}.0",
                   "platform_python_implementation": "CPython", "implementation_name": "cpython",
                   "platform_system": {"darwin": "Darwin", "win32": "Windows", "linux": "Linux"}[sys_platform]}
            wanted = [r for r in pins() if r.marker is None or Marker(str(r.marker)).evaluate(env)]
            wanted = [r for r in wanted if (canonicalize_name(r.name), tag) not in KNOWN_SOURCE_BUILDS]
            with tempfile.TemporaryDirectory() as tmp:
                req = Path(tmp) / "req.txt"
                req.write_text("\n".join(f"{r.name}{r.specifier}" for r in wanted), encoding="utf-8")
                done = subprocess.run([sys.executable, "-m", "pip", "download", "--quiet", "--no-deps",
                                       "--disable-pip-version-check", "--only-binary=:all:",
                                       *(arg for t in _compatible(tag) for arg in ("--platform", t)),
                                       "--python-version", python, "-d", str(Path(tmp) / "w"), "-r", str(req)],
                                      capture_output=True, text=True)
            status = "ok" if done.returncode == 0 else "缺安裝包"
            print(f"  {tag:<24} Python {python}：{status}", flush=True)
            if done.returncode != 0:
                missing = re.findall(r"satisfies the requirement ([^\s]+)", done.stderr)
                problems.append(f"{tag} / Python {python}：沒有現成安裝包：{', '.join(missing) or done.stderr[-300:]}")
    return problems


def main() -> int:
    problems = check_offline()
    if "--offline" not in sys.argv:
        print("檢查每個平台都有現成的安裝包：")
        problems += check_wheels()
    for (name, tag), why in KNOWN_SOURCE_BUILDS.items():
        print(f"已知例外：{name} @ {tag}：{why}")
    if problems:
        print("\n".join(["constraints.txt 有問題：", *problems]), file=sys.stderr)
        return 1
    print("constraints.txt 沒有問題。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
