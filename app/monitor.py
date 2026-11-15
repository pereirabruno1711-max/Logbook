"""Verificação noturna: calcula os últimos dias de cada motorista com a GPS e gera alertas para o gestor."""
import logging
from datetime import datetime, timedelta, timezone

from . import auth, config, db, logbook
from .providers import sync_devices
from .utils import from_iso, local_str, now_iso

log = logging.getLogger("monitor")
KINDS = {
    "por_assinar": "Dia com condução por assinar",
    "gps_erro": "Sem dados GPS",
    "conducao_excessiva": "Condução acima do limite diário",
    "sem_sinal": "Objeto GPS sem posição recente",
    "trabalho_esquecido": "«Trabalho» ligado há demasiado tempo",
    "sem_objeto": "Motorista sem objeto GPS atribuído",
}


def _fmt(m):
    return f"{int(m) // 60}h{int(m) % 60:02d}"


def evaluate(company, driver, day, today):
    """Lista de (tipo, detalhe) para um dia que ainda não está assinado."""
    r = logbook.compute_day(company, driver, day)
    out = []
    if not r["gps_ok"]:
        out.append(("gps_erro", r["gps_error"]))
    sm = r["summary"]
    if sm["driving"] > config.MAX_DRIVING_MIN:
        out.append(("conducao_excessiva", f"{_fmt(sm['driving'])} de condução (limite {_fmt(config.MAX_DRIVING_MIN)})"))
    age = (today - datetime.fromisoformat(day).date()).days
    if age >= config.UNSIGNED_ALERT_DAYS and (sm["driving"] > 0 or sm["working"] > 0):
        out.append(("por_assinar", f"{_fmt(sm['driving'])} de condução e {_fmt(sm['working'])} de trabalho sem assinatura"))
    return out


def check_driver(company, driver, today, days):
    found = []
    for k in range(1, days + 1):
        day = (today - timedelta(days=k)).isoformat()
        lb = logbook.get_logbook(company["id"], driver["id"], day)
        db.run("DELETE FROM alerts WHERE company_id=? AND driver_id=? AND day=? AND dismissed=0",
               company["id"], driver["id"], day)
        if lb and lb["status"] == "signed":
            continue
        try:
            for kind, detail in evaluate(company, driver, day, today):
                found.append((day, kind, detail))
        except Exception as e:  # noqa
            log.warning("verificação %s %s: %s", driver["name"], day, e)
    # situações atuais (associadas a hoje)
    t = today.isoformat()
    db.run("DELETE FROM alerts WHERE company_id=? AND driver_id=? AND day=? AND dismissed=0", company["id"], driver["id"], t)
    a = db.one("SELECT * FROM assignments WHERE driver_id=? AND to_ts IS NULL ORDER BY from_ts DESC LIMIT 1", driver["id"])
    if not a:
        found.append((t, "sem_objeto", "Atribua um objeto GPS para detetar a condução."))
    else:
        dev = db.one("SELECT last_time, name FROM devices WHERE company_id=? AND provider=? AND device_id=?",
                     company["id"], a["provider"], a["device_id"])
        if dev and dev["last_time"] and from_iso(dev["last_time"]) < datetime.now(timezone.utc) - timedelta(hours=config.OFFLINE_ALERT_HOURS):
            found.append((t, "sem_sinal", f"{dev['name']} sem posição desde {local_str(dev['last_time'], config.LOCAL_TZ)}"))
    st = logbook.current_state(driver["id"])
    if st["state"] == "working" and st["since"] and from_iso(st["since"]) < datetime.now(timezone.utc) - timedelta(hours=14):
        found.append((t, "trabalho_esquecido", "Estado «Trabalho» ligado há mais de 14 horas."))
    for day, kind, detail in found:
        db.run("INSERT OR IGNORE INTO alerts(company_id,driver_id,day,kind,detail,created_at) VALUES(?,?,?,?,?,?)",
               company["id"], driver["id"], day, kind, detail, now_iso())
    return len(found)


def run(company_id=None, days=None):
    """Verifica todas as empresas (ou uma). Devolve o número de alertas encontrados."""
    today = logbook.today_local()
    days = days or config.NIGHTLY_DAYS
    n = 0
    rows = db.q("SELECT id FROM companies" + (" WHERE id=?" if company_id else ""), *([company_id] if company_id else []))
    for r in rows:
        c = auth.company(r["id"])
        if c.get("gpswox_hash") or c.get("trusttrack_key"):
            try:
                sync_devices(c)       # posição atual dos objetos, para o alerta de «sem sinal»
                for dv in db.q("SELECT provider, device_id, odometer FROM devices WHERE company_id=? AND odometer IS NOT NULL", c["id"]):
                    # leitura do odómetro à noite = fim do dia anterior (e início do dia seguinte)
                    db.run("INSERT OR IGNORE INTO odometer_log(company_id,provider,device_id,snap_day,km,ts) VALUES(?,?,?,?,?,?)",
                           c["id"], dv["provider"], dv["device_id"], today.isoformat(), dv["odometer"], now_iso())
            except Exception:  # noqa
                log.exception("sync")
        for d in db.q("SELECT * FROM drivers WHERE company_id=? AND active=1", c["id"]):
            n += check_driver(c, d, today, days)
    db.run("DELETE FROM alerts WHERE day<?", (today - timedelta(days=60)).isoformat())
    log.info("verificação noturna concluída: %s alertas", n)
    return n
