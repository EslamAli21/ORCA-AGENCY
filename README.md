# ORCA Agency Dashboard — Python Edition

Bilingual (Arabic/English) management dashboard. **No Node.js needed.**

## How to run

1. Install Python 3.9 or later.
2. Open this folder in a terminal.
3. Install Flask:
   ```
   pip install -r requirements.txt
   ```
4. Start the server:
   ```
   python app.py
   ```
5. Open your browser at: **http://localhost:5000**

The first time it runs, an `orca.db` SQLite file is created automatically and the
default admin user is added.

## Default login
- **Username:** `admin`
- **Password:** `admin123`

## File map

| Path | Purpose |
|---|---|
| `app.py` | The whole Flask backend: API + serves the frontend. |
| `requirements.txt` | Python dependencies (just Flask). |
| `orca.db` | SQLite database (auto-created on first run). |
| `static/index.html` | The single HTML page that loads the app. |
| `static/css/styles.css` | All styles (brand colors, layout, RTL). |
| `static/js/app.js` | Boot, router, auth state, layout shell. |
| `static/js/api.js` | `fetch` wrappers for the backend. |
| `static/js/i18n.js` | All Arabic + English translations. |
| `static/js/settings.js` | App settings + dashboard layout (saved in browser). |
| `static/js/rich-text.js` | The Word-like rich text editor. |
| `static/js/components/topbar.js` | Top bar (language toggle, settings, logout). |
| `static/js/components/sidebar.js` | Side menu. |
| `static/js/pages/dashboard.js` | Dashboard with admin section editor. |
| `static/js/pages/clients.js` | Clients list + detail. |
| `static/js/pages/employees.js` | Employees list + detail. |
| `static/js/pages/tasks.js` | Tasks board. |
| `static/js/pages/documents.js` | Documents list. |
| `static/js/pages/document-new.js` | Create quote / invoice / contract (rich text for contracts). |
| `static/js/pages/document-detail.js` | Printable document with Hijri + Gregorian dates. |
| `static/js/pages/login.js` | Login. |
| `static/js/pages/register.js` | Register (creates a viewer account). |
| `static/assets/orca-logo.png` | Brand logo. |

## Features
- Full Arabic UI with RTL when language is set to Arabic; switch to English from the top bar.
- **Admin dashboard editor** — when logged in as admin, click "Edit Dashboard" to add, delete, reorder sections, or insert custom heading/text/notice blocks.
- **Word-like rich text editor** — used for contract Subject and Terms, plus custom dashboard sections (font size, bold/italic/underline, colors, alignment, lists).
- Documents (Quotes, Invoices, Contracts) with auto serial numbers, Gregorian + Hijri (Umm al-Qura) dates, items table with discount/VAT/total, printable A4 layout, export to Word.
- Clients with serial IDs, sectors (real estate, finance, e-commerce, beauty salons, clinics, restaurants, cafes), projects, attachments, document feed.
- Employees with serial IDs, role badges, assigned-tasks panel.
- Task board with statuses: pending → in_progress → blocked → done.

## Environment variables
- `ADMIN_PASSWORD` — override the default admin password (default: `admin123`).
- `SESSION_SECRET` — Flask session secret (auto-generated if missing).
- `PORT` — port to listen on (default: `5000`).
