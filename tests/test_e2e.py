import base64, io, os, tempfile, zipfile
from datetime import timedelta

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["DISABLE_SCHEDULER"] = "1"
os.environ["APP_PASSWORD"] = "segredo123"

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from app import auth, db, logbook  # noqa: E402
from app.main import app  # noqa: E402
from app.providers import Point  # noqa: E402


def sig():
    im = Image.new("RGBA", (300, 120), (255, 255, 255, 0))
    ImageDraw.Draw(im).line([(10, 100), (80, 20), (150, 100), (250, 30)], fill=(0, 0, 0, 255), width=4)
    b = io.BytesIO(); im.save(b, "PNG")
    return "data:image/png;base64," + base64.b64encode(b.getvalue()).decode()


def test_full_flow():
    with TestClient(app) as c:
        # backoffice: admin de emergência
        r = c.post("/login", data={"email": "admin", "password": "segredo123"}, follow_redirects=False)
        assert r.status_code == 303, r.text
        cid = auth.create_company("Empresa Teste")
        db.run("INSERT INTO devices(company_id,provider,device_id,name,plate) VALUES(?,?,?,?,?)", cid, "gpswox", "9", "Van 1", "AA11BB")
        u = db.one("SELECT id FROM users WHERE email='admin'") or db.one("SELECT id FROM users LIMIT 1")
        auth.add_member(cid, u["id"], "gestor")
        c.post("/x/company/switch", json={"id": cid})

        d = c.post("/x/drivers", json={"name": "João Silva"}).json()
        did = d["id"]
        assert c.post(f"/x/drivers/{did}/assign", json={"provider": "gpswox", "device_id": "9"}).status_code == 200
        link = c.get("/x/drivers").json()[0]["link"]
        token = link.rsplit("/", 1)[1]

        day = (logbook.today_local() - timedelta(days=1)).isoformat()
        s, _ = logbook.day_bounds(day)

        def fake(company, provider, device, a, b):
            out, t, lat = [], s + timedelta(hours=8), 41.0
            while t < s + timedelta(hours=9):
                out.append(Point(t, lat, -8.0, 80.0)); lat += 0.01; t += timedelta(seconds=60)
            return out
        logbook.fetch_points = fake

        m = TestClient(app)  # telemóvel do motorista
        assert m.get("/d/api/me").status_code == 401
        assert m.get(f"/d/{token}", follow_redirects=True).status_code == 200
        assert m.post("/d/api/state", json={"state": "working"}).json()["state"] == "working"
        v = m.get(f"/d/api/day/{day}").json()
        assert v["summary"]["driving"] == 60 and v["status"] == "none" and len(v["trips"]) == 1
        v = m.post(f"/d/api/day/{day}", json={"edits": [{"from": "12:00", "to": "13:00", "state": "working"}]}).json()
        assert v["status"] == "filling"
        # não pode assinar sem confirmar a viatura
        assert m.post(f"/d/api/day/{day}/sign", json={"signature": sig()}).status_code == 400
        m.post(f"/d/api/day/{day}", json={"vehicle": {"odo_start": "100", "odo_end": "180", "confirmed": True}})
        assert m.post(f"/d/api/day/{day}/sign", json={"signature": "x"}).status_code == 400
        v = m.post(f"/d/api/day/{day}/sign", json={"signature": sig()}).json()
        assert v["status"] == "signed"
        assert m.post(f"/d/api/day/{day}", json={"step": 1}).status_code == 400
        assert m.get(f"/d/pdf/{day}").content[:4] == b"%PDF"
        # hoje não se assina
        assert m.post(f"/d/api/day/{logbook.today_local().isoformat()}/sign", json={"signature": sig()}).status_code == 400

        # backoffice: lista e ZIP em lote
        rows = c.get(f"/x/logbooks?driver={did}&from={day}&to={day}").json()
        assert len(rows) == 1 and rows[0]["driving"] == 60
        z = c.get(f"/relatorios/zip?driver={did}&from={day}&to={day}")
        assert z.status_code == 200 and len(zipfile.ZipFile(io.BytesIO(z.content)).namelist()) == 1
        assert c.get(rows[0]["pdf"]).content[:4] == b"%PDF"
        assert c.delete(f"/x/drivers/{did}").status_code == 400  # tem relatórios assinados
        for p in ("/", "/motoristas", "/objetos", "/relatorios", "/empresas", "/definicoes", "/utilizadores"):
            assert c.get(p).status_code == 200, p
        n = c.get("/x/companies").json()
        assert [x for x in n if x["id"] == cid][0]["drv"] == 1


def test_monitor_and_odometer():
    from app import monitor
    from app.providers import Point
    with TestClient(app):
        cid = auth.create_company("Empresa Monitor")
        did = db.run("INSERT INTO drivers(company_id,name,token,created_at) VALUES(?,?,?,?)", cid, "Rui", "tokmon", "2026-01-01T00:00:00+00:00")
        today = logbook.today_local()
        d3 = (today - timedelta(days=3)).isoformat()
        s3, _ = logbook.day_bounds(d3)
        db.run("INSERT INTO assignments(driver_id,provider,device_id,device_name,plate,from_ts) VALUES(?,?,?,?,?,?)",
               did, "gpswox", "5", "Van", "AA11BB", (s3 - timedelta(days=5)).isoformat())

        def fake(company, provider, device, a, b):  # 10 h de condução num dia
            return [Point(s3 + timedelta(hours=6, minutes=i), 41 + i * 0.001, -8.0, 70.0) for i in range(600)]
        logbook.fetch_points = fake
        monitor.run(cid, days=3)
        kinds = {a["kind"] for a in db.q("SELECT kind FROM alerts WHERE company_id=?", cid)}
        assert "conducao_excessiva" in kinds and "por_assinar" in kinds
        # dica de odómetro vem do último relatório assinado
        db.run("INSERT INTO logbooks(company_id,driver_id,day,status,data,created_at) VALUES(?,?,?,?,?,?)", cid, did,
               (today - timedelta(days=9)).isoformat(), "signed", '{"vehicle": {"odo_end": "50000"}}', "x")
        co, dr = db.one("SELECT * FROM companies WHERE id=?", cid), db.one("SELECT * FROM drivers WHERE id=?", did)
        v = logbook.compute_day(co, dr, d3)["vehicle"]
        assert v["odo_start_hint"] == "50000" and v["km_gps"] > 0


def test_find_odometer_and_snapshots():
    from app.providers import find_odometer
    assert find_odometer({"id": 1, "total_distance": 12345.6}) == (12345.6, "total_distance")
    # sensor do tipo odómetro tem prioridade sobre a distância total
    k = find_odometer({"total_distance": 10, "sensors": [{"type": "odometer", "val": "98 765 km"}]})
    assert k[0] == 98765.0 and "sensor" in k[1]
    assert find_odometer({"vehicle_params": {"mileage": "150000"}}) == (150000.0, "vehicle_params.mileage")
    assert find_odometer({"id": 1, "name": "x"}) == (None, None)

    with TestClient(app):
        cid = auth.create_company("Empresa Odo")
        did = db.run("INSERT INTO drivers(company_id,name,token,created_at) VALUES(?,?,?,?)", cid, "Ana", "tokodo", "2026-01-01T00:00:00+00:00")
        day = (logbook.today_local() - timedelta(days=2)).isoformat()
        nxt = (logbook.today_local() - timedelta(days=1)).isoformat()
        s, _ = logbook.day_bounds(day)
        db.run("INSERT INTO assignments(driver_id,provider,device_id,device_name,plate,from_ts) VALUES(?,?,?,?,?,?)",
               did, "gpswox", "7", "Van", "ZZ99ZZ", (s - timedelta(days=9)).isoformat())
        for d, km in ((day, 80000.4), (nxt, 80120.6)):
            db.run("INSERT INTO odometer_log(company_id,provider,device_id,snap_day,km,ts) VALUES(?,?,?,?,?,?)", cid, "gpswox", "7", d, km, "x")
        logbook.fetch_points = lambda *a, **k: []
        v = logbook.compute_day(db.one("SELECT * FROM companies WHERE id=?", cid), db.one("SELECT * FROM drivers WHERE id=?", did), day)["vehicle"]
        assert (v["odo_start_hint"], v["odo_end_hint"], v["odo_src"]) == ("80000", "80121", "gps")


def test_gpswox_devices_keeps_top_level_item():
    """Regressão: o «device_data» aninhado não pode substituir o objeto (perdia posição e distância)."""
    from app import config
    from app.providers import GPSWox, find_odometer
    item = {"id": 151, "name": "Berlingo 23-MB-22", "online": "ack", "timestamp": 1794763436, "lat": 41.49, "lng": -8.51,
            "total_distance": 15872.54,
            "device_data": {"id": 151, "name": "Berlingo 23-MB-22", "imei": "1", "plate_number": "23-MB-22",
                            "traccar": {"id": 151, "other": "<info><odometer>39923801</odometer><totaldistance>15872540.46</totaldistance></info>"}}}
    g = GPSWox("x" * 20)
    g._get = lambda path, **kw: [{"items": [item]}]
    d = g.devices()[0]
    assert d.last_time and d.lat == 41.49
    assert d.odometer == 39923.8 and "odometer" in d.odo_key       # odómetro do equipamento (metros -> km)
    config.ODOMETER_SOURCE = "gps"
    try:
        assert find_odometer(item)[0] == 15872.5                    # distância total acumulada pelo GPS
    finally:
        config.ODOMETER_SOURCE = "equipamento"


def test_odometer_backfill_for_past_days():
    from datetime import datetime, timezone
    from app.providers import Point
    with TestClient(app):
        cid = auth.create_company("Empresa Retro")
        did = db.run("INSERT INTO drivers(company_id,name,token,created_at) VALUES(?,?,?,?)", cid, "Rui", "tokretro", "2026-01-01T00:00:00+00:00")
        day = (logbook.today_local() - timedelta(days=3)).isoformat()
        s, e = logbook.day_bounds(day)
        db.run("INSERT INTO assignments(driver_id,provider,device_id,device_name,plate,from_ts) VALUES(?,?,?,?,?,?)",
               did, "gpswox", "8", "Van", "AA00AA", (s - timedelta(days=9)).isoformat())
        db.run("INSERT INTO devices(company_id,provider,device_id,name,odometer) VALUES(?,?,?,?,?)", cid, "gpswox", "8", "Van", 50000.0)

        def drive(start, n=60, odo=None):  # n minutos a 60 km/h para norte
            return [Point(start + timedelta(minutes=i), 41 + i * 0.009, -8.0, 60.0, odo(i) if odo else None) for i in range(n)]
        co, dr = db.one("SELECT * FROM companies WHERE id=?", cid), db.one("SELECT * FROM drivers WHERE id=?", did)

        # 1) estimativa: o dia tem 1 h de condução e depois houve mais 1 h (até agora)
        logbook.fetch_points = lambda c, p, d, a, b: drive(s + timedelta(hours=8)) if a == s else drive(e + timedelta(hours=1))
        v = logbook.compute_day(co, dr, day)["vehicle"]
        assert v["odo_src"] == "estimate"
        assert int(v["odo_end_hint"]) < 50000 and int(v["odo_start_hint"]) < int(v["odo_end_hint"])

        # 2) a plataforma já traz o odómetro nos pontos: tem prioridade sobre a estimativa
        logbook.fetch_points = lambda c, p, d, a, b: drive(s + timedelta(hours=8), odo=lambda i: 40000 + i) if a == s else []
        v = logbook.compute_day(co, dr, day)["vehicle"]
        assert (v["odo_src"], v["odo_start_hint"], v["odo_end_hint"]) == ("gps", "40000", "40059")


def test_v2_states_journey_notes_vehicle_inspection():
    from app.providers import Point
    with TestClient(app) as c:
        c.post("/login", data={"email": "admin", "password": "segredo123"})
        cid = auth.create_company("Empresa V2")
        db.run("INSERT INTO devices(company_id,provider,device_id,name,plate) VALUES(?,?,?,?,?)", cid, "gpswox", "21", "Van A", "AA00AA")
        db.run("INSERT INTO devices(company_id,provider,device_id,name,plate) VALUES(?,?,?,?,?)", cid, "gpswox", "22", "Van B", "BB00BB")
        did = db.run("INSERT INTO drivers(company_id,name,token,created_at,can_pick_vehicle) VALUES(?,?,?,?,1)", cid, "Zé", "tokv2", "2026-01-01T00:00:00+00:00")
        logbook.fetch_points = lambda *a, **k: []
        m = TestClient(app)
        assert m.get("/d/tokv2", follow_redirects=True).status_code == 200
        me = m.get("/d/api/me").json()
        assert me["has_vehicle"] is False and me["can_pick_vehicle"] is True
        # sem viatura, a condução pode ser manual; disponibilidade e jornada
        assert m.post("/d/api/state", json={"state": "driving"}).json()["state"] == "driving"
        st = m.post("/d/api/state", json={"state": "availability"}).json()
        assert st["state"] == "availability" and st["journey_start"]
        today = m.get(f"/d/api/day/{logbook.today_local().isoformat()}").json()
        assert today["live"] == "availability" and today["limits"]["cont_max"] == 360
        assert today["summary"]["availability"] >= 0 and today["bar"].count("a") >= 1
        # nota do dia
        assert m.post("/d/api/notes", json={"text": "Espera na carga"}).status_code == 200
        assert m.post("/d/api/notes", json={"text": "  "}).status_code == 400
        assert m.get(f"/d/api/day/{logbook.today_local().isoformat()}").json()["notes"][0]["text"] == "Espera na carga"
        # escolher viatura no telemóvel: depois disso a condução é do GPS
        assert len(m.get("/d/api/vehicles").json()) == 2
        assert m.post("/d/api/vehicle", json={"provider": "gpswox", "device_id": "22"}).status_code == 200
        assert m.get("/d/api/me").json()["vehicle"]["plate"] == "BB00BB"
        assert m.post("/d/api/state", json={"state": "driving"}).status_code == 400
        # encerrar jornada
        st = m.post("/d/api/state", json={"state": "end"}).json()
        assert st["state"] == "resting" and st["journey_start"] is None
        # relatório de fiscalização (vários dias num só PDF), no telemóvel e no backoffice
        r = m.get("/d/pdf/fiscalizacao?days=3")
        assert r.status_code == 200 and r.content[:4] == b"%PDF"
        from io import BytesIO
        from pypdf import PdfReader
        assert len(PdfReader(BytesIO(r.content)).pages) >= 3
        c.post("/x/company/switch", json={"id": cid})
        u = db.one("SELECT id FROM users WHERE email='admin'") or db.one("SELECT id FROM users LIMIT 1")
        auth.add_member(cid, u["id"], "gestor")
        assert c.get(f"/relatorios/fiscalizacao/{did}.pdf?days=2").content[:4] == b"%PDF"


def test_limits_helper():
    # 7 h seguidas de trabalho (84 períodos de 5 min) e uma pausa de 30 min a meio faz reiniciar o contínuo
    L = logbook.limits("w" * 48 + "r" * 6 + "w" * 36)
    assert L["day"] == (48 + 36) * 5 and L["cont"] == 36 * 5
    L = logbook.limits("w" * 48 + "r" * 3 + "w" * 36)   # pausa curta não interrompe
    assert L["cont"] == (48 + 36) * 5
