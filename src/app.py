"""
owui-token-usage-management-console: Lambda backend + static site host.

One Lambda behind an API Gateway HTTP API ($default route):
  - Public (no data): GET /, /app.js, /app.css, /chart.umd.js, /config.json
  - Protected (/api/*): requires a Cognito access token (Authorization: Bearer ...)
    that Cognito itself validates via its /oauth2/userInfo endpoint. Only admins exist in the user pool
    (self sign-up is disabled), so a valid token == an admin.

Reads/writes the same PostgreSQL objects the Open WebUI usage tracking filter
(v1.2.0+) deploys: usage_records, usage_limits, user_limit_overrides,
resolve_user_tier(), plus Open WebUI's native "user", "group", group_member.
"""

import base64
import json
import logging
import os
import re
import time
import urllib.request
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3
import psycopg

log = logging.getLogger()
log.setLevel(logging.INFO)

TZ = os.environ.get("USAGE_TIMEZONE", "UTC")
ZoneInfo(TZ)
TIER_PREFIX = os.environ.get("USAGE_TIER_PREFIX", "token-limit-")
# Built-in tiers (listed in tie-break priority order, matching resolve_user_tier()).
BUILTIN_TIERS = [
    tier.strip()
    for tier in os.environ.get(
        "USAGE_BUILTIN_TIERS",
        ",".join(f"{TIER_PREFIX}{tier}" for tier in ("enterprise", "pro", "standard", "test")),
    ).split(",")
    if tier.strip()
]
DEFAULT_TIER = os.environ.get("USAGE_DEFAULT_TIER", f"{TIER_PREFIX}standard")
DEFAULT_DAILY = int(os.environ.get("USAGE_DEFAULT_DAILY_TOKENS", "500000"))
DEFAULT_MONTHLY = int(os.environ.get("USAGE_DEFAULT_MONTHLY_TOKENS", "5000000"))
if not TIER_PREFIX or len(TIER_PREFIX) >= 50:
    raise ValueError("USAGE_TIER_PREFIX must contain 1-49 characters")
if len(set(BUILTIN_TIERS)) != len(BUILTIN_TIERS) or not BUILTIN_TIERS:
    raise ValueError("USAGE_BUILTIN_TIERS must contain unique, non-empty group names")
if DEFAULT_TIER not in BUILTIN_TIERS:
    raise ValueError("USAGE_DEFAULT_TIER must be included in USAGE_BUILTIN_TIERS")
if any(not tier.startswith(TIER_PREFIX) for tier in BUILTIN_TIERS):
    raise ValueError("Every USAGE_BUILTIN_TIERS entry must start with USAGE_TIER_PREFIX")
if any(limit < -1 or limit > 10**12 for limit in (DEFAULT_DAILY, DEFAULT_MONTHLY)):
    raise ValueError("USAGE_DEFAULT_*_TOKENS must be -1 or between 0 and 10^12")
MAX_REPORT_DAYS = 366
MAX_TOKENS = 10**12
MAX_SLUG_LEN = 50 - len(TIER_PREFIX)  # usage_limits.group_name is VARCHAR(50)
SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
EMAIL_RE = re.compile(r"[^@\s,;<>\"']{1,64}@[^@\s,;<>\"']{1,255}\.[^@\s,;<>\"']{2,63}")
MAX_BULK_USERS = 1000
MAX_BODY = 10000
MAX_BULK_BODY = 400000
MAX_BULK_USERS = 5000
MAX_IMPORT_BODY = 1500000
MAX_IMPORT_ROWS = 10000
MAX_IMPORT_GROUPS = 100
NEW_USER_ROLE = "user"
# bcrypt hash of a random value that was discarded, so accounts created here cannot use
# password login. They sign in with SSO, which links to the account by e-mail
# (OAUTH_MERGE_ACCOUNTS_BY_EMAIL=True in the Open WebUI deployment).
UNUSABLE_PASSWORD_HASH = "$2b$12$SIz9JXp6toQgk76cEJDlB.wMwMOUciUf7EBakLxE1s0NRUQWwwOu."
STATIC_DIR = Path(__file__).parent / "static"
STATIC_TYPES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/chart.umd.js": ("chart.umd.js", "application/javascript; charset=utf-8"),
}
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self' https://*.amazoncognito.com; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=63072000",
    "Cache-Control": "no-store",
}

_secrets = boto3.client("secretsmanager")
_dsn = None
_conn = None
_token_cache = {}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------- infrastructure
def get_dsn():
    global _dsn
    if _dsn is None:
        secret = _secrets.get_secret_value(SecretId=os.environ["SECRET_ID"])
        _dsn = json.loads(secret["SecretString"])[os.environ.get("SECRET_DSN_KEY", "DATABASE_URL")]
    return _dsn


def get_conn():
    global _conn
    if _conn is not None:
        try:
            if _conn.closed or _conn.broken:
                _conn = None
        except Exception:
            _conn = None
    if _conn is None:
        _conn = psycopg.connect(get_dsn(), connect_timeout=8)
    return _conn


def authenticate(headers):
    auth = headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise ApiError(401, "Missing bearer token")
    token = auth[7:].strip()
    cached = _token_cache.get(token)
    now = time.time()
    if cached and cached[0] > now:
        return cached[1]
    try:
        req = urllib.request.Request(
            os.environ["COGNITO_DOMAIN"] + "/oauth2/userInfo",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            info = json.loads(resp.read())
    except Exception:
        raise ApiError(401, "Invalid or expired session")
    who = info.get("email") or info.get("username") or info.get("sub")
    if not who:
        raise ApiError(401, "Invalid or expired session")
    if len(_token_cache) > 50:
        _token_cache.clear()
    _token_cache[token] = (now + 60, who)
    return who


# ---------------------------------------------------------------- validation
def parse_tokens(value, field):
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ApiError(400, f"{field} must be an integer")
    try:
        n = int(value)
    except ValueError:
        raise ApiError(400, f"{field} must be an integer")
    if n < -1 or n > MAX_TOKENS:
        raise ApiError(400, f"{field} must be -1 (unlimited) or a positive number")
    return n


def parse_date(value, field):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ApiError(400, f"{field} must be YYYY-MM-DD")


def tier_names(cur):
    """Built-in tiers followed by custom tiers (token-limit-* rows in usage_limits)."""
    cur.execute(
        """SELECT group_name FROM usage_limits
           WHERE starts_with(group_name, %s) AND group_name <> ALL(%s) ORDER BY group_name""",
        (TIER_PREFIX, BUILTIN_TIERS),
    )
    return BUILTIN_TIERS + [r[0] for r in cur.fetchall()]


def require_tier(cur, name):
    names = tier_names(cur)
    if name not in names:
        raise ApiError(400, f"Invalid tier. Valid tiers: {', '.join(names)}")
    return names


def custom_tier_name(raw):
    """Normalize 'Biology Faculty' / 'token-limit-biology-faculty' to 'token-limit-biology-faculty'."""
    if not isinstance(raw, str):
        raise ApiError(400, "Tier name is required")
    slug = re.sub(r"[\s_]+", "-", raw.strip().lower()).removeprefix(TIER_PREFIX).strip("-")
    if not slug or len(slug) > MAX_SLUG_LEN or not SLUG_RE.fullmatch(slug):
        raise ApiError(
            400,
            f"Tier name must be 1-{MAX_SLUG_LEN} characters: letters, numbers and single hyphens "
            f"(it is stored as {TIER_PREFIX}<name>)",
        )
    name = TIER_PREFIX + slug
    if name in BUILTIN_TIERS:
        raise ApiError(409, f"{name} is a built-in tier")
    return name


def format_tokens(n):
    """500000 -> '500K', 5000000 -> '5M', -1 -> 'unlimited'."""
    if n is None:
        return "?"
    if n == -1:
        return "unlimited"
    for size, unit in ((10**9, "B"), (10**6, "M"), (10**3, "K")):
        if n >= size:
            v = f"{n / size:.2f}".rstrip("0").rstrip(".")
            return v + unit
    return str(n)


def default_description(daily, monthly):
    return f"{format_tokens(daily)}/day, {format_tokens(monthly)}/month"


def parse_limit(value):
    """Lenient limit parser for CSV values: 500000, '500,000', '500K', '5M', '-1'. None when blank."""
    if value is None:
        return None
    s = str(value).strip().lower().replace(",", "").replace("_", "")
    if not s:
        return None
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)([km]?)", s)
    if not m:
        raise ValueError(f"{str(value)[:30]!r} is not a number")
    n = float(m.group(1)) * {"": 1, "k": 10**3, "m": 10**6}[m.group(2)]
    if n != int(n) or n < -1 or n > MAX_TOKENS:
        raise ValueError(f"{str(value)[:30]!r} must be -1 (unlimited) or a whole number up to {MAX_TOKENS}")
    return int(n)


def clean_text(value, field, limit=500):
    if value is not None and (not isinstance(value, str) or len(value) > limit):
        raise ApiError(400, f"{field} must be text up to {limit} characters")
    return value or None


def require_user(cur, user_id):
    cur.execute('SELECT id FROM "user" WHERE id = %s', (user_id,))
    if not cur.fetchone():
        raise ApiError(404, "User not found")


def audit(admin, action, **details):
    log.info("AUDIT %s", json.dumps({"admin": admin, "action": action, **details}))


# ---------------------------------------------------------------- queries
USER_ROWS_SQL = f"""
WITH win AS (
    SELECT (DATE_TRUNC('day', NOW() AT TIME ZONE '{TZ}') AT TIME ZONE '{TZ}') AS day_start,
           (DATE_TRUNC('month', NOW() AT TIME ZONE '{TZ}') AT TIME ZONE '{TZ}') AS month_start
),
d AS (
    SELECT ur.user_id, SUM(ur.total_tokens)::BIGINT AS t
    FROM usage_records ur, win WHERE ur.recorded_at >= win.day_start GROUP BY 1
),
m AS (
    SELECT ur.user_id, SUM(ur.total_tokens)::BIGINT AS t
    FROM usage_records ur, win WHERE ur.recorded_at >= win.month_start GROUP BY 1
)
SELECT u.id, COALESCE(u.name, ''), COALESCE(u.email, ''), COALESCE(u.role, ''),
       r.default_tier, r.tiers, r.custom_tiers,
       (SELECT COUNT(*) FROM group_member gm JOIN "group" g ON g.id = gm.group_id
         WHERE gm.user_id = u.id AND g.name = ANY(%(builtin)s)) AS default_memberships,
       COALESCE(uo.daily_token_limit, r.daily_limit) AS daily_limit,
       COALESCE(uo.monthly_token_limit, r.monthly_limit) AS monthly_limit,
       COALESCE(d.t, 0), COALESCE(m.t, 0),
       uo.user_id IS NOT NULL AS has_override,
       r.limit_source
FROM "user" u
CROSS JOIN LATERAL resolve_user_limits(u.id) r
LEFT JOIN user_limit_overrides uo ON uo.user_id = u.id
LEFT JOIN d ON d.user_id = u.id
LEFT JOIN m ON m.user_id = u.id
ORDER BY COALESCE(u.name, u.email, u.id)
"""


def user_row(r):
    default_tier, tiers, custom = r[4], list(r[5]), list(r[6])
    daily_limit, monthly_limit = r[8], r[9]
    today, month = r[10], r[11]
    return {
        "id": r[0],
        "name": r[1],
        "email": r[2],
        "role": r[3],
        "tier": default_tier or DEFAULT_TIER,
        "default_tier": default_tier,
        "custom_tiers": custom,
        "tiers": tiers,
        "tier_is_default": default_tier is None and not custom,
        "tier_memberships": r[7],  # number of DEFAULT tier groups; more than one is a conflict
        "limit_source": r[13],
        "daily_limit": daily_limit,
        "monthly_limit": monthly_limit,
        "tokens_today": today,
        "tokens_month": month,
        "daily_pct": None if daily_limit == -1 else round(today / max(daily_limit, 1) * 100, 1),
        "monthly_pct": None if monthly_limit == -1 else round(month / max(monthly_limit, 1) * 100, 1),
        "has_override": r[12],
    }


def list_users(cur):
    cur.execute(USER_ROWS_SQL, {"builtin": BUILTIN_TIERS})
    return [user_row(r) for r in cur.fetchall()]


def list_tiers(cur):
    names = tier_names(cur)
    cur.execute(
        """SELECT group_name, daily_token_limit, monthly_token_limit, description, updated_at
           FROM usage_limits WHERE group_name = ANY(%s)""",
        (names,),
    )
    rows = {r[0]: r for r in cur.fetchall()}
    cur.execute(
        """SELECT t, COUNT(*) FROM "user" u
           CROSS JOIN LATERAL resolve_user_limits(u.id) r
           CROSS JOIN LATERAL unnest(r.tiers) AS t GROUP BY t"""
    )
    members = dict(cur.fetchall())
    return [
        {
            "name": t,
            "custom": t not in BUILTIN_TIERS,
            "members": members.get(t, 0),
            "daily_limit": rows[t][1] if t in rows else None,
            "monthly_limit": rows[t][2] if t in rows else None,
            "description": (rows[t][3] if t in rows else None)
            or (default_description(rows[t][1], rows[t][2]) if t in rows else None),
            "updated_at": rows[t][4].isoformat() if t in rows and rows[t][4] else None,
        }
        for t in names
    ]


def overview(cur):
    users = list_users(cur)
    tiers = list_tiers(cur)
    for t in tiers:
        members = [u for u in users if t["name"] in u["tiers"]]
        t["members"] = len(members)
        t["tokens_today"] = sum(u["tokens_today"] for u in members)
        t["tokens_month"] = sum(u["tokens_month"] for u in members)
    near = [
        u for u in users
        if max(u["daily_pct"] or 0, u["monthly_pct"] or 0) >= 80
    ]
    return {
        "timezone": TZ,
        "total_users": len(users),
        "tokens_today": sum(u["tokens_today"] for u in users),
        "tokens_month": sum(u["tokens_month"] for u in users),
        "users_over_limit": sum(
            1 for u in users
            if (u["daily_pct"] or 0) >= 100 or (u["monthly_pct"] or 0) >= 100
        ),
        "users_near_limit": len(near),
        "ambiguous_users": sum(1 for u in users if u["tier_memberships"] > 1),
        "default_tier_users": sum(1 for u in users if u["tier_is_default"]),
        "tiers": tiers,
        "near_limit": sorted(
            near, key=lambda u: -max(u["daily_pct"] or 0, u["monthly_pct"] or 0)
        )[:10],
    }


MAX_REPORT_USERS = 200


def report(cur, params):
    today = datetime.now(ZoneInfo(TZ)).date()
    end = parse_date(params["end"], "end") if params.get("end") else today
    start = parse_date(params["start"], "start") if params.get("start") else end - timedelta(days=29)
    if start > end:
        raise ApiError(400, "start must be on or before end")
    if (end - start).days + 1 > MAX_REPORT_DAYS:
        raise ApiError(400, f"Range may not exceed {MAX_REPORT_DAYS} days")
    user_id = params.get("user_id") or None

    where = (
        f"ur.recorded_at >= (%(start)s::date::timestamp AT TIME ZONE '{TZ}') "
        f"AND ur.recorded_at < ((%(end)s::date + 1)::timestamp AT TIME ZONE '{TZ}')"
    )
    args = {"start": start, "end": end}
    if user_id:
        where += " AND ur.user_id = %(user_id)s"
        args["user_id"] = user_id
    user_ids = [x for x in (params.get("user_ids") or "").split(",") if x]
    if len(user_ids) > MAX_REPORT_USERS:
        raise ApiError(400, f"Select at most {MAX_REPORT_USERS} users")
    if user_ids:
        where += " AND ur.user_id = ANY(%(user_ids)s)"
        args["user_ids"] = user_ids
    tier = params.get("tier") or None
    if tier:
        if tier not in tier_names(cur):
            raise ApiError(400, f"Unknown tier: {tier}")
        where += (
            ' AND ur.user_id IN (SELECT u.id FROM "user" u CROSS JOIN LATERAL resolve_user_limits(u.id) r'
            " WHERE %(tier)s = ANY(r.tiers))"
        )
        args["tier"] = tier

    cur.execute(
        f"""SELECT (ur.recorded_at AT TIME ZONE '{TZ}')::date, SUM(ur.prompt_tokens),
                   SUM(ur.completion_tokens), SUM(ur.total_tokens), COUNT(*)
            FROM usage_records ur WHERE {where} GROUP BY 1 ORDER BY 1""",
        args,
    )
    by_day = {r[0]: r for r in cur.fetchall()}
    daily = []
    for i in range((end - start).days + 1):
        d = start + timedelta(days=i)
        r = by_day.get(d)
        daily.append({
            "date": d.isoformat(),
            "prompt": int(r[1]) if r else 0,
            "completion": int(r[2]) if r else 0,
            "total": int(r[3]) if r else 0,
            "requests": int(r[4]) if r else 0,
        })

    cur.execute(
        f"""SELECT COALESCE(u.name, u.email, ur.user_id), COALESCE(u.email, ''), ur.user_id,
                   SUM(ur.total_tokens)::BIGINT, COUNT(*)
            FROM usage_records ur LEFT JOIN "user" u ON u.id = ur.user_id
            WHERE {where} GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT 15""",
        args,
    )
    top_users = [
        {"name": r[0], "email": r[1], "user_id": r[2], "tokens": int(r[3]), "requests": int(r[4])}
        for r in cur.fetchall()
    ]

    # A user in several tiers counts under each of them.
    cur.execute(
        f"""SELECT t, SUM(x.tokens)::BIGINT FROM (
                SELECT ur.user_id, SUM(ur.total_tokens) AS tokens FROM usage_records ur WHERE {where} GROUP BY 1
            ) x
            CROSS JOIN LATERAL resolve_user_limits(x.user_id) r
            CROSS JOIN LATERAL unnest(r.tiers) AS t GROUP BY t ORDER BY 2 DESC""",
        args,
    )
    by_tier = [{"tier": r[0], "tokens": int(r[1])} for r in cur.fetchall()]

    cur.execute(
        f"""SELECT COALESCE(ur.model_id, 'unknown'), SUM(ur.total_tokens)::BIGINT
            FROM usage_records ur WHERE {where} GROUP BY 1 ORDER BY 2 DESC LIMIT 10""",
        args,
    )
    by_model = [{"model": r[0], "tokens": int(r[1])} for r in cur.fetchall()]

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "user_id": user_id,
        "tier": tier,
        "user_count": len(user_ids),
        "timezone": TZ,
        "total_tokens": sum(x["total"] for x in daily),
        "total_requests": sum(x["requests"] for x in daily),
        "daily": daily,
        "top_users": top_users,
        "by_tier": by_tier,
        "by_model": by_model,
    }


def audit_tiers(cur):
    """Users in several DEFAULT (built-in) tier groups, and users in no default tier group (custom tiers are ignored)."""
    users = list_users(cur)
    cur.execute(
        """SELECT gm.user_id, ARRAY_AGG(g.name ORDER BY gm.created_at DESC)
           FROM group_member gm JOIN "group" g ON g.id = gm.group_id
           WHERE g.name = ANY(%s) GROUP BY 1""",
        (BUILTIN_TIERS,),
    )
    groups = dict(cur.fetchall())
    return {
        "multiple": [
            {**u, "groups": groups[u["id"]]} for u in users if u["tier_memberships"] > 1
        ],
        "none": [u for u in users if u["tier_memberships"] == 0],
    }


# ---------------------------------------------------------------- mutations
# The tier-resolution SQL (resolve_user_tier, resolve_user_limits, get_user_usage_status) is bundled
# by deploy.sh straight from the filter source so the console and the filter cannot drift apart.
_bundled = Path(__file__).parent / "tier_function.sql"
TIER_FUNCTION_SQL = _bundled.read_text(encoding="utf-8") if _bundled.exists() else None


OVERRIDES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS user_limit_overrides (
    user_id VARCHAR(255) PRIMARY KEY,
    daily_token_limit BIGINT,
    monthly_token_limit BIGINT,
    notes TEXT,
    updated_by VARCHAR(255),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
"""


def tier_function_current(cur):
    cur.execute("SELECT prosrc FROM pg_proc WHERE proname = 'get_user_usage_status' ORDER BY oid DESC LIMIT 1")
    row = cur.fetchone()
    return bool(row and "resolve_user_limits" in row[0])


def ensure_tier_function(cur, admin=None):
    """Install the multi-tier SQL functions if the database still has older ones. True if changed."""
    if tier_function_current(cur) and table_exists(cur, "user_limit_overrides"):
        return False
    if not TIER_FUNCTION_SQL:
        raise ApiError(500, "The deployment has no bundled tier functions; redeploy with deploy.sh")
    # Serialise concurrent requests: parallel CREATE OR REPLACE FUNCTION calls collide on pg_proc.
    cur.execute("SELECT pg_advisory_xact_lock(hashtext('owui_usage_tier_function'))")
    if tier_function_current(cur) and table_exists(cur, "user_limit_overrides"):
        return False
    cur.execute(OVERRIDES_TABLE_SQL)
    cur.execute(TIER_FUNCTION_SQL)
    audit(admin or "system", "update_tier_function")
    return True


def tier_group_ids(cur, names):
    cur.execute('SELECT id, name FROM "group" WHERE name = ANY(%s)', (names,))
    return {name: gid for gid, name in cur.fetchall()}


def table_exists(cur, name):
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (name,))
    return cur.fetchone()[0]


def insert_group(cur, name, description, daily, monthly):
    """Create the native Open WebUI group behind a tier (same shape the filter creates)."""
    now = int(time.time())
    gid = str(uuid.uuid4())
    cur.execute(
        """INSERT INTO "group" (id, user_id, name, description, data, meta, permissions, created_at, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (gid, "", name, description or default_description(daily, monthly),
         json.dumps({"daily_token_limit": daily, "monthly_token_limit": monthly}),
         json.dumps({}), json.dumps({}), now, now),
    )
    return gid


def place_users(cur, user_ids, tier):
    """Add users to `tier` (idempotent). A built-in tier replaces the user's other built-in tier;
    a custom tier is added alongside whatever else the user already has."""
    names = tier_names(cur)
    ids = tier_group_ids(cur, names)
    if tier not in ids:
        if tier in BUILTIN_TIERS:
            raise ApiError(409, f"Native group {tier!r} does not exist yet; load the usage filter once to create it")
        ids[tier] = insert_group(cur, tier, None, DEFAULT_DAILY, DEFAULT_MONTHLY)
    target = ids[tier]
    now = int(time.time())
    cur.execute(
        "SELECT user_id FROM group_member WHERE group_id = %s AND user_id = ANY(%s)", (target, user_ids)
    )
    already = len(cur.fetchall())
    moved = 0
    if tier in BUILTIN_TIERS:
        defaults = [ids[t] for t in BUILTIN_TIERS if t in ids]
        cur.execute(
            """DELETE FROM group_member WHERE user_id = ANY(%s) AND group_id = ANY(%s) AND group_id <> %s
               RETURNING user_id""",
            (user_ids, defaults, target),
        )
        moved = len({r[0] for r in cur.fetchall()})
    cur.executemany(
        """INSERT INTO group_member (id, group_id, user_id, created_at, updated_at)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (group_id, user_id) DO UPDATE SET created_at = EXCLUDED.created_at,
                                                          updated_at = EXCLUDED.updated_at""",
        [(str(uuid.uuid4()), target, uid, now, now) for uid in user_ids],
    )
    cur.execute('UPDATE "group" SET updated_at = %s WHERE id = ANY(%s)', (now, list(ids.values())))
    return {"already_in_tier": already, "moved_from_other_tiers": moved}


def bulk_assign_default_tier(cur, admin, body):
    """Put the given users into one default (built-in) tier, replacing any other default tier they have."""
    tier = body.get("tier")
    if tier not in BUILTIN_TIERS:
        raise ApiError(400, "tier must be one of the default tiers: " + ", ".join(BUILTIN_TIERS))
    ids = body.get("user_ids")
    if not isinstance(ids, list) or not ids:
        raise ApiError(400, "No users selected")
    if len(ids) > MAX_BULK_USERS:
        raise ApiError(400, f"Too many users in one request (max {MAX_BULK_USERS})")
    if not all(isinstance(i, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", i) for i in ids):
        raise ApiError(400, "Invalid user id")
    require_tier(cur, tier)
    cur.execute('SELECT id FROM "user" WHERE id = ANY(%s)', (list(set(ids)),))
    found = [r[0] for r in cur.fetchall()]
    if not found:
        raise ApiError(404, "None of the selected users exist")
    result = place_users(cur, found, tier)
    audit(admin, "bulk_assign_default_tier", tier=tier, users=len(found), **result)
    return {"assigned": len(found), "skipped_unknown": len(set(ids)) - len(found), **result}


def remove_from_tier(cur, admin, user_id, tier):
    require_tier(cur, tier)
    require_user(cur, user_id)
    cur.execute(
        'DELETE FROM group_member WHERE user_id = %s AND group_id IN (SELECT id FROM "group" WHERE name = %s)',
        (user_id, tier),
    )
    audit(admin, "remove_from_tier", user_id=user_id, tier=tier)


def assign_tier(cur, admin, user_id, tier):
    require_tier(cur, tier)
    require_user(cur, user_id)
    place_users(cur, [user_id], tier)
    audit(admin, "assign_tier", user_id=user_id, tier=tier)


def table_columns(cur, table):
    cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = %s",
        (table,),
    )
    return {r[0] for r in cur.fetchall()}


def insert_rows(cur, table, columns, rows):
    cols = list(columns)
    cur.executemany(
        f'INSERT INTO "{table}" ({", ".join(cols)}) VALUES ({", ".join(["%s"] * len(cols))})',
        [[row[c] for c in cols] for row in rows],
    )


def create_users(cur, entries):
    """Pre-provision Open WebUI accounts for (email, name) pairs; returns their new ids.

    Only columns that exist in this Open WebUI version are written. The account is linked to the
    person's SSO login by e-mail the first time they sign in.
    """
    now = int(time.time())
    user_cols = table_columns(cur, "user")
    auth_cols = table_columns(cur, "auth")
    users, auths, ids = [], [], []
    for email, name in entries:
        uid = str(uuid.uuid4())
        ids.append(uid)
        users.append({
            "id": uid, "email": email, "name": name, "role": NEW_USER_ROLE,
            "profile_image_url": "/user.png", "last_active_at": now, "created_at": now, "updated_at": now,
        })
        auths.append({
            "id": uid, "email": email, "password": UNUSABLE_PASSWORD_HASH, "active": True,
            "session_stamp": str(uuid.uuid4()),
        })
    insert_rows(cur, "user", [c for c in users[0] if c in user_cols], users)
    if auth_cols:
        insert_rows(cur, "auth", [c for c in auths[0] if c in auth_cols], auths)
    return ids


def parse_bulk_users(body):
    ids = body.get("user_ids") or []
    people = body.get("users") or []
    if not isinstance(ids, list) or not isinstance(people, list):
        raise ApiError(400, "user_ids and users must be lists")
    if not ids and not people:
        raise ApiError(400, "Select at least one user")
    if len(ids) + len(people) > MAX_BULK_USERS:
        raise ApiError(400, f"At most {MAX_BULK_USERS} users can be added at once")
    for i in ids:
        if not isinstance(i, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", i):
            raise ApiError(400, "Invalid user id")
    emails = {}
    for p in people:
        if not isinstance(p, dict) or not isinstance(p.get("email"), str):
            raise ApiError(400, "Each user needs an email")
        email = p["email"].strip().lower()
        if len(email) > 254 or not EMAIL_RE.fullmatch(email):
            raise ApiError(400, f"Invalid email address: {p['email'][:80]!r}")
        name = clean_text(p.get("name"), "name", 100)
        emails.setdefault(email, (name or email.split("@")[0]).strip())
    return list(dict.fromkeys(ids)), emails


def add_users(cur, admin, tier, body):
    require_tier(cur, tier)
    ensure_tier_function(cur, admin)
    ids, emails = parse_bulk_users(body)
    if ids:
        cur.execute('SELECT id FROM "user" WHERE id = ANY(%s)', (ids,))
        if len(cur.fetchall()) != len(ids):
            raise ApiError(404, "One or more users were not found")
    existing = {}
    if emails:
        cur.execute('SELECT LOWER(email), id FROM "user" WHERE LOWER(email) = ANY(%s)', (list(emails),))
        existing = dict(cur.fetchall())
    missing = [(e, n) for e, n in emails.items() if e not in existing]
    created = create_users(cur, missing) if missing else []
    user_ids = list(dict.fromkeys(ids + list(existing.values()) + created))
    result = place_users(cur, user_ids, tier)
    audit(admin, "add_users", tier=tier, users=len(user_ids), created=[e for e, _ in missing][:50], **result)
    return {"added": len(user_ids), "created": len(created), **result}


def create_tier(cur, admin, body):
    ensure_tier_function(cur, admin)
    name = custom_tier_name(body.get("name"))
    daily = parse_tokens(body.get("daily"), "daily")
    monthly = parse_tokens(body.get("monthly"), "monthly")
    daily = DEFAULT_DAILY if daily is None else daily
    monthly = DEFAULT_MONTHLY if monthly is None else monthly
    desc = clean_text(body.get("description"), "description")
    cur.execute("SELECT 1 FROM usage_limits WHERE LOWER(group_name) = %s", (name,))
    if cur.fetchone():
        raise ApiError(409, f"Tier {name} already exists")
    cur.execute('SELECT 1 FROM "group" WHERE LOWER(name) = %s', (name,))
    if cur.fetchone():
        raise ApiError(409, f"An Open WebUI group named {name} already exists; choose another name")
    insert_group(cur, name, desc, daily, monthly)
    cur.execute(
        """INSERT INTO usage_limits (group_name, daily_token_limit, monthly_token_limit, description, updated_at)
           VALUES (%s, %s, %s, %s, NOW())""",
        (name, daily, monthly, desc or default_description(daily, monthly)),
    )
    audit(admin, "create_tier", tier=name, daily=daily, monthly=monthly)
    return {"name": name}


def plan_import(cur, rows):
    """Validate a multi-group CSV (already split into rows) and describe what importing it would do.

    Rows are {line, group, email, daily, monthly, description}. Nothing is written. Rows are grouped by
    group name wherever they appear in the file. A group with any error blocks the whole import.
    """
    if not isinstance(rows, list) or not rows:
        raise ApiError(400, "The file has no data rows")
    if len(rows) > MAX_IMPORT_ROWS:
        raise ApiError(400, f"At most {MAX_IMPORT_ROWS} rows can be imported at once")
    cur.execute("SELECT LOWER(group_name) FROM usage_limits")
    taken = {r[0] for r in cur.fetchall()}
    cur.execute('SELECT LOWER(name) FROM "group"')
    taken |= {r[0] for r in cur.fetchall()}

    groups, order, global_errors = {}, [], []
    for r in rows:
        if not isinstance(r, dict):
            raise ApiError(400, "Invalid row")
        line = r.get("line") if isinstance(r.get("line"), int) else "?"
        raw = r.get("group")
        raw = raw.strip() if isinstance(raw, str) else ""
        if not raw:
            global_errors.append(f"Line {line}: group-name is empty")
            continue
        try:
            key, name_error = custom_tier_name(raw), None
        except ApiError as e:
            key, name_error = "invalid:" + raw.lower(), e.message
        if key not in groups:
            groups[key] = {
                "name": key if name_error is None else raw, "input_name": raw, "rows": [],
                "errors": [name_error] if name_error else [], "warnings": [],
            }
            order.append(key)
        groups[key]["rows"].append((line, r))
    if len(order) > MAX_IMPORT_GROUPS:
        raise ApiError(400, f"At most {MAX_IMPORT_GROUPS} groups can be imported at once")

    existing_by_email = {}
    all_emails = set()
    for key in order:
        g = groups[key]
        emails, dupes = [], 0
        daily = monthly = None
        desc = None
        bad_lines = {"email": [], "daily": [], "monthly": []}
        for line, r in g["rows"]:
            email = (r.get("email") or "").strip().lower() if isinstance(r.get("email"), str) else ""
            if not email:
                bad_lines["email"].append(f"Line {line}: email is empty")
            elif len(email) > 254 or not EMAIL_RE.fullmatch(email):
                bad_lines["email"].append(f"Line {line}: invalid email address {email[:60]!r}")
            elif email in emails:
                dupes += 1
            else:
                emails.append(email)
            for field in ("daily", "monthly"):
                try:
                    v = parse_limit(r.get(field))
                except ValueError as e:
                    bad_lines[field].append(f"Line {line}: {field}-token-limit {e}")
                    continue
                if v is None:
                    continue
                cur_best = daily if field == "daily" else monthly
                # -1 means unlimited, so it counts as the greatest value
                if cur_best is None or (v == -1 and cur_best != -1) or (cur_best != -1 and v > cur_best):
                    if field == "daily":
                        daily = v
                    else:
                        monthly = v
            d = r.get("description")
            if desc is None and isinstance(d, str) and d.strip():
                desc = d.strip()
        g["errors"] += bad_lines["email"][:10] + bad_lines["daily"][:10] + bad_lines["monthly"][:10]
        extra = sum(max(len(v) - 10, 0) for v in bad_lines.values())
        if extra:
            g["errors"].append(f"{extra} more row error(s) not shown")
        if daily is None and not bad_lines["daily"]:
            g["errors"].append("daily-token-limit is required on at least one row of the group")
        if monthly is None and not bad_lines["monthly"]:
            g["errors"].append("monthly-token-limit is required on at least one row of the group")
        if desc is not None and len(desc) > 500:
            g["errors"].append("description is longer than 500 characters")
            desc = None
        if not emails and not bad_lines["email"]:
            g["errors"].append("The group has no members")
        if dupes:
            g["warnings"].append(f"{dupes} duplicate row(s) for the same email ignored")
        if g["name"].lower() in taken and not key.startswith("invalid:"):
            g["errors"].append(f"A group named {g['name']} already exists in the system")
        g.update(
            daily=daily, monthly=monthly, emails=emails,
            description=desc or (default_description(daily, monthly) if daily is not None and monthly is not None else None),
            description_defaulted=desc is None,
        )
        all_emails.update(emails)
    if all_emails:
        cur.execute('SELECT LOWER(email), id FROM "user" WHERE LOWER(email) = ANY(%s)', (sorted(all_emails),))
        existing_by_email = dict(cur.fetchall())
    out = []
    for key in order:
        g = groups[key]
        out.append({
            "name": g["name"], "input_name": g["input_name"],
            "status": "error" if g["errors"] else "create",
            "daily": g["daily"], "monthly": g["monthly"], "description": g["description"],
            "description_defaulted": g["description_defaulted"],
            "members": len(g["emails"]),
            "existing_users": sum(1 for e in g["emails"] if e in existing_by_email),
            "new_users": sum(1 for e in g["emails"] if e not in existing_by_email),
            "errors": g["errors"], "warnings": g["warnings"],
        })
    can_commit = bool(out) and not global_errors and all(g["status"] == "create" for g in out)
    plan = {
        "groups": out, "errors": global_errors[:20], "can_commit": can_commit,
        "rows": len(rows), "new_users": len({e for k in order for e in groups[k]["emails"]} - set(existing_by_email)),
    }
    return plan, groups, order, existing_by_email


def import_tiers(cur, admin, body):
    """Create several custom tiers (with limits and members) from parsed CSV rows. commit=false only previews."""
    plan, groups, order, existing = plan_import(cur, body.get("rows"))
    if body.get("commit") is not True:
        return {**plan, "committed": False}
    if not plan["can_commit"]:
        raise ApiError(409, "The file has errors. Fix them and upload it again.")
    ensure_tier_function(cur, admin)
    fresh = sorted({e for k in order for e in groups[k]["emails"]} - set(existing))
    created = dict(zip(fresh, create_users(cur, [(e, e.split("@")[0]) for e in fresh]))) if fresh else {}
    ids = {**existing, **created}
    for key in order:
        g = groups[key]
        insert_group(cur, key, g["description"], g["daily"], g["monthly"])
        cur.execute(
            """INSERT INTO usage_limits (group_name, daily_token_limit, monthly_token_limit, description, updated_at)
               VALUES (%s, %s, %s, %s, NOW())""",
            (key, g["daily"], g["monthly"], g["description"]),
        )
        place_users(cur, [ids[e] for e in g["emails"]], key)
    audit(admin, "import_tiers", groups=order, users=sum(len(groups[k]["emails"]) for k in order), created_users=len(created))
    return {**plan, "committed": True}


def delete_tier(cur, admin, tier):
    if tier in BUILTIN_TIERS:
        raise ApiError(400, "Built-in tiers cannot be deleted")
    if tier not in tier_names(cur):
        raise ApiError(404, "Tier not found")
    cur.execute('SELECT id FROM "group" WHERE name = %s', (DEFAULT_TIER,))
    standard = cur.fetchone()
    if not standard:
        raise ApiError(409, f"Native group {DEFAULT_TIER!r} does not exist yet; load the usage filter once to create it")
    returned = members = 0
    cur.execute('SELECT id FROM "group" WHERE name = %s', (tier,))
    row = cur.fetchone()
    if row:
        gid = row[0]
        cur.execute("SELECT COUNT(*) FROM group_member WHERE group_id = %s", (gid,))
        members = cur.fetchone()[0]
        # Members with no other tier return to Standard; anyone in another tier keeps it.
        cur.execute(
            """SELECT gm.user_id FROM group_member gm
               WHERE gm.group_id = %s AND NOT EXISTS (
                   SELECT 1 FROM group_member o JOIN "group" og ON og.id = o.group_id
                   WHERE o.user_id = gm.user_id AND og.id <> %s AND og.name = ANY(%s))""",
            (gid, gid, tier_names(cur)),
        )
        orphans = [r[0] for r in cur.fetchall()]
        now = int(time.time())
        cur.executemany(
            """INSERT INTO group_member (id, group_id, user_id, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (group_id, user_id) DO UPDATE SET created_at = EXCLUDED.created_at,
                                                              updated_at = EXCLUDED.updated_at""",
            [(str(uuid.uuid4()), standard[0], uid, now, now) for uid in orphans],
        )
        returned = len(orphans)
        if table_exists(cur, "access_grant"):
            cur.execute("DELETE FROM access_grant WHERE principal_type = 'group' AND principal_id = %s", (gid,))
        cur.execute("DELETE FROM group_member WHERE group_id = %s", (gid,))
        cur.execute('DELETE FROM "group" WHERE id = %s', (gid,))
    if table_exists(cur, "user_groups"):
        cur.execute("DELETE FROM user_groups WHERE group_name = %s", (tier,))
    cur.execute("DELETE FROM usage_limits WHERE group_name = %s", (tier,))
    audit(admin, "delete_tier", tier=tier, members=members, returned_to_standard=returned)
    return {"members": members, "returned_to_standard": returned}


def set_override(cur, admin, user_id, body):
    require_user(cur, user_id)
    daily = parse_tokens(body.get("daily"), "daily")
    monthly = parse_tokens(body.get("monthly"), "monthly")
    if daily is None and monthly is None:
        raise ApiError(400, "Provide a daily and/or monthly limit")
    notes = body.get("notes")
    if notes is not None and (not isinstance(notes, str) or len(notes) > 500):
        raise ApiError(400, "notes must be text up to 500 characters")
    cur.execute(
        """INSERT INTO user_limit_overrides
               (user_id, daily_token_limit, monthly_token_limit, notes, updated_by, updated_at)
           VALUES (%(u)s, %(d)s, %(m)s, %(n)s, %(by)s, NOW())
           ON CONFLICT (user_id) DO UPDATE SET
             daily_token_limit = %(d)s, monthly_token_limit = %(m)s,
             notes = %(n)s, updated_by = %(by)s, updated_at = NOW()""",
        {"u": user_id, "d": daily, "m": monthly, "n": notes, "by": admin},
    )
    audit(admin, "set_override", user_id=user_id, daily=daily, monthly=monthly)


def clear_override(cur, admin, user_id):
    require_user(cur, user_id)
    cur.execute("DELETE FROM user_limit_overrides WHERE user_id = %s", (user_id,))
    audit(admin, "clear_override", user_id=user_id)


def rename_tier(cur, admin, tier, raw):
    """Rename a custom tier (the part after token-limit-); built-in tiers cannot be renamed."""
    if tier in BUILTIN_TIERS:
        raise ApiError(400, "Built-in tiers cannot be renamed")
    new = custom_tier_name(raw)
    if new == tier:
        return tier
    cur.execute("SELECT 1 FROM usage_limits WHERE LOWER(group_name) = %s", (new,))
    clash = cur.fetchone()
    cur.execute('SELECT 1 FROM "group" WHERE LOWER(name) = %s', (new,))
    if clash or cur.fetchone():
        raise ApiError(409, f"A group named {new} already exists; choose another name")
    now = int(time.time())
    cur.execute('UPDATE "group" SET name = %s, updated_at = %s WHERE name = %s', (new, now, tier))
    cur.execute("UPDATE usage_limits SET group_name = %s, updated_at = NOW() WHERE group_name = %s", (new, tier))
    if table_exists(cur, "user_groups"):
        cur.execute("UPDATE user_groups SET group_name = %s WHERE group_name = %s", (new, tier))
    audit(admin, "rename_tier", tier=tier, new_name=new)
    return new


def set_tier_limits(cur, admin, tier, body):
    require_tier(cur, tier)
    daily = parse_tokens(body.get("daily"), "daily")
    monthly = parse_tokens(body.get("monthly"), "monthly")
    renamed = None
    if body.get("name") is not None:
        renamed = rename_tier(cur, admin, tier, body.get("name"))
        tier = renamed
    if daily is None and monthly is None and renamed is None and body.get("description") is None:
        raise ApiError(400, "Provide a daily and/or monthly limit")
    desc = body.get("description")
    if desc is not None and (not isinstance(desc, str) or len(desc) > 500):
        raise ApiError(400, "description must be text up to 500 characters")
    cur.execute(
        f"""INSERT INTO usage_limits (group_name, daily_token_limit, monthly_token_limit, description, updated_at)
            VALUES (%(t)s, COALESCE(%(d)s, {DEFAULT_DAILY}), COALESCE(%(m)s, {DEFAULT_MONTHLY}), %(desc)s, NOW())
            ON CONFLICT (group_name) DO UPDATE SET
              daily_token_limit = COALESCE(%(d)s, usage_limits.daily_token_limit),
              monthly_token_limit = COALESCE(%(m)s, usage_limits.monthly_token_limit),
              description = COALESCE(%(desc)s, usage_limits.description),
              updated_at = NOW()""",
        {"t": tier, "d": daily, "m": monthly, "desc": desc},
    )
    audit(admin, "set_tier_limits", tier=tier, daily=daily, monthly=monthly)
    return {"name": tier}


# ---------------------------------------------------------------- routing
def respond(status, body, content_type="application/json", extra=None):
    headers = {**SECURITY_HEADERS, "Content-Type": content_type, **(extra or {})}
    if not isinstance(body, str):
        body = json.dumps(body, default=str)
    return {"statusCode": status, "headers": headers, "body": body}


def serve_static(path):
    name, ctype = STATIC_TYPES[path]
    extra = {"Cache-Control": "no-cache"}
    return respond(200, (STATIC_DIR / name).read_text(encoding="utf-8"), ctype, extra)


def handle_api(method, path, query, body, admin):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            m = re.fullmatch(r"/api/users/([A-Za-z0-9_-]{1,100})/(tier|override)", path)
            t = re.fullmatch(r"/api/tiers/([a-z0-9-]{1,50})", path)
            tm = re.fullmatch(r"/api/tiers/([a-z0-9-]{1,50})/members", path)
            # The queries below need resolve_user_limits(); install the bundled SQL if it is missing.
            if path not in ("/api/status", "/api/tier-function"):
                ensure_tier_function(cur, admin)
            if method == "GET" and path == "/api/overview":
                result = overview(cur)
            elif method == "GET" and path == "/api/users":
                result = {"users": list_users(cur)}
            elif method == "GET" and path == "/api/tiers":
                result = {"tiers": list_tiers(cur)}
            elif method == "POST" and path == "/api/tiers":
                result = {"ok": True, **create_tier(cur, admin, body)}
            elif method == "POST" and path == "/api/tiers/import":
                result = {"ok": True, **import_tiers(cur, admin, body)}
            elif method == "GET" and path == "/api/report":
                result = report(cur, query)
            elif method == "GET" and path == "/api/status":
                result = {"tier_function_current": tier_function_current(cur)}
            elif method == "POST" and path == "/api/tier-function":
                result = {"ok": True, "updated": ensure_tier_function(cur, admin)}
            elif method == "GET" and path == "/api/audit":
                result = audit_tiers(cur)
            elif method == "POST" and path == "/api/audit/assign-default":
                result = {"ok": True, **bulk_assign_default_tier(cur, admin, body)}
            elif m and m.group(2) == "tier" and method == "PUT":
                assign_tier(cur, admin, m.group(1), body.get("tier"))
                result = {"ok": True}
            elif m and m.group(2) == "tier" and method == "DELETE":
                remove_from_tier(cur, admin, m.group(1), (query.get("tier") or ""))
                result = {"ok": True}
            elif m and m.group(2) == "override" and method == "PUT":
                set_override(cur, admin, m.group(1), body)
                result = {"ok": True}
            elif m and m.group(2) == "override" and method == "DELETE":
                clear_override(cur, admin, m.group(1))
                result = {"ok": True}
            elif tm and method == "POST":
                result = {"ok": True, **add_users(cur, admin, tm.group(1), body)}
            elif t and method == "PUT":
                result = {"ok": True, **set_tier_limits(cur, admin, t.group(1), body)}
            elif t and method == "DELETE":
                result = {"ok": True, **delete_tier(cur, admin, t.group(1))}
            else:
                raise ApiError(404, "Not found")
        conn.commit()
        return respond(200, result)
    except Exception:
        conn.rollback()
        raise


def handler(event, context):
    http = event.get("requestContext", {}).get("http", {})
    method = http.get("method", "GET")
    path = event.get("rawPath", "/")
    try:
        if method == "GET" and path in STATIC_TYPES:
            return serve_static(path)
        if method == "GET" and path == "/config.json":
            return respond(200, {
                "cognitoDomain": os.environ["COGNITO_DOMAIN"],
                "clientId": os.environ["CLIENT_ID"],
                "environment": os.environ.get("ENVIRONMENT_NAME", ""),
                "owuiUrl": os.environ.get("OWUI_URL", ""),
                "timezone": TZ,
                "tierPrefix": TIER_PREFIX,
                "builtinTiers": BUILTIN_TIERS,
                "defaultTier": DEFAULT_TIER,
                "defaultDailyTokens": DEFAULT_DAILY,
                "defaultMonthlyTokens": DEFAULT_MONTHLY,
            })
        if not path.startswith("/api/"):
            raise ApiError(404, "Not found")

        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        admin = authenticate(headers)

        body = {}
        if event.get("body"):
            raw = event["body"]
            if event.get("isBase64Encoded"):
                raw = base64.b64decode(raw).decode("utf-8")
            limit = MAX_IMPORT_BODY if path == "/api/tiers/import" else MAX_BULK_BODY if path.endswith("/members") or path == "/api/audit/assign-default" else MAX_BODY
            if len(raw) > limit:
                raise ApiError(413, "Body too large")
            try:
                body = json.loads(raw)
            except ValueError:
                raise ApiError(400, "Body must be JSON")
            if not isinstance(body, dict):
                raise ApiError(400, "Body must be a JSON object")

        return handle_api(method, path, event.get("queryStringParameters") or {}, body, admin)
    except ApiError as e:
        return respond(e.status, {"error": e.message})
    except Exception:
        log.exception("Unhandled error")
        return respond(500, {"error": "Internal error. See CloudWatch logs."})
