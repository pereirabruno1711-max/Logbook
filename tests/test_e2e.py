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
