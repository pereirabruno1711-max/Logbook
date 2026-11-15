import hashlib
import io
import json
import logging
import os
import re
import secrets
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone

from fastapi import BackgroundTasks, Body, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, config, db, logbook, monitor
from .providers import AuthError, GPSWox, gpswox_login, sync_devices
from .utils import fmt_plate, from_iso, local_str, now_iso

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

HERE = os.path.dirname(__file__)
app = FastAPI(title=config.APP_NAME)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
templates.env.filters["plate"] = fmt_plate
templates.env.filters["lt"] = lambda s, fmt="%d/%m/%Y %H:%M": local_str(s, config.LOCAL_TZ, fmt)
templates.env.globals.update(config=config)

PUBLIC = ("/login", "/static", "/health", "/favicon.ico", "/manifest.webmanifest", "/sw.js")
PW_PATHS = ("/conta/palavra-passe", "/logout")


def _is_public(path):
    return path.startswith(PUBLIC) or path == "/d" or path.startswith("/d/")


@app.exception_handler(logbook.LogbookError)
async def _logbook_error(request: Request, exc: logbook.LogbookError):
    return JSONResponse({"erro": str(exc)}, status_code=400)


# ---------- autenticação (backoffice) ----------
@app.middleware("http")
async def session_mw(request: Request, call_next):
    path = request.url.path
    request.state.user = request.state.company = request.state.real_user = None
    qs = request.scope.get("query_string") or b""
    if qs:  # filtros vazios (?imp=&de=) contam como ausentes
        from urllib.parse import parse_qsl, urlencode
        request.scope["query_string"] = urlencode([(k, v) for k, v in parse_qsl(qs.decode(), keep_blank_values=True) if v]).encode()
    if _is_public(path):
        return await call_next(request)
    if path.startswith("/api/"):
        c = auth.company_from_api_token(request)
        if not c:
            return JSONResponse({"erro": "token inválido"}, status_code=401)
        request.state.company = c
        return await call_next(request)
    u, c, real = auth.from_request(request)
    request.state.real_user = real
    if not u:
        if path.startswith("/x/"):
            return JSONResponse({"erro": "sessão expirada"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    if not c:  # conta sem empresa: pode trocar a palavra-passe, mas não entra na aplicação
        request.state.user = u
        if path in PW_PATHS:
            return await call_next(request)
        if real.get("must_change"):
            return RedirectResponse(PW_PATHS[0], status_code=303)
        if path.startswith("/x/"):
            return JSONResponse({"erro": "A sua conta ainda não tem empresa atribuída."}, status_code=403)
        return templates.TemplateResponse(request, "no_company.html", dict(user=real), status_code=403)
    request.state.user, request.state.company = u, c
    if real.get("must_change") and path not in PW_PATHS:  # palavra-passe temporária: trocar antes de continuar
        if path.startswith("/x/"):
            return JSONResponse({"erro": "Tem de definir uma nova palavra-passe."}, status_code=403)
        return RedirectResponse(PW_PATHS[0], status_code=303)
    return await call_next(request)


def me(request):
    """Empresa ativa (dona dos dados: owner_id = me(request)["id"])."""
    return request.state.company


def person(request):
    """Pessoa com sessão iniciada (em personificação: a vista sem privilégios de plataforma)."""
    return request.state.user


def real_person(request):
    """Pessoa real com sessão iniciada, mesmo em personificação."""
    return request.state.real_user or request.state.user


def _initials(u):
    base = (u.get("name") or u.get("email") or "?").split("@")[0]
    parts = [x for x in base.replace(".", " ").replace("_", " ").replace("-", " ").split() if x]
    return ((parts[0][0] + (parts[1][0] if len(parts) > 1 else "")) if parts else "?").upper()


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, erro: str = ""):
    return templates.TemplateResponse(request, "login.html", dict(erro=erro, user=None))


@app.post("/login")
def login_post(request: Request, email: str = Form(...), password: str = Form(...)):
    try:
        u = auth.login(email, password)
    except AuthError as e:
        time.sleep(0.7)
        return templates.TemplateResponse(request, "login.html", dict(erro=str(e), email=email, user=None),
                                          status_code=401)
    tok = auth.new_session(u["id"])
    r = RedirectResponse("/", status_code=303)
    secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    r.set_cookie(auth.COOKIE, tok, max_age=config.SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=secure)
    for c in auth.companies_of(u):
        if c.get("gpswox_hash") and c.get("role") and \
                not db.one("SELECT 1 AS x FROM devices WHERE company_id=? LIMIT 1", c["id"]):
            threading.Thread(target=_safe_sync, args=(auth.company(c["id"]),), daemon=True).start()
    return r


@app.get("/logout")
def logout(request: Request):
    auth.end_session(request.cookies.get(auth.COOKIE))
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie(auth.COOKIE)
    return r


@app.get("/conta/palavra-passe", response_class=HTMLResponse)
def pw_page(request: Request, ok: str = ""):
    real = real_person(request)
    return templates.TemplateResponse(request, "password.html", dict(
        user=real, forced=bool(real.get("must_change")), first=not real.get("password_hash"), ok=ok, erro=""))


@app.post("/conta/palavra-passe")
def pw_post(request: Request, current: str = Form(""), new: str = Form(...), confirm: str = Form(...)):
    real = real_person(request)
    forced = bool(real.get("must_change"))
    ctx = dict(user=real, forced=forced, first=not real.get("password_hash"), ok="")
    try:
        if not forced and not auth.check_password(current, real.get("password_hash")):
            raise HTTPException(400, "A palavra-passe atual não está correta.")
        if new != confirm:
            raise HTTPException(400, "As duas palavras-passe não são iguais.")
        auth.validate_new_password(new, real.get("password_hash"))
    except HTTPException as e:
        return templates.TemplateResponse(request, "password.html", dict(ctx, erro=e.detail), status_code=400)
    auth.set_password(real["id"], new, False)
    tok = request.cookies.get(auth.COOKIE)  # termina as outras sessões desta pessoa
    db.run("DELETE FROM sessions WHERE user_id=? AND token!=?", real["id"],
           hashlib.sha256((tok or "").encode()).hexdigest())
    return RedirectResponse("/" if forced else "/conta/palavra-passe?ok=1", status_code=303)


_icons = templates.env.get_template("_icons.html").module
templates.env.globals.update(I=_icons.I, logo=_icons.logo)


def pending_by_driver(cid, days=7):
    """Dias (últimos 7, sem hoje) sem relatório assinado, por motorista ativo."""
    today = logbook.today_local()
    signed = {(r["driver_id"], r["day"]) for r in db.q(
        "SELECT driver_id, day FROM logbooks WHERE company_id=? AND status='signed' AND day>=?",
        cid, (today - timedelta(days=days)).isoformat())}
    out = {}
    for d in db.q("SELECT id, created_at FROM drivers WHERE company_id=? AND active=1", cid):
        created = from_iso(d["created_at"]).astimezone(logbook.TZ).date() if d["created_at"] else today
        out[d["id"]] = sum(1 for k in range(1, days + 1)
                           if (today - timedelta(days=k)) >= created and (d["id"], (today - timedelta(days=k)).isoformat()) not in signed)
    return out


def page(request, name, **ctx):
    u, c = person(request), me(request)
    n_alert = sum(pending_by_driver(c["id"]).values())
    return templates.TemplateResponse(request, name, dict(
        user=u, company=c, manager=auth.can_manage(u, c["id"]),
        companies=[x for x in auth.companies_of(u) if x["member"] or auth.is_super(u)],
        is_super=auth.is_super(u), level_name=auth.LEVELS.get(auth.level(u), "Administrador"),
        impersonating=c.get("impersonating"),
        active=name.split(".")[0], initials=_initials(u), n_alert=n_alert, **ctx))


def _safe_sync(c):
    try:
        log.info("sync %s: %s", c.get("name") or c.get("id"), sync_devices(c))
    except Exception:  # noqa
        log.exception("sync")


def _manager(request):
    c = me(request)
    auth.require_manager(person(request), c["id"])
    return c


def _own_driver(cid, driver_id):
    d = db.one("SELECT * FROM drivers WHERE id=? AND company_id=?", driver_id, cid)
    if not d:
        raise HTTPException(404, "Motorista não encontrado.")
    return d


# ---------- páginas ----------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return page(request, "dashboard.html")


@app.get("/motoristas", response_class=HTMLResponse)
def drivers_page(request: Request):
    return page(request, "drivers.html")


@app.get("/objetos", response_class=HTMLResponse)
def objects_page(request: Request):
    return page(request, "vehicles.html")


@app.get("/relatorios", response_class=HTMLResponse)
def reports_page(request: Request):
    drivers = db.q("SELECT id, name, active FROM drivers WHERE company_id=? ORDER BY name COLLATE NOCASE", me(request)["id"])
    return page(request, "logbooks.html", drivers=drivers, today=logbook.today_local().isoformat(),
                month_start=logbook.today_local().replace(day=1).isoformat())


# ---------- empresas ----------
def _company_row(c):
    cid = c["id"]
    since = (logbook.today_local() - timedelta(days=30)).isoformat()
    st = db.one("""SELECT (SELECT COUNT(*) FROM drivers WHERE company_id=? AND active=1) drv,
        (SELECT COUNT(*) FROM logbooks WHERE company_id=? AND status='signed' AND day>=?) sig,
        (SELECT COUNT(*) FROM members m JOIN users u ON u.id=m.user_id WHERE m.company_id=? AND COALESCE(u.is_admin,0)<?) mem,
        (SELECT MAX(signed_at) FROM logbooks WHERE company_id=? AND status='signed') last""",
                cid, cid, since, cid, auth.SUPER, cid)
    return dict(id=cid, name=c["name"], nif=c.get("nif"), role=c.get("role"), member=bool(c.get("member")),
                gps=bool(c.get("gpswox_hash")), tt=bool(c.get("trusttrack_key")),
                last=local_str(st["last"], config.LOCAL_TZ) if st["last"] else "",
                drv=st["drv"], sig=st["sig"], pend=sum(pending_by_driver(cid).values()), mem=st["mem"])


@app.delete("/x/companies/{cid}")
def x_company_delete(request: Request, cid: int, confirm: str = ""):
    """Superadministrador: apaga a empresa e todos os seus dados (motoristas, relatórios, objetos, membros)."""
    if person(request).get("_imp"):
        raise HTTPException(403, "Termine a personificação antes de apagar empresas.")
    u = person(request)
    auth.require_super(u)
    c = auth.company(cid)
    if not c:
        raise HTTPException(404)
    if (confirm or "").strip().lower() != c["name"].strip().lower():
        raise HTTPException(400, "Escreva o nome exato da empresa para confirmar.")
    if db.one("SELECT COUNT(*) n FROM companies")["n"] <= 1:
        raise HTTPException(400, "Não pode apagar a única empresa da plataforma.")
    paths = [r["pdf_path"] for r in db.q("SELECT pdf_path FROM logbooks WHERE company_id=? AND pdf_path IS NOT NULL", cid)]
    with db.tx() as x:
        ids = [r["id"] for r in x.execute("SELECT id FROM drivers WHERE company_id=?", (cid,)).fetchall()]
        for did in ids:
            x.execute("DELETE FROM assignments WHERE driver_id=?", (did,))
            x.execute("DELETE FROM driver_events WHERE driver_id=?", (did,))
        n_lb = x.execute("DELETE FROM logbooks WHERE company_id=?", (cid,)).rowcount
        x.execute("DELETE FROM drivers WHERE company_id=?", (cid,))
        x.execute("DELETE FROM devices WHERE company_id=?", (cid,))
        x.execute("DELETE FROM members WHERE company_id=?", (cid,))
        x.execute("DELETE FROM invites WHERE company_id=?", (cid,))
        x.execute("UPDATE sessions SET company_id=NULL, as_role=NULL WHERE company_id=?", (cid,))
        x.execute("DELETE FROM companies WHERE id=?", (cid,))
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass
    log.info("empresa %s (%s) apagada por %s", cid, c["name"], u["email"])
    return {"ok": True, "drivers": len(ids), "logbooks": n_lb}


# ---------- motoristas ----------
def _driver_row(d, base):
    a = db.one("SELECT * FROM assignments WHERE driver_id=? AND to_ts IS NULL ORDER BY from_ts DESC LIMIT 1", d["id"])
    since = (logbook.today_local() - timedelta(days=30)).isoformat()
    n = db.one("SELECT COUNT(*) n, MAX(day) last FROM logbooks WHERE driver_id=? AND status='signed' AND day>=?", d["id"], since)
    last = db.one("SELECT MAX(day) last FROM logbooks WHERE driver_id=? AND status='signed'", d["id"])
    return {"id": d["id"], "name": d["name"], "phone": d["phone"] or "", "licence": d["licence"] or "", "active": bool(d["active"]),
            "link": f"{base}/d/{d['token']}", "state": logbook.current_state(d["id"])["state"],
            "can_pick_vehicle": bool(d.get("can_pick_vehicle")),
            "device": ({"provider": a["provider"], "device_id": a["device_id"], "name": a["device_name"], "plate": a["plate"],
                        "from": local_str(a["from_ts"], config.LOCAL_TZ, "%d/%m/%Y")} if a else None),
            "signed30": n["n"], "last_signed": last["last"] if last else None,
            "last_seen": local_str(d["last_seen"], config.LOCAL_TZ) if d["last_seen"] else ""}


@app.get("/x/drivers")
def x_drivers(request: Request):
    c = me(request)
    base = str(request.base_url).rstrip("/")
    pend = pending_by_driver(c["id"])
    out = []
    for d in db.q("SELECT * FROM drivers WHERE company_id=? ORDER BY active DESC, name COLLATE NOCASE", c["id"]):
        r = _driver_row(d, base)
        r["pending"] = pend.get(d["id"], 0)
        out.append(r)
    return out


@app.post("/x/drivers")
def x_driver_create(request: Request, data: dict = Body(...)):
    c = _manager(request)
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Indique o nome do motorista.")
    did = db.run("INSERT INTO drivers(company_id,name,phone,licence,token,active,created_at) VALUES(?,?,?,?,?,1,?)",
                 c["id"], name[:80], (data.get("phone") or "").strip()[:30], (data.get("licence") or "").strip()[:30],
                 secrets.token_urlsafe(16), now_iso())
    return {"id": did}


@app.post("/x/drivers/{did}")
def x_driver_update(request: Request, did: int, data: dict = Body(...)):
    c = _manager(request)
    _own_driver(c["id"], did)
    for k, n in (("name", 80), ("phone", 30), ("licence", 30)):
        if k in data:
            v = (data[k] or "").strip()[:n]
            if k == "name" and not v:
                raise HTTPException(400, "O nome não pode ficar vazio.")
            db.run(f"UPDATE drivers SET {k}=? WHERE id=?", v, did)
    if "active" in data:
        db.run("UPDATE drivers SET active=? WHERE id=?", 1 if data["active"] else 0, did)
    if "can_pick_vehicle" in data:
        db.run("UPDATE drivers SET can_pick_vehicle=? WHERE id=?", 1 if data["can_pick_vehicle"] else 0, did)
    if data.get("new_token"):
        db.run("UPDATE drivers SET token=? WHERE id=?", secrets.token_urlsafe(16), did)
    return {"ok": True}


@app.delete("/x/drivers/{did}")
def x_driver_delete(request: Request, did: int):
    c = _manager(request)
    _own_driver(c["id"], did)
    if db.one("SELECT 1 AS x FROM logbooks WHERE driver_id=? AND status='signed' LIMIT 1", did):
        raise HTTPException(400, "Este motorista tem relatórios assinados, que têm de ser conservados. Desative-o em vez de o apagar.")
    with db.tx() as x:
        x.execute("DELETE FROM logbooks WHERE driver_id=?", (did,))
        x.execute("DELETE FROM assignments WHERE driver_id=?", (did,))
        x.execute("DELETE FROM driver_events WHERE driver_id=?", (did,))
        x.execute("DELETE FROM drivers WHERE id=?", (did,))
    return {"ok": True}


@app.post("/x/drivers/{did}/assign")
def x_driver_assign(request: Request, did: int, data: dict = Body(...)):
    """Atribui um objeto GPS ao motorista (a condução é detetada por esse objeto). Mantém o histórico."""
    c = _manager(request)
    _own_driver(c["id"], did)
    cur = db.one("SELECT * FROM assignments WHERE driver_id=? AND to_ts IS NULL ORDER BY from_ts DESC LIMIT 1", did)
    first = db.one("SELECT 1 AS x FROM assignments WHERE driver_id=? LIMIT 1", did) is None
    if data.get("from"):
        try:
            from_ts = logbook.day_bounds(data["from"])[0]
        except logbook.LogbookError:
            raise HTTPException(400, "Data inválida.")
    elif first:  # primeira atribuição: vale para os dias que a app mostra
        from_ts = logbook.day_bounds((logbook.today_local() - timedelta(days=config.DAYS_BACK)).isoformat())[0]
    else:
        from_ts = datetime.now(timezone.utc)
    if cur and from_ts <= from_iso(cur["from_ts"]):
        raise HTTPException(400, "A data de início tem de ser posterior à da atribuição atual.")
    if data.get("clear"):
        if cur:
            db.run("UPDATE assignments SET to_ts=? WHERE id=?", from_ts.isoformat(), cur["id"])
        return {"ok": True}
    dev = db.one("SELECT * FROM devices WHERE company_id=? AND provider=? AND device_id=?", c["id"],
                 data.get("provider"), str(data.get("device_id")))
    if not dev:
        raise HTTPException(400, "Objeto não encontrado. Atualize a lista de objetos.")
    if cur:
        db.run("UPDATE assignments SET to_ts=? WHERE id=?", from_ts.isoformat(), cur["id"])
    db.run("INSERT INTO assignments(driver_id,provider,device_id,device_name,plate,from_ts) VALUES(?,?,?,?,?,?)",
           did, dev["provider"], dev["device_id"], dev["name"], dev["plate"], from_ts.isoformat())
    logbook.clear_cache()
    return {"ok": True}


# ---------- objetos (equipamentos GPS) ----------
@app.get("/x/devices")
def x_devices(request: Request):
    c = me(request)
    rows = db.q("""SELECT v.*, (SELECT GROUP_CONCAT(d.name, ', ') FROM assignments a JOIN drivers d ON d.id=a.driver_id
                   WHERE a.provider=v.provider AND a.device_id=v.device_id AND a.to_ts IS NULL AND d.company_id=v.company_id) drivers
                   FROM devices v WHERE v.company_id=? ORDER BY v.name COLLATE NOCASE""", c["id"])
    for r in rows:
        r["last_time"] = local_str(r["last_time"], config.LOCAL_TZ) if r["last_time"] else ""
    return {"devices": rows, "gps": bool(c.get("gpswox_hash")), "tt": bool(c.get("trusttrack_key"))}


@app.get("/x/devices/raw")
def x_device_raw(request: Request, provider: str, device_id: str):
    """Dados em bruto de um objeto, para ver onde a plataforma GPS guarda o odómetro."""
    c = _manager(request)
    from .providers import clients_for, find_odometer
    prov = clients_for(auth.company(c["id"])).get(provider)
    if not prov or not prov.enabled:
        raise HTTPException(400, "Plataforma não ligada.")
    try:
        raw = prov.raw(device_id)
    except Exception as e:  # noqa
        raise HTTPException(400, f"A plataforma recusou o pedido ({e.__class__.__name__}).")
    odo, key = find_odometer(raw) if raw else (None, None)
    return {"raw": raw, "odometer": odo, "key": key}


@app.post("/x/devices/sync")
def x_devices_sync(request: Request):
    c = _manager(request)
    return {"report": sync_devices(auth.company(c["id"])) or ["Nenhuma plataforma GPS ligada."]}


# ---------- relatórios (diários) ----------
def _lb_where(cid, args):
    where, params = ["l.company_id=?"], [cid]
    if args.get("ids"):
        ids = [int(x) for x in args["ids"].split(",") if x.strip().isdigit()][:500]
        where.append(f"l.id IN ({','.join('?' * len(ids)) or 'NULL'})")
        params += ids
    if args.get("driver"):
        ds = [int(x) for x in args["driver"].split(",") if x.strip().isdigit()]
        if ds:
            where.append(f"l.driver_id IN ({','.join('?' * len(ds))})")
            params += ds
    if args.get("from"):
        where.append("l.day>=?")
        params.append(args["from"])
    if args.get("to"):
        where.append("l.day<=?")
        params.append(args["to"])
    st = args.get("status", "signed")
    if st in ("signed", "filling"):
        where.append("l.status=?")
        params.append(st)
    return " AND ".join(where), params


def _lb_row(r):
    snap = json.loads(r["snapshot"]) if r.get("snapshot") else None
    sm = (snap or {}).get("summary") or {}
    return {"id": r["id"], "day": r["day"], "driver_id": r["driver_id"], "driver": r["driver_name"], "status": r["status"],
            "signed_at": local_str(r["signed_at"], config.LOCAL_TZ) if r["signed_at"] else "",
            "plate": ((snap or {}).get("vehicle") or {}).get("plate", ""),
            "driving": sm.get("driving"), "working": sm.get("working"), "availability": sm.get("availability"),
            "resting": sm.get("resting"), "km": sm.get("km"),
            "pdf": f"/relatorios/{r['id']}.pdf" if r["status"] == "signed" else f"/relatorios/previa/{r['driver_id']}/{r['day']}.pdf"}


@app.get("/x/logbooks")
def x_logbooks(request: Request):
    c = me(request)
    where, params = _lb_where(c["id"], dict(request.query_params))
    rows = db.q(f"""SELECT l.*, d.name driver_name FROM logbooks l JOIN drivers d ON d.id=l.driver_id
                    WHERE {where} ORDER BY l.day DESC, d.name COLLATE NOCASE LIMIT 1000""", *params)
    return [_lb_row(r) for r in rows]


def _lb_pdf(r):
    try:
        with open(r["pdf_path"], "rb") as f:
            return f.read()
    except (OSError, TypeError):
        raise HTTPException(404, "PDF não encontrado.")


def _slug(s):
    return re.sub(r"[^A-Za-z0-9]+", "_", re.sub(r"[^\x00-\x7f]", lambda m: __import__("unicodedata").normalize("NFKD", m.group())[:1], s or "")).strip("_") or "motorista"


@app.get("/relatorios/zip")
def logbooks_zip(request: Request):
    """Descarrega em lote os relatórios assinados (filtro por motoristas e período, ou por ids)."""
    c = me(request)
    args = dict(request.query_params, status="signed")
    where, params = _lb_where(c["id"], args)
    rows = db.q(f"""SELECT l.*, d.name driver_name FROM logbooks l JOIN drivers d ON d.id=l.driver_id
                    WHERE {where} ORDER BY l.day, d.name LIMIT 500""", *params)
    if not rows:
        raise HTTPException(404, "Não há relatórios assinados com estes filtros.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for r in rows:
            z.writestr(f"{_slug(r['driver_name'])}_{r['day']}.pdf", _lb_pdf(r))
    name = f"diarios_{args.get('from', 'inicio')}_{args.get('to', 'fim')}.zip"
    return Response(buf.getvalue(), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/relatorios/fiscalizacao/{did}.pdf")
def logbook_inspection(request: Request, did: int, days: int = 7):
    """Relatório único dos últimos N dias de um motorista, para mostrar numa fiscalização."""
    c = me(request)
    d = _own_driver(c["id"], did)
    return Response(logbook.inspection_pdf(c, d, days), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="fiscalizacao_{_slug(d["name"])}.pdf"'})


@app.get("/relatorios/previa/{did}/{day}.pdf")
def logbook_preview(request: Request, did: int, day: str):
    c = me(request)
    d = _own_driver(c["id"], did)
    return Response(logbook.preview_pdf(c, d, day), media_type="application/pdf")


@app.get("/relatorios/{lid}.pdf")
def logbook_pdf(request: Request, lid: int):
    c = me(request)
    r = db.one("SELECT * FROM logbooks WHERE id=? AND company_id=? AND status='signed'", lid, c["id"])
    if not r:
        raise HTTPException(404, "Relatório não encontrado.")
    return Response(_lb_pdf(r), media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="diario_{r["day"]}.pdf"'})


# ---------- alertas / verificação noturna ----------
@app.get("/x/alerts")
def x_alerts(request: Request):
    c = me(request)
    rows = db.q("""SELECT a.*, d.name driver FROM alerts a JOIN drivers d ON d.id=a.driver_id
                   WHERE a.company_id=? AND a.dismissed=0 ORDER BY a.day DESC, d.name COLLATE NOCASE LIMIT 200""", c["id"])
    for r in rows:
        r["label"] = monitor.KINDS.get(r["kind"], r["kind"])
    last = db.one("SELECT v FROM meta WHERE k='nightly_last'")
    return {"alerts": rows, "last_run": last["v"] if last else None, "hour": config.NIGHTLY_HOUR}


@app.post("/x/alerts/{aid}/dismiss")
def x_alert_dismiss(request: Request, aid: int):
    c = _manager(request)
    db.run("UPDATE alerts SET dismissed=1 WHERE id=? AND company_id=?", aid, c["id"])
    return {"ok": True}


@app.post("/x/monitor/run")
def x_monitor_run(request: Request, bg: BackgroundTasks):
    c = _manager(request)
    bg.add_task(monitor.run, c["id"])
    return {"ok": True}


# ---------- API externa ----------
@app.get("/api/logbooks")
def api_logbooks(request: Request):
    c = me(request)
    args = dict(request.query_params)
    where, params = _lb_where(c["id"], args)
    rows = db.q(f"""SELECT l.*, d.name driver_name FROM logbooks l JOIN drivers d ON d.id=l.driver_id
                    WHERE {where} ORDER BY l.day DESC LIMIT 1000""", *params)
    return [dict(_lb_row(r), pdf=f"{request.base_url}api/logbooks/{r['id']}/pdf") for r in rows]


@app.get("/api/logbooks/{lid}/pdf")
def api_logbook_pdf(request: Request, lid: int):
    r = db.one("SELECT * FROM logbooks WHERE id=? AND company_id=? AND status='signed'", lid, me(request)["id"])
    if not r:
        raise HTTPException(404)
    return Response(_lb_pdf(r), media_type="application/pdf")


# ---------- app do motorista ----------
DRV_COOKIE = "lbdrv"


def _drv(request):
    tok = request.cookies.get(DRV_COOKIE)
    d = db.one("SELECT * FROM drivers WHERE token=? AND active=1", tok) if tok else None
    c = auth.company(d["company_id"]) if d else None
    if not (d and c):
        raise HTTPException(401, "Sessão terminada. Abra de novo o link que recebeu do seu gestor.")
    return d, c


def _set_drv_cookie(request, resp, token):
    secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    resp.set_cookie(DRV_COOKIE, token, max_age=365 * 86400, httponly=True, samesite="lax", secure=secure)
    return resp


@app.get("/d", response_class=HTMLResponse)
def driver_app(request: Request):
    try:
        d, c = _drv(request)
    except HTTPException:
        return templates.TemplateResponse(request, "driver_login.html", dict(erro=request.query_params.get("erro", "")))
    return templates.TemplateResponse(request, "driver.html", dict(driver=d, company=c))


@app.post("/d/login")
def driver_login(request: Request, code: str = Form(...)):
    tok = code.strip().rstrip("/").split("/")[-1]
    d = db.one("SELECT * FROM drivers WHERE token=? AND active=1", tok) if tok else None
    if not d:
        time.sleep(0.7)
        return RedirectResponse("/d?erro=Código+inválido.+Peça+um+novo+link+ao+seu+gestor.", status_code=303)
    return _set_drv_cookie(request, RedirectResponse("/d", status_code=303), tok)


@app.get("/d/sair")
def driver_logout():
    r = RedirectResponse("/d", status_code=303)
    r.delete_cookie(DRV_COOKIE)
    return r


@app.get("/d/api/me")
def d_me(request: Request):
    d, c = _drv(request)
    db.run("UPDATE drivers SET last_seen=? WHERE id=?", now_iso(), d["id"])
    a = db.one("SELECT * FROM assignments WHERE driver_id=? AND to_ts IS NULL ORDER BY from_ts DESC LIMIT 1", d["id"])
    return {"driver": {"name": d["name"], "phone": d["phone"] or "", "licence": d["licence"] or ""}, "company": c["name"],
            "state": logbook.current_state(d["id"]), "today": logbook.today_local().isoformat(), "app": config.APP_NAME,
            "has_vehicle": a is not None, "vehicle": ({"name": a["device_name"], "plate": a["plate"]} if a else None),
            "can_pick_vehicle": bool(d["can_pick_vehicle"])}


@app.post("/d/api/state")
def d_state(request: Request, data: dict = Body(...)):
    d, _ = _drv(request)
    if data.get("state") == "driving" and logbook.has_vehicle(d["id"]):
        raise HTTPException(400, "A condução é detetada automaticamente pelo GPS da viatura.")
    return logbook.set_state(d["id"], data.get("state"))


@app.post("/d/api/notes")
def d_note(request: Request, data: dict = Body(...)):
    d, c = _drv(request)
    logbook.add_note(c, d, data.get("text"))
    return {"ok": True}


@app.get("/d/api/vehicles")
def d_vehicles(request: Request):
    d, c = _drv(request)
    if not d["can_pick_vehicle"]:
        raise HTTPException(403, "O seu gestor não ativou a troca de viatura.")
    return db.q("SELECT provider, device_id, name, plate FROM devices WHERE company_id=? ORDER BY name COLLATE NOCASE", c["id"])


@app.post("/d/api/vehicle")
def d_vehicle(request: Request, data: dict = Body(...)):
    """O motorista escolhe a viatura no telemóvel (só se o gestor o permitir): a anterior fecha-se agora."""
    d, c = _drv(request)
    if not d["can_pick_vehicle"]:
        raise HTTPException(403, "O seu gestor não ativou a troca de viatura.")
    dev = db.one("SELECT * FROM devices WHERE company_id=? AND provider=? AND device_id=?", c["id"], data.get("provider"), str(data.get("device_id")))
    if not dev:
        raise HTTPException(400, "Viatura não encontrada.")
    cur = db.one("SELECT * FROM assignments WHERE driver_id=? AND to_ts IS NULL ORDER BY from_ts DESC LIMIT 1", d["id"])
    if cur and cur["provider"] == dev["provider"] and cur["device_id"] == dev["device_id"]:
        return {"ok": True}
    now = now_iso()
    if cur:
        db.run("UPDATE assignments SET to_ts=? WHERE id=?", now, cur["id"])
    db.run("INSERT INTO assignments(driver_id,provider,device_id,device_name,plate,from_ts) VALUES(?,?,?,?,?,?)",
           d["id"], dev["provider"], dev["device_id"], dev["name"], dev["plate"], now)
    logbook.clear_cache()
    return {"ok": True}


@app.get("/d/api/days")
def d_days(request: Request):
    d, c = _drv(request)
    return logbook.day_list(c, d)


@app.get("/d/api/day/{day}")
def d_day(request: Request, day: str):
    d, c = _drv(request)
    return logbook.view_day(c, d, day)


@app.post("/d/api/day/{day}")
def d_day_save(request: Request, day: str, data: dict = Body(...)):
    d, c = _drv(request)
    return logbook.save_data(c, d, day, data)


@app.post("/d/api/day/{day}/sign")
def d_day_sign(request: Request, day: str, data: dict = Body(...)):
    d, c = _drv(request)
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "")).split(",")[0].strip()
    return logbook.sign(c, d, day, data.get("signature"), ip)


@app.get("/d/pdf/fiscalizacao")
def d_inspection(request: Request, days: int = 7):
    d, c = _drv(request)
    return Response(logbook.inspection_pdf(c, d, days), media_type="application/pdf",
                    headers={"Content-Disposition": 'inline; filename="fiscalizacao.pdf"'})


@app.get("/d/pdf/{day}")
def d_pdf(request: Request, day: str):
    d, c = _drv(request)
    return Response(logbook.preview_pdf(c, d, day), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="diario_{day}.pdf"'})


@app.get("/d/{token}")
def driver_link(request: Request, token: str):
    """Link pessoal do motorista: guarda o acesso neste telemóvel e abre a app."""
    d = db.one("SELECT * FROM drivers WHERE token=? AND active=1", token)
    if not d:
        return RedirectResponse("/d?erro=Link+inválido+ou+motorista+desativado.", status_code=303)
    return _set_drv_cookie(request, RedirectResponse("/d", status_code=303), token)


@app.get("/manifest.webmanifest")
def manifest():
    return JSONResponse({"name": config.APP_NAME, "short_name": "Diário", "start_url": "/d", "scope": "/d",
                         "display": "standalone", "background_color": "#f4f6fa", "theme_color": config.BRAND_COLOR,
                         "icons": [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}]},
                        media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    return Response("self.addEventListener('install',e=>self.skipWaiting());self.addEventListener('activate',e=>e.waitUntil(clients.claim()));"
                    "self.addEventListener('fetch',e=>{});", media_type="application/javascript")


# ---------- tarefas automáticas ----------
def _nightly_due():
    """Corre uma vez por dia, a partir da hora configurada (hora local)."""
    now = datetime.now(logbook.TZ)
    if now.hour < config.NIGHTLY_HOUR:
        return False
    last = db.one("SELECT v FROM meta WHERE k='nightly_last'")
    return not last or last["v"] != now.date().isoformat()


def _scheduler():
    last_sync = 0.0
    while True:
        try:
            if time.time() - last_sync > config.VEHICLE_SYNC_HOURS * 3600:
                for c in db.q("SELECT id FROM companies WHERE gpswox_hash IS NOT NULL OR trusttrack_key IS NOT NULL"):
                    _safe_sync(auth.company(c["id"]))
                last_sync = time.time()
            if _nightly_due():
                db.run("INSERT OR REPLACE INTO meta(k,v) VALUES('nightly_last',?)", datetime.now(logbook.TZ).date().isoformat())
                monitor.run()
        except Exception:  # noqa
            log.exception("scheduler")
        time.sleep(300)


@app.on_event("startup")
def startup():
    db.init()
    if os.getenv("DISABLE_SCHEDULER") != "1":
        threading.Thread(target=_scheduler, daemon=True).start()


# ---------- empresas / utilizadores (gestão) ----------
@app.get("/empresas", response_class=HTMLResponse)
def companies_page(request: Request):
    return page(request, "companies.html", base_url=str(request.base_url).rstrip("/"))


@app.get("/x/companies")
def x_companies(request: Request):
    u = person(request)
    return [_company_row(c) for c in auth.companies_of(u)]


@app.get("/x/companies/{cid}")
def x_company(request: Request, cid: int):
    u = person(request)
    role = auth.role_in(u, cid)
    if not role:
        raise HTTPException(403, "Sem acesso a esta empresa.")
    c = auth.company(cid)
    out = _company_row(dict(c, role=role))
    out["manager"] = role == "gestor"
    if out["manager"]:
        out["api_token"] = c["api_token"]
        out["members"] = db.q("""SELECT u.id, u.email, u.name, u.last_login, m.role FROM members m JOIN users u
                                 ON u.id=m.user_id WHERE m.company_id=? AND COALESCE(u.is_admin,0)<? ORDER BY u.email""", cid, auth.SUPER)
        for m in out["members"]:
            m["last_login"] = local_str(m["last_login"], config.LOCAL_TZ) if m["last_login"] else ""
        out["invites"] = db.q("SELECT email, role, created_at FROM invites WHERE company_id=? ORDER BY email", cid)
    return out


@app.post("/x/companies")
def x_company_create(request: Request, data: dict = Body(...)):
    u = person(request)
    auth.require_admin(u)
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Indique o nome da empresa.")
    cid = auth.create_company(name, nif=(data.get("nif") or "").strip())
    auth.add_member(cid, u["id"], "gestor")
    return {"id": cid}


@app.post("/x/companies/{cid}")
def x_company_update(request: Request, cid: int, data: dict = Body(...)):
    auth.require_manager(person(request), cid)
    if "name" in data and (data["name"] or "").strip():
        db.run("UPDATE companies SET name=? WHERE id=?", data["name"].strip(), cid)
    if "nif" in data:
        db.run("UPDATE companies SET nif=? WHERE id=?", (data["nif"] or "").strip() or None, cid)
    if "trusttrack_key" in data:
        db.run("UPDATE companies SET trusttrack_key=? WHERE id=?", (data["trusttrack_key"] or "").strip() or None, cid)
    if data.get("new_token"):
        db.run("UPDATE companies SET api_token=? WHERE id=?", secrets.token_urlsafe(24), cid)
    return {"ok": True}


@app.post("/x/companies/{cid}/gps")
def x_company_gps(request: Request, cid: int, bg: BackgroundTasks, data: dict = Body(...)):
    """Liga a empresa a uma conta GPSWox (email e palavra-passe escritos pelo gestor; só se guarda a chave)."""
    auth.require_manager(person(request), cid)
    try:
        h = gpswox_login(data.get("email") or "", data.get("password") or "")
    except AuthError as e:
        raise HTTPException(400, str(e))
    db.run("UPDATE companies SET gpswox_hash=? WHERE id=?", h, cid)
    bg.add_task(_safe_sync, auth.company(cid))
    return {"ok": True}


@app.post("/x/companies/{cid}/gps_key")
def x_company_gps_key(request: Request, cid: int, bg: BackgroundTasks, data: dict = Body(...)):
    """Liga a empresa à GPSWox com a chave API (user_api_hash), sem email nem palavra-passe."""
    auth.require_manager(person(request), cid)
    key = (data.get("api_hash") or "").strip()
    if len(key) < 10:
        raise HTTPException(400, "Chave API inválida.")
    from .providers import GPSWox
    try:
        n = len(GPSWox(key).devices())
    except Exception as e:  # noqa
        raise HTTPException(400, f"A plataforma GPS recusou esta chave ({e.__class__.__name__}).")
    db.run("UPDATE companies SET gpswox_hash=? WHERE id=?", key, cid)
    bg.add_task(_safe_sync, auth.company(cid))
    return {"ok": True, "devices": n}


@app.post("/x/companies/{cid}/members")
def x_member_add(request: Request, cid: int, data: dict = Body(...)):
    auth.require_manager(person(request), cid)
    email = (data.get("email") or "").strip().lower()
    role = data.get("role") if data.get("role") in auth.ROLES else "utilizador"
    if "@" not in email:
        raise HTTPException(400, "Email inválido.")
    u = db.one("SELECT * FROM users WHERE email=?", email)
    if u:
        auth.add_member(cid, u["id"], role)
        return {"added": True}
    # pessoa nova: cria a conta nesta plataforma com palavra-passe automática (trocada na 1.ª entrada)
    u, pw = auth.create_user(email, data.get("name"))
    auth.add_member(cid, u["id"], role)
    return {"added": True, "created": True, "email": email, "password": pw}


@app.post("/x/companies/{cid}/members/{uid}")
def x_member_role(request: Request, cid: int, uid: int, data: dict = Body(...)):
    me_ = person(request)
    auth.require_manager(me_, cid)
    role = data.get("role")
    if role not in auth.ROLES:
        raise HTTPException(400, "Papel inválido.")
    if role != "gestor" and not _other_manager(cid, uid):
        raise HTTPException(400, "A empresa tem de ter pelo menos um gestor.")
    db.run("UPDATE members SET role=? WHERE company_id=? AND user_id=?", role, cid, uid)
    return {"ok": True}


def _other_manager(cid, uid):
    return db.one("SELECT 1 AS x FROM members WHERE company_id=? AND role='gestor' AND user_id!=?", cid, uid)


@app.delete("/x/companies/{cid}/members/{uid}")
def x_member_del(request: Request, cid: int, uid: int):
    auth.require_manager(person(request), cid)
    if not _other_manager(cid, uid):
        raise HTTPException(400, "A empresa tem de ter pelo menos um gestor.")
    db.run("DELETE FROM members WHERE company_id=? AND user_id=?", cid, uid)
    return {"ok": True}


@app.delete("/x/companies/{cid}/invites")
def x_invite_del(request: Request, cid: int, email: str):
    auth.require_manager(person(request), cid)
    db.run("DELETE FROM invites WHERE company_id=? AND email=?", cid, email.lower())
    return {"ok": True}


@app.post("/x/company/switch")
def x_company_switch(request: Request, data: dict = Body(...)):
    tok, cid = request.cookies.get(auth.COOKIE), int(data.get("id") or 0)
    real, c = real_person(request), me(request)
    if c.get("impersonating") and cid == c["id"]:  # mudar o papel da personificação (gestor ↔ utilizador)
        auth.switch_company(tok, real, cid, data.get("as_role"))
    else:
        auth.switch_company(tok, person(request), cid, data.get("as_role"))
    return {"ok": True}


@app.post("/x/impersonate/stop")
def x_impersonate_stop(request: Request):
    auth.stop_impersonation(request.cookies.get(auth.COOKIE), real_person(request))
    return {"ok": True}


@app.post("/x/users")
def x_user_create(request: Request, data: dict = Body(...)):
    """Administrador: cria uma conta com palavra-passe automática (obrigatório trocar na 1.ª entrada)."""
    u = person(request)
    auth.require_admin(u)
    lvl = int(data.get("level") or 0)
    if lvl not in auth.LEVELS:
        raise HTTPException(400, "Tipo de conta inválido.")
    if lvl == auth.SUPER and not auth.is_super(u):
        raise HTTPException(403, "Só um superadministrador pode criar superadministradores.")
    cid = int(data.get("company_id") or 0)
    if cid and not db.one("SELECT 1 AS x FROM companies WHERE id=?", cid):
        raise HTTPException(400, "Empresa não encontrada.")
    if not cid and lvl < auth.SUPER:  # sem empresa a pessoa não tem onde entrar
        raise HTTPException(400, "Escolha a empresa a que esta pessoa vai ter acesso.")
    nu, pw = auth.create_user(data.get("email"), data.get("name"), lvl)
    if cid:
        auth.add_member(cid, nu["id"], data.get("role") if data.get("role") in auth.ROLES else "utilizador")
    return {"id": nu["id"], "email": nu["email"], "password": pw}


@app.post("/x/users/{uid}/password")
def x_user_password(request: Request, uid: int, data: dict = Body(...)):
    """Administrador: redefine a palavra-passe (automática); opcionalmente obriga a trocá-la na próxima entrada."""
    u = person(request)
    auth.require_admin(u)
    target = db.one("SELECT * FROM users WHERE id=?", uid)
    if not target:
        raise HTTPException(404, "Utilizador não encontrado.")
    if uid == u["id"]:
        raise HTTPException(400, "Para mudar a sua própria palavra-passe use «Alterar palavra-passe».")
    if auth.is_super(target) and not auth.is_super(u):
        raise HTTPException(403, "Só um superadministrador pode redefinir a palavra-passe de um superadministrador.")
    pw = auth.reset_password(uid, bool(data.get("must_change", True)))
    return {"email": target["email"], "password": pw, "must_change": bool(data.get("must_change", True))}


@app.post("/x/users/{uid}/admin")
def x_user_admin(request: Request, uid: int, data: dict = Body(...)):
    """Tipo de conta da plataforma: 0 utilizador, 1 administrador, 2 superadministrador."""
    u = person(request)
    auth.require_admin(u)
    if uid == u["id"]:
        raise HTTPException(400, "Não pode alterar o seu próprio acesso.")
    target = db.one("SELECT * FROM users WHERE id=?", uid)
    if not target:
        raise HTTPException(404, "Utilizador não encontrado.")
    lvl = data.get("level")
    lvl = int(lvl) if lvl is not None else (auth.ADMIN if data.get("admin") else 0)
    if lvl not in auth.LEVELS:
        raise HTTPException(400, "Tipo de conta inválido.")
    if (lvl == auth.SUPER or auth.is_super(target)) and not auth.is_super(u):
        raise HTTPException(403, "Só um superadministrador pode atribuir ou retirar o acesso de superadministrador.")
    db.run("UPDATE users SET is_admin=? WHERE id=?", lvl, uid)
    return {"ok": True}


@app.get("/utilizadores", response_class=HTMLResponse)
def users_page(request: Request):
    auth.require_admin(person(request))
    users = db.q("""SELECT u.id, u.email, u.name, u.is_admin, u.created_at, u.last_login, u.must_change, u.pw_changed_at,
                    u.password_hash IS NOT NULL AS has_pw, (SELECT GROUP_CONCAT(c.name || CASE m.role WHEN 'gestor' THEN ' (gestor)' ELSE '' END, ', ')
                    FROM members m JOIN companies c ON c.id=m.company_id WHERE m.user_id=u.id) AS empresas
                    FROM users u ORDER BY u.last_login IS NULL, u.last_login DESC, u.email""")
    return page(request, "users.html", users=users,
                all_companies=db.q("SELECT id, name FROM companies ORDER BY name COLLATE NOCASE"))


@app.get("/definicoes", response_class=HTMLResponse)
def settings_page(request: Request):
    return page(request, "settings.html", base_url=str(request.base_url).rstrip("/"))


@app.get("/health")
def health():
    return {"ok": True}
