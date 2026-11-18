"""在裝了 LSEG Workspace（路透 / Eikon）的電腦上運行：檢測能不能取數，並把行情導出成 crudelab 能直接導入的 CSV。

只需要這一個文件和同目錄的 lseg_rics.txt，不需要裝 crudelab。前提：
  1. 這台電腦上的 Workspace（或 Eikon）已經打開並登錄
  2. 裝了 Python 3.9+，並安裝 LSEG 的 Python 庫（三選一，有哪個用哪個）：
         pip install lseg-data            （新版，推薦）
         pip install refinitiv-data       （舊名）
         pip install eikon                （最舊的 Eikon Data API）
  3. 一個 App Key：在 Workspace 裡搜索「APPKEY」打開 App Key Generator，建一個（類型選 EDP API 或 Eikon Data API）

用法：
    python lseg_export.py probe                 檢測：Workspace 有沒有在運行、裝了哪個庫、能不能取到 LCOc1 最近幾天
    python lseg_export.py export                導出 lseg_rics.txt 裡的全部序列（預設最近 1100 天），寫成 CSV
    python lseg_export.py export --days 10      只導最近 10 天（每天更新用）

App Key 的給法（不要寫進任何文件）：運行時會提示輸入（不顯示）；要定時運行時，設環境變量 LSEG_APP_KEY。
腳本不會打印、不會保存 App Key；錯誤訊息裡如果出現了它，會被換成 ***。

導出的 CSV：第一列是日期，之後每列一條序列，表頭就是 crudelab 的序列代碼，所以導入時會自動識別。
把文件放進 crudelab 的 data/inbox/（十分鐘內自動導入），或在後台「數據導入」頁上傳，數據源選「路透（Excel 整理）」。

請求量很小（每條序列一次請求，之間停 0.3 秒），不會對公司的帳號造成壓力。
"""
from __future__ import annotations

import argparse
import csv
import getpass
import os
import socket
import sys
import time
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
PREFERRED_FIELDS = ("SETTLE", "TRDPRC_1", "CLOSE", "MID_PRICE", "BID")   # 期貨優先用結算價
PAUSE_SECONDS = 0.3
PROBE_RIC = "LCOc1"


class Fail(Exception):
    """給人看的錯誤訊息（已脫敏）。"""


# ------------------------------ 讀 RIC 清單 ------------------------------

def read_rics(path: Path) -> list[tuple[str, str, str | None]]:
    """每行：序列代碼  RIC  [字段]。# 開頭是註釋。"""
    if not path.exists():
        raise Fail(f"找不到 {path.name}：它應該和這個腳本放在同一個文件夾")
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) not in (2, 3):
            raise Fail(f"{path.name} 第 {n} 行格式不對（應為：序列代碼 RIC [字段]）：{line}")
        out.append((parts[0], parts[1], parts[2] if len(parts) == 3 else None))
    return out


# ------------------------------ 連接 ------------------------------

def _redact(text: object, key: str | None) -> str:
    out = str(text)
    return out.replace(key, "***") if key else out


def app_key() -> str:
    key = os.environ.get("LSEG_APP_KEY", "").strip()
    if not key:
        key = getpass.getpass("App Key（輸入不顯示）：").strip()
    if not key:
        raise Fail("沒有 App Key：在 Workspace 裡搜索 APPKEY 生成一個")
    return key


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


class Client:
    """把三種庫的取歷史數據接口統一成 history(ric, field, start, end) -> {date: value}。"""

    def __init__(self, key: str):
        self.key, self.kind, self.lib = key, None, None
        for name in ("lseg.data", "refinitiv.data"):
            try:
                module = __import__(name, fromlist=["open_session"])
            except ImportError:
                continue
            try:
                module.open_session(app_key=key)
            except Exception as exc:
                raise Fail(f"{name} 打開會話失敗：{_redact(exc, key)}") from None
            self.kind, self.lib = name, module
            return
        try:
            import eikon
        except ImportError:
            raise Fail("沒有裝 LSEG 的 Python 庫：運行 pip install lseg-data（或 refinitiv-data / eikon）") from None
        try:
            eikon.set_app_key(key)
        except Exception as exc:
            raise Fail(f"eikon 設置 App Key 失敗：{_redact(exc, key)}") from None
        self.kind, self.lib = "eikon", eikon

    def close(self) -> None:
        if self.kind in ("lseg.data", "refinitiv.data"):
            try:
                self.lib.close_session()
            except Exception:
                pass

    def frame(self, ric: str, start: date, end: date):
        """取一個 RIC 的日度歷史，回傳 pandas DataFrame（索引是日期，列是各字段）。"""
        try:
            if self.kind == "eikon":
                return self.lib.get_timeseries(ric, start_date=start.isoformat(), end_date=end.isoformat(),
                                               interval="daily")
            return self.lib.get_history(universe=ric, interval="daily", start=start.isoformat(),
                                        end=end.isoformat())
        except Exception as exc:
            raise Fail(f"{ric}：{_redact(exc, self.key)}") from None


def pick_field(frame, wanted: str | None) -> str | None:
    columns = [str(c[-1] if isinstance(c, tuple) else c) for c in frame.columns]
    if wanted:
        return wanted if wanted in columns else None
    for name in PREFERRED_FIELDS:
        if name in columns and frame.iloc[:, columns.index(name)].notna().any():
            return name
    return None


def series_of(frame, field: str) -> dict[date, float]:
    columns = [str(c[-1] if isinstance(c, tuple) else c) for c in frame.columns]
    col = frame.iloc[:, columns.index(field)]
    out = {}
    for ts, value in col.items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number == number:                       # 去掉 NaN
            out[ts.date() if hasattr(ts, "date") else ts] = number
    return out


# ------------------------------ 兩個命令 ------------------------------

def probe() -> int:
    print(f"Python {sys.version.split()[0]}")
    ports = {9060: "Workspace", 9000: "Eikon"}
    running = [name for port, name in ports.items() if port_open(port)]
    found = "、".join(running) + " 在運行" if running else "沒有偵測到（Workspace / Eikon 要先打開並登錄）"
    print(f"本機接口：{found}")
    libs = []
    for name in ("lseg.data", "refinitiv.data", "eikon"):
        try:
            module = __import__(name, fromlist=["__version__"])
            libs.append(f"{name} {getattr(module, '__version__', '')}".strip())
        except ImportError:
            pass
    print(f"Python 庫：{'、'.join(libs) or '都沒有裝（pip install lseg-data）'}")
    if not libs:
        return 1
    client = Client(app_key())
    try:
        print(f"會話：已用 {client.kind} 打開")
        end = date.today()
        frame = client.frame(PROBE_RIC, end - timedelta(days=14), end)
        columns = [str(c[-1] if isinstance(c, tuple) else c) for c in frame.columns]
        print(f"{PROBE_RIC} 最近兩周：{len(frame)} 行，字段：{'、'.join(columns)}")
        field = pick_field(frame, None)
        if field:
            last = sorted(series_of(frame, field).items())[-3:]
            print(f"會用字段 {field}，最後幾天：" + "，".join(f"{d} {v}" for d, v in last))
            print("\n✓ 可以取數。下一步：python lseg_export.py export")
        else:
            print("\n取到了數據，但沒有認識的價格字段。請把上面的輸出發給負責平台的人")
    finally:
        client.close()
    return 0


def export(days: int, out: Path, rics_path: Path) -> int:
    rics = read_rics(rics_path)
    client = Client(app_key())
    end, start = date.today(), date.today() - timedelta(days=days)
    table: dict[str, dict[date, float]] = {}
    problems = []
    try:
        for i, (code, ric, wanted) in enumerate(rics, 1):
            try:
                frame = client.frame(ric, start, end)
            except Fail as exc:
                problems.append(str(exc))
                continue
            finally:
                time.sleep(PAUSE_SECONDS)
            field = pick_field(frame, wanted) if frame is not None and len(frame) else None
            if field is None:
                problems.append(f"{ric}（{code}）：沒有取到數據" + (f"或沒有字段 {wanted}" if wanted else ""))
                continue
            table[code] = series_of(frame, field)
            print(f"[{i}/{len(rics)}] {code:<18} {ric:<10} {field:<9} {len(table[code])} 天")
    finally:
        client.close()
    if not table:
        print("一條都沒取到：\n  " + "\n  ".join(problems))
        return 1
    dates = sorted({d for values in table.values() for d in values})
    codes = list(table)
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["日期", *codes])
        for d in dates:
            writer.writerow([d.isoformat(), *[table[c].get(d, "") for c in codes]])
    print(f"\n已寫出 {out}：{len(codes)} 條序列，{len(dates)} 個日期")
    if problems:
        print(f"有 {len(problems)} 條沒取到（RIC 不對、沒有權限，或那段時間沒有數據）：\n  " + "\n  ".join(problems))
    print("把這個文件放進 crudelab 的 data/inbox/，或在後台「數據導入」頁上傳（數據源選「路透（Excel 整理）」）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="從 LSEG Workspace 導出行情給 crudelab")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("probe", help="檢測能不能取數")
    p = sub.add_parser("export", help="導出 lseg_rics.txt 裡的序列")
    p.add_argument("--days", type=int, default=1100, help="往回取多少天（預設 1100，約三年）")
    p.add_argument("--out", default=f"reuters_{date.today():%Y%m%d}.csv", help="輸出文件名")
    p.add_argument("--rics", default=str(HERE / "lseg_rics.txt"), help="RIC 清單文件")
    args = parser.parse_args(argv)
    try:
        if args.command == "probe":
            return probe()
        return export(args.days, Path(args.out), Path(args.rics))
    except Fail as exc:
        print(f"✗ {exc}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
