ORCA Agency Dashboard — modified files
  Date: 2026-04-30T02:32:18.905Z

  Drop these files into your existing project preserving the same paths,
  then restart Flask. Only files that were changed in this round are
  included; everything else stays as-is.

  Changes:
  1. app.py — branded email template (dark-navy hero + CTA + auto language),
     POST /api/admin/wipe endpoint.
  2. static/css/styles.css — dark-mode KPI status pills, contract / invoice
     templates, larger logo (.doc-logo-xl), party cards, signature boxes,
     billed-to dark card, payment-methods grid, footer band, print rules.
  3. static/js/i18n.js — new keys for billed-to, payment methods, bismillah,
     signatures, danger zone.
  4. static/js/pages/dashboard.js — KPI status cards now use class+dataset
     so CSS can theme them in both light and dark mode.
  5. static/js/pages/settings.js — new Danger zone tab with two-step
     confirmation calling /api/admin/wipe.
  6. static/js/pages/clients.js — wraps PDF/Excel/Word exports in toast +
     busy state; PDF now uses a hidden iframe instead of window.open()
     so the buttons no longer get popup-blocked.
  7. static/js/pages/document-detail.js — full redesign: contracts render
     as cover + body + signature pages; invoices match invoice.jpeg with
     billed-to + payment methods; quotes keep the single-page layout.
  8. static/js/pages/register.js — full registration form (from prior round).
  