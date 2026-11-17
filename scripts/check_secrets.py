#!/usr/bin/env python3
"""掃描倉庫裡被 git 跟蹤的文件，找疑似密鑰的內容。CI 每次都跑；也可以在提交前自己跑：

    python scripts/check_secrets.py

發現任何一處就以非零狀態退出。它只能擋住常見樣式，不能代替「密鑰只放在密鑰文件裡」這條規矩。
誤報時，把那一行改寫得不像密鑰，或者在行尾加註釋 `# secret-scan: ok` 並在旁邊說明為什麼。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PATTERNS = {
    "私鑰文件": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "sk- 開頭的 API key": re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    "連接串裡的密碼": re.compile(r"://[^:/\s@\"\'{}$]+:[^@/\s\"\'{}$<>你]{6,}@"),
    "寫死的密鑰賦值": re.compile(
        r"(?i)\b[A-Z0-9_]*(api[_-]?key|secret|token|passwd|password)[A-Z0-9_]*\s*[:=]\s*[\"\']([A-Za-z0-9+/_\-]{20,})[\"\']"),
}
ALLOW_MARK = "secret-scan: ok"
SKIP_SUFFIXES = (".png", ".jpg", ".ico", ".xlsx", ".db", ".lock")


def tracked_files(root: Path) -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True).stdout
    return [root / name for name in out.decode().split("\0") if name]


def scan(paths: list[Path]) -> list[str]:
    findings = []
    for path in paths:
        if path.suffix in SKIP_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if ALLOW_MARK in line:
                continue
            for label, pattern in PATTERNS.items():
                if pattern.search(line):
                    findings.append(f"{path}:{number}: 疑似{label}")     # 不打印那一行的內容
    return findings


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    findings = scan(tracked_files(root))
    for finding in findings:
        print(finding)
    if findings:
        print(f"\n發現 {len(findings)} 處疑似密鑰。密鑰不能進倉庫：請移除，並把已經暴露的密鑰作廢重發。")
        return 1
    print("沒有發現疑似密鑰的內容。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
