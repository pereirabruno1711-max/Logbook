import os


def env(key, default=None):
    v = os.getenv(key)
    return v if v not in (None, "") else default


DATA_DIR = env("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "app.db")
PDF_DIR = os.path.join(DATA_DIR, "logbooks")

APP_NAME = env("APP_NAME", "Diário de Bordo")
BRAND_LOGO = env("BRAND_LOGO", "https://track.seemee.pt/images/logo-main.png")
BRAND_BG = env("BRAND_BG", "https://track.seemee.pt/images/background.jpg")
BRAND_COLOR = env("BRAND_COLOR", "#16a34a")

LOCAL_TZ = env("LOCAL_TZ", "Europe/Lisbon")           # fuso em que o dia do motorista começa e acaba

# Regras do diário (iguais à solução Logbook da FMS: períodos de 5 min, movimento = velocidade > 1 km/h)
PERIOD_MIN = int(env("LOGBOOK_PERIOD_MIN", "5"))
DRIVING_SPEED_KMH = float(env("LOGBOOK_DRIVING_SPEED", "1"))
TRIP_GAP_MIN = float(env("LOGBOOK_TRIP_GAP_MIN", "15"))   # paragem acima disto separa duas viagens
DAYS_BACK = int(env("LOGBOOK_DAYS_BACK", "31"))           # dias mostrados na app do motorista
GPS_CACHE_SEC = int(env("GPS_CACHE_SEC", "300"))

# GPSWox (servidor partilhado: cada empresa liga a sua conta)
GPSWOX_URL = (env("GPSWOX_URL", "") or "").rstrip("/")
GPSWOX_API_HASH = env("GPSWOX_API_HASH")
GPSWOX_TZ = env("GPSWOX_TZ", "Europe/Lisbon")
GPSWOX_RAW_TIME_UTC = env("GPSWOX_RAW_TIME_UTC", "true").lower() == "true"

# Trust Track (Ruptela) — opcional, por empresa
TRUSTTRACK_URL = (env("TRUSTTRACK_URL", "https://api.fm-track.com") or "").rstrip("/")
TRUSTTRACK_API_KEY = env("TRUSTTRACK_API_KEY")

# Acesso
APP_USER = env("APP_USER", "admin")                    # login local de emergência (superadministrador)
APP_PASSWORD = env("APP_PASSWORD")
API_TOKEN = env("API_TOKEN")
ADMIN_EMAILS = [e.strip().lower() for e in (env("ADMIN_EMAILS", "") or "").split(",") if e.strip()]
SESSION_DAYS = int(env("SESSION_DAYS", "30"))

# Automação
VEHICLE_SYNC_HOURS = float(env("VEHICLE_SYNC_HOURS", "24"))
GEOCODE_ENABLED = env("GEOCODE_ENABLED", "false").lower() == "true"   # nomes de moradas via OpenStreetMap

# Verificação noturna (calcula os últimos dias de cada motorista e gera alertas)
NIGHTLY_HOUR = int(env("NIGHTLY_HOUR", "3"))              # hora local a que corre
NIGHTLY_DAYS = int(env("NIGHTLY_DAYS", "3"))              # quantos dias para trás verifica
MAX_DRIVING_MIN = int(env("MAX_DRIVING_MIN", "540"))      # alerta acima de 9 h de condução/dia
UNSIGNED_ALERT_DAYS = int(env("UNSIGNED_ALERT_DAYS", "2"))  # alerta de dia por assinar a partir de N dias
OFFLINE_ALERT_HOURS = int(env("OFFLINE_ALERT_HOURS", "48"))  # objeto sem posição há mais de N horas

# Odómetro lido da plataforma GPS: 1 se vem em km, 1000 se vem em metros
ODOMETER_DIVISOR = float(env("ODOMETER_DIVISOR", "1"))
# Qual o odómetro a usar quando a plataforma tem dois: «equipamento» (odómetro enviado pelo tracker/CAN)
# ou «gps» (distância total acumulada pelo GPS desde a instalação)
ODOMETER_SOURCE = env("ODOMETER_SOURCE", "equipamento").lower()

# Limites mostrados ao motorista (definidos pela empresa; confirme com a legislação aplicável)
LIMIT_CONT_WORK_MIN = int(env("LIMIT_CONT_WORK_MIN", "360"))   # horas contínuas em trabalho (6 h)
LIMIT_DAY_WORK_MIN = int(env("LIMIT_DAY_WORK_MIN", "600"))     # horas trabalhadas por dia (10 h)
BREAK_MIN = int(env("BREAK_MIN", "30"))                        # pausa mínima que interrompe o trabalho contínuo
JOURNEY_MAX_HOURS = int(env("JOURNEY_MAX_HOURS", "24"))        # jornada esquecida aberta deixa de contar
