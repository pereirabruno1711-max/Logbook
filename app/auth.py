"""Sessões e empresas.

- As contas são criadas nesta plataforma (por um administrador, ou pelo gestor ao adicionar alguém à empresa),
  com uma palavra-passe automática que tem de ser trocada na primeira entrada.
- Contas antigas (de quando se entrava com a conta GPSWox) ainda sem palavra-passe própria entram uma última vez
  com a conta GPSWox e são obrigadas a definir a palavra-passe desta plataforma.
- Os dados (viaturas, auditorias, passagens) pertencem a uma EMPRESA; owner_id = número da empresa.
- Uma pessoa pode pertencer a várias empresas (papel 'gestor' ou 'utilizador') e muda de empresa no topo.
- Contas da plataforma (users.is_admin):
    2 = superadministrador: acesso a tudo, incluindo entrar (personificar) em qualquer empresa;
    1 = administrador: acesso a tudo exceto personificação (só entra nas empresas de que é membro);
    0 = utilizador normal.
"""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

from . import config, db
from .providers import AuthError, gpswox_login
from .utils import now_iso

COOKIE = "lbsess"
ROLES = ("gestor", "utilizador")
ADMIN, SUPER = 1, 2
LEVELS = {0: "Utilizador", ADMIN: "Administrador", SUPER: "Superadministrador"}


def level(user):
    return int((user or {}).get("is_admin") or 0)


def is_super(user):
    return level(user) >= SUPER


def is_member(user, company_id):
    return bool(user and db.one("SELECT 1 AS x FROM members WHERE company_id=? AND user_id=?", company_id, user["id"]))


def can_enter(user, company_id):
    """Pode tornar esta empresa ativa? Membros sim; sem ser membro, só o superadministrador (personificação)."""
    return bool(company_id) and (is_member(user, company_id) or
                                 (is_super(user) and db.one("SELECT 1 AS x FROM companies WHERE id=?", company_id)))


# ---------- empresas ----------
def company(cid):
    c = db.one("SELECT * FROM companies WHERE id=?", cid) if cid else None
    if c:
        # a empresa da conta GPS configurada no servidor pode usar a chave Trust Track do servidor
        c["is_admin"] = 1 if (c.get("gpswox_hash") and c["gpswox_hash"] == config.GPSWOX_API_HASH) else 0
    return c


def default_company():
    c = db.one("SELECT id FROM companies WHERE gpswox_hash=? ORDER BY id LIMIT 1", config.GPSWOX_API_HASH) \
        if config.GPSWOX_API_HASH else None
    c = c or db.one("""SELECT m.company_id id FROM members m JOIN users u ON u.id=m.user_id
                       WHERE u.is_admin>=1 ORDER BY m.company_id LIMIT 1""") or \
        db.one("SELECT id FROM companies ORDER BY id LIMIT 1")
    return company(c["id"]) if c else None


def create_company(name, gpswox_hash=None, nif=None):
    return db.run("INSERT INTO companies(name,nif,gpswox_hash,api_token,created_at) VALUES(?,?,?,?,?)",
                  (name or "").strip() or "Nova empresa", nif or None, gpswox_hash, secrets.token_urlsafe(24), now_iso())


def add_member(company_id, user_id, role="utilizador"):
    db.run("""INSERT INTO members(company_id,user_id,role,created_at) VALUES(?,?,?,?)
              ON CONFLICT(company_id,user_id) DO UPDATE SET role=excluded.role""",
           company_id, user_id, role if role in ROLES else "utilizador", now_iso())


def companies_of(user):
    """Empresas a que a pessoa tem acesso (administrador: todas; em personificação: só a empresa personificada)."""
    if user.get("_imp"):
        c = company(user["_imp"]["cid"])
        return [dict(c, role=user["_imp"]["role"], member=0)] if c else []
    if user.get("is_admin"):
        rows = db.q("""SELECT c.*, COALESCE(m.role, 'gestor') role, m.user_id IS NOT NULL AS member FROM companies c
                       LEFT JOIN members m ON m.company_id=c.id AND m.user_id=? ORDER BY c.name COLLATE NOCASE""",
                    user["id"])
    else:
        rows = db.q("""SELECT c.*, m.role, 1 AS member FROM companies c JOIN members m ON m.company_id=c.id
                       WHERE m.user_id=? ORDER BY c.name COLLATE NOCASE""", user["id"])
    return rows


def role_in(user, company_id):
    if not user:
        return None
    if user.get("_imp"):  # personificação: só a empresa personificada, com o papel escolhido
        return user["_imp"]["role"] if user["_imp"]["cid"] == company_id else None
    m = db.one("SELECT role FROM members WHERE company_id=? AND user_id=?", company_id, user["id"])
    if m:
        return m["role"]
    return "gestor" if user.get("is_admin") else None


def can_manage(user, company_id):
    return role_in(user, company_id) == "gestor"


def require_manager(user, company_id):
    if not can_manage(user, company_id):
        raise HTTPException(403, "Só o gestor da empresa pode fazer isto.")


# ---------- pessoas ----------
def _upsert_user(email, gpswox_hash, name=None):
    email = (email or "").strip().lower()
    u = db.one("SELECT * FROM users WHERE gpswox_hash=?", gpswox_hash) if gpswox_hash else None
    if not u:
        u = db.one("SELECT * FROM users WHERE email=?", email)
    admin = SUPER if email in config.ADMIN_EMAILS else None
    if u:
        db.run("UPDATE users SET email=?, gpswox_hash=?, last_login=?, is_admin=MAX(COALESCE(?, 0), COALESCE(is_admin, 0)) WHERE id=?",
               email or u["email"], gpswox_hash or u["gpswox_hash"], now_iso(), admin, u["id"])
        uid = u["id"]
    else:
        first = db.one("SELECT COUNT(*) AS n FROM users")["n"] == 0
        uid = db.run("""INSERT INTO users(email,name,gpswox_hash,is_admin,api_token,created_at,last_login)
                        VALUES(?,?,?,?,?,?,?)""", email, name or email, gpswox_hash, SUPER if (admin or first) else 0,
                     secrets.token_urlsafe(24), now_iso(), now_iso())
    # convites pendentes para este email
    for inv in db.q("SELECT * FROM invites WHERE email=?", email):
        add_member(inv["company_id"], uid, inv["role"])
    db.run("DELETE FROM invites WHERE email=?", email)
    # sem empresa: junta-se à empresa da mesma conta GPS, ou cria a sua
    if not db.one("SELECT 1 AS x FROM members WHERE user_id=? LIMIT 1", uid):
        same = db.one("SELECT id FROM companies WHERE gpswox_hash=? ORDER BY id LIMIT 1", gpswox_hash) \
            if gpswox_hash else None
        cid = same["id"] if same else create_company(name if name and "@" not in name else email, gpswox_hash)
        add_member(cid, uid, "gestor")
    # dados da versão anterior pertencem à empresa da conta configurada no servidor
    if gpswox_hash and gpswox_hash == config.GPSWOX_API_HASH:
        c = db.one("SELECT id FROM companies WHERE gpswox_hash=? ORDER BY id LIMIT 1", gpswox_hash)
        if c:
            db.claim_orphans(c["id"])
    return db.one("SELECT * FROM users WHERE id=?", uid)


# ---------- palavras-passe ----------
PW_ITER = 240_000
PW_MIN = 8
_PW_ALPHA = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
BAD_LOGIN = "Email ou palavra-passe incorretos."


def hash_password(pw):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, PW_ITER)
    return f"pbkdf2_sha256${PW_ITER}${salt.hex()}${dk.hex()}"


def check_password(pw, stored):
    try:
        alg, it, salt, h = (stored or "").split("$")
        dk = hashlib.pbkdf2_hmac("sha256", (pw or "").encode(), bytes.fromhex(salt), int(it))
        return alg == "pbkdf2_sha256" and secrets.compare_digest(dk.hex(), h)
    except ValueError:
        return False


def generate_password():
    """Palavra-passe automática legível (sem 0/O, 1/l/I), ex.: Xk7p-Qm3a-R9tw."""
    while True:
        raw = "".join(secrets.choice(_PW_ALPHA) for _ in range(12))
        if any(ch.isdigit() for ch in raw) and any(ch.isupper() for ch in raw) and any(ch.islower() for ch in raw):
            return "-".join(raw[i:i + 4] for i in range(0, 12, 4))


def validate_new_password(pw, old_hash=None):
    if len(pw or "") < PW_MIN:
        raise HTTPException(400, f"A palavra-passe tem de ter pelo menos {PW_MIN} caracteres.")
    if not (any(ch.isalpha() for ch in pw) and any(ch.isdigit() for ch in pw)):
        raise HTTPException(400, "A palavra-passe tem de ter letras e números.")
    if old_hash and check_password(pw, old_hash):
        raise HTTPException(400, "A nova palavra-passe tem de ser diferente da atual.")


def set_password(user_id, pw, must_change):
    db.run("UPDATE users SET password_hash=?, must_change=?, pw_changed_at=? WHERE id=?",
           hash_password(pw), 1 if must_change else 0, now_iso(), user_id)


def reset_password(user_id, must_change=True):
    """Gera uma palavra-passe automática nova e termina as sessões abertas da pessoa."""
    pw = generate_password()
    set_password(user_id, pw, must_change)
    db.run("DELETE FROM sessions WHERE user_id=?", user_id)
    return pw


def create_user(email, name=None, level_=0):
    """Cria uma conta nesta plataforma com palavra-passe automática (a trocar na 1.ª entrada).
    Devolve (pessoa, palavra-passe). Convites pendentes para o email ficam ativos."""
    email = (email or "").strip().lower()
    if "@" not in email or " " in email:
        raise HTTPException(400, "Email inválido.")
    if db.one("SELECT 1 AS x FROM users WHERE email=?", email):
        raise HTTPException(400, "Já existe uma conta com este email.")
    pw = generate_password()
    uid = db.run("""INSERT INTO users(email,name,is_admin,api_token,created_at,password_hash,must_change,pw_changed_at)
                    VALUES(?,?,?,?,?,?,1,?)""", email, (name or "").strip() or None, int(level_ or 0),
                 secrets.token_urlsafe(24), now_iso(), hash_password(pw), now_iso())
    for inv in db.q("SELECT * FROM invites WHERE email=?", email):
        add_member(inv["company_id"], uid, inv["role"])
    db.run("DELETE FROM invites WHERE email=?", email)
    return db.one("SELECT * FROM users WHERE id=?", uid), pw


def login(email, password):
    email = (email or "").strip().lower()
    if config.APP_PASSWORD and email == config.APP_USER.lower() and \
            secrets.compare_digest(password, config.APP_PASSWORD):  # acesso de emergência (variável de ambiente)
        u = _upsert_user(config.APP_USER.lower(), config.GPSWOX_API_HASH, "Administrador")
        db.run("UPDATE users SET is_admin=? WHERE id=?", SUPER, u["id"])
        return db.one("SELECT * FROM users WHERE id=?", u["id"])
    u = db.one("SELECT * FROM users WHERE email=?", email)
    if not u:
        raise AuthError(BAD_LOGIN)
    if u.get("password_hash"):
        if not check_password(password, u["password_hash"]):
            raise AuthError(BAD_LOGIN)
        db.run("UPDATE users SET last_login=? WHERE id=?", now_iso(), u["id"])
        return db.one("SELECT * FROM users WHERE id=?", u["id"])
    # conta antiga sem palavra-passe própria: última entrada com a conta GPSWox, depois tem de definir uma
    try:
        h = gpswox_login(email, password)
    except AuthError:
        raise AuthError(BAD_LOGIN)
    u = _upsert_user(email, h)
    db.run("UPDATE users SET must_change=1 WHERE id=?", u["id"])
    return db.one("SELECT * FROM users WHERE id=?", u["id"])


# ---------- sessões ----------
def _first_company(user):
    m = db.one("SELECT company_id FROM members WHERE user_id=? ORDER BY company_id LIMIT 1", user["id"])
    if m:
        return m["company_id"]
    if not is_super(user):
        return None
    c = companies_of(user)
    return c[0]["id"] if c else None


def new_session(user_id):
    token = secrets.token_urlsafe(32)
    exp = datetime.now(timezone.utc) + timedelta(days=config.SESSION_DAYS)
    u = db.one("SELECT * FROM users WHERE id=?", user_id)
    db.run("INSERT INTO sessions(token,user_id,expires,company_id) VALUES(?,?,?,?)",
           hashlib.sha256(token.encode()).hexdigest(), user_id, exp.isoformat(), _first_company(u))
    return token


def end_session(token):
    if token:
        db.run("DELETE FROM sessions WHERE token=?", hashlib.sha256(token.encode()).hexdigest())


def switch_company(token, user, company_id, as_role=None):
    """Muda a empresa ativa. Sem ser membro, só o superadministrador entra (personificação), como gestor ou utilizador."""
    if user.get("_imp") or not can_enter(user, company_id):
        raise HTTPException(403, "Termine a personificação antes de mudar de empresa." if user.get("_imp") else
                            "Só o superadministrador pode entrar em empresas de que não é membro."
                            if level(user) else "Sem acesso a esta empresa.")
    role = None
    if is_super(user) and as_role in ROLES:  # personificação pedida explicitamente (mesmo sendo membro)
        role = as_role
    elif not is_member(user, company_id):
        role = "gestor"
    db.run("UPDATE sessions SET company_id=?, as_role=? WHERE token=?", company_id, role,
           hashlib.sha256(token.encode()).hexdigest())


def stop_impersonation(token, user):
    db.run("UPDATE sessions SET company_id=?, as_role=NULL WHERE token=?", _first_member_company(user),
           hashlib.sha256(token.encode()).hexdigest())


def _first_member_company(user):
    m = db.one("SELECT company_id FROM members WHERE user_id=? ORDER BY company_id LIMIT 1", user["id"])
    return m["company_id"] if m else None


def from_request(request: Request):
    """(pessoa, empresa ativa, pessoa real) da sessão, ou (None, None, None).

    Em personificação (superadministrador numa empresa de que não é membro) a «pessoa» devolvida é uma vista
    sem privilégios de plataforma: vê e pode fazer só o que um gestor/utilizador dessa empresa faria."""
    tok = request.cookies.get(COOKIE)
    if not tok:
        return None, None, None
    s = db.one("SELECT * FROM sessions WHERE token=?", hashlib.sha256(tok.encode()).hexdigest())
    if not s or s["expires"] <= now_iso():
        return None, None, None
    u = db.one("SELECT * FROM users WHERE id=?", s["user_id"])
    if not u:
        return None, None, None
    cid = s["company_id"] if can_enter(u, s["company_id"]) else _first_company(u)
    if cid != s["company_id"]:
        db.run("UPDATE sessions SET company_id=? WHERE token=?", cid, s["token"])
    c = company(cid)
    view = u
    if c and is_super(u) and (s.get("as_role") in ROLES or not is_member(u, cid)):
        role = s.get("as_role") if s.get("as_role") in ROLES else "gestor"
        view = dict(u, is_admin=0, _imp={"cid": cid, "role": role})
        c["impersonating"] = role
    if c:
        c["role"] = role_in(view, cid)
    return view, c, u


def company_from_api_token(request: Request):
    tok = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not tok:
        return None
    c = db.one("SELECT id FROM companies WHERE api_token=?", tok)
    if c:
        return company(c["id"])
    u = db.one("SELECT * FROM users WHERE api_token=?", tok)  # tokens pessoais antigos
    if u:
        cid = _first_company(u)
        return company(cid) if cid else None
    if config.API_TOKEN and secrets.compare_digest(tok, config.API_TOKEN):
        return default_company()
    return None


def require_admin(user):
    if not user or not user.get("is_admin"):
        raise HTTPException(403, "Só administradores.")


def require_super(user):
    if not is_super(user):
        raise HTTPException(403, "Só o superadministrador.")


__all__ = ["AuthError", "login", "new_session", "end_session", "from_request", "company_from_api_token",
           "require_admin", "require_super", "create_user", "reset_password", "set_password", "check_password", "validate_new_password", "generate_password", "stop_impersonation", "is_super", "level", "can_enter", "LEVELS", "COOKIE", "companies_of", "role_in", "can_manage", "require_manager", "switch_company",
           "create_company", "add_member", "company", "default_company"]
