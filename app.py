"""ORCA Agency Dashboard — Flask backend.

Adds (this revision):
  * users.email column (registration & lookup); admin assigns tasks by email
  * SMTP email notifications (Gmail / Outlook / any provider) — free, via stdlib
  * settings table for SMTP config, manageable from the admin UI
  * task review flow: needs_changes returns the task to "in_progress"
  * task private message thread (employee ↔ admin) with attachments
  * editable site content (WordPress-like): /api/content GET/PUT for admins

Run:  python app.py    →    open http://localhost:5000
"""
import os
import sqlite3
import hashlib
import secrets
import json
import uuid
import smtplib
import ssl
from email.message import EmailMessage
from datetime import datetime
from werkzeug.utils import secure_filename
from flask import Flask, request, jsonify, send_from_directory, session, g

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "orca.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")
UPLOAD_DIR = os.path.join(STATIC_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="")
app.secret_key = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=20 * 1024 * 1024,
)


# ============================================================
# Database helpers
# ============================================================

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE,
  email TEXT UNIQUE,
  password_hash TEXT NOT NULL,
  password_salt TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'viewer',
  approved INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS clients (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  serial TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  company TEXT,
  sector TEXT NOT NULL DEFAULT 'other',
  email TEXT,
  phone TEXT,
  address TEXT,
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  description TEXT,
  cost REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS attachments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  client_id INTEGER REFERENCES clients(id) ON DELETE CASCADE,
  task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
  file_name TEXT NOT NULL,
  url TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS employees (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  serial TEXT NOT NULL UNIQUE,
  full_name TEXT NOT NULL,
  email TEXT NOT NULL,
  phone TEXT,
  position TEXT,
  department TEXT,
  role TEXT NOT NULL DEFAULT 'employee',
  user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  description TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  assignee_id INTEGER REFERENCES employees(id) ON DELETE SET NULL,
  client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL,
  due_date TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS task_submissions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  message TEXT,
  attachment_name TEXT,
  attachment_url TEXT,
  submitted_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS task_reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  decision TEXT NOT NULL,
  feedback TEXT,
  reviewed_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS task_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  sender_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  body TEXT,
  attachment_name TEXT,
  attachment_url TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  recipient_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  type TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT,
  related_task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
  read INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  serial TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  language TEXT NOT NULL DEFAULT 'ar',
  client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL,
  client_name TEXT NOT NULL,
  client_company TEXT,
  client_address TEXT,
  client_email TEXT,
  client_phone TEXT,
  items_json TEXT NOT NULL DEFAULT '[]',
  subtotal REAL NOT NULL DEFAULT 0,
  discount_percent REAL NOT NULL DEFAULT 0,
  vat_percent REAL NOT NULL DEFAULT 15,
  total REAL NOT NULL DEFAULT 0,
  currency TEXT NOT NULL DEFAULT 'SAR',
  notes TEXT,
  valid_until TEXT,
  contract_subject TEXT,
  contract_terms TEXT,
  party_one_name TEXT,
  party_two_name TEXT,
  issued_at TEXT NOT NULL DEFAULT (datetime('now')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS content_overrides (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS password_reset_tokens (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token TEXT NOT NULL UNIQUE,
  expires_at TEXT NOT NULL,
  used INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS employee_permissions (
  employee_id INTEGER PRIMARY KEY REFERENCES employees(id) ON DELETE CASCADE,
  can_view_clients INTEGER NOT NULL DEFAULT 1,
  can_edit_clients INTEGER NOT NULL DEFAULT 0,
  can_view_documents INTEGER NOT NULL DEFAULT 1,
  can_view_tasks INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    # idempotent column adds for upgrades
    for stmt in (
        "ALTER TABLE employees ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE SET NULL",
        "ALTER TABLE users ADD COLUMN email TEXT",
        "ALTER TABLE documents ADD COLUMN contract_body TEXT",
        "ALTER TABLE documents ADD COLUMN contract_agreement TEXT",
        "ALTER TABLE documents ADD COLUMN party_one_capacity TEXT",
        "ALTER TABLE documents ADD COLUMN party_one_id_number TEXT",
        "ALTER TABLE documents ADD COLUMN party_one_cr_number TEXT",
        "ALTER TABLE documents ADD COLUMN party_two_capacity TEXT",
        "ALTER TABLE documents ADD COLUMN party_two_id_number TEXT",
        "ALTER TABLE documents ADD COLUMN party_two_cr_number TEXT",
        "ALTER TABLE users ADD COLUMN approved INTEGER NOT NULL DEFAULT 1",
    ):
        try:
            db.execute(stmt)
        except sqlite3.OperationalError:
            pass
    # bootstrap admin
    row = db.execute("SELECT id FROM users WHERE username='admin'").fetchone()
    if not row:
        password = os.environ.get("ADMIN_PASSWORD", "admin123")
        salt = secrets.token_hex(16)
        h = hash_password(password, salt)
        db.execute(
            "INSERT INTO users (username, email, password_hash, password_salt, role, approved) VALUES (?,?,?,?,?,1)",
            ("admin", os.environ.get("ADMIN_EMAIL"), h, salt, "admin"),
        )
        db.commit()
    # Ensure admin and all role=admin accounts are always approved (migration safety)
    db.execute("UPDATE users SET approved=1 WHERE role='admin'")
    db.commit()
    # ---- Backfill: link any employee row that has no user_id but whose
    # email matches an existing user (case-insensitive). This repairs
    # legacy/mistyped data so notifications and email reach the right
    # person without manual intervention.
    try:
        linked = db.execute(
            "UPDATE employees "
            "   SET user_id = (SELECT id FROM users "
            "                  WHERE LOWER(TRIM(users.email)) = LOWER(TRIM(employees.email)) "
            "                    AND users.email IS NOT NULL AND users.email <> '' LIMIT 1) "
            " WHERE user_id IS NULL "
            "   AND email IS NOT NULL AND email <> ''"
        ).rowcount
        if linked:
            print(f"[orca] backfilled employee↔user links: {linked}")
        db.commit()
    except sqlite3.OperationalError as exc:
        print(f"[orca] employee backfill skipped: {exc}")
    db.close()


# ============================================================
# Auth helpers
# ============================================================

def hash_password(password: str, salt: str) -> str:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt.encode("utf-8"),
        n=16384, r=8, p=1, dklen=64,
    ).hex()


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    row = get_db().execute(
        "SELECT id, username, email, role, created_at FROM users WHERE id=?", (uid,)
    ).fetchone()
    return dict(row) if row else None


def require_login():
    u = current_user()
    if not u:
        return jsonify({"error": "unauthorized"}), 401
    return None


def require_admin():
    u = current_user()
    if not u or u["role"] != "admin":
        return jsonify({"error": "forbidden"}), 403
    return None


def is_super_admin():
    """Returns True only for the root 'admin' account (username == 'admin')."""
    u = current_user()
    return bool(u and u["role"] == "admin" and u.get("username") == "admin")


# ============================================================
# Settings (for SMTP / email)
# ============================================================

DEFAULT_SETTING_KEYS = (
    "smtp_host", "smtp_port", "smtp_user", "smtp_password",
    "smtp_from", "smtp_from_name", "smtp_use_tls",
)


def get_setting(key, default=None):
    db = get_db()
    row = db.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    if row and row["value"] is not None:
        return row["value"]
    return os.environ.get(key.upper(), default)


def set_setting(key, value):
    db = get_db()
    db.execute(
        "INSERT INTO app_settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    db.commit()


def smtp_config():
    return {
        "host": get_setting("smtp_host", ""),
        "port": int(get_setting("smtp_port", "587") or 587),
        "user": get_setting("smtp_user", ""),
        "password": get_setting("smtp_password", ""),
        "from": get_setting("smtp_from", "") or get_setting("smtp_user", ""),
        "from_name": get_setting("smtp_from_name", "ORCA Agency"),
        "use_tls": (get_setting("smtp_use_tls", "1") or "1") not in ("0", "false", "False"),
    }


# ============================================================
# Branded HTML email — "ORCA Manage System" dark hero design.
#
# Matches the supplied design (email1.jpeg / email2.jpeg):
#   • dark navy body with blue radial glow + circuit dot pattern
#   • ORCA logo (cid:orca-white) + "ORCA" wordmark + "Manage System" sub
#   • glowing ring with large envelope+bell icon
#   • bold headline + blue accent bar
#   • greeting + body copy
#   • dark card block with clipboard icon, card title, card body
#   • wide blue pill CTA button with chain-link icon
#   • "Thank you" sign-off
#   • security note with shield icon
#
# Bilingual: rendered Arabic (RTL) by default; switches to English (LTR)
# automatically when the body/subject is detected as English.
# ============================================================

# ---- Inline SVG icons — no external requests, renders in all clients ----

# Large envelope with notification bell badge (goes in glowing ring)
_SVG_ENVELOPE_BELL = """
<svg width="64" height="64" viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  <rect x="6" y="18" width="42" height="30" rx="5" fill="#1a4fa0" stroke="#5ba3ff" stroke-width="1.5"/>
  <path d="M6 23l21 15 21-15" fill="none" stroke="#7fb1ff" stroke-width="2" stroke-linejoin="round"/>
  <circle cx="50" cy="18" r="12" fill="#3a8cff" stroke="#08142e" stroke-width="2"/>
  <path d="M50 10c0 0-5 2-5 6v4h-1.5l-1 1.5h15l-1-1.5H56v-4c0-4-5-6-5-6z" fill="#fff"/>
  <ellipse cx="50" cy="21.5" rx="2" ry="1" fill="#fff"/>
</svg>
""".strip()

# Small clipboard icon for the card section
_SVG_CLIPBOARD_CARD = """
<svg width="48" height="52" viewBox="0 0 48 52" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  <rect x="4" y="8" width="40" height="42" rx="6" fill="rgba(15,40,100,.7)" stroke="#3a8cff" stroke-width="1.5"/>
  <rect x="16" y="2" width="16" height="10" rx="4" fill="#3a8cff"/>
  <circle cx="14" cy="24" r="5.5" fill="#3a8cff"/>
  <path d="M11.5 24l2 2.2 4-4.5" stroke="#fff" stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
  <rect x="24" y="21" width="16" height="3.5" rx="1.75" fill="#7fb1ff"/>
  <rect x="24" y="26.5" width="10" height="3" rx="1.5" fill="#3a5d9c"/>
  <circle cx="14" cy="38" r="5.5" fill="#3a8cff"/>
  <path d="M11.5 38l2 2.2 4-4.5" stroke="#fff" stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
  <rect x="24" y="35" width="16" height="3.5" rx="1.75" fill="#7fb1ff"/>
  <rect x="24" y="40.5" width="10" height="3" rx="1.5" fill="#3a5d9c"/>
</svg>
""".strip()

# Chain-link icon for CTA button
_SVG_LINK = """
<svg width="22" height="22" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  <path d="M10 14a4 4 0 005.66 0l3-3a4 4 0 00-5.66-5.66l-1.5 1.5" stroke="#ffffff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M14 10a4 4 0 00-5.66 0l-3 3a4 4 0 005.66 5.66l1.5-1.5" stroke="#ffffff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
""".strip()

# Shield icon for security footer
_SVG_SHIELD = """
<svg width="16" height="16" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  <path d="M12 2l8 3v6c0 5-3.5 9-8 11C7.5 20 4 16 4 11V5l8-3z" fill="rgba(58,140,255,.2)" stroke="#7fb1ff" stroke-width="1.8"/>
  <path d="M9 12l2 2 4-4" stroke="#7fb1ff" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
""".strip()


BRANDED_EMAIL_TEMPLATE = """\
<!DOCTYPE html>
<html lang="{lang}" dir="{dir}">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>{subject}</title>
</head>
<body style="margin:0;padding:0;background:#040d1c;
             font-family:Tajawal,'Segoe UI',Tahoma,Arial,sans-serif;color:#e6ecff;">

  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"
         style="background:#040d1c;
                background-image:radial-gradient(ellipse 110% 55% at 50% 0%,
                  rgba(10,45,120,.95) 0%,rgba(4,13,28,1) 60%);
                min-height:100vh;">
    <tr><td align="center" style="padding:36px 16px 32px;">

      <!-- ── MAIN CARD ─────────────────────────────────────────────── -->
      <table role="presentation" width="520" cellspacing="0" cellpadding="0" border="0"
             style="max-width:520px;width:100%;">

        <!-- ── LOGO ───────────────────────────────────────────────── -->
        <tr><td align="center" style="padding:0 0 4px;">
          <img src="cid:orca-white" alt="ORCA" width="110" height="110"
               style="display:block;width:110px;height:110px;object-fit:contain;
                      filter:drop-shadow(0 0 24px rgba(58,140,255,.65));"/>
          <div style="font-size:38px;font-weight:900;color:#ffffff;letter-spacing:7px;
                      margin-top:4px;text-shadow:0 0 22px rgba(58,140,255,.6);
                      font-family:'Segoe UI',Tahoma,Arial,sans-serif;">ORCA</div>
          <div style="font-size:13px;color:#9ec1ff;letter-spacing:2px;margin-top:3px;">
            {wordmark}
          </div>
        </td></tr>

        <!-- ── SEPARATOR ──────────────────────────────────────────── -->
        <tr><td style="padding:22px 0 0;">
          <div style="height:1px;
                      background:linear-gradient(90deg,transparent 0%,
                        rgba(58,140,255,.75) 50%,transparent 100%);"></div>
        </td></tr>

        <!-- ── EMAIL IMAGE in glowing ring ──────────────────────────  -->
        <tr><td align="center" style="padding:32px 0 4px;">
          <table role="presentation" cellpadding="0" cellspacing="0" border="0">
            <tr><td align="center" valign="middle"
                style="width:130px;height:130px;border-radius:50%;
                       background:radial-gradient(circle,rgba(18,56,140,.85) 0%,rgba(4,13,28,.95) 70%);
                       border:1.5px solid rgba(58,140,255,.55);
                       box-shadow:0 0 55px rgba(30,94,255,.5),
                                  0 0 110px rgba(30,94,255,.2),
                                  inset 0 0 35px rgba(30,94,255,.3);">
              <img src="cid:orca-email" alt="" width="80" height="80"
                   style="display:block;width:80px;height:80px;object-fit:contain;
                          filter:drop-shadow(0 0 14px rgba(80,160,255,.75));"/>
            </td></tr>
          </table>
        </td></tr>

        <!-- ── HEADLINE ───────────────────────────────────────────── -->
        <tr><td align="center" style="padding:26px 20px 4px;">
          <h1 style="margin:0;font-size:27px;line-height:1.38;color:#ffffff;
                     font-weight:800;text-align:center;
                     text-shadow:0 0 18px rgba(58,140,255,.3);">
            {subject}
          </h1>
          <div style="width:60px;height:3px;border-radius:2px;background:#3a8cff;
                      margin:14px auto 0;
                      box-shadow:0 0 12px rgba(58,140,255,.9);"></div>
        </td></tr>

        <!-- ── BODY COPY ───────────────────────────────────────────── -->
        <tr><td align="center" style="padding:20px 28px 6px;">
          <div style="font-size:15px;line-height:1.95;color:#cfe0ff;text-align:center;">
            {body}
          </div>
        </td></tr>

        <!-- ── CARD BLOCK ─────────────────────────────────────────── -->
        <tr><td style="padding:18px 12px 6px;">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"
                 style="background:rgba(8,22,60,.85);
                        border:1px solid rgba(58,140,255,.28);
                        border-radius:16px;">
            <tr>
              <td width="76" align="center" valign="middle"
                  style="padding:20px 8px 20px 20px;">
                {icon_clipboard}
              </td>
              <td valign="middle" style="padding:20px 20px 20px 8px;">
                <div style="font-size:16px;font-weight:700;color:#ffffff;
                            margin-bottom:5px;">{card_title}</div>
                <div style="font-size:13px;color:#9ec1ff;line-height:1.65;">
                  {card_body}
                </div>
              </td>
            </tr>
          </table>
        </td></tr>

        <!-- ── CTA BUTTON (wide pill) ─────────────────────────────── -->
        {cta_block}

        <!-- ── THANK YOU ──────────────────────────────────────────── -->
        <tr><td align="center" style="padding:20px 24px 4px;">
          <div style="font-size:13px;color:#9ec1ff;line-height:1.8;text-align:center;">
            {thankyou}
          </div>
        </td></tr>

        <!-- ── BOTTOM SEPARATOR ───────────────────────────────────── -->
        <tr><td style="padding:18px 0 4px;">
          <div style="height:1px;
                      background:linear-gradient(90deg,transparent 0%,
                        rgba(58,140,255,.22) 50%,transparent 100%);"></div>
        </td></tr>

        <!-- ── SECURITY NOTE ──────────────────────────────────────── -->
        <tr><td align="center" style="padding:12px 20px 28px;">
          <div style="font-size:11px;color:#4e6a90;line-height:1.7;text-align:center;">
            <span style="display:inline-block;vertical-align:middle;
                         margin-inline-end:5px;">{icon_shield}</span>
            <span style="vertical-align:middle;">{security}</span>
          </div>
        </td></tr>

      </table>
      <!-- ── END MAIN CARD ──────────────────────────────────────── -->

    </td></tr>
  </table>

</body>
</html>
"""


def _is_english(text: str) -> bool:
    """Heuristic — treat the message as English when it contains no
    Arabic letters. Used to flip the email template direction."""
    if not text:
        return False
    for ch in text:
        if "\u0600" <= ch <= "\u06FF":
            return False
    return True


def _build_branded_email_html(subject: str, body: str, link: str = "") -> str:
    from html import escape as _esc
    en = _is_english((subject or "") + " " + (body or ""))
    lang = "en" if en else "ar"
    direction = "ltr" if en else "rtl"
    wordmark = "Manage System" if en else "نظام الإدارة"
    security = (
        "This is an automated message. Please do not reply to this email."
        if en else
        "هذه رسالة آلية من النظام. يرجى عدم الرد على هذا البريد الإلكتروني."
    )
    cta_label = "Click the link to view the details" if en else "اضغط على الرابط لمعرفة التفاصيل"
    card_title = "New Task Assigned" if en else "مهمة جديدة"
    card_body_text = (
        "Please review the task details and take the necessary action."
        if en else
        "يرجى مراجعة تفاصيل المهمة واتخاذ الإجراء اللازم."
    )
    thankyou = (
        "Thank you for using<br/>ORCA Manage System."
        if en else
        "شكرًا لاستخدامك<br/>نظام ORCA Manage System."
    )
    greeting = "Hello," if en else "مرحبًا،"

    # If the body already contains a URL, hyperlink the CTA to it.
    href = link or ""
    if not href:
        import re as _re
        m = _re.search(r"https?://\S+", body or "")
        if m:
            href = m.group(0)

    cta_block = ""
    if href:
        cta_block = f"""
        <tr><td align="center" style="padding:22px 16px 6px;">
          <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%">
            <tr><td align="center">
              <a href="{_esc(href)}"
                 style="display:block;padding:18px 28px;border-radius:50px;
                        background:linear-gradient(180deg,#3e90ff 0%,#1555d4 100%);
                        color:#ffffff;font-weight:700;text-decoration:none;
                        font-size:16px;text-align:center;
                        box-shadow:0 8px 32px rgba(30,94,255,.55),
                                   0 2px 8px rgba(30,94,255,.35);
                        border:1px solid rgba(130,180,255,.5);">
                <span style="display:inline-block;vertical-align:middle;
                             margin-inline-end:10px;">{_SVG_LINK}</span>
                <span style="vertical-align:middle;">{_esc(cta_label)}</span>
              </a>
            </td></tr>
          </table>
        </td></tr>"""

    body_html = f"{greeting}<br/>{_esc(body or '').replace(chr(10), '<br/>')}"

    return BRANDED_EMAIL_TEMPLATE.format(
        lang=lang,
        dir=direction,
        wordmark=_esc(wordmark),
        subject=_esc(subject or ""),
        body=body_html,
        card_title=_esc(card_title),
        card_body=_esc(card_body_text),
        thankyou=thankyou,
        security=_esc(security),
        cta_block=cta_block,
        year=datetime.utcnow().strftime("%Y"),
        icon_clipboard=_SVG_CLIPBOARD_CARD,
        icon_shield=_SVG_SHIELD,
    )


def send_email(to_addr: str, subject: str, body: str) -> dict:
    """Send an email via SMTP. Returns {ok:bool, error?:str}.
    The message is sent as multipart/alternative — plain-text body PLUS
    a branded HTML version with the ORCA logo (inlined as cid:orca-white).
    Free providers supported:
      - Gmail:    smtp.gmail.com:587 + Google App Password
      - Outlook:  smtp-mail.outlook.com:587 + account password / app password
      - Yahoo, Zoho, SendGrid free tier, etc.
    """
    if not to_addr:
        return {"ok": False, "error": "no_recipient"}
    cfg = smtp_config()
    if not cfg["host"] or not cfg["user"]:
        return {"ok": False, "error": "smtp_not_configured"}
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        from_addr = cfg["from"] or cfg["user"]
        msg["From"] = f'{cfg["from_name"]} <{from_addr}>' if cfg["from_name"] else from_addr
        msg["To"] = to_addr
        msg.set_content(body or "")
        # Attach the branded HTML alternative + inline logo
        msg.add_alternative(_build_branded_email_html(subject, body), subtype="html")
        html_part = msg.get_payload()[-1]
        logo_path = os.path.join(STATIC_DIR, "assets", "orca-white.png")
        if os.path.isfile(logo_path):
            try:
                with open(logo_path, "rb") as fh:
                    img_data = fh.read()
                html_part.add_related(img_data, maintype="image", subtype="png", cid="orca-white")
            except Exception as exc:
                app.logger.warning(f"could not inline logo: {exc}")
        email_icon_path = os.path.join(STATIC_DIR, "assets", "email.png")
        if os.path.isfile(email_icon_path):
            try:
                with open(email_icon_path, "rb") as fh:
                    icon_data = fh.read()
                html_part.add_related(icon_data, maintype="image", subtype="png", cid="orca-email")
            except Exception as exc:
                app.logger.warning(f"could not inline email icon: {exc}")
        if cfg["port"] == 465:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=ctx, timeout=15) as s:
                s.login(cfg["user"], cfg["password"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as s:
                if cfg["use_tls"]:
                    s.starttls(context=ssl.create_default_context())
                if cfg["password"]:
                    s.login(cfg["user"], cfg["password"])
                s.send_message(msg)
        return {"ok": True}
    except Exception as e:
        app.logger.warning(f"email send failed: {e}")
        return {"ok": False, "error": str(e)}


# ============================================================
# Notification helper (in-app + email)
# ============================================================

def _notify(user_id, ntype, title, body=None, task_id=None, send_mail=True):
    if not user_id:
        print(f"[orca] _notify skipped — no user_id for '{title}'")
        return
    db = get_db()
    db.execute(
        "INSERT INTO notifications (recipient_user_id, type, title, body, related_task_id) VALUES (?,?,?,?,?)",
        (user_id, ntype, title, body, task_id),
    )
    db.commit()
    if send_mail:
        # Resolve a destination email. Prefer the user's registered email,
        # then fall back to the linked employee row's email so newly-created
        # employees still receive mail even if their user account was created
        # before email collection was added.
        addr_row = db.execute(
            "SELECT COALESCE(NULLIF(TRIM(u.email),''), NULLIF(TRIM(e.email),'')) AS addr "
            "FROM users u LEFT JOIN employees e ON e.user_id=u.id WHERE u.id=?",
            (user_id,),
        ).fetchone()
        addr = addr_row["addr"] if addr_row else None
        if addr:
            print(f"[orca] _notify → user#{user_id} <{addr}> :: {title}")
            send_email(addr, title, body or title)
        else:
            print(f"[orca] _notify in-app only — user#{user_id} has no email on file")


def _notify_all_admins(ntype, title, body=None, task_id=None, exclude_user_id=None):
    db = get_db()
    rows = db.execute("SELECT id FROM users WHERE role='admin'").fetchall()
    for r in rows:
        if exclude_user_id and r["id"] == exclude_user_id:
            continue
        _notify(r["id"], ntype, title, body, task_id)


# ============================================================
# Serial helpers
# ============================================================

def next_serial(prefix: str, table: str, year_in: bool = True) -> str:
    db = get_db()
    if year_in:
        year = datetime.utcnow().strftime("%Y")
        like = f"{prefix}{year}-%"
        last = db.execute(
            f"SELECT serial FROM {table} WHERE serial LIKE ? ORDER BY id DESC LIMIT 1", (like,)
        ).fetchone()
        next_n = 1
        if last:
            try: next_n = int(last["serial"].split("-")[-1]) + 1
            except Exception: next_n = 1
        return f"{prefix}{year}-{next_n:04d}"
    last = db.execute(
        f"SELECT serial FROM {table} WHERE serial LIKE ? ORDER BY id DESC LIMIT 1", (f"{prefix}%",)
    ).fetchone()
    next_n = 1
    if last:
        try: next_n = int(last["serial"].split("-")[-1]) + 1
        except Exception: next_n = 1
    return f"{prefix}{next_n:04d}"


# ============================================================
# Frontend serving
# ============================================================

@app.route("/")
def root():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/<path:path>")
def static_or_index(path):
    full = os.path.join(STATIC_DIR, path)
    if os.path.isfile(full):
        return send_from_directory(STATIC_DIR, path)
    return send_from_directory(STATIC_DIR, "index.html")


# ============================================================
# Auth API
# ============================================================

@app.post("/api/auth/login")
def api_login():
    data = request.get_json(silent=True) or {}
    ident = (data.get("username") or "").strip()  # username OR email
    password = data.get("password") or ""
    if not ident or not password:
        return jsonify({"error": "missing_credentials"}), 400
    row = get_db().execute(
        "SELECT * FROM users WHERE username=? OR email=?", (ident, ident)
    ).fetchone()
    if not row or hash_password(password, row["password_salt"]) != row["password_hash"]:
        return jsonify({"error": "invalid_credentials"}), 401
    # Block login for unapproved accounts (admin is always approved)
    approved = row["approved"] if "approved" in row.keys() else 1
    if not approved and row["role"] != "admin":
        return jsonify({"error": "pending_approval"}), 403
    session["user_id"] = row["id"]
    return jsonify({"id": row["id"], "username": row["username"], "email": row["email"], "role": row["role"]})


@app.post("/api/auth/register")
def api_register():
    """Register a new client/employee account.

    Per requirement (2): the registration form collects first name, last
    name, email and password only — no username. The system auto-creates
    a linked employee record and returns its serial as the public ID
    that admins use to look up employees in the table.

    Backwards-compatible: if a caller still posts {username, password},
    that flow continues to work.
    """
    data = request.get_json(silent=True) or {}
    first_name = (data.get("firstName") or "").strip()
    last_name  = (data.get("lastName")  or "").strip()
    email      = (data.get("email") or "").strip().lower() or None
    password   = data.get("password") or ""
    username   = (data.get("username") or "").strip()
    full_name  = (first_name + " " + last_name).strip()

    # New flow per requirement: form supplies username explicitly.
    # If a caller still posts without a username, derive one as a fallback.
    if not username:
        if not (first_name and last_name and email):
            return jsonify({"error": "missing_fields"}), 400
        base = email.split("@")[0] if email else (first_name + "." + last_name).lower()
        base = "".join(c for c in base if c.isalnum() or c in "._-") or "user"
        username = base
    else:
        # Sanitise: only allow alphanumerics + . _ -
        username = "".join(c for c in username if c.isalnum() or c in "._-")
        if not username:
            return jsonify({"error": "invalid_username"}), 400

    if len(username) < 3 or len(password) < 4:
        return jsonify({"error": "invalid_input"}), 400

    db = get_db()
    # Username must be unique. If the user explicitly chose one and it
    # is taken, reject with a clear error rather than silently mutating
    # their chosen identifier.
    if db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone():
        if data.get("username"):
            return jsonify({"error": "username_taken"}), 409
        # Auto-suffix only for the legacy derive-from-email path
        candidate = username
        n = 1
        while db.execute("SELECT id FROM users WHERE username=?", (candidate,)).fetchone():
            n += 1
            candidate = f"{username}{n}"
        username = candidate

    if email and db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
        return jsonify({"error": "email_taken"}), 409

    salt = secrets.token_hex(16)
    h = hash_password(password, salt)
    cur = db.execute(
        "INSERT INTO users (username, email, password_hash, password_salt, role) VALUES (?,?,?,?,'viewer')",
        (username, email, h, salt),
    )
    db.commit()
    new_id = cur.lastrowid

    # Create / link an employee record so the admin can pick this person
    # from the assignee dropdown and search for them by ID (the serial).
    employee_serial = None
    promoted_role = "viewer"
    if email:
        emp = db.execute(
            "SELECT id, serial, role FROM employees WHERE LOWER(email)=?", (email,)
        ).fetchone()
        if emp:
            db.execute(
                "UPDATE employees SET user_id=? WHERE id=? AND (user_id IS NULL)",
                (new_id, emp["id"]),
            )
            employee_serial = emp["serial"]
            # If this employee was pre-configured as admin, promote the new user
            if emp["role"] == "admin":
                db.execute("UPDATE users SET role='admin' WHERE id=?", (new_id,))
                promoted_role = "admin"
        else:
            employee_serial = next_serial("EMP-", "employees", year_in=False)
            display_name = full_name or username
            db.execute(
                "INSERT INTO employees (serial, full_name, email, role, user_id) "
                "VALUES (?,?,?,?,?)",
                (employee_serial, display_name, email, "employee", new_id),
            )
        db.commit()

    # New accounts require admin approval before they can log in
    if promoted_role != "admin":
        get_db().execute("UPDATE users SET approved=0 WHERE id=?", (new_id,))
        get_db().commit()
        # Notify all admins in-app + email
        notif_title_ar = f"طلب تسجيل جديد: {username}"
        notif_body_ar  = f"مستخدم جديد ({username}) يطلب الانضمام. راجع قسم المستخدمين المعلقين في الإعدادات."
        try:
            _notify_all_admins("new_registration", notif_title_ar, notif_body_ar)
        except Exception as exc:
            print(f"[orca] registration notify failed: {exc}")
        # Do NOT start a session for unapproved users
        return jsonify({
            "pending_approval": True,
            "username": username,
            "email": email,
        })

    session["user_id"] = new_id
    return jsonify({
        "id": new_id,
        "username": username,
        "email": email,
        "role": promoted_role,
        "employeeSerial": employee_serial,
        "firstName": first_name or None,
        "lastName": last_name or None,
    })


@app.post("/api/auth/logout")
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/auth/me")
def api_me():
    u = current_user()
    if not u:
        return jsonify(None)
    return jsonify(u)

# ============================================================
# Admin — pending user approvals
# ============================================================

@app.get("/api/admin/pending-users")
def admin_pending_users():
    """List users who have registered but not yet been approved."""
    if (e := require_admin()): return e
    db = get_db()
    rows = db.execute(
        "SELECT id, username, email, role, created_at FROM users WHERE approved=0 ORDER BY id DESC"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/admin/users/<int:uid>/approve")
def admin_approve_user(uid):
    """Approve a pending user — they can now log in."""
    if (e := require_admin()): return e
    db = get_db()
    row = db.execute("SELECT id, username, email FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        return jsonify({"error": "not_found"}), 404
    db.execute("UPDATE users SET approved=1 WHERE id=?", (uid,))
    db.commit()
    # Notify the user that their account has been approved
    try:
        _notify(uid, "account_approved",
                "تمت الموافقة على حسابك" if True else "Your account has been approved",
                "يمكنك الآن تسجيل الدخول إلى لوحة التحكم.",
                send_mail=True)
    except Exception as exc:
        print(f"[orca] approve notify failed: {exc}")
    return jsonify({"ok": True, "id": uid})


@app.post("/api/admin/users/<int:uid>/reject")
def admin_reject_user(uid):
    """Reject and delete a pending user account."""
    if (e := require_admin()): return e
    db = get_db()
    row = db.execute("SELECT id, username FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        return jsonify({"error": "not_found"}), 404
    # Delete linked employee record if auto-created and has no other data
    emp = db.execute("SELECT id FROM employees WHERE user_id=?", (uid,)).fetchone()
    if emp:
        has_tasks = db.execute("SELECT id FROM tasks WHERE assignee_id=?", (emp["id"],)).fetchone()
        if not has_tasks:
            db.execute("DELETE FROM employees WHERE id=?", (emp["id"],))
    db.execute("DELETE FROM users WHERE id=?", (uid,))
    db.commit()
    return jsonify({"ok": True})


# ============================================================
# Employee permissions API
# ============================================================

@app.get("/api/employees/<int:eid>/permissions")
def get_employee_permissions(eid):
    """Get the permission settings for an employee."""
    if (e := require_admin()): return e
    db = get_db()
    row = db.execute("SELECT * FROM employee_permissions WHERE employee_id=?", (eid,)).fetchone()
    if row:
        return jsonify(dict(row))
    # Return defaults if no custom permissions set yet
    return jsonify({
        "employee_id": eid,
        "can_view_clients": 1,
        "can_edit_clients": 0,
        "can_view_documents": 1,
        "can_view_tasks": 1,
    })


@app.put("/api/employees/<int:eid>/permissions")
def set_employee_permissions(eid):
    """Set permission flags for an employee. Admin-only."""
    if (e := require_admin()): return e
    # Super-admin permissions cannot be changed
    db = get_db()
    emp_user = db.execute(
        "SELECT u.username FROM employees emp JOIN users u ON u.id=emp.user_id WHERE emp.id=?", (eid,)
    ).fetchone()
    if emp_user and emp_user["username"] == "admin":
        return jsonify({"error": "forbidden", "detail": "cannot restrict the super-admin"}), 403
    data = request.get_json(silent=True) or {}
    can_view_clients   = int(bool(data.get("can_view_clients",   True)))
    can_edit_clients   = int(bool(data.get("can_edit_clients",   False)))
    can_view_documents = int(bool(data.get("can_view_documents", True)))
    can_view_tasks     = int(bool(data.get("can_view_tasks",     True)))
    db.execute(
        "INSERT INTO employee_permissions (employee_id, can_view_clients, can_edit_clients, can_view_documents, can_view_tasks, updated_at) "
        "VALUES (?,?,?,?,?,datetime('now')) "
        "ON CONFLICT(employee_id) DO UPDATE SET "
        "  can_view_clients=excluded.can_view_clients, "
        "  can_edit_clients=excluded.can_edit_clients, "
        "  can_view_documents=excluded.can_view_documents, "
        "  can_view_tasks=excluded.can_view_tasks, "
        "  updated_at=excluded.updated_at",
        (eid, can_view_clients, can_edit_clients, can_view_documents, can_view_tasks),
    )
    db.commit()
    return jsonify({"ok": True})




@app.post("/api/auth/forgot-password")
def api_forgot_password():
    """Send a password-reset code to the user's registered email."""
    import datetime as _dt
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "missing_email"}), 400
    db = get_db()
    row = db.execute(
        "SELECT id, email FROM users WHERE LOWER(TRIM(email))=?", (email,)
    ).fetchone()
    # Always respond OK to avoid email enumeration
    if not row or not row["email"]:
        return jsonify({"ok": True})
    # Invalidate any prior unused tokens for this user
    db.execute(
        "UPDATE password_reset_tokens SET used=1 WHERE user_id=? AND used=0",
        (row["id"],),
    )
    token = secrets.token_hex(4).upper()  # 8-char hex code, easy to type
    expires = (_dt.datetime.utcnow() + _dt.timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "INSERT INTO password_reset_tokens (user_id, token, expires_at) VALUES (?,?,?)",
        (row["id"], token, expires),
    )
    db.commit()
    subject = "ORCA — رمز إعادة تعيين كلمة المرور"
    body = (
        f"تم طلب إعادة تعيين كلمة المرور لحسابك.\n\n"
        f"رمز التحقق الخاص بك:\n\n"
        f"  {token}\n\n"
        f"صالح لمدة 30 دقيقة. إذا لم تطلب هذا، تجاهل هذه الرسالة."
    )
    send_email(row["email"], subject, body)
    return jsonify({"ok": True})


@app.post("/api/auth/reset-password")
def api_reset_password():
    """Verify the reset code and set a new password."""
    data = request.get_json(silent=True) or {}
    email    = (data.get("email")    or "").strip().lower()
    token    = (data.get("token")    or "").strip().upper()
    password = (data.get("password") or "")
    if not email or not token or len(password) < 4:
        return jsonify({"error": "invalid_input"}), 400
    db = get_db()
    user = db.execute(
        "SELECT id FROM users WHERE LOWER(TRIM(email))=?", (email,)
    ).fetchone()
    if not user:
        return jsonify({"error": "invalid_token"}), 400
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    rec = db.execute(
        "SELECT id FROM password_reset_tokens "
        "WHERE user_id=? AND token=? AND used=0 AND expires_at > ?",
        (user["id"], token, now),
    ).fetchone()
    if not rec:
        return jsonify({"error": "invalid_token"}), 400
    salt = secrets.token_hex(16)
    h = hash_password(password, salt)
    db.execute("UPDATE users SET password_hash=?, password_salt=? WHERE id=?", (h, salt, user["id"]))
    db.execute("UPDATE password_reset_tokens SET used=1 WHERE id=?", (rec["id"],))
    db.commit()
    return jsonify({"ok": True})


# ============================================================
# Users API
# ============================================================

@app.get("/api/users")
def list_users():
    if (e := require_login()): return e
    rows = get_db().execute(
        "SELECT id, username, email, role, created_at FROM users ORDER BY username"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/users/by_email")
def user_by_email():
    if (e := require_login()): return e
    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "missing_email"}), 400
    row = get_db().execute(
        "SELECT id, username, email, role FROM users WHERE LOWER(email)=?", (email,)
    ).fetchone()
    if not row:
        return jsonify({"error": "not_found"}), 404
    return jsonify(dict(row))


@app.post("/api/users")
def admin_create_user():
    """Admin-only: create a new account with an explicit role.
    Fixes requirement (2): the admin can now spin up another admin (or
    employee/viewer) from the admin panel and the role is honoured —
    the previous /register path always forced 'viewer'."""
    if (e := require_admin()): return e
    data = request.get_json(silent=True) or {}
    username = "".join(c for c in (data.get("username") or "").strip()
                       if c.isalnum() or c in "._-")
    email    = (data.get("email") or "").strip().lower() or None
    password = data.get("password") or ""
    role     = (data.get("role") or "viewer").strip().lower()
    if role not in ("admin", "employee", "viewer"):
        return jsonify({"error": "invalid_role"}), 400
    if len(username) < 3 or len(password) < 4:
        return jsonify({"error": "invalid_input"}), 400

    db = get_db()
    if db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone():
        return jsonify({"error": "username_taken"}), 409
    if email and db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
        return jsonify({"error": "email_taken"}), 409
    salt = secrets.token_hex(16)
    h = hash_password(password, salt)
    cur = db.execute(
        "INSERT INTO users (username, email, password_hash, password_salt, role) VALUES (?,?,?,?,?)",
        (username, email, h, salt, role),
    )
    db.commit()
    return jsonify({
        "id": cur.lastrowid, "username": username, "email": email, "role": role,
    })


@app.put("/api/users/<int:uid>/role")
def admin_set_role(uid):
    """Admin-only: change an existing user's role (e.g. promote to admin).
    Only the super-admin (username='admin') can demote another admin or touch
    the super-admin account itself."""
    if (e := require_admin()): return e
    data = request.get_json(silent=True) or {}
    role = (data.get("role") or "").strip().lower()
    if role not in ("admin", "employee", "viewer"):
        return jsonify({"error": "invalid_role"}), 400
    db = get_db()
    target_user = db.execute("SELECT id, username, role FROM users WHERE id=?", (uid,)).fetchone()
    if not target_user:
        return jsonify({"error": "not_found"}), 404
    # Protect the super-admin account — nobody can change its role
    if target_user["username"] == "admin":
        return jsonify({"error": "forbidden", "detail": "cannot change the super-admin role"}), 403
    # Only the super-admin can demote another admin
    if target_user["role"] == "admin" and role != "admin":
        if not is_super_admin():
            return jsonify({"error": "forbidden", "detail": "only the super-admin can demote an admin"}), 403
    db.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
    db.commit()
    return jsonify({"ok": True, "role": role})


# ============================================================
# Clients API
# ============================================================

@app.get("/api/clients")
def list_clients():
    if (e := require_login()): return e
    me = current_user()
    # Non-admins need can_view_clients permission
    if me and me["role"] != "admin":
        db2 = get_db()
        emp = db2.execute("SELECT id FROM employees WHERE user_id=?", (me["id"],)).fetchone()
        if emp:
            perm = db2.execute("SELECT can_view_clients FROM employee_permissions WHERE employee_id=?", (emp["id"],)).fetchone()
            if perm and not perm["can_view_clients"]:
                return jsonify({"error": "permission_denied"}), 403
    q = (request.args.get("search") or "").strip()
    db = get_db()
    if q:
        like = f"%{q}%"
        rows = db.execute(
            "SELECT * FROM clients WHERE name LIKE ? OR company LIKE ? OR serial LIKE ? ORDER BY id DESC",
            (like, like, like),
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM clients ORDER BY id DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/clients")
def create_client():
    if (e := require_admin()): return e
    data = request.get_json(silent=True) or {}
    if not data.get("name"):
        return jsonify({"error": "missing_name"}), 400
    serial = next_serial("CLI-", "clients", year_in=False)
    db = get_db()
    cur = db.execute(
        "INSERT INTO clients (serial, name, company, sector, email, phone, address, notes) VALUES (?,?,?,?,?,?,?,?)",
        (serial, data["name"], data.get("company"), data.get("sector") or "other",
         data.get("email"), data.get("phone"), data.get("address"), data.get("notes")),
    )
    db.commit()
    return jsonify(dict(db.execute("SELECT * FROM clients WHERE id=?", (cur.lastrowid,)).fetchone()))


@app.get("/api/clients/<int:cid>")
def get_client(cid):
    if (e := require_login()): return e
    db = get_db()
    row = db.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
    if not row:
        return jsonify({"error": "not_found"}), 404
    out = dict(row)
    out["projects"] = [dict(p) for p in db.execute("SELECT * FROM projects WHERE client_id=? ORDER BY id DESC", (cid,)).fetchall()]
    out["attachments"] = [dict(a) for a in db.execute("SELECT * FROM attachments WHERE client_id=? ORDER BY id DESC", (cid,)).fetchall()]
    out["documents"] = [dict(d) for d in db.execute("SELECT id, serial, kind, total, currency FROM documents WHERE client_id=? ORDER BY id DESC", (cid,)).fetchall()]
    return jsonify(out)


@app.put("/api/clients/<int:cid>")
def update_client(cid):
    if (e := require_admin()): return e
    data = request.get_json(silent=True) or {}
    db = get_db()
    if not db.execute("SELECT id FROM clients WHERE id=?", (cid,)).fetchone():
        return jsonify({"error": "not_found"}), 404
    db.execute(
        "UPDATE clients SET name=?, company=?, sector=?, email=?, phone=?, address=?, notes=? WHERE id=?",
        (data.get("name") or "", data.get("company"), data.get("sector") or "other",
         data.get("email"), data.get("phone"), data.get("address"), data.get("notes"), cid),
    )
    db.commit()
    return jsonify(dict(db.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()))


@app.delete("/api/clients/<int:cid>")
def delete_client(cid):
    if (e := require_admin()): return e
    db = get_db()
    db.execute("DELETE FROM clients WHERE id=?", (cid,))
    db.commit()
    return jsonify({"ok": True})


@app.get("/api/clients/<int:cid>/export")
def client_export_bundle(cid):
    """Returns a complete JSON bundle for a single client — used by the
    frontend to generate per-client PDF / Excel / Word exports that
    contain ALL of the client's data, projects, attachments and
    documents in one file."""
    if (e := require_login()): return e
    db = get_db()
    row = db.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
    if not row:
        return jsonify({"error": "not_found"}), 404
    bundle = dict(row)
    bundle["projects"] = [dict(p) for p in db.execute(
        "SELECT id, name, description, cost, status, created_at "
        "FROM projects WHERE client_id=? ORDER BY id DESC", (cid,)).fetchall()]
    bundle["attachments"] = [dict(a) for a in db.execute(
        "SELECT id, file_name, url, created_at "
        "FROM attachments WHERE client_id=? ORDER BY id DESC", (cid,)).fetchall()]
    bundle["documents"] = [dict(d) for d in db.execute(
        "SELECT id, serial, kind, total, currency, issued_at "
        "FROM documents WHERE client_id=? ORDER BY id DESC", (cid,)).fetchall()]
    bundle["tasks"] = [dict(t) for t in db.execute(
        "SELECT t.id, t.title, t.status, t.due_date, t.created_at, "
        "       e.full_name AS assignee_name "
        "FROM tasks t LEFT JOIN employees e ON e.id=t.assignee_id "
        "WHERE t.client_id=? ORDER BY t.id DESC", (cid,)).fetchall()]
    return jsonify(bundle)


# ============================================================
# Employees API
# ============================================================

def _emp_to_json(row):
    return {
        "id": row["id"], "serial": row["serial"], "fullName": row["full_name"],
        "email": row["email"], "phone": row["phone"], "position": row["position"],
        "department": row["department"], "role": row["role"],
        "userId": row["user_id"] if "user_id" in row.keys() else None,
        "createdAt": row["created_at"],
    }


@app.get("/api/employees")
def list_employees():
    if (e := require_login()): return e
    q = (request.args.get("search") or "").strip()
    db = get_db()
    if q:
        like = f"%{q}%"
        rows = db.execute(
            "SELECT * FROM employees WHERE full_name LIKE ? OR email LIKE ? OR serial LIKE ? ORDER BY id DESC",
            (like, like, like),
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM employees ORDER BY id DESC").fetchall()
    return jsonify([_emp_to_json(r) for r in rows])


@app.post("/api/employees")
def create_employee():
    if (e := require_admin()): return e
    data = request.get_json(silent=True) or {}
    if not data.get("fullName") or not data.get("email"):
        return jsonify({"error": "missing_fields"}), 400
    serial = next_serial("EMP-", "employees", year_in=False)
    db = get_db()
    # try to auto-link to a user with the same email
    user_id = data.get("userId")
    if not user_id:
        u = db.execute("SELECT id FROM users WHERE LOWER(email)=?", (data["email"].strip().lower(),)).fetchone()
        if u: user_id = u["id"]
    emp_role = data.get("role") or "employee"
    cur = db.execute(
        "INSERT INTO employees (serial, full_name, email, phone, position, department, role, user_id) VALUES (?,?,?,?,?,?,?,?)",
        (serial, data["fullName"], data["email"].strip(), data.get("phone"),
         data.get("position"), data.get("department"),
         emp_role, user_id or None),
    )
    db.commit()
    # If role is admin and no user account is linked, auto-create one from supplied credentials
    if emp_role == "admin" and not user_id:
        new_username = (data.get("username") or "").strip()
        new_password = (data.get("password") or "").strip()
        if new_username and new_password:
            try:
                salt = secrets.token_hex(16)
                h = hash_password(new_password, salt)
                uid_cur2 = db.execute(
                    "INSERT INTO users (username, email, password_hash, password_salt, role) VALUES (?,?,?,?,?)",
                    (new_username, data["email"].strip(), h, salt, "admin"),
                )
                db.commit()
                user_id = uid_cur2.lastrowid
                db.execute("UPDATE employees SET user_id=? WHERE id=?", (user_id, cur.lastrowid))
                db.commit()
            except Exception:
                pass  # username already taken — skip
    # Sync role to the linked user account so privileges take effect immediately
    if user_id and emp_role == "admin":
        db.execute("UPDATE users SET role='admin' WHERE id=? AND role <> 'admin'", (user_id,))
        db.commit()
    return jsonify(_emp_to_json(db.execute("SELECT * FROM employees WHERE id=?", (cur.lastrowid,)).fetchone()))


@app.get("/api/employees/<int:eid>")
def get_employee(eid):
    if (e := require_login()): return e
    db = get_db()
    row = db.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
    if not row:
        return jsonify({"error": "not_found"}), 404
    out = _emp_to_json(row)
    tasks = db.execute(
        "SELECT t.*, c.name as client_name FROM tasks t LEFT JOIN clients c ON c.id=t.client_id WHERE assignee_id=? ORDER BY t.id DESC",
        (eid,),
    ).fetchall()
    out["tasks"] = [{"id": t["id"], "title": t["title"], "status": t["status"],
                     "clientName": t["client_name"], "createdAt": t["created_at"]} for t in tasks]
    return jsonify(out)


@app.put("/api/employees/<int:eid>")
def update_employee(eid):
    if (e := require_admin()): return e
    data = request.get_json(silent=True) or {}
    db = get_db()
    existing = db.execute("SELECT user_id, role FROM employees WHERE id=?", (eid,)).fetchone()
    if not existing:
        return jsonify({"error": "not_found"}), 404
    # Only the super-admin can demote an admin employee back to a regular role
    new_role = (data.get("role") or "employee")
    if existing["role"] == "admin" and new_role != "admin":
        if not is_super_admin():
            return jsonify({"error": "forbidden", "detail": "only the super-admin can demote an admin"}), 403
    new_email = (data.get("email") or "").strip()
    # if no explicit userId was provided, try to (re)link by matching email
    user_id = data.get("userId")
    if not user_id and new_email:
        u = db.execute(
            "SELECT id FROM users WHERE LOWER(TRIM(email))=?",
            (new_email.lower(),),
        ).fetchone()
        if u:
            user_id = u["id"]
    # never silently break an existing link if the admin just edited unrelated fields
    if not user_id and not new_email:
        user_id = existing["user_id"]
    # new_role was already resolved above for the super-admin guard; keep it
    db.execute(
        "UPDATE employees SET full_name=?, email=?, phone=?, position=?, department=?, role=?, user_id=? WHERE id=?",
        (data.get("fullName") or "", new_email, data.get("phone"),
         data.get("position"), data.get("department"),
         new_role, user_id or None, eid),
    )
    db.commit()
    # If role changed to admin and no user account is linked, auto-create one
    linked_uid_check = user_id or existing["user_id"]
    if new_role == "admin" and not linked_uid_check:
        new_username2 = (data.get("username") or "").strip()
        new_password2 = (data.get("password") or "").strip()
        emp_email2 = new_email or (
            db.execute("SELECT email FROM employees WHERE id=?", (eid,)).fetchone() or {}
        ).get("email", "")
        if new_username2 and new_password2:
            try:
                salt2 = secrets.token_hex(16)
                h2 = hash_password(new_password2, salt2)
                uid_cur3 = db.execute(
                    "INSERT INTO users (username, email, password_hash, password_salt, role) VALUES (?,?,?,?,?)",
                    (new_username2, emp_email2, h2, salt2, "admin"),
                )
                db.commit()
                user_id = uid_cur3.lastrowid
                db.execute("UPDATE employees SET user_id=? WHERE id=?", (user_id, eid))
                db.commit()
            except Exception:
                pass  # username taken — skip
    # Sync the role change to the linked user account so access privileges take effect immediately.
    linked_uid = user_id or existing["user_id"]
    if linked_uid:
        if new_role == "admin":
            db.execute("UPDATE users SET role='admin' WHERE id=?", (linked_uid,))
        else:
            # Downgrade only if no other employee row still grants admin.
            other_admin = db.execute(
                "SELECT id FROM employees WHERE user_id=? AND role='admin' AND id<>?",
                (linked_uid, eid),
            ).fetchone()
            if not other_admin:
                db.execute(
                    "UPDATE users SET role='employee' WHERE id=? AND role='admin'",
                    (linked_uid,),
                )
        db.commit()
    return jsonify(_emp_to_json(db.execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()))


@app.delete("/api/employees/<int:eid>")
def delete_employee(eid):
    if (e := require_admin()): return e
    db = get_db()
    target = db.execute("SELECT role FROM employees WHERE id=?", (eid,)).fetchone()
    if not target:
        return jsonify({"error": "not_found"}), 404
    # Only the super-admin can delete an employee who has the admin role
    if target["role"] == "admin" and not is_super_admin():
        return jsonify({"error": "forbidden", "detail": "only the super-admin can delete an admin"}), 403
    db.execute("DELETE FROM employees WHERE id=?", (eid,))
    db.commit()
    return jsonify({"ok": True})


# ============================================================
# Tasks API (with workflow)
# ============================================================

def _task_to_json(r):
    return {
        "id": r["id"], "title": r["title"], "description": r["description"],
        "status": r["status"], "assigneeId": r["assignee_id"],
        "assigneeName": r["assignee_name"] if "assignee_name" in r.keys() else None,
        "assigneeEmail": r["assignee_email"] if "assignee_email" in r.keys() else None,
        "assigneeUserId": r["assignee_user_id"] if "assignee_user_id" in r.keys() else None,
        "clientId": r["client_id"],
        "clientName": r["client_name"] if "client_name" in r.keys() else None,
        "dueDate": r["due_date"], "createdAt": r["created_at"],
    }


def _resolve_assignee_by_email(email):
    """Resolve an assignee email → (employee_id, user_id, error).
    Rule: a registered user account with that email MUST exist, otherwise no
    notification could ever be delivered. Returns ('error_code', None, None)
    on failure."""
    if not email:
        return None, None, None
    email = email.strip().lower()
    db = get_db()
    user = db.execute("SELECT id, username FROM users WHERE LOWER(email)=?", (email,)).fetchone()
    if not user:
        return None, None, "user_not_registered"
    # find existing employee row by email (case-insensitive); else create one
    emp = db.execute("SELECT id, user_id FROM employees WHERE LOWER(email)=?", (email,)).fetchone()
    if emp:
        if not emp["user_id"]:
            db.execute("UPDATE employees SET user_id=? WHERE id=?", (user["id"], emp["id"]))
            db.commit()
        return emp["id"], user["id"], None
    serial = next_serial("EMP-", "employees", year_in=False)
    cur = db.execute(
        "INSERT INTO employees (serial, full_name, email, role, user_id) VALUES (?,?,?,?,?)",
        (serial, user["username"], email, "employee", user["id"]),
    )
    db.commit()
    return cur.lastrowid, user["id"], None


@app.get("/api/tasks")
def list_tasks():
    if (e := require_login()): return e
    me = current_user()
    db = get_db()
    base = (
        "SELECT t.*, e.full_name as assignee_name, e.email as assignee_email, "
        "e.user_id as assignee_user_id, c.name as client_name "
        "FROM tasks t LEFT JOIN employees e ON e.id=t.assignee_id "
        "LEFT JOIN clients c ON c.id=t.client_id "
    )
    if me["role"] == "admin":
        rows = db.execute(base + "ORDER BY t.id DESC").fetchall()
    else:
        rows = db.execute(base + "WHERE e.user_id=? ORDER BY t.id DESC", (me["id"],)).fetchall()
    return jsonify([_task_to_json(r) for r in rows])


@app.get("/api/tasks/<int:tid>")
def get_task(tid):
    if (e := require_login()): return e
    me = current_user()
    db = get_db()
    row = db.execute(
        "SELECT t.*, e.full_name as assignee_name, e.email as assignee_email, "
        "e.user_id as assignee_user_id, c.name as client_name "
        "FROM tasks t LEFT JOIN employees e ON e.id=t.assignee_id "
        "LEFT JOIN clients c ON c.id=t.client_id WHERE t.id=?", (tid,)
    ).fetchone()
    if not row:
        return jsonify({"error": "not_found"}), 404
    if me["role"] != "admin" and row["assignee_user_id"] != me["id"]:
        return jsonify({"error": "forbidden"}), 403
    out = _task_to_json(row)
    subs = db.execute(
        "SELECT s.*, u.username as submitted_by_name FROM task_submissions s "
        "LEFT JOIN users u ON u.id=s.submitted_by_user_id WHERE s.task_id=? ORDER BY s.id ASC",
        (tid,),
    ).fetchall()
    out["submissions"] = [{
        "id": s["id"], "message": s["message"],
        "attachmentName": s["attachment_name"], "attachmentUrl": s["attachment_url"],
        "submittedByName": s["submitted_by_name"], "createdAt": s["created_at"],
    } for s in subs]
    revs = db.execute(
        "SELECT r.*, u.username as reviewer_name FROM task_reviews r "
        "LEFT JOIN users u ON u.id=r.reviewed_by_user_id WHERE r.task_id=? ORDER BY r.id ASC",
        (tid,),
    ).fetchall()
    out["reviews"] = [{
        "id": r["id"], "decision": r["decision"], "feedback": r["feedback"],
        "reviewerName": r["reviewer_name"], "createdAt": r["created_at"],
    } for r in revs]
    msgs = db.execute(
        "SELECT m.*, u.username as sender_name, u.role as sender_role FROM task_messages m "
        "LEFT JOIN users u ON u.id=m.sender_user_id WHERE m.task_id=? ORDER BY m.id ASC",
        (tid,),
    ).fetchall()
    out["messages"] = [{
        "id": m["id"], "body": m["body"],
        "attachmentName": m["attachment_name"], "attachmentUrl": m["attachment_url"],
        "senderName": m["sender_name"], "senderRole": m["sender_role"],
        "createdAt": m["created_at"],
    } for m in msgs]
    return jsonify(out)


@app.post("/api/tasks")
def create_task():
    if (e := require_admin()): return e
    data = request.get_json(silent=True) or {}
    if not data.get("title"):
        return jsonify({"error": "missing_title"}), 400

    db = get_db()
    assignee_id = data.get("assigneeId")
    assignee_user_id = None
    assignee_email = (data.get("assigneeEmail") or "").strip()

    if assignee_email and not assignee_id:
        emp_id, user_id, err = _resolve_assignee_by_email(assignee_email)
        if err == "user_not_registered":
            return jsonify({
                "error": "user_not_registered",
                "message": "لا يوجد مستخدم مسجّل بهذا البريد. اطلب من الموظف التسجيل أولًا بنفس البريد.",
            }), 400
        assignee_id = emp_id
        assignee_user_id = user_id
    elif assignee_id:
        # Pull the linked user account AND the email so we can notify in
        # both ways (in-app for users with an account, plain email otherwise).
        emp = db.execute(
            "SELECT user_id, email FROM employees WHERE id=?", (assignee_id,)
        ).fetchone()
        if emp:
            assignee_user_id = emp["user_id"]
            if not assignee_email:
                assignee_email = emp["email"] or ""

    cur = db.execute(
        "INSERT INTO tasks (title, description, status, assignee_id, client_id, due_date) VALUES (?,?,?,?,?,?)",
        (data["title"], data.get("description"),
         data.get("status") or "pending", assignee_id,
         data.get("clientId"), data.get("dueDate")),
    )
    db.commit()
    tid = cur.lastrowid

    notif_title = f"تم تكليفك بمهمة جديدة: {data['title']}"
    notif_body = (data.get("description") or "") + \
        "\n\nيرجى مراجعة تفاصيل المهمة وإرسال عملك من صفحة المهمة."
    if assignee_user_id:
        # Creates the in-app notification AND sends an email when the user
        # has an email address + SMTP is configured.
        _notify(assignee_user_id, "task_assigned", notif_title, notif_body, tid)
    elif assignee_email:
        # Employee was added by the admin without registering; we can still
        # email them so they know a task was assigned. Once they register
        # with the same email, the employee record auto-links to their user
        # and future notifications will also appear inside the dashboard.
        send_email(assignee_email, notif_title, notif_body)
    return jsonify({"id": tid})


@app.put("/api/tasks/<int:tid>")
def update_task(tid):
    if (e := require_admin()): return e
    data = request.get_json(silent=True) or {}
    db = get_db()
    if not db.execute("SELECT id FROM tasks WHERE id=?", (tid,)).fetchone():
        return jsonify({"error": "not_found"}), 404

    assignee_id = data.get("assigneeId")
    assignee_email = (data.get("assigneeEmail") or "").strip()
    if assignee_email and not assignee_id:
        emp_id, _user_id, err = _resolve_assignee_by_email(assignee_email)
        if err == "user_not_registered":
            return jsonify({"error": "user_not_registered",
                            "message": "لا يوجد مستخدم مسجّل بهذا البريد."}), 400
        assignee_id = emp_id

    db.execute(
        "UPDATE tasks SET title=?, description=?, status=?, assignee_id=?, client_id=?, due_date=? WHERE id=?",
        (data.get("title") or "", data.get("description"),
         data.get("status") or "pending", assignee_id,
         data.get("clientId"), data.get("dueDate"), tid),
    )
    db.commit()
    return jsonify({"ok": True})


@app.delete("/api/tasks/<int:tid>")
def delete_task(tid):
    if (e := require_admin()): return e
    db = get_db()
    db.execute("DELETE FROM tasks WHERE id=?", (tid,))
    db.commit()
    return jsonify({"ok": True})


@app.post("/api/tasks/<int:tid>/submit")
def submit_task(tid):
    """Employee submits work for review."""
    if (e := require_login()): return e
    me = current_user()
    data = request.get_json(silent=True) or {}
    db = get_db()
    task = db.execute(
        "SELECT t.*, e.user_id as assignee_user_id FROM tasks t "
        "LEFT JOIN employees e ON e.id=t.assignee_id WHERE t.id=?", (tid,),
    ).fetchone()
    if not task:
        return jsonify({"error": "not_found"}), 404
    if me["role"] != "admin" and task["assignee_user_id"] != me["id"]:
        return jsonify({"error": "forbidden"}), 403
    db.execute(
        "INSERT INTO task_submissions (task_id, message, attachment_name, attachment_url, submitted_by_user_id) VALUES (?,?,?,?,?)",
        (tid, data.get("message"), data.get("attachmentName"), data.get("attachmentUrl"), me["id"]),
    )
    db.execute("UPDATE tasks SET status='submitted' WHERE id=?", (tid,))
    db.commit()
    body = data.get("message") or "تم إرسال المرفق وهو الآن بانتظار مراجعة الإدارة."
    if data.get("attachmentName"):
        body += f"\nالمرفق: {data['attachmentName']}"
    _notify_all_admins(
        "task_submitted",
        f"الموظف {me['username']} سلّم المهمة \"{task['title']}\" — قيد المراجعة",
        body, tid,
        exclude_user_id=me["id"] if me["role"] == "admin" else None,
    )
    return jsonify({"ok": True})


@app.post("/api/tasks/<int:tid>/review")
def review_task(tid):
    """Admin reviews. needs_changes returns task to in_progress so the
    employee continues working until they resubmit."""
    if (e := require_admin()): return e
    me = current_user()
    data = request.get_json(silent=True) or {}
    decision = data.get("decision")
    if decision not in ("approved", "rejected", "needs_changes"):
        return jsonify({"error": "invalid_decision"}), 400
    db = get_db()
    task = db.execute(
        "SELECT t.*, e.user_id as assignee_user_id, e.email as assignee_email "
        "FROM tasks t LEFT JOIN employees e ON e.id=t.assignee_id WHERE t.id=?",
        (tid,),
    ).fetchone()
    if not task:
        return jsonify({"error": "not_found"}), 404
    db.execute(
        "INSERT INTO task_reviews (task_id, decision, feedback, reviewed_by_user_id) VALUES (?,?,?,?)",
        (tid, decision, data.get("feedback"), me["id"]),
    )
    new_status = {
        "approved": "done",
        "rejected": "rejected",
        "needs_changes": "in_progress",  # back to work, per requirement
    }[decision]
    db.execute("UPDATE tasks SET status=? WHERE id=?", (new_status, tid))
    db.commit()
    decision_ar = {
        "approved": "تمت الموافقة على",
        "rejected": "تم رفض",
        "needs_changes": "يلزم تعديل",
    }[decision]
    notif_title = f"رد الإدارة على المهمة \"{task['title']}\": {decision_ar}"
    notif_body = data.get("feedback") or "اطلع على تفاصيل المراجعة من صفحة المهمة."
    if task["assignee_user_id"]:
        _notify(task["assignee_user_id"], "task_" + decision, notif_title, notif_body, tid)
    elif task["assignee_email"]:
        # Email-only fallback when the employee has no in-app account yet.
        send_email(task["assignee_email"], notif_title, notif_body)
    return jsonify({"ok": True, "status": new_status})


@app.post("/api/tasks/<int:tid>/messages")
def post_task_message(tid):
    """Private message thread between assignee and admins (with optional attachment)."""
    if (e := require_login()): return e
    me = current_user()
    data = request.get_json(silent=True) or {}
    db = get_db()
    task = db.execute(
        "SELECT t.*, e.user_id as assignee_user_id, e.full_name as assignee_name FROM tasks t "
        "LEFT JOIN employees e ON e.id=t.assignee_id WHERE t.id=?", (tid,),
    ).fetchone()
    if not task:
        return jsonify({"error": "not_found"}), 404
    if me["role"] != "admin" and task["assignee_user_id"] != me["id"]:
        return jsonify({"error": "forbidden"}), 403
    if not (data.get("body") or data.get("attachmentUrl")):
        return jsonify({"error": "empty_message"}), 400
    db.execute(
        "INSERT INTO task_messages (task_id, sender_user_id, body, attachment_name, attachment_url) VALUES (?,?,?,?,?)",
        (tid, me["id"], data.get("body"), data.get("attachmentName"), data.get("attachmentUrl")),
    )

    # ---- Auto-promote workflow status:
    #   pending → in_progress  the moment the assignee posts their first reply
    auto_status_changed = False
    if me["role"] != "admin" \
       and task["assignee_user_id"] == me["id"] \
       and task["status"] == "pending":
        db.execute("UPDATE tasks SET status='in_progress' WHERE id=?", (tid,))
        auto_status_changed = True
    db.commit()

    title = f"رسالة جديدة بخصوص المهمة \"{task['title']}\""
    body = (data.get("body") or "")[:500]
    if data.get("attachmentName"):
        body += f"\nمرفق: {data['attachmentName']}"
    if me["role"] == "admin":
        if task["assignee_user_id"]:
            _notify(task["assignee_user_id"], "task_message", title, body, tid)
    else:
        _notify_all_admins("task_message", title, body, tid, exclude_user_id=me["id"])
        # let admins know the task moved to "in progress" automatically
        if auto_status_changed:
            _notify_all_admins(
                "task_status",
                f"المهمة \"{task['title']}\" أصبحت قيد التنفيذ",
                f"بدأ {task['assignee_name'] or 'الموظف'} العمل بعد رده على الرسالة.",
                tid,
                exclude_user_id=me["id"],
            )
    return jsonify({"ok": True, "autoStatus": "in_progress" if auto_status_changed else None})


# ============================================================
# Notifications API
# ============================================================

@app.get("/api/notifications")
def list_notifications():
    if (e := require_login()): return e
    me = current_user()
    rows = get_db().execute(
        "SELECT * FROM notifications WHERE recipient_user_id=? ORDER BY id DESC LIMIT 100",
        (me["id"],),
    ).fetchall()
    return jsonify([{
        "id": r["id"], "type": r["type"], "title": r["title"], "body": r["body"],
        "relatedTaskId": r["related_task_id"],
        "read": bool(r["read"]), "createdAt": r["created_at"],
    } for r in rows])


@app.get("/api/notifications/unread_count")
def unread_count():
    if (e := require_login()): return e
    me = current_user()
    row = get_db().execute(
        "SELECT COUNT(*) c FROM notifications WHERE recipient_user_id=? AND read=0",
        (me["id"],),
    ).fetchone()
    return jsonify({"count": row["c"]})


@app.post("/api/notifications/<int:nid>/read")
def mark_read(nid):
    if (e := require_login()): return e
    me = current_user()
    db = get_db()
    db.execute("UPDATE notifications SET read=1 WHERE id=? AND recipient_user_id=?", (nid, me["id"]))
    db.commit()
    return jsonify({"ok": True})


@app.post("/api/notifications/read_all")
def mark_all_read():
    if (e := require_login()): return e
    me = current_user()
    db = get_db()
    db.execute("UPDATE notifications SET read=1 WHERE recipient_user_id=?", (me["id"],))
    db.commit()
    return jsonify({"ok": True})


# ============================================================
# File uploads
# ============================================================

@app.post("/api/uploads")
def upload_file():
    if (e := require_login()): return e
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "no_file"}), 400
    safe = secure_filename(f.filename) or "file"
    name = f"{uuid.uuid4().hex}_{safe}"
    path = os.path.join(UPLOAD_DIR, name)
    f.save(path)
    return jsonify({"name": f.filename, "url": f"/uploads/{name}"})


# ============================================================
# Email settings (admin)
# ============================================================

@app.get("/api/settings/email")
def get_email_settings():
    if (e := require_admin()): return e
    cfg = smtp_config()
    cfg.pop("password", None)
    cfg["password_set"] = bool(get_setting("smtp_password"))
    return jsonify(cfg)


@app.put("/api/settings/email")
def put_email_settings():
    if (e := require_admin()): return e
    data = request.get_json(silent=True) or {}
    set_setting("smtp_host", (data.get("host") or "").strip())
    set_setting("smtp_port", str(int(data.get("port") or 587)))
    set_setting("smtp_user", (data.get("user") or "").strip())
    if data.get("password"):
        set_setting("smtp_password", data["password"])
    if data.get("clearPassword"):
        set_setting("smtp_password", "")
    set_setting("smtp_from", (data.get("from") or "").strip())
    set_setting("smtp_from_name", (data.get("fromName") or "ORCA Agency").strip())
    set_setting("smtp_use_tls", "1" if data.get("useTls", True) else "0")
    return jsonify({"ok": True})


@app.post("/api/settings/email/test")
def test_email():
    if (e := require_admin()): return e
    data = request.get_json(silent=True) or {}
    to = (data.get("to") or "").strip() or (current_user() or {}).get("email")
    if not to:
        return jsonify({"ok": False, "error": "no_recipient"}), 400
    res = send_email(
        to,
        "ORCA — رسالة اختبارية",
        "هذه رسالة اختبارية من لوحة تحكم ORCA. إذا وصلتك فالإعدادات صحيحة.",
    )
    return jsonify(res)


# ============================================================
# Editable site content (WordPress-like)
# ============================================================

@app.get("/api/content")
def get_content():
    """Public — returns ALL overrides as a flat dict so the frontend can hydrate."""
    rows = get_db().execute("SELECT key, value FROM content_overrides").fetchall()
    return jsonify({r["key"]: r["value"] for r in rows})


@app.put("/api/content")
def put_content():
    """Admin — bulk update. Body: {key: value, ...}"""
    if (e := require_admin()): return e
    me = current_user()
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "invalid_body"}), 400
    db = get_db()
    for k, v in data.items():
        if v is None or v == "":
            db.execute("DELETE FROM content_overrides WHERE key=?", (k,))
        else:
            db.execute(
                "INSERT INTO content_overrides(key,value,updated_at,updated_by_user_id) "
                "VALUES(?,?,datetime('now'),?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "  updated_at=excluded.updated_at, updated_by_user_id=excluded.updated_by_user_id",
                (k, str(v), me["id"]),
            )
    db.commit()
    return jsonify({"ok": True, "count": len(data)})


# ============================================================
# DANGER ZONE — wipe all operational data
# Admin-only. Resets the workspace to "day-zero":
#   • all clients, projects, attachments
#   • all employees (except the linked admin user)
#   • all documents (quotes / contracts / invoices) and their items
#   • all tasks, task messages, attachments, notifications
#   • all content overrides + uploaded files
# Preserves: users with role='admin', SMTP & site settings.
# ============================================================
# NOTE: accepts BOTH POST and DELETE so the call works no matter which
# HTTP verb the front-end (or a cached/older build) chooses to send.
# This fixes the previously-reported "HTTP 405 deletion failed" error.
@app.route("/api/admin/wipe", methods=["POST", "DELETE"])
def admin_wipe_data():
    if (e := require_admin()): return e
    data = request.get_json(silent=True) or {}
    # Accept the confirmation token from JSON body OR ?confirm=… query
    # param so the action is survivable even when the body is dropped
    # by an intermediate proxy.
    token = (data.get("confirm") or request.args.get("confirm") or "").strip().upper()
    if token != "DELETE":
        return jsonify({"error": "confirmation_required",
                        "hint": "Send {confirm:'DELETE'} to proceed."}), 400
    db = get_db()
    tables_to_clear = [
        "task_messages", "task_attachments", "tasks",
        "document_items", "documents",
        "attachments", "projects", "clients",
        "employees", "notifications", "content_overrides",
    ]
    cleared = {}
    for t in tables_to_clear:
        try:
            cur = db.execute(f"DELETE FROM {t}")
            cleared[t] = cur.rowcount
        except Exception as exc:
            # Table may not exist in older schemas — skip gracefully.
            app.logger.warning(f"wipe: skip {t}: {exc}")
            cleared[t] = 0
    # Reset the per-table serial counters so the next created
    # client/employee/document starts from 0001 again.
    try:
        db.execute("DELETE FROM sqlite_sequence WHERE name IN "
                   "('clients','employees','documents','projects',"
                   " 'tasks','attachments','notifications')")
    except Exception:
        pass
    # Drop non-admin users so the team is wiped too.
    try:
        cur = db.execute("DELETE FROM users WHERE role <> 'admin'")
        cleared["users"] = cur.rowcount
    except Exception as exc:
        app.logger.warning(f"wipe: users: {exc}")
        cleared["users"] = 0
    db.commit()
    # Also remove uploaded files from disk so storage is reclaimed.
    upload_dir = os.path.join(STATIC_DIR, "uploads")
    removed_files = 0
    try:
        if os.path.isdir(upload_dir):
            for fn in os.listdir(upload_dir):
                fp = os.path.join(upload_dir, fn)
                try:
                    if os.path.isfile(fp):
                        os.remove(fp)
                        removed_files += 1
                except Exception:
                    pass
    except Exception:
        pass
    return jsonify({"ok": True, "cleared": cleared, "files_removed": removed_files})


# ============================================================
# Documents API
# ============================================================

@app.get("/api/documents")
def list_documents():
    if (e := require_login()): return e
    kind = request.args.get("kind")
    q = (request.args.get("search") or "").strip()
    db = get_db()
    sql = "SELECT * FROM documents WHERE 1=1"
    params = []
    if kind:
        sql += " AND kind=?"
        params.append(kind)
    if q:
        sql += " AND (client_name LIKE ? OR serial LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY id DESC"
    rows = db.execute(sql, params).fetchall()
    return jsonify([_doc_to_json(r) for r in rows])


@app.post("/api/documents")
def create_document():
    if (e := require_admin()): return e
    data = request.get_json(silent=True) or {}
    kind = data.get("kind") or "quote"
    if kind not in ("quote", "invoice", "contract"):
        return jsonify({"error": "invalid_kind"}), 400
    prefix = {"quote": "QTN-", "invoice": "INV-", "contract": "CON-"}[kind]
    serial = next_serial(prefix, "documents", year_in=True)
    items = data.get("items") or []
    subtotal = sum((i.get("quantity", 0) or 0) * (i.get("unitPrice", 0) or 0) for i in items)
    discount = float(data.get("discountPercent") or 0)
    vat = 0.0
    after_disc = subtotal * (1 - discount / 100.0)
    total = after_disc
    db = get_db()
    cur = db.execute(
        "INSERT INTO documents (serial, kind, language, client_id, client_name, client_company, client_address, client_email, client_phone, items_json, subtotal, discount_percent, vat_percent, total, currency, notes, valid_until, contract_subject, contract_terms, party_one_name, party_two_name, contract_body, contract_agreement, party_one_capacity, party_one_id_number, party_one_cr_number, party_two_capacity, party_two_id_number, party_two_cr_number) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
          (serial, kind, data.get("language") or "ar",
           data.get("clientId"), data.get("clientName") or "", data.get("clientCompany"),
           data.get("clientAddress"), data.get("clientEmail"), data.get("clientPhone"),
           json.dumps(items), subtotal, discount, vat, total,
           data.get("currency") or "SAR", data.get("notes"), data.get("validUntil"),
           data.get("contractSubject"), data.get("contractTerms"),
           data.get("partyOneName"), data.get("partyTwoName"),
           data.get("contractBody"), data.get("contractAgreement"),
           data.get("partyOneCapacity"), data.get("partyOneIdNumber"), data.get("partyOneCrNumber"),
           data.get("partyTwoCapacity"), data.get("partyTwoIdNumber"), data.get("partyTwoCrNumber")),
    )
    db.commit()
    return jsonify({"id": cur.lastrowid, "serial": serial})


@app.get("/api/documents/<int:did>")
def get_document(did):
    if (e := require_login()): return e
    row = get_db().execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone()
    if not row:
        return jsonify({"error": "not_found"}), 404
    return jsonify(_doc_to_json(row))


def _doc_to_json(r):
    return {
        "id": r["id"], "serial": r["serial"], "kind": r["kind"], "language": r["language"],
        "clientId": r["client_id"], "clientName": r["client_name"], "clientCompany": r["client_company"],
        "clientAddress": r["client_address"], "clientEmail": r["client_email"], "clientPhone": r["client_phone"],
        "items": json.loads(r["items_json"] or "[]"),
        "subtotal": r["subtotal"], "discountPercent": r["discount_percent"],
        "vatPercent": r["vat_percent"], "total": r["total"], "currency": r["currency"],
        "notes": r["notes"], "validUntil": r["valid_until"],
        "contractSubject": r["contract_subject"], "contractTerms": r["contract_terms"],
          "partyOneName": r["party_one_name"], "partyTwoName": r["party_two_name"],
          "contractBody": r["contract_body"], "contractAgreement": r["contract_agreement"],
          "partyOneCapacity": r["party_one_capacity"], "partyOneIdNumber": r["party_one_id_number"],
          "partyOneCrNumber": r["party_one_cr_number"] if "party_one_cr_number" in r.keys() else None,
          "partyTwoCapacity": r["party_two_capacity"], "partyTwoIdNumber": r["party_two_id_number"],
          "partyTwoCrNumber": r["party_two_cr_number"] if "party_two_cr_number" in r.keys() else None,
          "issuedAt": r["issued_at"], "createdAt": r["created_at"],
    }


# ============================================================
# Dashboard summary
# ============================================================

@app.get("/api/dashboard/summary")
def dashboard_summary():
    if (e := require_login()): return e
    db = get_db()
    total_clients = db.execute("SELECT COUNT(*) c FROM clients").fetchone()["c"]
    total_employees = db.execute("SELECT COUNT(*) c FROM employees").fetchone()["c"]
    total_quotes = db.execute("SELECT COUNT(*) c FROM documents WHERE kind='quote'").fetchone()["c"]
    total_invoices = db.execute("SELECT COUNT(*) c FROM documents WHERE kind='invoice'").fetchone()["c"]
    total_contracts = db.execute("SELECT COUNT(*) c FROM documents WHERE kind='contract'").fetchone()["c"]
    revenue = db.execute("SELECT COALESCE(SUM(total),0) s FROM documents WHERE kind='invoice'").fetchone()["s"]
    statuses = ["pending", "in_progress", "blocked", "done", "submitted", "rejected"]
    by = {}
    for s in statuses:
        by_key = "inProgress" if s == "in_progress" else s
        by[by_key] = db.execute("SELECT COUNT(*) c FROM tasks WHERE status=?", (s,)).fetchone()["c"]
    return jsonify({
        "totalClients": total_clients, "totalEmployees": total_employees,
        "totalQuotes": total_quotes, "totalInvoices": total_invoices,
        "totalContracts": total_contracts, "revenue": revenue, "tasksByStatus": by,
    })


@app.get("/api/dashboard/activity")
def recent_activity():
    if (e := require_login()): return e
    db = get_db()
    items = []
    for r in db.execute("SELECT id, name, created_at FROM clients ORDER BY id DESC LIMIT 5"):
        items.append({"id": f"c{r['id']}", "title": f"New client: {r['name']}", "subtitle": "", "createdAt": r["created_at"]})
    for r in db.execute("SELECT id, serial, kind, total, currency, created_at FROM documents ORDER BY id DESC LIMIT 5"):
        items.append({"id": f"d{r['id']}", "title": f"{r['kind'].title()} {r['serial']}", "subtitle": f"{r['total']:.0f} {r['currency']}", "createdAt": r["created_at"]})
    items.sort(key=lambda x: x["createdAt"], reverse=True)
    return jsonify(items[:10])


# ============================================================
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "5000"))
    print(f"\n  ORCA Agency Dashboard running at  →  http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
