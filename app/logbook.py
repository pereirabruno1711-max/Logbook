"""Lógica do diário de bordo (igual à solução Logbook da FMS).

O dia é dividido em períodos de 5 min:
  1. se há pelo menos um ponto do objeto atribuído ao motorista com velocidade > 1 km/h -> CONDUÇÃO;
  2. senão, se o motorista indicou «Trabalho» na app nesse período -> TRABALHO;
  3. senão -> DESCANSO.
O motorista só pode corrigir períodos que não sejam de condução (a condução é detetada automaticamente).
"""
import base64
import json
import math
import re
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from . import config, db
from .providers import HistoryCache
from .utils import from_iso, iso, now_iso

TZ = ZoneInfo(config.LOCAL_TZ)
UTC = timezone.utc
STEP = timedelta(minutes=config.PERIOD_MIN)
STATE_LABEL = {"driving": "Condução", "working": "Trabalho", "resting": "Descanso"}
STATUS_LABEL = {"none": "Sem relatório", "filling": "Em preenchimento", "signed": "Assinado"}
EDITABLE_STATES = ("working", "resting")
WORK_CARRY_HOURS = 16          # «Trabalho» esquecido deixa de contar passadas estas horas
HM = re.compile(r"^([01]?\d|2[0-4]):([0-5]\d)$")


class GpsError(Exception):
    pass


class LogbookError(Exception):
    """Erro de validação mostrado ao motorista."""


# ---------- tempo ----------
def today_local():
    return datetime.now(TZ).date()


def parse_day(day):
    try:
        return date.fromisoformat(day)
    except (TypeError, ValueError):
        raise LogbookError("Data inválida.")


def day_bounds(day):
    d = parse_day(day)
    s = datetime.combine(d, dtime.min, tzinfo=TZ)
    e = datetime.combine(d + timedelta(days=1), dtime.min, tzinfo=TZ)
    return s.astimezone(UTC), e.astimezone(UTC)


def hm_minutes(s):
    m = HM.match(s or "")
    if not m:
        raise LogbookError("Hora inválida (use HH:MM).")
    v = int(m.group(1)) * 60 + int(m.group(2))
    if v > 1440:
        raise LogbookError("Hora inválida (use HH:MM).")
    return v


def hm_label(dt, day_end=None):
    if day_end is not None and dt >= day_end:
        return "24:00"
    return dt.astimezone(TZ).strftime("%H:%M")


def fmt_dur(minutes):
    minutes = int(round(minutes or 0))
    return f"{minutes // 60}h{minutes % 60:02d}"


# ---------- GPS ----------
def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


_cache = {}


def fetch_points(company, provider, device_id, start, end):
    """Pontos GPS do objeto entre start e end (UTC), com cache curta. Levanta GpsError se a plataforma falhar."""
    key = (company["id"], provider, device_id, start.isoformat(), end.isoformat())
    hit = _cache.get(key)
    ttl = 3600 if end < datetime.now(UTC) - timedelta(hours=2) else config.GPS_CACHE_SEC
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        pts = HistoryCache(company).points(provider, device_id, start, end)
    except Exception as e:  # noqa
        raise GpsError(f"Não foi possível ler o histórico GPS ({e.__class__.__name__}).")
    pts = [p for p in pts if start <= p.ts < end]
    if len(_cache) > 400:
        _cache.clear()
    _cache[key] = (time.time(), pts)
    return pts


def clear_cache():
    _cache.clear()


def geocode(lat, lon):
    if not config.GEOCODE_ENABLED or lat is None:
        return None
    k = f"{lat:.4f},{lon:.4f}"
    hit = db.one("SELECT name FROM geocache WHERE k=?", k)
    if hit:
        return hit["name"]
    try:
        r = httpx.get("https://nominatim.openstreetmap.org/reverse", timeout=6,
                      params={"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 16, "accept-language": "pt"},
                      headers={"User-Agent": "diario-de-bordo/1.0"})
        name = (r.json() or {}).get("display_name")
    except Exception:  # noqa
        return None
    if name:
        name = ", ".join(name.split(", ")[:4])
        db.run("INSERT OR REPLACE INTO geocache(k,name) VALUES(?,?)", k, name)
    return name


# ---------- dados guardados ----------
def get_logbook(company_id, driver_id, day):
    return db.one("SELECT * FROM logbooks WHERE company_id=? AND driver_id=? AND day=?", company_id, driver_id, day)


def lb_data(row):
    try:
        return json.loads(row["data"]) if row and row.get("data") else {}
    except ValueError:
        return {}


def assignments_for(driver_id, s, e):
    out = []
    for a in db.q("SELECT * FROM assignments WHERE driver_id=? ORDER BY from_ts", driver_id):
        a0 = from_iso(a["from_ts"])
        a1 = from_iso(a["to_ts"]) if a["to_ts"] else None
        if a0 < e and (a1 is None or a1 > s):
            out.append(dict(a, w0=max(a0, s), w1=min(a1 or e, e)))
    return out


def work_intervals(driver_id, s, e):
    """Intervalos em que o motorista indicou «Trabalho» e que tocam [s, e)."""
    prev = db.one("SELECT ts, state FROM driver_events WHERE driver_id=? AND ts<? ORDER BY ts DESC LIMIT 1", driver_id, s.isoformat())
    evs = db.q("SELECT ts, state FROM driver_events WHERE driver_id=? AND ts>=? AND ts<? ORDER BY ts", driver_id,
               s.isoformat(), e.isoformat())
    seq = ([prev] if prev else []) + evs
    out = []
    for i, ev in enumerate(seq):
        if ev["state"] != "working":
            continue
        a = from_iso(ev["ts"])
        b = from_iso(seq[i + 1]["ts"]) if i + 1 < len(seq) else e
        b = min(b, a + timedelta(hours=WORK_CARRY_HOURS), e)
        a = max(a, s)
        if b > a:
            out.append((a, b))
    return out


def current_state(driver_id):
    ev = db.one("SELECT ts, state FROM driver_events WHERE driver_id=? ORDER BY ts DESC LIMIT 1", driver_id)
    if not ev or ev["state"] != "working":
        return {"state": "resting", "since": ev["ts"] if ev else None}
    if from_iso(ev["ts"]) < datetime.now(UTC) - timedelta(hours=WORK_CARRY_HOURS):
        return {"state": "resting", "since": None}
    return {"state": "working", "since": ev["ts"]}


def set_state(driver_id, state):
    if state not in EDITABLE_STATES:
        raise LogbookError("Estado inválido.")
    if current_state(driver_id)["state"] != state:
        db.run("INSERT INTO driver_events(driver_id,ts,state) VALUES(?,?,?)", driver_id, now_iso(), state)
    return current_state(driver_id)


# ---------- validação das correções ----------
def clean_edits(edits):
    out = []
    for ed in (edits or [])[:30]:
        a, b = hm_minutes(ed.get("from")), hm_minutes(ed.get("to"))
        if a >= b:
            raise LogbookError("A hora final tem de ser depois da inicial.")
        if ed.get("state") not in EDITABLE_STATES:
            raise LogbookError("Só pode escolher Trabalho ou Descanso (a condução é detetada automaticamente).")
        out.append({"from": f"{a // 60:02d}:{a % 60:02d}", "to": f"{b // 60:02d}:{b % 60:02d}" if b < 1440 else "24:00",
                    "state": ed["state"]})
    return out


def clean_vehicle(v):
    v = v or {}
    out = {}
    for k, n in (("plate", 20), ("odo_start", 12), ("odo_end", 12), ("notes", 400)):
        if k in v:
            out[k] = str(v[k] or "").strip()[:n]
    if "confirmed" in v:
        out["confirmed"] = bool(v["confirmed"])
    for k in ("odo_start", "odo_end"):
        if out.get(k):
            if not re.fullmatch(r"\d{1,9}([.,]\d)?", out[k]):
                raise LogbookError("O conta-quilómetros tem de ser um número.")
    if out.get("odo_start") and out.get("odo_end"):
        if float(out["odo_end"].replace(",", ".")) < float(out["odo_start"].replace(",", ".")):
            raise LogbookError("O conta-quilómetros final não pode ser inferior ao inicial.")
    return out


# ---------- cálculo do dia ----------
def _moving(p):
    return p.speed is not None and p.speed > config.DRIVING_SPEED_KMH


def _path_km(pts):
    km = 0.0
    for a, b in zip(pts, pts[1:]):
        dt = (b.ts - a.ts).total_seconds()
        if dt <= 0 or dt > 1800 or not (_moving(a) or _moving(b)):
            continue
        d = haversine_km(a.lat, a.lon, b.lat, b.lon)
        if d / (dt / 3600) > 250:   # salto de GPS
            continue
        km += d
    return km


def _name_or_coords(lat, lon):
    return geocode(lat, lon) or f"{lat:.5f}, {lon:.5f}"


def compute_day(company, driver, day, data=None, now=None):
    """Estados do dia (da GPS + app + correções do motorista), resumo, viagens e dados da viatura."""
    s, e = day_bounds(day)
    now = now or datetime.now(UTC)
    data = data or {}
    n = int((e - s) / STEP)
    valid = max(0, min(n, math.ceil((now - s) / STEP)))
    is_today = s <= now < e
    states = ["resting"] * n

    # trabalho indicado na app
    for a, b in work_intervals(driver["id"], s, e):
        i0 = int((a - s) / STEP)
        i1 = min(n, math.ceil((b - s) / STEP))
        for i in range(i0, i1):
            states[i] = "working"

    # condução detetada pelo GPS do objeto atribuído
    gps_error, asg, pts_all, per_asg = None, assignments_for(driver["id"], s, e), [], {}
    for a in asg:
        try:
            pts = fetch_points(company, a["provider"], a["device_id"], a["w0"], a["w1"])
        except GpsError as ex:
            gps_error = str(ex)
            continue
        per_asg[a["id"]] = pts
        pts_all.extend(pts)
        for p in pts:
            if _moving(p):
                i = int((p.ts - s) / STEP)
                if 0 <= i < n:
                    states[i] = "driving"
    pts_all.sort(key=lambda p: p.ts)

    # correções do motorista (nunca em períodos de condução)
    edits = data.get("edits") or []
    for ed in edits:
        a, b = hm_minutes(ed["from"]), hm_minutes(ed["to"])
        for i in range(n):
            if states[i] == "driving":
                continue
            m = (s + i * STEP).astimezone(TZ)
            mm = m.hour * 60 + m.minute
            if a <= mm < b:
                states[i] = ed["state"]

    for i in range(valid, n):
        states[i] = "future"

    # blocos contíguos
    segments, i = [], 0
    while i < valid:
        j = i
        while j + 1 < valid and states[j + 1] == states[i]:
            j += 1
        st = states[i]
        segments.append({"state": st, "label": STATE_LABEL[st], "start": hm_label(s + i * STEP),
                         "end": hm_label(s + (j + 1) * STEP, e), "minutes": (j - i + 1) * config.PERIOD_MIN,
                         "locked": st == "driving"})
        i = j + 1

    mins = {k: sum(1 for x in states if x == k) * config.PERIOD_MIN for k in ("driving", "working", "resting")}
    km = sum(_path_km(p) for p in per_asg.values())
    summary = dict(mins, total=sum(mins.values()), km=round(km, 1))

    # viagens: blocos de condução separados por paragens longas
    trips, cur = [], None
    gap = max(1, int(round(config.TRIP_GAP_MIN / config.PERIOD_MIN)))
    for i in range(n):
        if states[i] != "driving":
            continue
        if cur and i - cur[1] <= gap:
            cur[1] = i + 1
        else:
            cur = [i, i + 1]
            trips.append(cur)
    notes = data.get("trip_notes") or {}
    out_trips = []
    for k, (i0, i1) in enumerate(trips):
        t0, t1 = s + i0 * STEP, s + i1 * STEP
        seg = [p for p in pts_all if t0 - STEP <= p.ts <= t1 + STEP]
        t = {"n": k + 1, "start": hm_label(t0), "end": hm_label(t1, e), "minutes": (i1 - i0) * config.PERIOD_MIN,
             "km": round(_path_km(seg), 1), "max_kmh": round(max([p.speed or 0 for p in seg] or [0])),
             "note": notes.get(str(k + 1), "")}
        if seg:
            t["from"] = {"lat": seg[0].lat, "lon": seg[0].lon}
            t["to"] = {"lat": seg[-1].lat, "lon": seg[-1].lon}
        out_trips.append(t)

    # viatura: a do período com mais pontos em movimento (senão a atual)
    veh_auto = None
    if asg:
        best = max(asg, key=lambda a: (sum(1 for p in per_asg.get(a["id"], []) if _moving(p)), a["from_ts"]))
        veh_auto = {"plate": best.get("plate") or "", "name": best.get("device_name") or ""}
    v = dict(data.get("vehicle") or {})
    vehicle = {"plate": v.get("plate") or (veh_auto or {}).get("plate") or "", "name": (veh_auto or {}).get("name", ""),
               "odo_start": v.get("odo_start", ""), "odo_end": v.get("odo_end", ""), "notes": v.get("notes", ""),
               "confirmed": bool(v.get("confirmed")), "auto": veh_auto is not None}

    prev = db.one("SELECT data FROM logbooks WHERE driver_id=? AND status='signed' AND day<? ORDER BY day DESC LIMIT 1",
                  driver["id"], day)
    prev_end = ""
    if prev:
        try:
            prev_end = (json.loads(prev["data"]).get("vehicle") or {}).get("odo_end", "") or ""
        except ValueError:
            pass
    vehicle["odo_start_hint"], vehicle["odo_end_hint"], vehicle["odo_src"] = prev_end, "", ("previous" if prev_end else "")
    if asg:  # leituras noturnas do odómetro da plataforma GPS (início = leitura desse dia, fim = leitura do dia seguinte)
        b = best
        nxt = (parse_day(day) + timedelta(days=1)).isoformat()
        r0, r1 = (db.one("SELECT km FROM odometer_log WHERE company_id=? AND provider=? AND device_id=? AND snap_day=?",
                         company["id"], b["provider"], b["device_id"], dd) for dd in (day, nxt))
        if r0:
            vehicle["odo_start_hint"], vehicle["odo_src"] = str(round(r0["km"])), "gps"
        if r1:
            vehicle["odo_end_hint"] = str(round(r1["km"]))
            vehicle["odo_src"] = "gps"
        if not (r0 and r1):
            _odo_from_history(company, b, per_asg.get(b["id"], []), s, e, summary["km"], vehicle)
    vehicle["km_gps"] = summary["km"]             # a app soma isto ao inicial para sugerir o final

    bar = "".join({"driving": "d", "working": "w", "resting": "r", "future": "-"}[x] for x in states)
    return {"day": day, "driver": {"id": driver["id"], "name": driver["name"]}, "is_today": is_today,
            "past": e <= now, "gps_ok": gps_error is None, "gps_error": gps_error, "has_vehicle": bool(asg),
            "bar": bar, "period_min": config.PERIOD_MIN, "segments": segments, "summary": summary, "trips": out_trips,
            "vehicle": vehicle, "edits": edits}


def _odo_from_history(company, a, pts, s, e, day_km, vehicle):
    """Dias passados sem leitura noturna: (1) odómetro que a plataforma põe nos pontos do histórico; senão
    (2) estimativa para trás = odómetro atual do objeto - distância GPS percorrida desde o fim desse dia."""
    with_odo = [p for p in pts if p.odo]
    if with_odo and not vehicle["odo_start_hint"] or with_odo and vehicle["odo_src"] != "gps":
        vehicle["odo_start_hint"], vehicle["odo_end_hint"] = str(round(with_odo[0].odo)), str(round(with_odo[-1].odo))
        vehicle["odo_src"] = "gps"
        return
    now = datetime.now(UTC)
    if e > now or now - e > timedelta(days=config.DAYS_BACK):
        return
    dev = db.one("SELECT odometer FROM devices WHERE company_id=? AND provider=? AND device_id=?",
                 company["id"], a["provider"], a["device_id"])
    if not dev or not dev["odometer"]:
        return
    try:
        later = fetch_points(company, a["provider"], a["device_id"], e, now)
    except GpsError:
        return
    end = dev["odometer"] - _path_km(later)
    vehicle["odo_end_hint"], vehicle["odo_start_hint"] = str(round(end)), str(round(end - day_km))
    vehicle["odo_src"] = "estimate"


# ---------- fluxo do relatório ----------
def view_day(company, driver, day):
    """O que a app mostra para um dia: relatório assinado (instantâneo) ou cálculo atual."""
    lb = get_logbook(company["id"], driver["id"], day)
    if lb and lb["status"] == "signed" and lb["snapshot"]:
        snap = json.loads(lb["snapshot"])
        snap.update(status="signed", signed_at=lb["signed_at"], editable=False, logbook_id=lb["id"])
        return snap
    out = compute_day(company, driver, day, lb_data(lb))
    data = lb_data(lb)
    out.update(status=lb["status"] if lb else "none", editable=out["past"], step=data.get("step", 1),
               logbook_id=lb["id"] if lb else None)
    return out


def save_data(company, driver, day, patch):
    s, e = day_bounds(day)
    if s > datetime.now(UTC):
        raise LogbookError("Não pode preencher um dia futuro.")
    lb = get_logbook(company["id"], driver["id"], day)
    if lb and lb["status"] == "signed":
        raise LogbookError("Este relatório já está assinado e não pode ser alterado.")
    data = lb_data(lb)
    if "edits" in patch:
        data["edits"] = clean_edits(patch["edits"])
    if "vehicle" in patch:
        data["vehicle"] = {**(data.get("vehicle") or {}), **clean_vehicle(patch["vehicle"])}
    if "trip_notes" in patch:
        data["trip_notes"] = {str(k): str(v or "")[:200] for k, v in (patch["trip_notes"] or {}).items()}
    if "step" in patch:
        data["step"] = max(1, min(3, int(patch["step"] or 1)))
    if lb:
        db.run("UPDATE logbooks SET data=?, updated_at=? WHERE id=?", json.dumps(data), now_iso(), lb["id"])
    else:
        db.run("INSERT INTO logbooks(company_id,driver_id,day,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
               company["id"], driver["id"], day, "filling", json.dumps(data), now_iso(), now_iso())
    return view_day(company, driver, day)


def sign(company, driver, day, signature, ip=None):
    from . import pdfgen
    s, e = day_bounds(day)
    if e > datetime.now(UTC):
        raise LogbookError("Só pode assinar um dia depois de terminar.")
    lb = get_logbook(company["id"], driver["id"], day)
    if lb and lb["status"] == "signed":
        raise LogbookError("Este relatório já está assinado.")
    m = re.match(r"^data:image/png;base64,([A-Za-z0-9+/=]+)$", signature or "")
    if not m or len(m.group(1)) < 600:
        raise LogbookError("Assine no quadrado antes de concluir.")
    try:
        base64.b64decode(m.group(1), validate=True)
    except ValueError:
        raise LogbookError("Assinatura inválida.")
    data = lb_data(lb)
    snap = compute_day(company, driver, day, data)
    if not snap["gps_ok"]:
        raise LogbookError("O histórico GPS não está disponível neste momento. Tente assinar mais tarde.")
    if not (data.get("vehicle") or {}).get("confirmed"):
        raise LogbookError("Confirme os dados da viatura e das viagens antes de assinar.")
    for t in snap["trips"]:
        if t.get("from"):
            t["from"]["name"] = _name_or_coords(t["from"]["lat"], t["from"]["lon"])
            t["to"]["name"] = _name_or_coords(t["to"]["lat"], t["to"]["lon"])
    signed_at = now_iso()
    snap.update(company={"id": company["id"], "name": company["name"], "nif": company.get("nif")},
                signed_at=signed_at, signed_name=driver["name"])
    pdf = pdfgen.render(snap, signature, preview=False)
    path = f"{config.PDF_DIR}/{company['id']}/{day}-{driver['id']}.pdf"
    import hashlib
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(pdf)
    digest = hashlib.sha256(pdf).hexdigest()
    vals = ("signed", json.dumps(data), signed_at, signature, json.dumps(snap), path, digest, ip, signed_at)
    if lb:
        db.run("""UPDATE logbooks SET status=?, data=?, signed_at=?, signature=?, snapshot=?, pdf_path=?, pdf_sha256=?,
                  signed_ip=?, updated_at=? WHERE id=?""", *vals, lb["id"])
    else:
        db.run("""INSERT INTO logbooks(status,data,signed_at,signature,snapshot,pdf_path,pdf_sha256,signed_ip,updated_at,
                  company_id,driver_id,day,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               *vals, company["id"], driver["id"], day, now_iso())
    return view_day(company, driver, day)


def preview_pdf(company, driver, day):
    """PDF de pré-visualização (sem assinatura) ou o PDF assinado, se já existir."""
    from . import pdfgen
    lb = get_logbook(company["id"], driver["id"], day)
    if lb and lb["status"] == "signed" and lb["pdf_path"]:
        with open(lb["pdf_path"], "rb") as f:
            return f.read()
    snap = compute_day(company, driver, day, lb_data(lb))
    snap.update(company={"id": company["id"], "name": company["name"], "nif": company.get("nif")}, signed_name=driver["name"])
    return pdfgen.render(snap, None, preview=True)


def day_list(company, driver, days=None):
    """Lista de dias para a app: estado do relatório de cada dia (mais recente primeiro)."""
    days = days or config.DAYS_BACK
    today = today_local()
    rows = {r["day"]: r for r in db.q("SELECT day, status, signed_at FROM logbooks WHERE company_id=? AND driver_id=? AND day>=?",
                                      company["id"], driver["id"], (today - timedelta(days=days)).isoformat())}
    out = []
    for k in range(days + 1):
        d = (today - timedelta(days=k)).isoformat()
        r = rows.get(d)
        out.append({"day": d, "status": r["status"] if r else "none", "today": k == 0, "can_sign": k > 0})
    return out
