"""CFTC 持倉接口：請求怎麼發、結果怎麼解析、出錯怎麼說；取回的數據能算出擁擠度和信號。"""
from datetime import date

import httpx
import pandas as pd
import pytest
from sqlalchemy import select

from crudelab import signals
from crudelab.connectors import ConnectorError
from crudelab.connectors.cftc import CFTCConnector
from crudelab.indicators import IndicatorEngine
from crudelab.ingest import write_observations
from crudelab.models import Series


def fake(handler):
    requests = []

    def wrapped(request: httpx.Request):
        requests.append(request)
        return handler(request)
    return httpx.Client(transport=httpx.MockTransport(wrapped)), requests


def rows(n=170):
    out = []
    for i, d in enumerate(pd.date_range(end="2026-11-10", periods=n, freq="7D")):
        out.append({"report_date_as_yyyy_mm_dd": f"{d.date()}T00:00:00.000",
                    "m_money_positions_long_all": str(300_000 + 1000 * (i % 30)),
                    "m_money_positions_short_all": str(80_000 + 500 * (i % 7)),
                    "open_interest_all": "1800000"})
    out.append({"report_date_as_yyyy_mm_dd": "bad", "open_interest_all": "1"})          # 壞行跳過
    return out


def test_one_request_per_market_and_parsing(session, cfg):
    client, requests = fake(lambda r: httpx.Response(200, json=rows()))
    series = list(session.scalars(select(Series)))
    obs = CFTCConnector(client).fetch(series, date(2023, 1, 1), None)
    assert len(requests) == 1                                               # 三個字段合成一次請求
    q = requests[0].url.params
    assert requests[0].url.path == "/resource/72hh-3qpy.json"
    assert "cftc_contract_market_code='067651'" in q["$where"] and "2023-01-01" in q["$where"]
    assert set(q["$select"].split(",")) == {"report_date_as_yyyy_mm_dd", "m_money_positions_long_all",
                                            "m_money_positions_short_all", "open_interest_all"}
    codes = {o.series_code for o in obs}
    assert codes == {"CFTC_WTI_MM_LONG", "CFTC_WTI_MM_SHORT", "CFTC_WTI_OI"} and len(obs) == 170 * 3
    assert all(o.obs_date.weekday() == 1 for o in obs)                      # 周二

    write_observations(session, obs, "cftc")
    session.commit()
    engine = IndicatorEngine(session, cfg)
    net_oi = engine.compute("POS_WTI_MM_NET_OI")
    assert net_oi.iloc[-1] == pytest.approx((300_000 + 1000 * (169 % 30) - 80_000 - 500 * (169 % 7)) / 1_800_000)
    result = signals.data_signal(engine, "SIG_WTI_POSITIONING", pd.Timestamp("2026-11-13"))
    assert result["status"] == "ok" and result["direction"] == -1


@pytest.mark.parametrize(("status", "message"), [(400, "字段名不對"), (503, "503")])
def test_errors_are_readable(session, status, message):
    client, _ = fake(lambda r: httpx.Response(status, text="upstream detail"))
    with pytest.raises(ConnectorError, match=message) as err:
        CFTCConnector(client).fetch(list(session.scalars(select(Series))), None, None)
    assert "upstream detail" not in str(err.value)


def test_status_needs_no_key():
    assert CFTCConnector().status().ready
