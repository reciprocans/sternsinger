import hmac
import io
import hashlib
import json
import secrets
import smtplib
import ssl
import math
import os
import re
import sqlite3
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "sternsinger.db"
UPLOAD_DIR = DATA_DIR / "uploads"
VENDOR_DIR = DATA_DIR / "vendor-cache"
TILE_DIR = DATA_DIR / "map-tiles"
MAX_STATE_BYTES = int(os.getenv("MAX_STATE_BYTES", str(5 * 1024 * 1024)))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
ALLOWED_UPLOAD_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}
UPLOAD_ID_RE = re.compile(r"^[a-f0-9]{32}$")

ROLE_SUPERADMIN = "superadmin"
ROLE_DONATION = "spenden"
ROLE_MANAGEMENT = "verwaltung"
VALID_ROLES = {ROLE_SUPERADMIN, ROLE_DONATION, ROLE_MANAGEMENT}
ROLE_LABELS = {
    ROLE_SUPERADMIN: "Superadmin",
    ROLE_DONATION: "Spenden",
    ROLE_MANAGEMENT: "Verwaltung",
}

APP_VERSION = "v66"

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "BITTE-SECRET-KEY-AENDERN")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=max(MAX_STATE_BYTES, MAX_UPLOAD_BYTES),
)

# Existing v27/v28 ADMIN_* variables remain the superadmin bootstrap credentials.
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "bitte-aendern")
MANAGEMENT_USER = os.getenv("MANAGEMENT_USER", "verwaltung")
MANAGEMENT_PASSWORD = os.getenv("MANAGEMENT_PASSWORD", "bitte-aendern")
DONATION_USER = os.getenv("DONATION_USER", "spenden")
DONATION_PASSWORD = os.getenv("DONATION_PASSWORD", "bitte-aendern")

SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER).strip()
SMTP_SECURITY = os.getenv("SMTP_SECURITY", "starttls").strip().lower()
APP_BASE_URL = os.getenv("APP_BASE_URL", "").strip().rstrip("/")
RESET_TOKEN_TTL_MINUTES = int(os.getenv("RESET_TOKEN_TTL_MINUTES", "30"))

OSM_NOMINATIM_URL = os.getenv("OSM_NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
OSM_OVERPASS_URL = os.getenv("OSM_OVERPASS_URL", "https://overpass-api.de/api/interpreter")
OSM_OVERPASS_FALLBACK_URL = os.getenv("OSM_OVERPASS_FALLBACK_URL", "https://overpass.kumi.systems/api/interpreter")
OSM_MAP_URL = os.getenv("OSM_MAP_URL", "https://api.openstreetmap.org/api/0.6/map")
OSM_OSRM_URL = os.getenv("OSM_OSRM_URL", "https://router.project-osrm.org").rstrip("/")
OSM_USER_AGENT = os.getenv("OSM_USER_AGENT", "sternsinger-verwaltung/1.1")
LEAFLET_JS_URL = os.getenv("LEAFLET_JS_URL", "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js")
LEAFLET_CSS_URL = os.getenv("LEAFLET_CSS_URL", "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css")
LEAFLET_ASSET_BASE = os.getenv("LEAFLET_ASSET_BASE", "https://unpkg.com/leaflet@1.9.4/dist/images/")
OSM_TILE_URL = os.getenv("OSM_TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png")


def fetch_bytes(url, *, data=None, timeout=25, accept="application/json"):
    headers = {
        "User-Agent": OSM_USER_AGENT,
        "Accept": accept,
        "Accept-Language": "de",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def fetch_json(url, *, data=None, timeout=25):
    return json.loads(fetch_bytes(url, data=data, timeout=timeout).decode("utf-8"))


def db_connection():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    TILE_DIR.mkdir(parents=True, exist_ok=True)
    with db_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                data TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO app_state (id, data) VALUES (1, ?)",
            (json.dumps({"people": [], "groups": [], "settings": {}}, ensure_ascii=False),),
        )
        # Retain the old table for seamless migration from v27/v28.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                username TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO auth_config (id, username, password_hash) VALUES (1, ?, ?)",
            (ADMIN_USER, generate_password_hash(ADMIN_PASSWORD)),
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_accounts (
                username TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(user_accounts)").fetchall()}
        if "email" not in columns:
            conn.execute("ALTER TABLE user_accounts ADD COLUMN email TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                token_hash TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                used_at INTEGER,
                created_at INTEGER NOT NULL,
                FOREIGN KEY(username) REFERENCES user_accounts(username)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_password_reset_user ON password_reset_tokens(username)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS server_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                username TEXT,
                role TEXT,
                action TEXT NOT NULL,
                details TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at)")

        count = conn.execute("SELECT COUNT(*) AS c FROM user_accounts").fetchone()["c"]
        if count == 0:
            old = conn.execute("SELECT username, password_hash FROM auth_config WHERE id = 1").fetchone()
            super_user = old["username"] if old else ADMIN_USER
            super_hash = old["password_hash"] if old else generate_password_hash(ADMIN_PASSWORD)
            conn.execute(
                "INSERT INTO user_accounts (username, password_hash, role) VALUES (?, ?, ?)",
                (super_user, super_hash, ROLE_SUPERADMIN),
            )
            bootstrap = [
                (MANAGEMENT_USER, MANAGEMENT_PASSWORD, ROLE_MANAGEMENT),
                (DONATION_USER, DONATION_PASSWORD, ROLE_DONATION),
            ]
            for username, password, role in bootstrap:
                if username and username != super_user:
                    conn.execute(
                        "INSERT OR IGNORE INTO user_accounts (username, password_hash, role) VALUES (?, ?, ?)",
                        (username, generate_password_hash(password), role),
                    )
        conn.commit()


def load_state_row(conn):
    row = conn.execute("SELECT data, updated_at FROM app_state WHERE id = 1").fetchone()
    try:
        data = json.loads(row["data"]) if row else {"people": [], "groups": [], "settings": {}}
    except (json.JSONDecodeError, TypeError):
        data = {"people": [], "groups": [], "settings": {}}
    if not isinstance(data, dict):
        data = {"people": [], "groups": [], "settings": {}}
    data.setdefault("people", [])
    data.setdefault("groups", [])
    data.setdefault("settings", {})
    return data, (row["updated_at"] if row else None)



def _xlsx_column_name(index):
    """1-based Excel column number to A1 column name."""
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _xlsx_text(value):
    value = "" if value is None else str(value)
    value = "".join(ch for ch in value if ch in "\t\n\r" or ord(ch) >= 32)
    return value[:32767]


def _xlsx_sheet_xml(headers, rows, widths):
    row_count = len(rows) + 1
    col_count = len(headers)
    end_ref = f"{_xlsx_column_name(col_count)}{max(1, row_count)}"
    cols = "".join(
        f'<col min="{idx}" max="{idx}" width="{width}" customWidth="1"/>'
        for idx, width in enumerate(widths, start=1)
    )

    def cell_xml(row_idx, col_idx, value, style=0):
        ref = f"{_xlsx_column_name(col_idx)}{row_idx}"
        text = xml_escape(_xlsx_text(value))
        style_attr = f' s="{style}"' if style else ""
        preserve = ' xml:space="preserve"' if text[:1].isspace() or text[-1:].isspace() else ""
        return f'<c r="{ref}" t="inlineStr"{style_attr}><is><t{preserve}>{text}</t></is></c>'

    sheet_rows = []
    sheet_rows.append('<row r="1" ht="24" customHeight="1">' + ''.join(
        cell_xml(1, idx, value, 1) for idx, value in enumerate(headers, start=1)
    ) + '</row>')
    for row_idx, row in enumerate(rows, start=2):
        cells = ''.join(cell_xml(row_idx, idx, value) for idx, value in enumerate(row, start=1))
        sheet_rows.append(f'<row r="{row_idx}">{cells}</row>')

    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <dimension ref="A1:{end_ref}"/>
  <sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
  <sheetFormatPr defaultRowHeight="18"/>
  <cols>{cols}</cols>
  <sheetData>{''.join(sheet_rows)}</sheetData>
  <autoFilter ref="A1:{end_ref}"/>
</worksheet>"""


def build_mailmerge_xlsx(state):
    people = [p for p in state.get("people", []) if isinstance(p, dict)]
    groups = [g for g in state.get("groups", []) if isinstance(g, dict)]
    group_names = {str(g.get("id")): str(g.get("name") or "Gruppe") for g in groups if g.get("id") is not None}

    category_labels = {
        "sternsinger": "Sternsinger",
        "begleiter": "Begleiter",
        "mittagessen": "Mittagessen",
        "helfer": "Helfer",
    }
    nutrition_labels = {"vegan": "Vegan", "vegetarisch": "Vegetarisch"}

    def group_name(person):
        gid = person.get("groupId")
        return group_names.get(str(gid), "Nicht zugewiesen") if gid else "Nicht zugewiesen"

    def helper_tasks(person):
        parts = []
        tasks = person.get("taetigkeiten", []) if isinstance(person.get("taetigkeiten"), list) else []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            name = str(task.get("name") or "Tätigkeit").strip()
            date = str(task.get("datum") or "").strip()
            start = str(task.get("von") or "").strip()
            end = str(task.get("bis") or "").strip()
            time_part = (f"{start}–{end}" if start and end else start or end)
            when = " ".join(x for x in [date, time_part] if x)
            parts.append(f"{name} – {when}" if when else name)
        return "; ".join(parts)

    def common(person):
        first = str(person.get("vorname") or "").strip()
        last = str(person.get("nachname") or "").strip()
        full = " ".join(x for x in [first, last] if x)
        return first, last, full, str(person.get("adresse") or "").strip(), str(person.get("tel") or "").strip()

    sorted_people = sorted(people, key=lambda person: (
        str(person.get("nachname") or "").casefold(),
        str(person.get("vorname") or "").casefold(),
    ))

    stern_headers = ["Vorname", "Nachname", "Vollständiger Name", "Adresse", "Telefon", "Gruppe", "Ernährung", "Allergien / Unverträglichkeiten", "Einverständnis", "Foto-Erlaubnis", "Sternsinger-Notiz", "Hinweise"]
    basic_headers = ["Vorname", "Nachname", "Vollständiger Name", "Adresse", "Telefon", "Gruppe", "Hinweise"]
    helper_headers = ["Vorname", "Nachname", "Vollständiger Name", "Adresse", "Telefon", "Tätigkeiten", "Hinweise"]
    all_headers = ["Vorname", "Nachname", "Vollständiger Name", "Kategorie", "Adresse", "Telefon", "Gruppe", "Ernährung", "Allergien / Unverträglichkeiten", "Einverständnis", "Foto-Erlaubnis", "Tätigkeiten", "Sternsinger-Notiz", "Hinweise"]

    stern_rows, companion_rows, lunch_rows, helper_rows, all_rows = [], [], [], [], []
    for person in sorted_people:
        category = str(person.get("category") or "").strip()
        first, last, full, address, phone = common(person)
        group = group_name(person)
        info = str(person.get("info") or "").strip()
        nutrition = nutrition_labels.get(str(person.get("ernaehrung") or ""), "") if category == "sternsinger" else ""
        allergies = str(person.get("unvertraeglichkeiten") or "").strip() if category == "sternsinger" else ""
        consent = "Ja" if category == "sternsinger" and person.get("einverstaendnis") else ("Nein" if category == "sternsinger" else "")
        photo = "Ja" if category == "sternsinger" and person.get("fotoEinverstaendnis") else ("Nein" if category == "sternsinger" else "")
        stern_note = str(person.get("sternsingerNotiz") or "").strip() if category == "sternsinger" else ""
        tasks = helper_tasks(person) if category == "helfer" else ""

        if category == "sternsinger":
            stern_rows.append([first, last, full, address, phone, group, nutrition, allergies, consent, photo, stern_note, info])
        elif category == "begleiter":
            companion_rows.append([first, last, full, address, phone, group, info])
        elif category == "mittagessen":
            lunch_rows.append([first, last, full, address, phone, group, info])
        elif category == "helfer":
            helper_rows.append([first, last, full, address, phone, tasks, info])

        all_rows.append([
            first, last, full, category_labels.get(category, category or "—"), address, phone, group,
            nutrition, allergies, consent, photo, tasks, stern_note, info,
        ])

    sheets = [
        ("Sternsinger", stern_headers, stern_rows, [16, 18, 24, 34, 18, 20, 16, 28, 16, 16, 30, 34]),
        ("Begleiter", basic_headers, companion_rows, [16, 18, 24, 34, 18, 20, 36]),
        ("Mittagessen", basic_headers, lunch_rows, [16, 18, 24, 34, 18, 20, 36]),
        ("Helfer", helper_headers, helper_rows, [16, 18, 24, 34, 18, 42, 36]),
        ("Alle Personen", all_headers, all_rows, [16, 18, 24, 18, 34, 18, 20, 16, 28, 16, 16, 42, 30, 36]),
    ]

    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets>' + ''.join(
            f'<sheet name="{xml_escape(name)}" sheetId="{idx}" r:id="rId{idx}"/>'
            for idx, (name, _, _, _) in enumerate(sheets, start=1)
        ) + '</sheets></workbook>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + ''.join(
            f'<Relationship Id="rId{idx}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{idx}.xml"/>'
            for idx in range(1, len(sheets) + 1)
        ) + f'<Relationship Id="rId{len(sheets)+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '</Relationships>'
    )
    styles_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="2"><font><sz val="11"/><name val="Aptos"/><family val="2"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Aptos"/><family val="2"/></font></fonts>
  <fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF2F343B"/><bgColor indexed="64"/></patternFill></fill></fills>
  <borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border><border><left style="thin"><color rgb="FFE2E6EA"/></left><right style="thin"><color rgb="FFE2E6EA"/></right><top style="thin"><color rgb="FFE2E6EA"/></top><bottom style="thin"><color rgb="FFE2E6EA"/></bottom><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf><xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf></cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>' + ''.join(
            f'<Override PartName="/xl/worksheets/sheet{idx}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for idx in range(1, len(sheets) + 1)
        ) + '</Types>'
    )
    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/styles.xml", styles_xml)
        for idx, (_, headers, rows, widths) in enumerate(sheets, start=1):
            archive.writestr(f"xl/worksheets/sheet{idx}.xml", _xlsx_sheet_xml(headers, rows, widths))
    stream.seek(0)
    return stream

def donation_unlocked():
    try:
        return float(session.get("donation_unlocked_until") or 0) > time.time()
    except (TypeError, ValueError):
        return False


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Nicht angemeldet"}), 401
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def roles_required(*roles):
    allowed = set(roles)
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if session.get("role") not in allowed:
                if request.path.startswith("/api/"):
                    return jsonify({"error": "Keine Berechtigung für diese Funktion."}), 403
                return redirect(url_for("index"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


def current_account(conn, username):
    return conn.execute(
        "SELECT username, password_hash, role, active, email FROM user_accounts WHERE username = ?",
        (username,),
    ).fetchone()


def normalize_email(value):
    value = str(value or "").strip().lower()
    if not value:
        return ""
    if len(value) > 254 or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value):
        return None
    return value


def _smtp_env_defaults():
    return {
        "host": SMTP_HOST,
        "port": SMTP_PORT,
        "username": SMTP_USER,
        "password": SMTP_PASSWORD,
        "from_address": SMTP_FROM,
        "security": SMTP_SECURITY,
        "app_base_url": APP_BASE_URL,
        "reset_token_ttl_minutes": RESET_TOKEN_TTL_MINUTES,
    }


def _normalize_smtp_config(raw):
    raw = raw if isinstance(raw, dict) else {}
    cfg = _smtp_env_defaults()
    for key in cfg:
        if key in raw and raw[key] is not None:
            cfg[key] = raw[key]
    try:
        cfg["port"] = max(1, min(65535, int(cfg.get("port") or 587)))
    except (TypeError, ValueError):
        cfg["port"] = 587
    security = str(cfg.get("security") or "starttls").strip().lower()
    cfg["security"] = security if security in {"starttls", "ssl", "none"} else "starttls"
    try:
        cfg["reset_token_ttl_minutes"] = max(5, min(1440, int(cfg.get("reset_token_ttl_minutes") or 30)))
    except (TypeError, ValueError):
        cfg["reset_token_ttl_minutes"] = 30
    for key in ("host", "username", "password", "from_address", "app_base_url"):
        cfg[key] = str(cfg.get(key) or "").strip() if key != "password" else str(cfg.get(key) or "")
    cfg["app_base_url"] = cfg["app_base_url"].rstrip("/")
    return cfg


def load_smtp_config(conn=None):
    own_conn = conn is None
    if own_conn:
        conn = db_connection()
    try:
        row = conn.execute("SELECT value FROM server_config WHERE key = 'smtp'").fetchone()
        raw = {}
        if row:
            try:
                raw = json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                raw = {}
        cfg = _normalize_smtp_config(raw)
        cfg["stored"] = bool(row)
        return cfg
    finally:
        if own_conn:
            conn.close()


def save_smtp_config(config):
    clean = _normalize_smtp_config(config)
    payload = {k: clean[k] for k in ("host", "port", "username", "password", "from_address", "security", "app_base_url", "reset_token_ttl_minutes")}
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO server_config (key, value, updated_at) VALUES ('smtp', ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
            """,
            (json.dumps(payload, ensure_ascii=False),),
        )
        conn.commit()
    return clean


def smtp_configured(config=None):
    cfg = config or load_smtp_config()
    return bool(cfg.get("host") and cfg.get("from_address"))


def _smtp_connect_and_send(msg, config):
    cfg = _normalize_smtp_config(config)
    if not smtp_configured(cfg):
        raise RuntimeError("E-Mail-Versand ist noch nicht konfiguriert.")
    context = ssl.create_default_context()
    if cfg["security"] == "ssl":
        server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=20, context=context)
    else:
        server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=20)
    try:
        server.ehlo()
        if cfg["security"] == "starttls":
            server.starttls(context=context)
            server.ehlo()
        if cfg["username"]:
            server.login(cfg["username"], cfg["password"])
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


def send_password_reset_email(recipient, username, reset_url, config=None):
    cfg = config or load_smtp_config()
    if not smtp_configured(cfg):
        raise RuntimeError("E-Mail-Versand ist noch nicht konfiguriert.")
    msg = EmailMessage()
    msg["Subject"] = "Sternsinger Verwaltung – Passwort zurücksetzen"
    msg["From"] = cfg["from_address"]
    msg["To"] = recipient
    msg.set_content(
        f"Hallo {username},\n\n"
        "für dein Benutzerkonto in der Sternsinger-Verwaltung wurde ein Passwort-Reset angefordert.\n\n"
        f"Neues Passwort setzen: {reset_url}\n\n"
        f"Der Link ist {cfg['reset_token_ttl_minutes']} Minuten gültig und kann nur einmal verwendet werden.\n"
        "Wenn du den Reset nicht angefordert hast, kannst du diese E-Mail ignorieren.\n"
    )
    _smtp_connect_and_send(msg, cfg)


def send_smtp_test_email(recipient, config=None):
    cfg = config or load_smtp_config()
    if not smtp_configured(cfg):
        raise RuntimeError("E-Mail-Versand ist noch nicht konfiguriert.")
    msg = EmailMessage()
    msg["Subject"] = "Sternsinger Verwaltung – SMTP-Test erfolgreich"
    msg["From"] = cfg["from_address"]
    msg["To"] = recipient
    msg.set_content(
        "Hallo,\n\n"
        "diese Test-E-Mail wurde erfolgreich von der Sternsinger-Verwaltung versendet.\n"
        "Damit ist der E-Mail-Versand für Passwort-Resets grundsätzlich eingerichtet.\n\n"
        "Viele Grüße\nSternsinger Verwaltung\n"
    )
    _smtp_connect_and_send(msg, cfg)



def write_audit(action, details=None, *, username=None, role=None):
    """Minimal audit trail: intentionally stores no IP address and no person payloads."""
    try:
        username = username if username is not None else session.get("username")
        role = role if role is not None else session.get("role")
        detail_text = ""
        if details is not None:
            detail_text = json.dumps(details, ensure_ascii=False, separators=(",", ":")) if not isinstance(details, str) else details
        detail_text = detail_text[:1500]
        with db_connection() as conn:
            conn.execute(
                "INSERT INTO audit_log (username, role, action, details) VALUES (?, ?, ?, ?)",
                (username, role, str(action)[:120], detail_text),
            )
            # Keep the audit trail bounded. Older entries can additionally be purged in Privacy settings.
            conn.execute("DELETE FROM audit_log WHERE id NOT IN (SELECT id FROM audit_log ORDER BY id DESC LIMIT 5000)")
            conn.commit()
    except Exception:
        app.logger.exception("Could not write audit log")


def _cached_remote(path, url, *, mimetype, timeout=30):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.stat().st_size == 0:
        data = fetch_bytes(url, timeout=timeout, accept="*/*")
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
    response = send_file(path, mimetype=mimetype, conditional=True, max_age=86400 * 30)
    response.headers["Cache-Control"] = "public, max-age=2592000"
    return response


def _privacy_public_settings():
    try:
        with db_connection() as conn:
            state, _ = load_state_row(conn)
        settings = state.get("settings", {}) if isinstance(state.get("settings"), dict) else {}
        privacy = settings.get("privacy", {}) if isinstance(settings.get("privacy"), dict) else {}
    except Exception:
        privacy = {}
    return {
        "controller_name": str(privacy.get("controllerName") or "").strip(),
        "controller_contact": str(privacy.get("controllerContact") or "").strip(),
        "retention_years": int(privacy.get("retentionYears") or 3),
        "audit_retention_days": int(privacy.get("auditRetentionDays") or 180),
        "external_maps_enabled": privacy.get("externalMapsEnabled", True) is not False,
    }


def external_maps_enabled():
    try:
        with db_connection() as conn:
            state, _ = load_state_row(conn)
        settings = state.get("settings", {}) if isinstance(state.get("settings"), dict) else {}
        privacy = settings.get("privacy", {}) if isinstance(settings.get("privacy"), dict) else {}
        return privacy.get("externalMapsEnabled", True) is not False
    except Exception:
        return True



@app.after_request
def add_security_headers(response):
    # No analytics/tracking; the CSP keeps browser connections on the same origin.
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; connect-src 'self'; font-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
    )
    if app.config.get("SESSION_COOKIE_SECURE"):
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    # Personen-/Anwendungsdaten sollen nicht in Shared Browser-/Proxy-Caches landen.
    if (request.path.startswith("/api/") and not request.path.startswith("/api/map/tiles/")) or request.path in {"/", "/login", "/forgot-password"} or request.path.startswith("/reset-password/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/vendor/leaflet.css")
def vendor_leaflet_css():
    try:
        return _cached_remote(VENDOR_DIR / "leaflet.css", LEAFLET_CSS_URL, mimetype="text/css")
    except Exception:
        app.logger.exception("Leaflet CSS proxy failed")
        return ("/* Leaflet konnte nicht geladen werden. */", 502, {"Content-Type": "text/css; charset=utf-8"})


@app.get("/vendor/leaflet.js")
def vendor_leaflet_js():
    try:
        return _cached_remote(VENDOR_DIR / "leaflet.js", LEAFLET_JS_URL, mimetype="application/javascript")
    except Exception:
        app.logger.exception("Leaflet JS proxy failed")
        return ("console.error('Leaflet konnte nicht geladen werden.');", 502, {"Content-Type": "application/javascript; charset=utf-8"})


@app.get("/vendor/images/<name>")
def vendor_leaflet_image(name):
    allowed = {"marker-icon.png", "marker-icon-2x.png", "marker-shadow.png"}
    if name not in allowed:
        return ("", 404)
    try:
        return _cached_remote(VENDOR_DIR / "images" / name, LEAFLET_ASSET_BASE + name, mimetype="image/png")
    except Exception:
        app.logger.exception("Leaflet image proxy failed")
        return ("", 502)


@app.get("/api/map/tiles/<int:z>/<int:x>/<int:y>.png")
@login_required
def map_tile(z, x, y):
    if not external_maps_enabled():
        return ("", 403)
    if z < 0 or z > 19 or x < 0 or y < 0 or x >= 2 ** z or y >= 2 ** z:
        return ("", 404)
    path = TILE_DIR / str(z) / str(x) / f"{y}.png"
    try:
        return _cached_remote(path, OSM_TILE_URL.format(z=z, x=x, y=y), mimetype="image/png", timeout=20)
    except Exception:
        app.logger.exception("Map tile proxy failed: z=%s x=%s y=%s", z, x, y)
        return ("", 502)


@app.get("/datenschutz")
def privacy_notice():
    return render_template("privacy.html", privacy=_privacy_public_settings())

def donation_total(donations):
    total = 0.0
    records = donations.get("records") if isinstance(donations, dict) else {}
    if not isinstance(records, dict):
        return 0.0
    for record in records.values():
        if not isinstance(record, dict):
            continue
        for bucket in (record.get("coins", {}), record.get("bills", {})):
            if not isinstance(bucket, dict):
                continue
            for denomination, count in bucket.items():
                try:
                    total += float(denomination) * int(count or 0)
                except (TypeError, ValueError):
                    pass
    return round(total + 1e-9, 2)


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    message = None
    error = None
    smtp_cfg = load_smtp_config()
    mail_ready = smtp_configured(smtp_cfg)
    if request.method == "POST":
        if not mail_ready:
            error = "Der E-Mail-Versand ist noch nicht eingerichtet. Bitte den Superadmin kontaktieren."
        else:
            identifier = str(request.form.get("identifier") or "").strip()
            if not identifier:
                error = "Bitte Benutzername oder E-Mail-Adresse eingeben."
            else:
                try:
                    with db_connection() as conn:
                        account = conn.execute(
                            "SELECT username, email, active FROM user_accounts WHERE lower(username)=lower(?) OR lower(email)=lower(?) LIMIT 1",
                            (identifier, identifier),
                        ).fetchone()
                        if account and account["active"] and account["email"]:
                            raw_token = secrets.token_urlsafe(32)
                            token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
                            now = int(time.time())
                            expires = now + smtp_cfg["reset_token_ttl_minutes"] * 60
                            conn.execute("DELETE FROM password_reset_tokens WHERE username = ? OR expires_at < ? OR used_at IS NOT NULL", (account["username"], now))
                            conn.execute(
                                "INSERT INTO password_reset_tokens (token_hash, username, expires_at, created_at) VALUES (?, ?, ?, ?)",
                                (token_hash, account["username"], expires, now),
                            )
                            conn.commit()
                            base = smtp_cfg.get("app_base_url") or request.url_root.rstrip("/")
                            reset_url = f"{base}/reset-password/{raw_token}"
                            send_password_reset_email(account["email"], account["username"], reset_url, smtp_cfg)
                    message = "Wenn ein passendes Konto mit hinterlegter E-Mail-Adresse gefunden wurde, wurde ein Reset-Link verschickt."
                except Exception as exc:
                    app.logger.exception("Password reset email failed: %s", exc)
                    error = "Der Passwort-Reset konnte gerade nicht versendet werden. Bitte die E-Mail-Einstellungen prüfen oder den Superadmin kontaktieren."
    return render_template("forgot_password.html", message=message, error=error, smtp_configured=mail_ready)


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = int(time.time())
    with db_connection() as conn:
        row = conn.execute(
            "SELECT token_hash, username, expires_at, used_at FROM password_reset_tokens WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
    valid = bool(row and row["used_at"] is None and int(row["expires_at"]) >= now)
    error = None
    success = None
    if request.method == "POST":
        new_password = str(request.form.get("password") or "")
        repeat = str(request.form.get("repeat") or "")
        if not valid:
            error = "Dieser Reset-Link ist ungültig oder abgelaufen."
        elif len(new_password) < 8:
            error = "Das neue Passwort muss mindestens 8 Zeichen lang sein."
        elif len(new_password) > 256:
            error = "Das neue Passwort ist zu lang."
        elif new_password != repeat:
            error = "Die beiden Passwörter stimmen nicht überein."
        else:
            with db_connection() as conn:
                conn.execute(
                    "UPDATE user_accounts SET password_hash = ?, updated_at = CURRENT_TIMESTAMP WHERE username = ?",
                    (generate_password_hash(new_password), row["username"]),
                )
                conn.execute("UPDATE password_reset_tokens SET used_at = ? WHERE token_hash = ?", (now, token_hash))
                conn.commit()
            success = "Passwort wurde geändert. Du kannst dich jetzt anmelden."
            valid = False
    return render_template("reset_password.html", valid=valid, error=error, success=success)


@app.context_processor
def inject_app_version():
    return {"app_version": APP_VERSION}


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        with db_connection() as conn:
            auth = current_account(conn, username)
        valid_user = bool(auth and auth["active"])
        valid_password = bool(auth and check_password_hash(auth["password_hash"], password))
        if valid_user and valid_password:
            session.clear()
            session["logged_in"] = True
            session["username"] = auth["username"]
            session["role"] = auth["role"]
            session.permanent = True
            next_url = request.args.get("next")
            if not next_url or not next_url.startswith("/") or next_url.startswith("//"):
                next_url = url_for("index")
            write_audit("login_success", {"role": auth["role"]}, username=auth["username"], role=auth["role"])
            return redirect(next_url)
        write_audit("login_failed", {"reason": "invalid_credentials"}, username=None, role=None)
        error = "Benutzername oder Passwort ist falsch."
    return render_template("login.html", error=error)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def index():
    return render_template(
        "index.html",
        current_user=session.get("username", ""),
        current_role=session.get("role", ROLE_MANAGEMENT),
        role_label=ROLE_LABELS.get(session.get("role"), "Benutzer"),
        donation_unlocked=donation_unlocked(),
    )


@app.get("/spenden-live")
@login_required
def donation_live_page():
    return render_template("donation_live.html")


@app.get("/gruppenkarte")
@roles_required(ROLE_SUPERADMIN, ROLE_MANAGEMENT)
def group_route_map_page():
    return render_template("route_map.html", group_id=(request.args.get("group") or ""))


@app.get("/api/me")
@login_required
def api_me():
    return jsonify({
        "username": session.get("username"),
        "role": session.get("role"),
        "roleLabel": ROLE_LABELS.get(session.get("role"), "Benutzer"),
        "donationUnlocked": donation_unlocked(),
    })


@app.get("/api/state")
@login_required
def get_state():
    with db_connection() as conn:
        data, updated_at = load_state_row(conn)
    # The donation account only needs group names/IDs and donation data. Do not
    # deliver the person/contact database to that role.
    if session.get("role") == ROLE_DONATION:
        source_settings = data.get("settings", {}) if isinstance(data.get("settings"), dict) else {}
        data = {
            "people": [],
            "groups": [
                {"id": g.get("id"), "name": g.get("name") or "Gruppe"}
                for g in data.get("groups", []) if isinstance(g, dict)
            ],
            "settings": {"donations": source_settings.get("donations", {})},
            "savedDate": data.get("savedDate"),
        }
    data["serverUpdatedAt"] = updated_at
    data["currentUser"] = session.get("username")
    data["currentRole"] = session.get("role")
    data["donationUnlocked"] = donation_unlocked()
    return jsonify(data)


@app.get("/api/export/serienbrief.xlsx")
@roles_required(ROLE_SUPERADMIN, ROLE_MANAGEMENT)
def export_mailmerge_excel():
    with db_connection() as conn:
        state, _ = load_state_row(conn)
    workbook = build_mailmerge_xlsx(state)
    write_audit("mailmerge_excel_exported", {"people": len(state.get("people", [])), "sheets": 5})
    stamp = datetime.now().strftime("%Y-%m-%d")
    return send_file(
        workbook,
        as_attachment=True,
        download_name=f"sternsinger_serienbrief_{stamp}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        max_age=0,
    )


@app.put("/api/state")
@login_required
def put_state():
    incoming = request.get_json(silent=True)
    if not isinstance(incoming, dict):
        return jsonify({"error": "Ungültige Daten"}), 400
    if not isinstance(incoming.get("people", []), list) or not isinstance(incoming.get("groups", []), list):
        return jsonify({"error": "people und groups müssen Listen sein"}), 400
    if not isinstance(incoming.get("settings", {}), dict):
        return jsonify({"error": "settings muss ein Objekt sein"}), 400

    role = session.get("role")
    with db_connection() as conn:
        current, _ = load_state_row(conn)
        current_settings = current.get("settings", {}) if isinstance(current.get("settings"), dict) else {}
        incoming_settings = incoming.get("settings", {})
        current_donations = current_settings.get("donations", {}) if isinstance(current_settings.get("donations"), dict) else {}
        incoming_donations = incoming_settings.get("donations", {}) if isinstance(incoming_settings.get("donations"), dict) else {}

        if role == ROLE_SUPERADMIN:
            clean = {
                "people": incoming.get("people", []),
                "groups": incoming.get("groups", []),
                "settings": incoming_settings,
                "savedDate": incoming.get("savedDate"),
            }
        elif role == ROLE_MANAGEMENT:
            merged_settings = dict(incoming_settings)
            merged_settings["donations"] = current_donations
            # Datenschutz-Grundeinstellungen sind systemweit und dem Superadmin vorbehalten.
            merged_settings["privacy"] = current_settings.get("privacy", {})
            clean = {
                "people": incoming.get("people", []),
                "groups": incoming.get("groups", []),
                "settings": merged_settings,
                "savedDate": incoming.get("savedDate"),
            }
        elif role == ROLE_DONATION:
            merged_settings = dict(current_settings)
            merged_settings["donations"] = incoming_donations
            clean = {
                "people": current.get("people", []),
                "groups": current.get("groups", []),
                "settings": merged_settings,
                "savedDate": incoming.get("savedDate"),
            }
        else:
            return jsonify({"error": "Unbekannte Benutzerrolle."}), 403

        new_donations = clean.get("settings", {}).get("donations", {})
        donations_changed = json.dumps(new_donations, sort_keys=True, ensure_ascii=False) != json.dumps(current_donations, sort_keys=True, ensure_ascii=False)
        if current_donations.get("closed") and donations_changed and not donation_unlocked():
            return jsonify({"error": "Die Spendenerfassung ist abgeschlossen. Zum Ändern muss sie mit einem Spenden-/Superadmin-Passwort entsperrt werden.", "code": "donations_locked"}), 423

        encoded = json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_STATE_BYTES:
            return jsonify({"error": "Datensatz ist zu groß"}), 413
        conn.execute(
            """
            INSERT INTO app_state (id, data, updated_at)
            VALUES (1, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET data = excluded.data, updated_at = CURRENT_TIMESTAMP
            """,
            (encoded,),
        )
        row = conn.execute("SELECT updated_at FROM app_state WHERE id = 1").fetchone()
        conn.commit()
    return jsonify({"ok": True, "updatedAt": row["updated_at"]})


@app.post("/api/change-password")
@login_required
def change_password():
    payload = request.get_json(silent=True) or {}
    current_password = str(payload.get("currentPassword") or "")
    new_password = str(payload.get("newPassword") or "")
    if len(new_password) < 8:
        return jsonify({"error": "Das neue Passwort muss mindestens 8 Zeichen lang sein."}), 400
    if len(new_password) > 256:
        return jsonify({"error": "Das neue Passwort ist zu lang."}), 400
    username = session.get("username")
    with db_connection() as conn:
        auth = current_account(conn, username)
        if not auth or not check_password_hash(auth["password_hash"], current_password):
            return jsonify({"error": "Das aktuelle Passwort ist nicht korrekt."}), 403
        conn.execute(
            "UPDATE user_accounts SET password_hash = ?, updated_at = CURRENT_TIMESTAMP WHERE username = ?",
            (generate_password_hash(new_password), username),
        )
        conn.commit()
    write_audit("password_changed")
    return jsonify({"ok": True})


@app.get("/api/account")
@login_required
def get_account():
    with db_connection() as conn:
        auth = current_account(conn, session.get("username"))
    return jsonify({"username": auth["username"], "role": auth["role"], "email": auth["email"] or ""})


@app.put("/api/account/email")
@login_required
def update_own_email():
    payload = request.get_json(silent=True) or {}
    email = normalize_email(payload.get("email"))
    password = str(payload.get("password") or "")
    if email is None:
        return jsonify({"error": "Bitte eine gültige E-Mail-Adresse eingeben."}), 400
    with db_connection() as conn:
        auth = current_account(conn, session.get("username"))
        if not auth or not check_password_hash(auth["password_hash"], password):
            return jsonify({"error": "Das aktuelle Passwort ist nicht korrekt."}), 403
        if email:
            duplicate = conn.execute("SELECT username FROM user_accounts WHERE lower(email)=lower(?) AND username<>?", (email, auth["username"])).fetchone()
            if duplicate:
                return jsonify({"error": "Diese E-Mail-Adresse ist bereits einem anderen Benutzer zugeordnet."}), 409
        conn.execute("UPDATE user_accounts SET email = ?, updated_at = CURRENT_TIMESTAMP WHERE username = ?", (email or None, auth["username"]))
        conn.commit()
    write_audit("account_email_changed", {"configured": bool(email)})
    return jsonify({"ok": True, "email": email or ""})


@app.get("/api/users")
@roles_required(ROLE_SUPERADMIN)
def list_users():
    with db_connection() as conn:
        rows = conn.execute("SELECT username, role, active, email, updated_at FROM user_accounts ORDER BY role, username").fetchall()
    return jsonify({"users": [dict(row) for row in rows], "roles": ROLE_LABELS})


@app.put("/api/users/<username>/email")
@roles_required(ROLE_SUPERADMIN)
def admin_set_user_email(username):
    payload = request.get_json(silent=True) or {}
    email = normalize_email(payload.get("email"))
    if email is None:
        return jsonify({"error": "Bitte eine gültige E-Mail-Adresse eingeben."}), 400
    with db_connection() as conn:
        user = current_account(conn, username)
        if not user:
            return jsonify({"error": "Benutzer nicht gefunden."}), 404
        if email:
            duplicate = conn.execute("SELECT username FROM user_accounts WHERE lower(email)=lower(?) AND username<>?", (email, username)).fetchone()
            if duplicate:
                return jsonify({"error": "Diese E-Mail-Adresse ist bereits einem anderen Benutzer zugeordnet."}), 409
        conn.execute("UPDATE user_accounts SET email = ?, updated_at = CURRENT_TIMESTAMP WHERE username = ?", (email or None, username))
        conn.commit()
    write_audit("user_email_changed", {"target": username, "configured": bool(email)})
    return jsonify({"ok": True, "email": email or ""})


@app.put("/api/users/<username>/password")
@roles_required(ROLE_SUPERADMIN)
def admin_set_user_password(username):
    payload = request.get_json(silent=True) or {}
    new_password = str(payload.get("newPassword") or "")
    if len(new_password) < 8:
        return jsonify({"error": "Das neue Passwort muss mindestens 8 Zeichen lang sein."}), 400
    with db_connection() as conn:
        user = current_account(conn, username)
        if not user:
            return jsonify({"error": "Benutzer nicht gefunden."}), 404
        conn.execute(
            "UPDATE user_accounts SET password_hash = ?, updated_at = CURRENT_TIMESTAMP WHERE username = ?",
            (generate_password_hash(new_password), username),
        )
        conn.commit()
    write_audit("user_password_reset_by_admin", {"target": username})
    return jsonify({"ok": True})



@app.get("/api/privacy/audit")
@roles_required(ROLE_SUPERADMIN)
def privacy_audit():
    try:
        limit = max(10, min(int(request.args.get("limit") or 200), 500))
    except ValueError:
        limit = 200
    with db_connection() as conn:
        rows = conn.execute(
            "SELECT id, created_at, username, role, action, details FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return jsonify({"entries": [dict(row) for row in rows]})


@app.post("/api/privacy/purge-audit")
@roles_required(ROLE_SUPERADMIN)
def privacy_purge_audit():
    payload = request.get_json(silent=True) or {}
    try:
        days = max(7, min(int(payload.get("days") or 180), 3650))
    except (TypeError, ValueError):
        return jsonify({"error": "Ungültige Aufbewahrungsdauer."}), 400
    with db_connection() as conn:
        cur = conn.execute("DELETE FROM audit_log WHERE created_at < datetime('now', ?)", (f"-{days} days",))
        deleted = cur.rowcount if cur.rowcount is not None else 0
        conn.commit()
    write_audit("audit_log_purged", {"days": days, "deleted": deleted})
    return jsonify({"ok": True, "deleted": deleted})


@app.get("/api/privacy/person/<person_id>")
@roles_required(ROLE_SUPERADMIN, ROLE_MANAGEMENT)
def privacy_person_export(person_id):
    with db_connection() as conn:
        state, _ = load_state_row(conn)
    person = next((p for p in state.get("people", []) if isinstance(p, dict) and p.get("id") == person_id), None)
    if not person:
        return jsonify({"error": "Person nicht gefunden."}), 404
    assignments = []
    for group in state.get("groups", []):
        if not isinstance(group, dict):
            continue
        for row, ids in (group.get("cells") or {}).items():
            if isinstance(ids, list) and person_id in ids:
                assignments.append({"groupId": group.get("id"), "groupName": group.get("name"), "role": row})
    payload = {
        "exportedAt": datetime.now(timezone.utc).isoformat(),
        "person": person,
        "groupAssignments": assignments,
    }
    write_audit("person_data_exported", {"personId": person_id})
    return jsonify(payload)


def _upload_referenced(state, upload_id):
    if not upload_id:
        return False
    for person in state.get("people", []):
        if isinstance(person, dict) and (person.get("einverstaendnisDatei") or {}).get("id") == upload_id:
            return True
    settings = state.get("settings", {}) if isinstance(state.get("settings"), dict) else {}
    for archive in settings.get("archives", []) if isinstance(settings.get("archives"), list) else []:
        for person in archive.get("people", []) if isinstance(archive, dict) else []:
            if isinstance(person, dict) and (person.get("einverstaendnisDatei") or {}).get("id") == upload_id:
                return True
    return False


@app.delete("/api/privacy/person/<person_id>")
@roles_required(ROLE_SUPERADMIN)
def privacy_person_delete(person_id):
    with db_connection() as conn:
        state, _ = load_state_row(conn)
        people = state.get("people", []) if isinstance(state.get("people"), list) else []
        person = next((p for p in people if isinstance(p, dict) and p.get("id") == person_id), None)
        if not person:
            return jsonify({"error": "Person nicht gefunden."}), 404
        upload_id = (person.get("einverstaendnisDatei") or {}).get("id")
        state["people"] = [p for p in people if not (isinstance(p, dict) and p.get("id") == person_id)]
        for group in state.get("groups", []):
            if not isinstance(group, dict):
                continue
            cells = group.get("cells") if isinstance(group.get("cells"), dict) else {}
            for row, ids in list(cells.items()):
                if isinstance(ids, list):
                    cells[row] = [pid for pid in ids if pid != person_id]
        settings = state.get("settings", {}) if isinstance(state.get("settings"), dict) else {}
        archives = settings.get("archives", []) if isinstance(settings.get("archives"), list) else []
        for archive in archives:
            if not isinstance(archive, dict):
                continue
            archive["people"] = [p for p in archive.get("people", []) if not (isinstance(p, dict) and p.get("id") == person_id)]
            for group in archive.get("groups", []) if isinstance(archive.get("groups"), list) else []:
                cells = group.get("cells") if isinstance(group, dict) and isinstance(group.get("cells"), dict) else {}
                for row, ids in list(cells.items()):
                    if isinstance(ids, list):
                        cells[row] = [pid for pid in ids if pid != person_id]
        encoded = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        conn.execute("UPDATE app_state SET data = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1", (encoded,))
        conn.commit()
    if upload_id and not _upload_referenced(state, upload_id):
        path = UPLOAD_DIR / upload_id
        for candidate in UPLOAD_DIR.glob(upload_id + ".*"):
            try: candidate.unlink()
            except OSError: pass
    write_audit("person_data_erased", {"personId": person_id, "archivesScrubbed": True})
    return jsonify({"ok": True})

@app.get("/api/smtp-settings")
@roles_required(ROLE_SUPERADMIN)
def get_smtp_settings():
    cfg = load_smtp_config()
    return jsonify({
        "configured": smtp_configured(cfg),
        "stored": bool(cfg.get("stored")),
        "host": cfg.get("host", ""),
        "port": cfg.get("port", 587),
        "username": cfg.get("username", ""),
        "passwordSet": bool(cfg.get("password")),
        "fromAddress": cfg.get("from_address", ""),
        "security": cfg.get("security", "starttls"),
        "appBaseUrl": cfg.get("app_base_url", ""),
        "resetTokenTtlMinutes": cfg.get("reset_token_ttl_minutes", 30),
    })


@app.put("/api/smtp-settings")
@roles_required(ROLE_SUPERADMIN)
def update_smtp_settings():
    payload = request.get_json(silent=True) or {}
    current = load_smtp_config()
    host = str(payload.get("host") or "").strip()
    from_address = normalize_email(payload.get("fromAddress"))
    username = str(payload.get("username") or "").strip()
    security = str(payload.get("security") or "starttls").strip().lower()
    app_base_url = str(payload.get("appBaseUrl") or "").strip().rstrip("/")
    password = str(payload.get("password") or "")
    if from_address is None:
        return jsonify({"error": "Bitte eine gültige Absender-E-Mail-Adresse eingeben."}), 400
    if security not in {"starttls", "ssl", "none"}:
        return jsonify({"error": "Ungültige SMTP-Sicherheitsart."}), 400
    try:
        port = int(payload.get("port") or 587)
    except (TypeError, ValueError):
        return jsonify({"error": "SMTP-Port muss eine Zahl sein."}), 400
    if not 1 <= port <= 65535:
        return jsonify({"error": "SMTP-Port muss zwischen 1 und 65535 liegen."}), 400
    try:
        ttl = int(payload.get("resetTokenTtlMinutes") or 30)
    except (TypeError, ValueError):
        return jsonify({"error": "Gültigkeitsdauer muss eine Zahl sein."}), 400
    if not 5 <= ttl <= 1440:
        return jsonify({"error": "Gültigkeitsdauer muss zwischen 5 und 1440 Minuten liegen."}), 400
    if app_base_url and not re.match(r"^https?://", app_base_url, re.I):
        return jsonify({"error": "Die öffentliche App-URL muss mit http:// oder https:// beginnen."}), 400
    config = {
        "host": host, "port": port, "username": username,
        "password": password if password else current.get("password", ""),
        "from_address": from_address or "", "security": security,
        "app_base_url": app_base_url, "reset_token_ttl_minutes": ttl,
    }
    saved = save_smtp_config(config)
    write_audit("smtp_settings_changed", {"configured": smtp_configured(saved), "security": saved.get("security")})
    return jsonify({"ok": True, "configured": smtp_configured(saved), "passwordSet": bool(saved.get("password"))})


@app.post("/api/smtp-test")
@roles_required(ROLE_SUPERADMIN)
def smtp_test():
    payload = request.get_json(silent=True) or {}
    recipient = normalize_email(payload.get("recipient"))
    if not recipient:
        return jsonify({"error": "Bitte eine gültige Empfänger-E-Mail-Adresse eingeben."}), 400
    cfg = load_smtp_config()
    if not smtp_configured(cfg):
        return jsonify({"error": "SMTP ist noch nicht vollständig eingerichtet."}), 400
    try:
        send_smtp_test_email(recipient, cfg)
        write_audit("smtp_test_sent", {"recipientConfigured": True})
    except Exception as exc:
        app.logger.exception("SMTP test failed: %s", exc)
        return jsonify({"error": f"Test-E-Mail konnte nicht versendet werden: {exc}"}), 502
    return jsonify({"ok": True, "message": f"Test-E-Mail wurde an {recipient} versendet."})


@app.post("/api/donations/unlock")
@roles_required(ROLE_SUPERADMIN, ROLE_DONATION)
def unlock_donations():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    with db_connection() as conn:
        auth = current_account(conn, username)
    if not auth or not auth["active"] or auth["role"] not in {ROLE_SUPERADMIN, ROLE_DONATION} or not check_password_hash(auth["password_hash"], password):
        return jsonify({"error": "Spenden-/Superadmin-Zugangsdaten sind nicht korrekt."}), 403
    session["donation_unlocked_until"] = time.time() + 30 * 60
    session["donation_unlocked_by"] = username
    return jsonify({"ok": True, "unlockedMinutes": 30, "unlockedBy": username})


@app.post("/api/donations/relock")
@roles_required(ROLE_SUPERADMIN, ROLE_DONATION)
def relock_donations():
    session.pop("donation_unlocked_until", None)
    session.pop("donation_unlocked_by", None)
    return jsonify({"ok": True})


@app.post("/api/donations/finalize")
@roles_required(ROLE_SUPERADMIN, ROLE_DONATION)
def finalize_donations():
    with db_connection() as conn:
        state, _ = load_state_row(conn)
        settings = state.setdefault("settings", {})
        donations = settings.setdefault("donations", {})
        if not donations.get("date") or not str(donations.get("auditor1") or "").strip() or not str(donations.get("auditor2") or "").strip():
            return jsonify({"error": "Vor dem Abschluss müssen Datum sowie beide Prüfer angegeben sein."}), 400
        if donations.get("closed"):
            return jsonify({"ok": True, "alreadyClosed": True})
        donations["started"] = True
        donations["closed"] = True
        donations["closedAt"] = datetime.now(timezone.utc).isoformat()
        donations["closedBy"] = session.get("username")
        donations["finalTotal"] = donation_total(donations)
        encoded = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        conn.execute("UPDATE app_state SET data = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1", (encoded,))
        conn.commit()
    session.pop("donation_unlocked_until", None)
    return jsonify({"ok": True, "finalTotal": donations["finalTotal"]})


@app.post("/api/donations/reset")
@roles_required(ROLE_SUPERADMIN, ROLE_DONATION)
def reset_donations():
    payload = request.get_json(silent=True) or {}
    password = str(payload.get("password") or "")
    if not password:
        return jsonify({"error": "Bitte das Superadmin-Passwort eingeben."}), 400

    with db_connection() as conn:
        superadmins = conn.execute(
            "SELECT username, password_hash FROM user_accounts WHERE role = ? AND active = 1",
            (ROLE_SUPERADMIN,),
        ).fetchall()
        verified = next((row for row in superadmins if check_password_hash(row["password_hash"], password)), None)
        if not verified:
            return jsonify({"error": "Das Superadmin-Passwort ist nicht korrekt."}), 403

        state, _ = load_state_row(conn)
        settings = state.setdefault("settings", {})
        settings["donations"] = {
            "date": "",
            "auditor1": "",
            "auditor2": "",
            "records": {},
            "started": False,
            "closed": False,
            "closedAt": "",
            "closedBy": "",
        }
        encoded = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        conn.execute("UPDATE app_state SET data = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1", (encoded,))
        conn.commit()

    session.pop("donation_unlocked_until", None)
    session.pop("donation_unlocked_by", None)
    return jsonify({"ok": True, "resetBy": verified["username"]})


@app.get("/api/donations/live")
@login_required
def donations_live_data():
    with db_connection() as conn:
        state, updated_at = load_state_row(conn)
    donations = state.get("settings", {}).get("donations", {})
    records = donations.get("records", {}) if isinstance(donations, dict) else {}
    groups_out = []
    for group in state.get("groups", []):
        gid = group.get("id")
        record = records.get(gid, {}) if isinstance(records, dict) else {}
        subtotal = donation_total({"records": {gid: record}})
        groups_out.append({"id": gid, "name": group.get("name") or "Gruppe", "sum": subtotal})
    return jsonify({
        "date": donations.get("date") or "",
        "started": bool(donations.get("started")),
        "closed": bool(donations.get("closed")),
        "closedAt": donations.get("closedAt"),
        "total": donation_total(donations),
        "groups": groups_out,
        "updatedAt": updated_at,
    })


@app.post("/api/consent/<person_id>")
@roles_required(ROLE_SUPERADMIN, ROLE_MANAGEMENT)
def upload_consent(person_id):
    if not person_id or len(person_id) > 160:
        return jsonify({"error": "Ungültige Personen-ID"}), 400
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "Keine Datei ausgewählt"}), 400
    original_name = secure_filename(file.filename)
    ext = Path(original_name).suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        return jsonify({"error": "Erlaubt sind PDF, PNG, JPG und JPEG"}), 400
    upload_id = uuid.uuid4().hex
    stored_path = UPLOAD_DIR / f"{upload_id}{ext}"
    file.save(stored_path)
    if stored_path.stat().st_size > MAX_UPLOAD_BYTES:
        stored_path.unlink(missing_ok=True)
        return jsonify({"error": "Datei ist zu groß"}), 413
    return jsonify({"id": upload_id, "name": original_name or f"einverstaendnis{ext}", "extension": ext, "size": stored_path.stat().st_size})


def find_upload(upload_id):
    if not UPLOAD_ID_RE.fullmatch(upload_id or ""):
        return None
    matches = list(UPLOAD_DIR.glob(f"{upload_id}.*"))
    return matches[0] if matches else None


@app.get("/api/uploads/<upload_id>")
@login_required
def get_upload(upload_id):
    path = find_upload(upload_id)
    if not path or not path.is_file():
        return jsonify({"error": "Datei nicht gefunden"}), 404
    return send_file(path, as_attachment=False)


@app.delete("/api/uploads/<upload_id>")
@roles_required(ROLE_SUPERADMIN, ROLE_MANAGEMENT)
def delete_upload(upload_id):
    path = find_upload(upload_id)
    if path and path.is_file():
        path.unlink(missing_ok=True)
    return jsonify({"ok": True})


@app.get("/api/geocode")
@roles_required(ROLE_SUPERADMIN, ROLE_MANAGEMENT)
def geocode_address():
    if not external_maps_enabled():
        return jsonify({"error": "Externe Kartendienste sind in den Datenschutz-Einstellungen deaktiviert."}), 403
    address = (request.args.get("address") or "").strip()
    postcode = (request.args.get("postcode") or "").strip()
    if not address and not postcode:
        return jsonify({"error": "Adresse oder PLZ fehlt"}), 400
    if len(address) > 300 or len(postcode) > 16:
        return jsonify({"error": "Suchangabe ist zu lang"}), 400
    query = address if address else f"{postcode}, Deutschland"
    params = urllib.parse.urlencode({"q": query, "format": "jsonv2", "limit": 1, "addressdetails": 1, "countrycodes": "de"})
    try:
        results = fetch_json(f"{OSM_NOMINATIM_URL}?{params}", timeout=15)
        if not isinstance(results, list) or not results:
            return jsonify({"error": "Adresse wurde bei OpenStreetMap nicht gefunden"}), 404
        hit = results[0]
        addr = hit.get("address") or {}
        city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality") or addr.get("suburb") or ""
        return jsonify({
            "lat": float(hit["lat"]) if hit.get("lat") is not None else None,
            "lon": float(hit["lon"]) if hit.get("lon") is not None else None,
            "city": city,
            "postcode": addr.get("postcode") or postcode,
            "displayName": hit.get("display_name", query),
            "source": "OpenStreetMap",
        })
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, KeyError, json.JSONDecodeError) as exc:
        app.logger.warning("OSM geocode failed: %s", exc)
        return jsonify({"error": "Adresse konnte momentan nicht über OpenStreetMap gesucht werden"}), 502



@app.post("/api/action/rollover")
@roles_required(ROLE_SUPERADMIN)
def rollover_action():
    payload = request.get_json(silent=True) or {}
    next_date = str(payload.get("nextDate") or "").strip()
    match = re.fullmatch(r"(\d{4})-01-06", next_date)
    if not match:
        return jsonify({"error": "Die neue Aktion muss am 6. Januar liegen."}), 400
    next_year = int(match.group(1))
    if next_year < 2020 or next_year > 2100:
        return jsonify({"error": "Ungültiges Aktionsjahr."}), 400
    requested_categories = payload.get("transferCategories") or []
    allowed_categories = {"sternsinger", "begleiter", "mittagessen", "helfer"}
    transfer_categories = [c for c in requested_categories if c in allowed_categories]
    keep_group_structure = bool(payload.get("keepGroupStructure", True))

    with db_connection() as conn:
        state, _ = load_state_row(conn)
        people = state.get("people", []) if isinstance(state.get("people"), list) else []
        groups = state.get("groups", []) if isinstance(state.get("groups"), list) else []
        settings = state.get("settings", {}) if isinstance(state.get("settings"), dict) else {}
        archives = settings.get("archives", []) if isinstance(settings.get("archives"), list) else []
        current_action = settings.get("action", {}) if isinstance(settings.get("action"), dict) else {}
        current_year = current_action.get("year") or str(settings.get("gdDatum") or "")[:4] or str(next_year - 1)
        archive_label = str(payload.get("archiveLabel") or "").strip()[:160] or f"Sternsinger {current_year} – abgeschlossen"

        settings_snapshot = json.loads(json.dumps(settings, ensure_ascii=False))
        settings_snapshot.pop("archives", None)
        archives.insert(0, {
            "id": f"archive_{uuid.uuid4().hex}",
            "label": archive_label,
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "archiveType": "action",
            "actionDate": current_action.get("date") or settings.get("gdDatum") or "",
            "people": json.loads(json.dumps(people, ensure_ascii=False)),
            "groups": json.loads(json.dumps(groups, ensure_ascii=False)),
            "settings": settings_snapshot,
        })

        new_people = []
        for person in people:
            if not isinstance(person, dict) or person.get("category") not in transfer_categories:
                continue
            copy_person = json.loads(json.dumps(person, ensure_ascii=False))
            copy_person["groupId"] = None
            if copy_person.get("category") == "helfer":
                copy_person["taetigkeiten"] = []
            if copy_person.get("category") == "sternsinger":
                copy_person["einverstaendnis"] = False
                copy_person["fotoEinverstaendnis"] = False
                copy_person["einverstaendnisDatei"] = None
                copy_person["sternsingerNotiz"] = ""
            new_people.append(copy_person)

        rows = ['Sternträger', 'Sternhüter', 'Kaspar (Weißer)', 'Melchior (Brauner)', 'Balthasar (Schwarzer)', 'Begleiter', 'Mittagessen']
        new_groups = []
        if keep_group_structure:
            for idx, group in enumerate(groups):
                if not isinstance(group, dict):
                    continue
                new_groups.append({
                    "id": f"group_{uuid.uuid4().hex}",
                    "name": str(group.get("name") or f"Gruppe {idx + 1}"),
                    "cells": {row: [] for row in rows},
                    "streetAssignments": [],
                    "extra": {
                        "kleiderDatum": f"{next_year}-01-03",
                        "kleiderZeit": settings.get("kleiderZeit", ""),
                        "gdDatum": next_date,
                        "gdZeit": settings.get("gdZeit", ""),
                        "gestartetUm": "", "zurueckUm": "", "spendenErgebnis": "", "geprueftVon": "",
                    },
                })

        new_settings = {
            "kirche": settings.get("kirche", ""),
            "kleiderDatum": f"{next_year}-01-03",
            "kleiderZeit": settings.get("kleiderZeit", ""),
            "gdDatum": next_date,
            "gdZeit": settings.get("gdZeit", ""),
            "streetPostcode": settings.get("streetPostcode", ""),
            "streetCity": settings.get("streetCity", ""),
            "streetSearchRadius": settings.get("streetSearchRadius", 1400),
            "streets": json.loads(json.dumps(settings.get("streets", []), ensure_ascii=False)),
            "houseEnabled": settings.get("houseEnabled", True),
            "houseRegistrations": [],
            "houseDefaultPostcode": settings.get("houseDefaultPostcode", settings.get("streetPostcode", "")),
            "houseDefaultCity": settings.get("houseDefaultCity", settings.get("streetCity", "")),
            "archives": archives,
            "action": {
                "id": f"action_{uuid.uuid4().hex}", "year": next_year, "date": next_date,
                "label": f"Sternsinger {next_year}", "status": "active", "startedAt": datetime.now(timezone.utc).isoformat(),
            },
            "donations": {
                "date": "", "auditor1": "", "auditor2": "", "started": False, "closed": False,
                "closedAt": "", "closedBy": "", "finalTotal": None, "records": {},
            },
        }
        clean = {"people": new_people, "groups": new_groups, "settings": new_settings, "savedDate": datetime.now(timezone.utc).isoformat()}
        encoded = json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_STATE_BYTES:
            return jsonify({"error": "Der archivierte Datenbestand ist zu groß. Bitte zuerst ein externes JSON-Backup erstellen und alte Archive entfernen."}), 413
        conn.execute("""
            INSERT INTO app_state (id, data, updated_at) VALUES (1, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET data = excluded.data, updated_at = CURRENT_TIMESTAMP
        """, (encoded,))
        conn.commit()
    session.pop("donation_unlocked_until", None)
    session.pop("donation_unlocked_by", None)
    write_audit("action_rollover", {"year": next_year, "archiveLabel": archive_label, "peopleTransferred": len(new_people), "groupsCreated": len(new_groups)})
    return jsonify({"ok": True, "year": next_year, "archiveLabel": archive_label, "peopleTransferred": len(new_people), "groupsCreated": len(new_groups)})


def _nominatim_candidates(query, limit=6, geojson=False):
    params = {"q": query, "limit": limit, "countrycodes": "de", "addressdetails": 1}
    if geojson:
        params.update({"format": "geojson", "polygon_geojson": 1})
    else:
        params.update({"format": "jsonv2"})
    return fetch_json(f"{OSM_NOMINATIM_URL}?{urllib.parse.urlencode(params)}", timeout=18)


def _geocode_one(query):
    try:
        hits = _nominatim_candidates(query, limit=5, geojson=False)
        if isinstance(hits, list) and hits:
            hit = hits[0]
            return {"lat": float(hit["lat"]), "lon": float(hit["lon"]), "displayName": hit.get("display_name", query)}
    except Exception as exc:
        app.logger.info("Geocode failed for %s: %s", query, exc)
    return None


def _overpass_street_lines(name, lat, lon, radius=6500):
    """Return every OSM way segment for the named street around the resolved place."""
    try:
        radius = max(3500, min(int(radius or 6500), 12000))
    except (TypeError, ValueError):
        radius = 6500
    regex_name = re.escape(str(name or "").strip()).replace('"', '\\\"')
    query = (
        f'[out:json][timeout:28];'
        f'way(around:{radius},{lat},{lon})["highway"]["name"~"^{regex_name}$",i];'
        f'out geom;'
    )
    for endpoint in (OSM_OVERPASS_URL, OSM_OVERPASS_FALLBACK_URL):
        if not endpoint:
            continue
        try:
            data = fetch_json(endpoint, data=urllib.parse.urlencode({"data": query}).encode("utf-8"), timeout=36)
            lines = []
            seen = set()
            for element in (data.get("elements") or []) if isinstance(data, dict) else []:
                way_id = element.get("id")
                if way_id is not None and way_id in seen:
                    continue
                geom = element.get("geometry") or []
                line = [[float(p["lat"]), float(p["lon"])] for p in geom if p.get("lat") is not None and p.get("lon") is not None]
                if len(line) >= 2:
                    lines.append(line)
                    if way_id is not None:
                        seen.add(way_id)
            if lines:
                return lines
        except Exception as exc:
            app.logger.info("Overpass geometry failed for %s: %s", name, exc)
    return []

def _nominatim_geojson_lines(query):
    try:
        data = _nominatim_candidates(query, limit=8, geojson=True)
        lines = []
        for feature in data.get("features", []) if isinstance(data, dict) else []:
            geom = feature.get("geometry") or {}
            coords = geom.get("coordinates") or []
            if geom.get("type") == "LineString":
                line = [[float(lat), float(lon)] for lon, lat in coords]
                if len(line) >= 2: lines.append(line)
            elif geom.get("type") == "MultiLineString":
                for part in coords:
                    line = [[float(lat), float(lon)] for lon, lat in part]
                    if len(line) >= 2: lines.append(line)
        return lines
    except Exception as exc:
        app.logger.info("Nominatim geometry failed for %s: %s", query, exc)
        return []


def _section_house_numbers(section):
    match = re.search(r"\b(\d+[a-zA-Z]?)\s*(?:-|–|—|bis)\s*(\d+[a-zA-Z]?)\b", str(section or ""), re.IGNORECASE)
    return (match.group(1), match.group(2)) if match else (None, None)


def _osrm_line(start, end):
    if not start or not end or not OSM_OSRM_URL:
        return []
    try:
        url = f"{OSM_OSRM_URL}/route/v1/driving/{start['lon']},{start['lat']};{end['lon']},{end['lat']}?overview=full&geometries=geojson&steps=false"
        data = fetch_json(url, timeout=20)
        coords = (((data.get("routes") or [{}])[0].get("geometry") or {}).get("coordinates") or [])
        return [[float(lat), float(lon)] for lon, lat in coords]
    except Exception as exc:
        app.logger.info("OSRM section route failed: %s", exc)
        return []


@app.get("/api/group-route-map/<group_id>")
@roles_required(ROLE_SUPERADMIN, ROLE_MANAGEMENT)
def group_route_map_data(group_id):
    if not external_maps_enabled():
        return jsonify({"error": "Externe Kartendienste sind in den Datenschutz-Einstellungen deaktiviert."}), 403
    with db_connection() as conn:
        state, _ = load_state_row(conn)
    settings = state.get("settings", {}) if isinstance(state.get("settings"), dict) else {}
    group = next((g for g in state.get("groups", []) if isinstance(g, dict) and str(g.get("id")) == group_id), None)
    if not group:
        return jsonify({"error": "Gruppe nicht gefunden."}), 404
    assignments = group.get("streetAssignments", []) if isinstance(group.get("streetAssignments"), list) else []
    postcode = str(settings.get("streetPostcode") or "").strip()
    city = str(settings.get("streetCity") or "").strip()
    routes = []
    for idx, assignment in enumerate(assignments[:30]):
        if not isinstance(assignment, dict):
            continue
        street = str(assignment.get("streetName") or "").strip()
        if not street:
            sid = assignment.get("streetId")
            street_obj = next((x for x in settings.get("streets", []) if isinstance(x, dict) and x.get("id") == sid), None)
            street = str((street_obj or {}).get("name") or "").strip()
        if not street:
            continue
        section = str(assignment.get("section") or "").strip()
        query_variants = [f"{street}, {postcode} {city}, Deutschland" if postcode or city else f"{street}, Deutschland"]
        if postcode == "77933" and city.casefold() == "sulz":
            query_variants.append(f"{street}, {postcode} Lahr/Schwarzwald, Deutschland")
        center = None
        for query in query_variants:
            center = _geocode_one(query)
            if center: break
        # Straßen bestehen in OSM oft aus vielen einzelnen Ways. Für die Gruppenkarte
        # suchen wir deshalb großzügiger als beim Stammdaten-Abruf, damit auch weiter
        # entfernte Teilstücke derselben Straße (z. B. Altengai) vollständig erscheinen.
        configured_radius = settings.get("streetSearchRadius", 1400)
        try:
            geometry_radius = max(6000, min(int(configured_radius or 1400) * 4, 10000))
        except (TypeError, ValueError):
            geometry_radius = 6500
        lines = _overpass_street_lines(street, center["lat"], center["lon"], geometry_radius) if center else []
        if not lines:
            for query in query_variants:
                lines = _nominatim_geojson_lines(query)
                if lines: break
        start_no, end_no = _section_house_numbers(section)
        start = end = None
        section_line = []
        if start_no and end_no:
            for query_city in ([city, "Lahr/Schwarzwald"] if postcode == "77933" and city.casefold() == "sulz" else [city]):
                start = start or _geocode_one(f"{street} {start_no}, {postcode} {query_city}, Deutschland")
                end = end or _geocode_one(f"{street} {end_no}, {postcode} {query_city}, Deutschland")
            section_line = _osrm_line(start, end)
        routes.append({
            "id": assignment.get("id") or str(idx), "street": street, "section": section,
            "lines": lines, "sectionLine": section_line, "start": start, "end": end,
            "segmentCount": len(lines), "resolved": bool(lines or center or section_line), "center": center,
        })
    return jsonify({
        "group": {"id": group.get("id"), "name": group.get("name") or "Gruppe"},
        "postcode": postcode, "city": city, "church": settings.get("kirche", ""),
        "routes": routes, "source": "Kartendaten © OpenStreetMap-Mitwirkende",
    })


@app.get("/api/streets")
@roles_required(ROLE_SUPERADMIN, ROLE_MANAGEMENT)
def lookup_streets():
    if not external_maps_enabled():
        return jsonify({"error": "Externe Kartendienste sind in den Datenschutz-Einstellungen deaktiviert."}), 403
    postcode = (request.args.get("postcode") or "").strip()
    city = (request.args.get("city") or "").strip()
    try:
        radius = int(request.args.get("radius") or 1400)
    except ValueError:
        radius = 1400
    radius = max(300, min(radius, 5000))
    if not postcode or not city:
        return jsonify({"error": "Bitte PLZ und Ort / Ortsteil angeben"}), 400
    if len(postcode) > 12 or len(city) > 120:
        return jsonify({"error": "PLZ oder Ort ist zu lang"}), 400

    query_variants = [
        f"{city}, {postcode}, Deutschland",
        f"{city}, Lahr/Schwarzwald, {postcode}, Deutschland" if postcode == "77933" else None,
        f"{postcode} {city}, Deutschland",
        f"{city}, Deutschland",
    ]
    query_variants = [q for q in query_variants if q]
    hits, seen, nominatim_errors = [], set(), []
    for query in query_variants:
        params = urllib.parse.urlencode({"q": query, "format": "jsonv2", "limit": 10, "addressdetails": 1, "namedetails": 1, "countrycodes": "de"})
        try:
            result = fetch_json(f"{OSM_NOMINATIM_URL}?{params}", timeout=20)
        except Exception as exc:
            nominatim_errors.append(str(exc))
            continue
        if not isinstance(result, list):
            continue
        for hit in result:
            key = (hit.get("osm_type"), hit.get("osm_id"), hit.get("lat"), hit.get("lon"))
            if key not in seen:
                seen.add(key)
                hits.append(hit)
    if not hits:
        app.logger.warning("OSM Nominatim failed/no hits: %s", " | ".join(nominatim_errors))
        return jsonify({"error": "Ort/PLZ wurde bei OpenStreetMap nicht gefunden oder der Suchdienst ist nicht erreichbar.", "stage": "nominatim"}), 502

    city_cf, postcode_cf = city.casefold(), postcode.casefold()
    def score_hit(hit):
        addr = hit.get("address") or {}
        display = str(hit.get("display_name") or "").casefold()
        postcode_hit = str(addr.get("postcode") or "").casefold()
        locality_values = [addr.get("city"), addr.get("town"), addr.get("village"), addr.get("suburb"), addr.get("hamlet"), addr.get("municipality"), addr.get("city_district"), addr.get("quarter"), addr.get("neighbourhood")]
        localities = [str(v or "").casefold() for v in locality_values if v]
        score = 0
        if city_cf in localities: score += 500
        elif any(city_cf in value for value in localities): score += 260
        elif city_cf in display: score += 150
        if postcode_hit == postcode_cf: score += 260
        elif postcode_cf and postcode_cf in display: score += 120
        if str(hit.get("type") or "").casefold() in {"suburb", "village", "quarter", "neighbourhood", "hamlet"}: score += 100
        return score
    hits.sort(key=score_hit, reverse=True)
    hit = hits[0]
    try:
        lat, lon = float(hit.get("lat")), float(hit.get("lon"))
    except (TypeError, ValueError):
        return jsonify({"error": "Für den Ort konnten keine Koordinaten ermittelt werden.", "stage": "nominatim"}), 502

    allowed_highways = {"residential", "living_street", "unclassified", "tertiary", "secondary", "primary", "pedestrian", "service", "road"}
    def extract_overpass_names(data):
        names = set()
        for el in (data.get("elements") or []):
            tags = el.get("tags", {}) or {}
            name, highway = str(tags.get("name", "")).strip(), str(tags.get("highway", "")).strip()
            if name and highway in allowed_highways: names.add(name)
        return sorted(names, key=lambda x: x.casefold())

    query = '[out:json][timeout:25];\n(' + f'way(around:{radius},{lat},{lon})["highway"]["name"];\n' + ');\nout tags;'
    provider_errors = []
    endpoints = []
    for endpoint in (OSM_OVERPASS_URL, OSM_OVERPASS_FALLBACK_URL):
        if endpoint and endpoint not in endpoints: endpoints.append(endpoint)
    for endpoint in endpoints:
        for method in ("GET", "POST"):
            try:
                if method == "GET":
                    data = fetch_json(f"{endpoint}?{urllib.parse.urlencode({'data': query})}", timeout=35)
                else:
                    data = fetch_json(endpoint, data=urllib.parse.urlencode({"data": query}).encode("utf-8"), timeout=35)
                names = extract_overpass_names(data) if isinstance(data, dict) else []
                if names:
                    return jsonify({"streets": names[:2500], "source": "OpenStreetMap / Overpass", "provider": f"overpass-{method.lower()}", "displayName": hit.get("display_name", f"{postcode} {city}"), "postcode": postcode, "city": city, "count": len(names), "radius": radius, "center": {"lat": lat, "lon": lon}, "searchMode": f"Umkreissuche {radius} m"})
            except Exception as exc:
                provider_errors.append(f"{method} {endpoint}: {exc}")

    lat_delta = radius / 111_320.0
    lon_scale = max(0.25, abs(math.cos(math.radians(lat))))
    lon_delta = radius / (111_320.0 * lon_scale)
    south, north, west, east = lat - lat_delta, lat + lat_delta, lon - lon_delta, lon + lon_delta
    try:
        xml_data = fetch_bytes(f"{OSM_MAP_URL}?bbox={west},{south},{east},{north}", timeout=45, accept="application/xml,text/xml;q=0.9,*/*;q=0.8")
        root = ET.fromstring(xml_data)
        names = set()
        for way in root.findall("way"):
            tags = {tag.attrib.get("k"): tag.attrib.get("v") for tag in way.findall("tag")}
            highway, name = (tags.get("highway") or "").strip(), (tags.get("name") or "").strip()
            if name and highway in allowed_highways: names.add(name)
        names = sorted(names, key=lambda x: x.casefold())
        if names:
            return jsonify({"streets": names[:2500], "source": "OpenStreetMap Karten-API", "provider": "osm-map", "displayName": hit.get("display_name", f"{postcode} {city}"), "postcode": postcode, "city": city, "count": len(names), "radius": radius, "center": {"lat": lat, "lon": lon}, "searchMode": f"Umkreissuche {radius} m"})
    except Exception as exc:
        provider_errors.append(f"OSM map fallback: {exc}")
    app.logger.warning("All street providers failed: %s", " | ".join(provider_errors))
    return jsonify({"error": "Der Ort wurde gefunden, aber die Straßendaten konnten aktuell nicht geladen werden. Prüfe die Internet-/DNS-Verbindung des Portainer-Hosts oder trage Straßen manuell ein.", "stage": "providers", "displayName": hit.get("display_name", f"{postcode} {city}"), "radius": radius}), 502


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"error": "Datei oder Datensatz ist zu groß"}), 413


init_db()
