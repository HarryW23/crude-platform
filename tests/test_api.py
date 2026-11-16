from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from crudelab.api.app import create_app


@pytest.fixture()
def client(settings):
    return TestClient(create_app(settings))


@pytest.fixture()
def loaded_client(client, sample_path):
    with sample_path.open("rb") as fh:
        preview = client.post("/api/imports/preview", files={"file": ("s.xlsx", fh)},
                              data={"source_code": "demo"}).json()
    done = client.post(f"/api/imports/{preview['batch_id']}/commit", json={})
    assert done.status_code == 200 and done.json()["inserted"] > 1000
    return client


def test_pages_and_empty_state(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/js/app.js").status_code == 200
    o = client.get("/api/overview").json()
    assert o["observations"] == 0 and o["indicators_ok"] == 0 and o["series_total"] == 84
    assert o["parameters_missing"] == 5 and o["refinery_models"] == 5
    assert all(i["status"] != "ok" and i["last_value"] is None for i in client.get("/api/indicators").json())
    assert client.get("/api/refineries/CN_FUEL_SD").json()["breakdown"] is None


def test_data_flow(loaded_client):
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
    assert client.post("/api/observations", json={"points": [point]}).json()["inserted"] == 1
    assert client.post("/api/observations", json={"points": [{**point, "value": 124.0}]}).json()["updated"] == 1
    assert client.get("/api/series/BRENT_M1/data").json()[-1]["value"] == 124.0
    assert client.get("/api/series/BRENT_M1/revisions").json()[0]["old_value"] == 123.0
    assert client.get("/api/series/BRENT_M1/data", params={"as_of": before}).json()[-1]["value"] == last["value"]
    assert client.get("/api/series/NOPE/data").status_code == 404
    assert client.post("/api/observations", json={"points": [{**point, "series_code": "NOPE"}]}).status_code == 400

    sources = {s["code"]: s for s in client.get("/api/sources").json()}
    assert sources["kpler"]["ready"] is False and sources["eia"]["series_supported"] == 6
    assert client.post("/api/sources/kpler/pull", json={}).status_code == 409
    assert client.post("/api/sources/manual/pull", json={}).status_code == 404
    assert {a["action"] for a in client.get("/api/audit").json()} >= {"import", "manual_entry"}
    assert client.post("/api/admin/reload-config").status_code == 200


def test_parameters_api(loaded_client):
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
    r = client.post("/api/parameters/BBL_PER_T_GASOIL/versions", json={"value": 7.6, "note": "測試"})
    assert r.status_code == 200 and r.json()["valid_from"] == "1900-01-01"
    new = crack()
    assert new["last_value"] != old["last_value"] and "BBL_PER_T_GASOIL" not in new["unverified_parameters"]
    versions = client.get("/api/parameters/BBL_PER_T_GASOIL/versions").json()
    assert [(v["value"], v["effective"]) for v in versions] == [(7.6, True), (7.45, False)]   # 舊值保留，標為已被取代

    client.post("/api/parameters/FLAT_TD3C/versions", json={"value": 22, "valid_from": "2026-01-01"})
    assert {i["code"]: i for i in client.get("/api/indicators").json()}["FRT_TD3C_BBL"]["status"] == "missing_data"
    assert client.post("/api/parameters/BBL_PER_T_GASOIL/verify", json={"verified": False}).status_code == 200
    assert client.post("/api/parameters/NOPE/versions", json={"value": 1}).status_code == 400


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
