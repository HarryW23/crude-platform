import pytest
from fastapi.testclient import TestClient

from crudelab.api.app import create_app
from crudelab.sample import write_sample_workbook


@pytest.fixture()
def client(settings):
    return TestClient(create_app(settings))


def test_full_flow_through_api(client, tmp_path):
    assert client.get("/").status_code == 200
    before = client.get("/api/overview").json()
    assert before["observations"] == 0 and before["indicators_ok"] == 0 and before["series_total"] == 76

    path = tmp_path / "s.xlsx"
    write_sample_workbook(path)
    with path.open("rb") as fh:
        preview = client.post("/api/imports/preview", files={"file": ("s.xlsx", fh)},
                              data={"source_code": "demo"}).json()
    done = client.post(f"/api/imports/{preview['batch_id']}/commit", json={"mapping": {}, "learn": True})
    assert done.status_code == 200 and done.json()["inserted"] > 800

    indicators = {i["code"]: i for i in client.get("/api/indicators").json()}
    assert indicators["SPR_BRENT_WTI"]["status"] == "ok"
    assert indicators["SPR_SC_BRENT"]["status"] == "missing_data"
    assert len(client.get("/api/indicators/SPR_BRENT_WTI/data").json()) == 59
    assert client.get("/api/indicators/SPR_BRENT_WTI/trace").json()["tree"]["children"][0]["source_code"] == "demo"
    assert client.get("/api/indicators/SPR_SC_BRENT/trace").status_code == 400

    # 手動錄入優先於導入的數據
    last = client.get("/api/series/BRENT_M1/data").json()[-1]
    r = client.post("/api/observations", json={"points": [
        {"series_code": "BRENT_M1", "obs_date": last["date"], "value": 123.0}]})
    assert r.json()["inserted"] == 1
    assert client.get("/api/series/BRENT_M1/data").json()[-1] == {
        "date": last["date"], "value": 123.0, "source": "manual", "ref": "後台手動錄入"}

    # 改參數 → 指標跟著變，並留下歷史
    old = indicators["CRK_ICE_GASOIL"]["last_value"]
    assert client.put("/api/parameters/BBL_PER_T_GASOIL", json={"value": 7.6, "note": "測試"}).status_code == 200
    new = {i["code"]: i for i in client.get("/api/indicators").json()}["CRK_ICE_GASOIL"]["last_value"]
    assert new != old
    history = client.get("/api/parameters/BBL_PER_T_GASOIL/history").json()
    assert history[0]["old_value"] == 7.45 and history[0]["new_value"] == 7.6 and history[0]["note"] == "測試"

    sources = {s["code"]: s for s in client.get("/api/sources").json()}
    assert sources["kpler"]["ready"] is False and sources["eia"]["series_supported"] == 6
    assert client.post("/api/sources/kpler/pull", json={}).status_code == 409
    assert client.post("/api/sources/manual/pull", json={}).status_code == 404
    assert {a["action"] for a in client.get("/api/audit").json()} >= {"import", "manual_entry"}
    assert client.post("/api/admin/reload-config").status_code == 200
    assert client.get("/api/series/NOPE/data").status_code == 404
