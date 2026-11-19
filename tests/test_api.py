from datetime import UTC, datetime

import pytest


@pytest.fixture()
def loaded_client(client, admin, sample_path):
    with sample_path.open("rb") as fh:
        preview = admin.post("/api/imports/preview", files={"file": ("s.xlsx", fh)},
                              data={"source_code": "demo"}).json()
    done = admin.post(f"/api/imports/{preview['batch_id']}/commit", json={})
    assert done.status_code == 200 and done.json()["inserted"] > 1000
    return client


def test_pages_and_empty_state(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/js/app.js").status_code == 200
    o = client.get("/api/overview").json()
    assert o["observations"] == 0 and o["indicators_ok"] == 0 and o["series_total"] == 164
    assert o["parameters_missing"] == 6 and o["refinery_models"] == 5
    assert all(i["status"] != "ok" and i["last_value"] is None for i in client.get("/api/indicators").json())
    assert client.get("/api/refineries/CN_FUEL_SD").json()["breakdown"] is None


def test_data_flow(loaded_client, admin):
    client = loaded_client
    indicators = {i["code"]: i for i in client.get("/api/indicators").json()}
    assert indicators["SPR_BRENT_WTI"]["status"] == "ok"
    assert indicators["SPR_SC_BRENT"]["status"] == "ok"
    assert indicators["FRT_TD3C_BBL"]["status"] == "missing_parameter"
    assert indicators["SPR_PXN"]["status"] == "missing_data"
    assert "RM_SG_COMPLEX_REV_HSFO" not in indicators                       # 中間量預設不列出
    assert "RM_SG_COMPLEX_REV_HSFO" in {i["code"] for i in client.get("/api/indicators?include_hidden=true").json()}
    assert {i["module"] for i in client.get("/api/indicators?module=spread").json()} == {"spread"}
    assert len(client.get("/api/indicators/SPR_BRENT_WTI/data").json()) == 59
    assert client.get("/api/indicators/SPR_BRENT_WTI/trace").json()["tree"]["children"][0]["source_code"] == "demo"
    assert client.get("/api/indicators/SPR_PXN/trace").status_code == 400
    assert client.get("/api/indicators/NOPE/data").status_code == 404

    # 手動錄入優先於導入的數據；被改寫的值留在修訂歷史裡；as_of 能看回當時
    last = client.get("/api/series/BRENT_M1/data").json()[-1]
    before = datetime.now(UTC).isoformat()
    point = {"series_code": "BRENT_M1", "obs_date": last["date"], "value": 123.0}
    assert admin.post("/api/observations", json={"points": [point]}).json()["inserted"] == 1
    assert admin.post("/api/observations", json={"points": [{**point, "value": 124.0}]}).json()["updated"] == 1
    assert client.get("/api/series/BRENT_M1/data").json()[-1]["value"] == 124.0
    assert client.get("/api/series/BRENT_M1/revisions").json()[0]["old_value"] == 123.0
    assert client.get("/api/series/BRENT_M1/data", params={"as_of": before}).json()[-1]["value"] == last["value"]
    assert client.get("/api/series/NOPE/data").status_code == 404
    assert admin.post("/api/observations", json={"points": [{**point, "series_code": "NOPE"}]}).status_code == 400

    sources = {s["code"]: s for s in admin.get("/api/sources").json()}
    assert sources["kpler"]["ready"] is False and sources["eia"]["series_supported"] == 27
    assert admin.post("/api/sources/kpler/pull", json={}).status_code == 409
    assert admin.post("/api/sources/manual/pull", json={}).status_code == 404
    assert {a["action"] for a in admin.get("/api/audit").json()} >= {"import", "manual_entry"}
    assert admin.post("/api/admin/reload-config").status_code == 200


def test_parameters_api(loaded_client, admin):
    client = loaded_client
    params = {p["key"]: p for p in client.get("/api/parameters").json()}
    assert params["CN_VAT_RATE"]["value"] == 0.13 and params["CN_VAT_RATE"]["valid_from"] == "2019-04-01"
    assert params["FLAT_TD3C"]["value"] is None and params["GAL_PER_BBL"]["verified"] is True
    assert "FRT_TD3C_BBL" in params["FLAT_TD3C"]["used_by"]
    assert params["RM_CN_FUEL_SD_Y_GASOLINE"]["model"] == "CN_FUEL_SD"

    def crack():
        return {i["code"]: i for i in client.get("/api/indicators").json()}["CRK_ICE_GASOIL"]

    old = crack()
    assert "BBL_PER_T_GASOIL" in old["unverified_parameters"]
    r = admin.post("/api/parameters/BBL_PER_T_GASOIL/versions", json={"value": 7.6, "note": "測試"})
    assert r.status_code == 200 and r.json()["valid_from"] == "1900-01-01"
    new = crack()
    assert new["last_value"] != old["last_value"] and "BBL_PER_T_GASOIL" not in new["unverified_parameters"]
    versions = client.get("/api/parameters/BBL_PER_T_GASOIL/versions").json()
    assert [(v["value"], v["effective"]) for v in versions] == [(7.6, True), (7.45, False)]   # 舊值保留，標為已被取代

    admin.post("/api/parameters/FLAT_TD3C/versions", json={"value": 22, "valid_from": "2026-01-01"})
    assert {i["code"]: i for i in client.get("/api/indicators").json()}["FRT_TD3C_BBL"]["status"] == "missing_data"
    assert admin.post("/api/parameters/BBL_PER_T_GASOIL/verify", json={"verified": False}).status_code == 200
    assert admin.post("/api/parameters/NOPE/versions", json={"value": 1}).status_code == 400


def test_refinery_api(loaded_client):
    client = loaded_client
    models = {m["code"]: m for m in client.get("/api/refineries").json()}
    assert models["SG_COMPLEX"]["margin"]["status"] == "ok" and models["CN_FUEL_SD"]["margin"]["status"] == "ok"
    assert models["CN_INTEGRATED"]["margin"]["status"] == "missing_data"
    assert models["NWE_COMPLEX"]["has_tax"] is False and models["CN_FUEL_SD"]["has_tax"] is True

    detail = client.get("/api/refineries/CN_FUEL_SD").json()
    assert detail["breakdown"]["date"] == detail["margin"]["last_date"] and len(detail["history"]) > 40
    assert detail["breakdown"]["components"][-1]["value"] == pytest.approx(detail["margin"]["last_value"])
    assert "SC_M1" in detail["inputs"]["series"] and "CN_VAT_RATE" in detail["inputs"]["parameters"]

    r = client.post("/api/refineries/CN_FUEL_SD/scenario",
                    json={"parameters": {"RM_CN_FUEL_SD_C_OPEX": 400}, "shifts": {"SD_DIESEL_0": 100}})
    body = r.json()
    base, scenario = body["base"]["components"][-1]["value"], body["scenario"]["components"][-1]["value"]
    # 柴油 +100：收入 +37；增值稅多 37/1.13×0.13，附加再多 12%；加工費 +100
    extra_vat = 0.37 * 100 / 1.13 * 0.13
    assert scenario - base == pytest.approx(0.37 * 100 - extra_vat * 1.12 - 100)
    assert client.get("/api/refineries/CN_FUEL_SD").json()["margin"]["last_value"] == pytest.approx(base)   # 沒有保存
    assert client.post("/api/refineries/CN_FUEL_SD/scenario", json={"parameters": {"NOPE": 1}}).status_code == 400
    assert client.post("/api/refineries/CN_INTEGRATED/scenario", json={}).status_code == 400
    assert client.get("/api/refineries/NOPE").status_code == 404


def test_balance_scenario_and_analysis_api(loaded_client):
    client = loaded_client
    tables = {b["code"]: b for b in client.get("/api/balances").json()}
    assert set(tables) == {"GLOBAL", "US_CRUDE"} and tables["GLOBAL"]["balance"]["status"] == "ok"

    table = client.get("/api/balances/GLOBAL?past=6&future=6").json()
    assert len(table["periods"]) == 12 and [p["forecast"] for p in table["periods"]] == [False] * 6 + [True] * 6
    rows = {r["indicator"]: r for r in table["rows"]}
    supply, demand = rows["BAL_GLOBAL_SUPPLY"]["values"], rows["BAL_GLOBAL_DEMAND"]["values"]
    assert rows["BAL_GLOBAL_BALANCE"]["values"] == pytest.approx([s - d for s, d in zip(supply, demand)])
    assert rows["BAL_GLOBAL_S_OPEC"]["adjustable"] and not rows["BAL_GLOBAL_SUPPLY"]["adjustable"]
    weekly = client.get("/api/balances/US_CRUDE?past=4&future=4").json()
    assert len(weekly["periods"]) == 4 and weekly["rows"][-1]["kind"] == "residual"     # 周度數據沒有預測期

    # 建情境、加調整：平衡表在情境下變化，基準不變
    assert client.post("/api/scenarios", json={"code": "MY_VIEW", "name_zh": "我的判斷"}).status_code == 200
    assert client.post("/api/scenarios", json={"code": "MY_VIEW", "name_zh": "x"}).status_code == 400
    start = table["periods"][8]["date"]
    r = client.post("/api/scenarios/MY_VIEW/adjustments", json={
        "target": "BAL_GLOBAL_S_OPEC", "date_from": start, "delta": 0.5, "note": "OPEC+ 增產"})
    assert r.status_code == 200 and r.json()["unit"] == "mb/d" and r.json()["target_name"].endswith("OPEC")
    assert client.post("/api/scenarios/MY_VIEW/adjustments",
                       json={"target": "NOPE", "date_from": start, "delta": 1}).status_code == 400
    adjusted = client.get("/api/balances/GLOBAL?past=6&future=6&scenario=MY_VIEW").json()
    balance = next(r for r in adjusted["rows"] if r["kind"] == "balance")
    diff = [v - b for v, b in zip(balance["values"], balance["base_values"])]
    assert diff == pytest.approx([0] * 8 + [0.5] * 4)
    assert client.get("/api/balances/GLOBAL?past=6&future=6").json()["rows"] == table["rows"]
    assert client.get("/api/scenarios").json()[0]["adjustments"] == 1
    assert client.get("/api/indicators/BAL_GLOBAL_BALANCE/data?scenario=NOPE").status_code == 404

    adjustment_id = client.get("/api/scenarios/MY_VIEW/adjustments").json()[0]["id"]
    assert client.delete(f"/api/adjustments/{adjustment_id}").status_code == 200
    assert client.delete(f"/api/adjustments/{adjustment_id}").status_code == 404
    assert client.delete("/api/scenarios/MY_VIEW").status_code == 200
    assert client.get("/api/scenarios").json() == []

    seasonal = client.get("/api/seasonal/EIA_CRUDE_STOCKS").json()
    assert seasonal["unit"] == "kb" and 50 <= len(seasonal["points"]) <= 54
    last = seasonal["points"][-1]
    assert last["min"] <= last["avg"] <= last["max"]
    assert client.get("/api/seasonal/BRENT_M1").json()["points"][-1]["avg"] is None      # 歷史不夠五年 → 不給區間
    assert client.get("/api/seasonal/NOPE").status_code == 404

    relations = {r["code"]: r for r in client.get("/api/relations").json()}
    assert relations["BRENT_STRUCTURE_VS_US_STOCKS"]["status"] == "insufficient"         # 演示行情只有 60 天
    assert "points" not in relations["BRENT_STRUCTURE_VS_US_STOCKS"]
    detail = client.get("/api/relations/BRENT_STRUCTURE_VS_US_STOCKS").json()
    assert detail["n"] == len(detail["points"]) and detail["x"]["unit"] == "kb"
    assert client.get("/api/relations/NOPE").status_code == 404


def test_front_and_admin_are_separate_sites(sites, sample_path):
    front, admin = sites["front"], sites["admin"]
    assert front.get("/api/site").json()["site"] == "front" and admin.get("/api/site").json()["site"] == "admin"
    assert "後台" in admin.get("/").text and "後台" not in front.get("/").text.split("<body")[1].split("<script")[0]

    # 前台沒有任何管理數據和參數的接口
    with sample_path.open("rb") as fh:
        assert front.post("/api/imports/preview", files={"file": ("s.xlsx", fh)}).status_code == 404
    point = {"points": [{"series_code": "BRENT_M1", "obs_date": "2026-09-01", "value": 1}]}
    assert front.post("/api/observations", json=point).status_code == 404
    assert front.post("/api/parameters/CN_PORT_MISC/versions", json={"value": 1}).status_code == 405
    assert front.post("/api/parameters/CN_PORT_MISC/verify", json={}).status_code == 404
    for path in ("/api/sources", "/api/audit", "/api/imports"):
        assert front.get(path).status_code == 404
    assert front.post("/api/sources/eia/pull", json={}).status_code == 404
    assert front.post("/api/admin/reload-config").status_code == 404

    # 後台沒有研究用的接口
    for path in ("/api/scenarios", "/api/events", "/api/projections", "/api/reports", "/api/signals", "/api/refineries",
                 "/api/balances", "/api/chat/tools", "/api/llm"):
        assert admin.get(path).status_code == 404, path
    assert admin.post("/api/chat", json={"question": "x"}).status_code == 404

    # 兩邊共用同一份數據：後台錄入，前台立刻看得到
    assert admin.post("/api/observations", json=point).json()["inserted"] == 1
    assert front.get("/api/series/BRENT_M1/data").json()[0]["value"] == 1
    assert admin.post("/api/parameters/CN_PORT_MISC/versions", json={"value": 55}).status_code == 200
    assert {p["key"]: p for p in front.get("/api/parameters").json()}["CN_PORT_MISC"]["value"] == 55


def test_config_changes_are_picked_up_without_restart(tmp_path, monkeypatch):
    import shutil

    from fastapi.testclient import TestClient

    from crudelab.api import deps
    from crudelab.api.app import create_app
    from crudelab.settings import PROJECT_ROOT, Settings
    monkeypatch.setattr(deps, "CONFIG_CHECK_INTERVAL", 0)
    config = tmp_path / "config"
    shutil.copytree(PROJECT_ROOT / "config", config)
    settings = Settings(config_dir=config, data_dir=tmp_path / "data",
                        database_url=f"sqlite:///{tmp_path / 'data' / 't.db'}")
    front = TestClient(create_app(settings, "front"))
    n = len(front.get("/api/indicators").json())

    extra = config / "indicators" / "extra.yaml"
    extra.write_text('- {code: SPR_TEST, name_zh: 測試, module: spread, unit: USD/bbl, formula: "BRENT_M1 - WTI_M2"}\n',
                     encoding="utf-8")
    assert len(front.get("/api/indicators").json()) == n + 1               # 沒有重啟，也沒有人通知它
    extra.write_text('- {code: SPR_TEST, name_zh: 測試, module: spread, unit: USD/bbl, formula: "BRENT_M1 - NOPE"}\n',
                     encoding="utf-8")
    assert len(front.get("/api/indicators").json()) == n + 1               # 新配置有錯：沿用上一份正確的
    assert "NOPE" in front.get("/api/site").json()["config_error"]
    extra.unlink()
    assert len(front.get("/api/indicators").json()) == n and front.get("/api/site").json()["config_error"] is None
