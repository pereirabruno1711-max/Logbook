"""Clientes GPSWox e Trust Track (Ruptela). Cada empresa usa as suas próprias credenciais."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from dateutil import parser as dtparser

from . import config
from .utils import guess_plate, norm_plate


@dataclass
class Point:
    ts: datetime  # UTC
    lat: float
    lon: float
    speed: float | None = None


@dataclass
class Device:
    provider: str
    device_id: str
    name: str
    plate_guess: str | None
    online: str | None = None      # online / offline / sem sinal
    last_time: str | None = None   # ISO UTC da última posição
    lat: float | None = None
    lon: float | None = None


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ts(v):
    try:
        v = float(v)
        return datetime.fromtimestamp(v, timezone.utc).isoformat() if v > 0 else None
    except (TypeError, ValueError):
        return None


def _status(v):
    v = (str(v) if v is not None else "").lower()
    if v in ("online", "engine", "ack", "true", "1", "moving", "stopped"):
        return "online"
    return "offline" if v else None


class AuthError(Exception):
    pass


def gpswox_login(email, password):
    """Valida as credenciais na GPSWox e devolve o user_api_hash."""
    if not config.GPSWOX_URL:
        raise AuthError("Servidor Track não configurado (GPSWOX_URL).")
    try:
        r = httpx.post(f"{config.GPSWOX_URL}/api/login", data={"email": email, "password": password}, timeout=20)
    except httpx.HTTPError as e:
        raise AuthError(f"Não foi possível contactar o servidor GPS: {e}")
    try:
        data = r.json()
    except ValueError:
        data = {}
    h = data.get("user_api_hash")
    if r.status_code >= 400 or not h:
        raise AuthError("Email ou palavra-passe incorretos.")
    return h


class GPSWox:
    name = "gpswox"

    def __init__(self, api_hash):
        self.base = config.GPSWOX_URL
        self._hash = api_hash
        self.http = httpx.Client(timeout=60, follow_redirects=True)

    @property
    def enabled(self):
        return bool(self.base and self._hash)

    def _get(self, path, **params):
        params.update(user_api_hash=self._hash, lang="en")
        r = self.http.get(f"{self.base}/api/{path}", params=params)
        r.raise_for_status()
        return r.json()

    def devices(self):
        data = self._get("get_devices")
        out = {}
        for d in _walk(data):
            if "id" in d and "name" in d and ("device_data" in d or "imei" in d or "lat" in d):
                dd = d.get("device_data") or {}
                plate = guess_plate(dd.get("plate_number"), dd.get("registration_number"),
                                    d.get("plate_number"), d.get("name"), dd.get("object_owner"))
                out[str(d["id"])] = Device(self.name, str(d["id"]), d["name"], plate,
                                           _status(d.get("online")), _ts(d.get("timestamp")),
                                           _f(d.get("lat")), _f(d.get("lng")))
        return list(out.values())

    def _parse_time(self, p):
        if config.GPSWOX_RAW_TIME_UTC and p.get("raw_time"):
            return dtparser.parse(p["raw_time"]).replace(tzinfo=timezone.utc)
        t = p.get("time") or p.get("raw_time")
        dt = dtparser.parse(t, dayfirst=True)
        return dt.replace(tzinfo=ZoneInfo(config.GPSWOX_TZ)).astimezone(timezone.utc) if dt.tzinfo is None else dt

    def history(self, device_id, start, end):
        tz = ZoneInfo(config.GPSWOX_TZ)
        s, e = start.astimezone(tz), end.astimezone(tz)
        data = self._get("get_history", device_id=device_id,
                         from_date=s.strftime("%Y-%m-%d"), from_time=s.strftime("%H:%M"),
                         to_date=e.strftime("%Y-%m-%d"), to_time=e.strftime("%H:%M"))
        pts = {}
        for p in _walk(data):
            if "lat" in p and "lng" in p and ("raw_time" in p or "time" in p):
                try:
                    lat, lon = float(p["lat"]), float(p["lng"])
                    if lat == 0 and lon == 0:
                        continue
                    ts = self._parse_time(p)
                    pts[ts] = Point(ts, lat, lon, _f(p.get("speed")))
                except (ValueError, TypeError):
                    continue
        return sorted(pts.values(), key=lambda x: x.ts)


class TrustTrack:
    name = "trusttrack"

    def __init__(self, key):
        self.base = config.TRUSTTRACK_URL
        self.key = key
        self.http = httpx.Client(timeout=60, follow_redirects=True)

    @property
    def enabled(self):
        return bool(self.key)

    def _get(self, path, **params):
        params.update(version=1, api_key=self.key)
        r = self.http.get(f"{self.base}/{path}", params=params)
        r.raise_for_status()
        return r.json()

    def devices(self):
        data = self._get("objects")
        items = data if isinstance(data, list) else data.get("items", data.get("objects", []))
        out = []
        for o in items:
            vp = o.get("vehicle_params") or {}
            plate = guess_plate(vp.get("license_plate"), vp.get("plate_number"), o.get("license_plate"), o.get("name"))
            lc = o.get("last_coordinate") or {}
            t = lc.get("datetime")
            out.append(Device(self.name, str(o.get("id")), o.get("name") or str(o.get("id")), plate,
                              None, t, _f(lc.get("latitude")), _f(lc.get("longitude"))))
        return out

    def history(self, device_id, start, end):
        pts, token = {}, None
        for _ in range(200):
            params = dict(fromDatetime=start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                          toDatetime=end.strftime("%Y-%m-%dT%H:%M:%S.000Z"), limit=1000)
            if token:
                params["continuation_token"] = token
            data = self._get(f"objects/{device_id}/coordinates", **params)
            items = data.get("items", []) if isinstance(data, dict) else data
            for it in items:
                pos = it.get("position") or it
                try:
                    lat, lon = float(pos["latitude"]), float(pos["longitude"])
                    ts = dtparser.isoparse(it.get("datetime") or it.get("gnss_time") or it["time"])
                    ts = (ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts).astimezone(timezone.utc)
                    if lat == 0 and lon == 0:
                        continue
                    pts[ts] = Point(ts, lat, lon, _f(pos.get("speed")))
                except (KeyError, ValueError, TypeError):
                    continue
            token = data.get("continuation_token") if isinstance(data, dict) else None
            if not token or not items:
                break
        return sorted(pts.values(), key=lambda x: x.ts)


def clients_for(user):
    return {"gpswox": GPSWox(user.get("gpswox_hash")),
            "trusttrack": TrustTrack(user.get("trusttrack_key") or (config.TRUSTTRACK_API_KEY if user.get("is_admin") else None))}


class HistoryCache:
    """Histórico por blocos de um dia, guardado em memória durante uma verificação."""

    def __init__(self, user):
        self.clients = clients_for(user)
        self.cache = {}

    def points(self, provider, device_id, start, end):
        prov = self.clients[provider]
        day = start.astimezone(timezone.utc).date()
        out = {}
        while day <= end.astimezone(timezone.utc).date():
            key = (provider, device_id, day)
            if key not in self.cache:
                d0 = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) - timedelta(hours=1)
                self.cache[key] = prov.history(device_id, d0, d0 + timedelta(hours=26))
            for p in self.cache[key]:
                out[p.ts] = p
            day += timedelta(days=1)
        return sorted(out.values(), key=lambda x: x.ts)


def sync_devices(company):
    """Lê os objetos das plataformas GPS da empresa e guarda-os (para associar a motoristas)."""
    from . import db
    from .utils import now_iso
    report = []
    for prov in clients_for(company).values():
        if not prov.enabled:
            continue
        try:
            devs = prov.devices()
        except Exception as e:  # noqa
            report.append(f"{prov.name}: erro {e}")
            continue
        for d in devs:
            db.run("""INSERT INTO devices(company_id,provider,device_id,name,plate,updated_at,online,last_time,lat,lon)
                      VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(company_id,provider,device_id) DO UPDATE SET
                      name=excluded.name, plate=COALESCE(excluded.plate, devices.plate), updated_at=excluded.updated_at,
                      online=excluded.online, last_time=excluded.last_time, lat=excluded.lat, lon=excluded.lon""",
                   company["id"], d.provider, d.device_id, d.name, norm_plate(d.plate_guess) if d.plate_guess else None,
                   now_iso(), d.online, d.last_time, d.lat, d.lon)
        report.append(f"{ {'gpswox': 'Track', 'trusttrack': 'Trust Track'}.get(prov.name, prov.name)}: {len(devs)} objetos")
    return report
