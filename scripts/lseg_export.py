"""在裝了 LSEG Workspace（路透 / Eikon）的電腦上運行：檢測能不能取數，並把行情導出成 crudelab 能直接導入的 CSV。

只需要這一個文件和同目錄的 lseg_rics.txt（find 還要 lseg_candidates.txt），不需要裝 crudelab。前提：
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
    python lseg_export.py find                  給還不知道 RIC 的序列找 RIC：按 lseg_candidates.txt 搜索並試取，
                                                結果寫進 lseg_find_日期.txt，發給負責平台的人寫進配置
    python lseg_export.py search "搜索詞"       只列出搜索到的 RIC 和標題（不取數），寫進 lseg_search_日期.txt；
                                                find 找不到時用它看路透上這一類到底有哪些

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
import re
import socket
import sys
import threading
import time
import warnings
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
PREFERRED_FIELDS = ("SETTLE", "TRDPRC_1", "CLOSE", "MID_PRICE", "BID")   # 期貨優先用結算價
PAUSE_SECONDS = 0.3
PROBE_RIC = "LCOc1"
DEFAULT_PORTS = (9060, 9000)                  # Workspace 的數據通道（9000 是沿用 Eikon 的老端口）
PORT_FILES = ("Refinitiv/Data API Proxy", "LSEG/Data API Proxy", "Thomson Reuters/Eikon API Proxy")
RESTART_HINT = ("Workspace 的數據通道沒有開。把 Workspace 從右下角托盤完全退出（不是只關窗口），"
                "等進程都結束後重新打開並登錄，等首頁加載完再試。\n"
                "  檢查方法（PowerShell）：netstat -ano | findstr LISTENING | findstr \":90\"\n"
                "  應該看到 127.0.0.1:9000 或 :9060\n"
                "  重開後還是沒有：這個帳號或這台機器可能沒開 API 權限，請找 IT / LSEG 確認")
FIND_TRIES = 12                               # find：每條序列最多試這麼多個 RIC（--tries 可改）
CHAIN_TAKE = 3                                # find：鏈（0#…）展開後試前幾個成員
CHAIN_SEARCH_MAX = 2                          # find：搜索結果裡的鏈最多展開幾個（候選裡寫的鏈都展開）
CHAIN_TIMEOUT = 20.0                          # 展開一個鏈最多等幾秒：庫在鏈不存在時可能卡住不返回
CHAIN_GIVE_UP = 3                             # 連續這麼多個鏈都展不開（例如帳號沒有推送權限），這次就不再試鏈
RETRY_WAIT = 3.0                              # 臨時錯誤（網絡、超時）等幾秒重試一次；沒權限、RIC 不存在不重試
# 期貨連續合約每天對應哪個合約：路透的歷史數據裡本來就有這兩個字段，導出時順便存下來，
# 平台用它核對換月規則（crudelab verify-rolls），不多發請求
CONTRACT_FIELDS = ("EXPIR_DATE", "CRT_MNTH")
# 路透歷史數據接口的錯誤代碼在括號裡，例如 (TS.Interday.UserNotPermission.92000, …)：要看它才分得清原因
ERROR_CODE = re.compile(r"\b(TS\.\w+\.\w+(?:\.\d+)?)")
ERROR_KINDS = (("NotPermission", "沒權限（帳號沒訂閱這個數據，常見於 Platts / Argus）"),
               ("Permission", "沒權限（帳號沒訂閱這個數據，常見於 Platts / Argus）"),
               ("InvalidRIC", "RIC 不存在"), ("NotFound", "RIC 不存在"), ("UserRequestError", "RIC 或請求無效"))
FIND_DAYS = 120                               # find 試取多長時間：月度數據（例如產量調查）30 天內可能只有一個點

# LSEG 的庫在新版 pandas 下會刷 FutureWarning，跟取到的數據無關，不顯示
warnings.filterwarnings("ignore", category=FutureWarning, module=r"(lseg|refinitiv)(\.|$)")


class Fail(Exception):
    """給人看的錯誤訊息（已脫敏）。"""


# ------------------------------ 讀 RIC 清單 ------------------------------

def strip_comment(line: str) -> str:
    """# 開頭、或空格後的 # 是註釋；RIC 裡的 #（例如鏈 0#LCO:）不算。"""
    return re.split(r"(?:^|\s)#", line, maxsplit=1)[0].strip()


def read_rics(path: Path) -> list[tuple[str, str, str | None]]:
    """每行：序列代碼  RIC  [字段]。# 開頭是註釋。"""
    if not path.exists():
        raise Fail(f"找不到 {path.name}：它應該和這個腳本放在同一個文件夾")
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = strip_comment(line)
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


def recorded_ports() -> list[tuple[Path, int]]:
    """Workspace 啟動數據通道時把端口寫進 .portInUse。文件可能是很久以前留下的，所以只當線索，以實際在聽為準。"""
    base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    out = []
    for folder in PORT_FILES:
        path = base / folder / ".portInUse"
        try:
            out.append((path, int(path.read_text(encoding="utf-8").strip())))
        except (OSError, ValueError):
            continue
    return out


def proxy_port() -> int | None:
    """正在聽的數據通道端口；沒有就是 None。"""
    candidates = [port for _, port in recorded_ports()] + list(DEFAULT_PORTS)
    for port in dict.fromkeys(candidates):
        if port_open(port):
            return port
    return None


def require_proxy() -> int:
    port = proxy_port()
    if port is None:
        raise Fail(RESTART_HINT)
    return port


_default_excepthook = threading.excepthook


def _quiet_library_threads(args) -> None:
    """LSEG 庫的推送線程（Msg-Proc-Thread…）裡的錯誤已經由我們自己處理（例如鏈不存在），不打印 traceback。"""
    name = getattr(args.thread, "name", "") or ""
    if name.startswith(("Msg-Proc-Thread", "crudelab-chain-")):
        return
    _default_excepthook(args)


threading.excepthook = _quiet_library_threads


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
                session = module.open_session(app_key=key)
            except Exception as exc:
                raise Fail(f"{name} 打開會話失敗：{_redact(exc, key)}") from None
            # 庫在連不上時不拋錯，只在日誌裡說一句，返回一個沒打開的會話：這裡要自己檢查
            state = getattr(session, "open_state", None)
            state_name = getattr(state, "name", str(state)).split(".")[-1].lower()
            if state is not None and state_name not in ("opened", "open"):
                try:
                    module.close_session()
                except Exception:
                    pass
                raise Fail(f"會話沒有打開（狀態：{getattr(state, 'name', state)}）。" + RESTART_HINT)
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

    def search(self, query: str, top: int) -> list[tuple[str, str]]:
        """用 LSEG 的搜索找 RIC，回傳 [(RIC, 標題)]。舊版 eikon 庫沒有搜索，回傳空。"""
        discovery = getattr(self.lib, "discovery", None)
        if discovery is None or not hasattr(discovery, "search"):
            return []
        try:
            frame = discovery.search(query=query, top=top, select="RIC,DocumentTitle")
        except Exception as exc:
            raise Fail(f"搜索「{query}」失敗：{_redact(exc, self.key)}") from None
        out = []
        if frame is None or not len(frame) or "RIC" not in frame.columns:
            return out
        for _, row in frame.iterrows():
            ric = str(row.get("RIC") or "").strip()
            if ric and ric.lower() != "nan":
                out.append((ric, str(row.get("DocumentTitle") or "").strip()))
        return out

    def chain(self, ric: str) -> list[str]:
        """展開鏈（0#…）成員 RIC。舊版 eikon 庫、展開失敗或超時都回傳空。
        庫是在後台線程裡用推送方式展開的：鏈不存在時錯誤在那個線程裡拋出（會打印一大段 traceback），
        主線程可能一直等。所以放到單獨的線程裡、限時等待，並且不打印庫線程裡的錯誤。"""
        discovery = getattr(self.lib, "discovery", None)
        maker = getattr(discovery, "Chain", None) if discovery is not None else None
        if maker is None:
            return []
        result: dict[str, list] = {}

        def work() -> None:
            try:
                result["members"] = list(maker(ric).constituents or [])
            except Exception:
                result["members"] = []

        worker = threading.Thread(target=work, name=f"crudelab-chain-{ric}", daemon=True)
        worker.start()
        worker.join(CHAIN_TIMEOUT)
        members = result.get("members", [])
        return [str(m) for m in members if m and not str(m).startswith("0#")]

    def frame(self, ric: str, start: date, end: date, interval: str = "daily"):
        """取一個 RIC 的歷史（預設日度），回傳 pandas DataFrame（索引是日期，列是各字段）。"""
        try:
            if self.kind == "eikon":
                return self.lib.get_timeseries(ric, start_date=start.isoformat(), end_date=end.isoformat(),
                                               interval=interval)
            return self.lib.get_history(universe=ric, interval=interval, start=start.isoformat(),
                                        end=end.isoformat())
        except Exception as exc:
            raise Fail(f"{ric}：{_redact(exc, self.key)}") from None


WRONG_INTERVAL = "70406"       # 周度、月度的統計數據按日度取時，路透回這個錯誤代碼


def fetch(client: Client, ric: str, start: date, end: date):
    """取歷史數據；臨時錯誤重試一次。沒權限、RIC 不存在重試也沒用，直接報。
    周度 / 月度的統計數據（例如隆眾的周度開工率）按日度取會報 70406：改按周、按月再取。"""
    try:
        return client.frame(ric, start, end)
    except Fail as exc:
        if WRONG_INTERVAL in str(exc):
            for interval in ("weekly", "monthly"):
                try:
                    return client.frame(ric, start, end, interval)
                except Fail as again:
                    if WRONG_INTERVAL not in str(again):
                        raise
            raise
        reason = explain_error(str(exc).split("：", 1)[-1])
        if reason.startswith(("沒權限", "RIC")) or "沒有數據" in reason:
            raise
    time.sleep(RETRY_WAIT)
    return client.frame(ric, start, end)


def contract_info(frame) -> dict[date, tuple[str, str]]:
    """每天的（到期日, 合約月），兩個字段都沒有時回傳空。"""
    columns = [str(c[-1] if isinstance(c, tuple) else c) for c in frame.columns]
    present = [f for f in CONTRACT_FIELDS if f in columns]
    if not present:
        return {}
    out = {}
    for ts, row in zip(frame.index, frame.itertuples(index=False), strict=True):
        values = []
        for f in CONTRACT_FIELDS:
            v = row[columns.index(f)] if f in present else None
            text = "" if v is None or str(v) in ("nan", "NaT", "<NA>", "None") else str(v).strip()
            values.append(text[:10] if f == "EXPIR_DATE" else text)
        if any(values):
            out[ts.date() if hasattr(ts, "date") else ts] = (values[0], values[1])
    return out


def columns_of(frame) -> list[str]:
    return [str(c[-1] if isinstance(c, tuple) else c) for c in frame.columns]


def pick_field(frame, wanted: str | None) -> str | None:
    columns = columns_of(frame)
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
    port = proxy_port()
    print(f"數據通道：{f'端口 {port} 在聽（Workspace）' if port else '沒有在聽'}")
    for path, recorded in recorded_ports():
        if recorded != port:
            print(f"  （{path} 記著端口 {recorded}，但它現在沒在聽：是之前留下的舊記錄）")
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
    if port is None:
        raise Fail(RESTART_HINT)
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
    require_proxy()
    client = Client(app_key())
    end, start = date.today(), date.today() - timedelta(days=days)
    table: dict[str, dict[date, float]] = {}
    contracts: list[list[str]] = []
    problems = []
    try:
        for i, (code, ric, wanted) in enumerate(rics, 1):
            try:
                frame = fetch(client, ric, start, end)
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
            contracts += [[d.isoformat(), code, ric, expiry, month]
                          for d, (expiry, month) in sorted(contract_info(frame).items())]
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
    if contracts:
        side = out.with_name(out.stem + "_contracts.csv")
        with side.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["日期", "序列代碼", "RIC", "到期日", "合約月"])
            writer.writerows(contracts)
        print(f"另外寫出 {side}：期貨連續合約每天對應的合約（{len(contracts)} 行），用來核對換月規則。\n"
              f"  這個文件不要放進 inbox、不用導入：交給負責平台的人，用 crudelab verify-rolls 核對")
    if problems:
        print(f"有 {len(problems)} 條沒取到（RIC 不對、沒有權限，或那段時間沒有數據）：\n  " + "\n  ".join(problems))
    print("把這個文件放進 crudelab 的 data/inbox/，或在後台「數據導入」頁上傳（數據源選「路透（Excel 整理）」）")
    return 0


def explain_error(text: str) -> str:
    """把庫的錯誤訊息歸類成人話，附上原始錯誤代碼。"""
    code = ERROR_CODE.search(text)
    if code:
        kind = next((zh for key, zh in ERROR_KINDS if key.lower() in code.group(1).lower()), "取數失敗")
        return f"{kind} [{code.group(1)}]"
    low = text.lower()
    if "no data to return" in low or "empty" in low:
        return "這段時間沒有數據"
    return " ".join(text.split())[:160]


def read_candidates(path: Path, only: set[str] | None) -> list[tuple[str, str, list[str]]]:
    """每行：序列代碼 | 搜索詞（幾個用 ; 分開，可以不寫）| 候選 RIC（空格分隔，可以不寫；RIC@字段 指定字段）。
    # 開頭是註釋。"""
    if not path.exists():
        raise Fail(f"找不到 {path.name}：它應該和這個腳本放在同一個文件夾")
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = strip_comment(line)
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 3 or not parts[0] or not (parts[1] or parts[2]):
            raise Fail(f"{path.name} 第 {n} 行格式不對（應為：序列代碼 | 搜索詞 | 候選 RIC）：{line}")
        if only is None or parts[0] in only:
            out.append((parts[0], parts[1], parts[2].split()))
    return out


SEARCH_TOP = 100


def search(queries: list[str], out: Path, top: int = SEARCH_TOP) -> int:
    """只列出搜索結果（RIC 和標題），不取數。找不到 RIC 時用來看路透上這一類到底有哪些，
    例如「ICE Europe Crude Oil Brent Futures Commitments of Traders」能列出 ICE 持倉報告的所有分類。"""
    require_proxy()
    client = Client(app_key())
    rows: list[list[str]] = []
    try:
        for q in queries:
            hits = client.search(q, top)
            print(f"\n「{q}」：{len(hits)} 個結果")
            for ric, title in hits:
                print(f"  {ric:<20} {title}")
                rows.append([q, ric, title])
            time.sleep(PAUSE_SECONDS)
    finally:
        client.close()
    with out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow(["搜索詞", "RIC", "標題"])
        writer.writerows(rows)
    print(f"\n結果已寫進 {out.name}（只有 RIC 和標題）：把這個文件發給負責平台的人")
    return 0 if rows else 1


def find(cands_path: Path, out: Path, only: set[str] | None, tries_per_series: int = FIND_TRIES) -> int:
    """給還不知道 RIC 的序列找 RIC：先試候選，再試搜索到的前幾個，每個取最近一個月看有沒有數、量級對不對。
    結果只含 RIC、標題、最後一天的值，不含歷史數據。"""
    items = read_candidates(cands_path, only)
    if not items:
        raise Fail("沒有要找的序列（--only 的代碼不在清單裡？）")
    require_proxy()
    client = Client(app_key())
    end = date.today()
    rows: list[list[str]] = []
    found = 0
    chain_fails, chains_off = 0, False
    try:
        for i, (code, query, guesses) in enumerate(items, 1):
            print(f"\n[{i}/{len(items)}] {code}  搜索：{query or '（不搜索）'}")
            tries: dict[str, str] = {ric: "候選" for ric in guesses}
            for q in [q.strip() for q in query.split(";") if q.strip()]:     # 可以寫幾個搜索詞，用 ; 分開
                try:
                    for ric, title in client.search(q, tries_per_series):
                        tries.setdefault(ric, title or "搜索結果")
                except Fail as exc:
                    print(f"  {exc}")
                finally:
                    time.sleep(PAUSE_SECONDS)
            listed = bool(tries)                                             # 有沒有任何候選或搜索結果（含鏈）
            searched_chains = 0
            for ric in [r for r in tries if r.startswith("0#")]:            # 鏈要展開成合約才取得到數
                title = tries.pop(ric)
                if ric not in guesses:
                    searched_chains += 1
                    if searched_chains > CHAIN_SEARCH_MAX:
                        continue
                if chains_off:
                    rows.append([code, ric, "none", "", "", "", "0", title, "沒試：這台機器上鏈都展不開"])
                    continue
                members = client.chain(ric)[:CHAIN_TAKE]
                chain_fails = 0 if members else chain_fails + 1
                if chain_fails >= CHAIN_GIVE_UP:
                    chains_off = True
                    print(f"  （連續 {CHAIN_GIVE_UP} 個鏈都展不開，可能是帳號沒有推送數據的權限；"
                          f"這次不再試鏈，省得每個等 {CHAIN_TIMEOUT:.0f} 秒）")
                if not members:
                    rows.append([code, ric, "none", "", "", "", "0", title, "鏈展開不了（不存在、沒權限或超時）"])
                    print(f"  ✗ {ric:<18} 鏈展開不了  {title}")
                for member in members:
                    tries.setdefault(member, f"鏈 {ric} 的成員")
            ok = 0
            for token, title in list(tries.items())[:tries_per_series]:
                ric, _, wanted = token.partition("@")             # 候選可以寫 RIC@字段（國債要取收益率不取價格）
                error = ""
                try:
                    frame = fetch(client, ric, end - timedelta(days=FIND_DAYS), end)
                    field = pick_field(frame, wanted or None) if frame is not None and len(frame) else None
                    if frame is not None and len(frame) and wanted and field is None:
                        error = f"沒有字段 {wanted}（有：{'、'.join(columns_of(frame))[:120]}）"
                    values = sorted(series_of(frame, field).items()) if field else []
                    if not values and not error:
                        error = "這段時間沒有數據"
                except Fail as exc:
                    field, values, error = None, [], explain_error(str(exc).split("：", 1)[-1])
                finally:
                    time.sleep(PAUSE_SECONDS)
                if values:
                    ok += 1
                    last_day, last_value = values[-1]
                    rows.append([code, ric, "ok", field or "", last_day.isoformat(), f"{last_value:g}",
                                 str(len(values)), title, ""])
                    print(f"  ✓ {ric:<18} {field:<9} {last_day} {last_value:<12g} {len(values):>2} 天  {title}")
                else:
                    rows.append([code, ric, "none", "", "", "", "0", title, error])
                    print(f"  ✗ {ric:<18} {error}  {title}")
            if not listed:
                rows.append([code, "", "none", "", "", "", "0", "", "沒有候選，搜索也沒結果"])
                print("  沒有候選，搜索也沒結果")
            found += ok > 0
    finally:
        client.close()
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow(["序列代碼", "RIC", "結果", "字段", "最後日期", "最後值", f"近 {FIND_DAYS} 天點數",
                         "標題", "錯誤"])
        writer.writerows(rows)
    print(f"\n{len(items)} 條序列裡，{found} 條找到了至少一個有數據的 RIC。")
    print(f"結果已寫進 {out}（只有 RIC、標題和最後一天的值）：把這個文件發給負責平台的人，由他確認後寫進配置")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="從 LSEG Workspace 導出行情給 crudelab")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("probe", help="檢測能不能取數")
    p = sub.add_parser("export", help="導出 lseg_rics.txt 裡的序列")
    p.add_argument("--days", type=int, default=1100, help="往回取多少天（預設 1100，約三年）")
    p.add_argument("--out", default=f"reuters_{date.today():%Y%m%d}.csv", help="輸出文件名")
    p.add_argument("--rics", default=str(HERE / "lseg_rics.txt"), help="RIC 清單文件")
    f = sub.add_parser("find", help="給還不知道 RIC 的序列找 RIC（按 lseg_candidates.txt）")
    f.add_argument("--only", help="只找這些序列，逗號分隔，例如 OMAN_M1,MURBAN_M1")
    f.add_argument("--out", default=f"lseg_find_{date.today():%Y%m%d}.txt", help="結果文件名")
    f.add_argument("--candidates", default=str(HERE / "lseg_candidates.txt"), help="候選清單文件")
    f.add_argument("--tries", type=int, default=FIND_TRIES, help=f"每條序列最多試幾個 RIC（預設 {FIND_TRIES}）")
    s = sub.add_parser("search", help="只列出搜索結果（RIC 和標題），不取數")
    s.add_argument("query", nargs="+", help="搜索詞，可以給幾個（有空格的用引號括起來）")
    s.add_argument("--top", type=int, default=SEARCH_TOP, help=f"每個搜索詞最多列多少個（預設 {SEARCH_TOP}）")
    s.add_argument("--out", default=f"lseg_search_{date.today():%Y%m%d}.txt", help="結果文件名")
    args = parser.parse_args(argv)
    try:
        if args.command == "probe":
            return probe()
        if args.command == "search":
            return search(args.query, Path(args.out), max(1, args.top))
        if args.command == "find":
            only = {c.strip() for c in args.only.split(",") if c.strip()} if args.only else None
            return find(Path(args.candidates), Path(args.out), only, max(1, args.tries))
        return export(args.days, Path(args.out), Path(args.rics))
    except Fail as exc:
        print(f"✗ {exc}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
