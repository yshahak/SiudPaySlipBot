# CLAUDE.md — SiudPaySlipBot

## What this project is

A Telegram bot for Israeli employers of foreign caregivers (עובד זר בסיעוד). It runs a Hebrew-language FSM conversation, calculates monthly salary per Israeli labor law, generates a formal Hebrew PDF payslip, sends it, and immediately deletes all payroll data.

**Data retention policy (nuanced):**
- Passport numbers, salary figures, and PDF files → deleted immediately after sending (Zero-Data Retention)
- Employer name, caregiver name, vacation balance, sick balance → stored in Firestore per user for UX convenience
- Users can delete their stored data with `/forget_me`

Community open-source tool. Primary users: private individuals with no payroll background. The bot must be self-explanatory in Hebrew.

---

## Module responsibilities (strict boundaries)

| File | Responsibility |
|---|---|
| `config.py` | All legal constants, dynamic wage lookup, paths, env vars |
| `calculator.py` | Pure math — no I/O, no Telegram, no PDF |
| `pdf_generator.py` | ReportLab PDF only — receives a `PayslipResult`, returns a temp file path |
| `bot.py` | aiogram v3 FSM, keyboards, user conversation, calls the other three |
| `database.py` | Firestore async layer — get/upsert/balance/delete per user |
| `scripts/download_fonts.py` | One-time font download; must work standalone |
| `scripts/generate_sample.py` | Visual PDF inspection; does NOT delete the file (unlike bot.py) |
| `tests/test_calculator.py` | 30 pytest unit tests; pure math, no mocking |

Do not mix these concerns. `calculator.py` must remain importable with no side effects.

---

## Critical: Zero-Data Retention (payroll data)

FSM state is cleared **before** PDF generation, not after. The `finally` block always deletes the PDF file — even if sending fails.

```python
# bot.py _generate_and_send()
await state.clear()          # data gone from memory before file is created
pdf_path = generate_payslip_pdf(result)
try:
    await message.answer_document(FSInputFile(pdf_path), ...)
finally:
    if pdf_path and os.path.exists(pdf_path):
        os.remove(pdf_path)  # always runs
```

Never add logging that persists personal data (passport numbers, salary details). Never buffer or cache `PayslipResult` objects.

---

## Firestore persistence (database.py)

Stores per-user data in collection `payslip_users`, document key = `str(telegram_user_id)`.

**Document schema:**
```
employer_name:    str   # last used employer name
caregiver_name:   str   # last used caregiver name
vacation_balance: float # cumulative accrued vacation days across all payslips
sick_balance:     float # cumulative accrued sick days across all payslips
```

**Key rules:**
- All functions degrade silently if Firestore is unavailable — bot still works without it
- `upsert_user` uses `merge=True` so it never overwrites balances
- `add_to_balances` uses `firestore.Increment` so it initialises to 0 on first write
- Skipped names (`"---"`) are never written to Firestore
- `/forget_me` deletes the entire document

---

## Critical: PDF Hebrew rendering — two-font architecture

**Do not regress this.** The original bug: the OpenMapTiles NotoSansHebrew font is a map-tile subset — it has Hebrew glyphs only. Digits, Latin characters, and even the ₪ symbol were completely invisible.

**Fix:** two fonts in every paragraph.

```python
_HE_FONT   = "NotoHebrew"   # Hebrew chars + ₪ symbol
_ASCII_FONT = "Helvetica"   # digits, Latin, punctuation (built-in ReportLab)
```

`_mixed_markup(text)` splits any string into contiguous Hebrew/non-Hebrew runs and wraps each in the correct `<font name="...">` tag. `_amount_para()` always puts ₪ in `_HE_FONT` and the number in Helvetica.

**Never pass a number or mixed string through `_h()` (the bidi function) without also going through `_mixed_markup()`.** The bidi algorithm processes the string but does not switch fonts — numbers still need Helvetica to be visible.

---

## Legal constants — how to update

When the Israeli minimum wage changes, add **one entry** to `WAGE_HISTORY` in `config.py`:

```python
WAGE_HISTORY: list[tuple[date, Decimal, Decimal]] = [
    (date(2023, 4, 1), Decimal("5571.75"), Decimal("391.89")),  # approx
    (date(2025, 4, 1), Decimal("6247.67"), Decimal("426.35")),  # confirmed
    (date(2026, 4, 1), Decimal("6443.85"), Decimal("439.73")),  # confirmed (current)
    # (date(2027, 4, 1), Decimal("XXXX.XX"), Decimal("XXX.XX")),  ← add here
]
```

`get_wage_params(month, year)` returns `(min_wage, shabbat_rate)` for any period. The shabbat rate scales proportionally with min wage — do not hardcode it.

**Source of truth:** siud.pirsuma.com/min_2026/ and siud.pirsuma.com/calc_nikuyim/

Current values (April 2026):
- Min wage: **6,443.85 ₪**
- Shabbat/holiday rate: **439.73 ₪/day**
- Housing deduction max: **192 ₪**
- Health deduction max: **169 ₪**
- Extras deduction max: **94 ₪**
- Food deduction max: **644 ₪** (10% of min wage, not pro-rated)
- Total deductions cap: **25% of total gross**
- Employer pension: **6.5%** of gross_base
- Employer severance: **6.0%** of gross_base

---

## Calculation rules (don't get these wrong)

- `gross_base = min_wage × (days_worked / 26)` — 26 is the monthly working-day basis
- Pocket money (דמי כיס) counts as salary, **100 ₪/week**, added to gross
- Shabbat and holiday additions use `shabbat_rate`, not `daily_rate`
- Housing, health, extras deductions are **pro-rated** for partial months (× days/26)
- Food deduction (כלכלה) is **not pro-rated** — it reflects actual food provided
- Employer pension and severance are informational only — they do **not** reduce net pay
- Total deductions > 25% of gross → raise `ValueError` (legal hard limit)

---

## FSM state order (bot.py)

```
/start
  → (Firestore lookup — saves saved_data + user_id to FSM state)
  → month_year      (quick-picker or MM/YYYY / MM/YY text)
  → work_period     (inline: full / partial)
  → days_worked     (only for partial)
  → employer_name   (shows "use previous details" button if Firestore has saved names)
      └─ use_saved  → skips caregiver_name, jumps to passport
  → caregiver_name  (skipped if use_saved)
  → passport
  → shabbat_days
  → holiday_days
  → pocket_money_weeks
  → advances
  → deductions      (inline multi-select toggle)
  → food_amount     (only if כלכלה selected)
  → _generate_and_send()
      → upsert names to Firestore (background task)
      → increment balances in Firestore (background task)
      → generate PDF → send → delete PDF
      → show updated vacation/sick balance
```

Commands available at any time: `/start` (restart), `/forget_me` (delete Firestore data)

The month picker shows prev/current/next month buttons (current pre-selected ✅), plus "📅 חודש אחר…" which falls back to text. Text input accepts `MM/YYYY` or `MM/YY` (2-digit year → `2000 + int(year)`).

Both the callback path and text path share `_confirm_month_and_proceed()` — keep them in sync.

---

## Running locally

```bash
# First time only
pip install -r requirements.txt
python scripts/download_fonts.py

# Authenticate with Google Cloud (for Firestore)
gcloud auth application-default login

# Run bot (polling mode — no WEBHOOK_URL needed locally)
python bot.py

# Visual PDF check
python scripts/generate_sample.py sample_payslip.pdf

# Tests (30 tests, all must pass)
pytest tests/ -v
```

Font is downloaded to `fonts/NotoSansHebrew-Regular.ttf` (git-ignored). If the bot raises `FileNotFoundError` about the font, run `download_fonts.py` first.

Without `gcloud auth application-default login`, Firestore will be unavailable — the bot still works, just without persistence.

---

## Docker

```bash
docker build -t payslipbot .
docker run --env-file .env payslipbot
```

The Dockerfile downloads the font at build time — no extra steps needed.

---

## Deployment (Google Cloud Run)

**Production URL:** `https://payslipbot-kqseclbsta-ew.a.run.app`
**GCP project:** `siud-payslip-bot` (europe-west1)

### CI/CD

Push to `main` → GitHub Actions (`.github/workflows/deploy.yml`) → builds Docker image → pushes to Artifact Registry → deploys to Cloud Run automatically.

Uses **Workload Identity Federation** — no long-lived credentials stored in GitHub.

Required GitHub secrets:
- `GCP_WORKLOAD_IDENTITY_PROVIDER` — WIF provider resource name
- `GCP_SERVICE_ACCOUNT` — `github-deployer@siud-payslip-bot.iam.gserviceaccount.com`
- `WEBHOOK_URL` — `https://payslipbot-kqseclbsta-ew.a.run.app`

`TELEGRAM_BOT_TOKEN` is pulled from **Secret Manager** at deploy time (not a GitHub secret).

### Webhook vs polling

- `WEBHOOK_URL` set → webhook mode (Cloud Run)
- `WEBHOOK_URL` unset → polling mode (local dev)

The HTTP server starts **before** registering the webhook with Telegram so Cloud Run's health check always passes.

### Known limitation

FSM uses `MemoryStorage` — mid-conversation state is lost on redeploy or if Cloud Run spins up a second instance. Future fix: replace with Firestore/Redis-backed FSM storage.

---

## What to test when making changes

- `pytest tests/ -v` must stay at 30/30
- After any `pdf_generator.py` change: run `generate_sample.py` and visually verify Hebrew RTL is correct and all numbers are visible
- After any `calculator.py` change: verify April 2026 full month — gross_base = 6443.85, 4 shabbats = 4×439.73 = 1758.92, total_gross = 6443.85 + pocket_money + 1758.92
- After any `bot.py` FSM change: trace the full conversation to confirm state transitions work for both full and partial months, with and without deductions, with and without saved Firestore data
- After any `database.py` change: test graceful degradation (run without GCP credentials — bot must still generate PDFs)

---

## Things to avoid

- Do not persist passport numbers, salary details, or PDF files
- Do not cache `PayslipResult` or delay `state.clear()`
- Do not pass numeric strings through `_h()` alone — use `_mixed_markup()` or `_amount_para()`
- Do not hardcode a single minimum wage — always go through `get_wage_params(month, year)`
- Do not pro-rate the food deduction (כלכלה)
- Do not subtract employer contributions from net pay
- Do not add deductions without checking the 25% cap
- Do not write `"---"` (the skipped-field sentinel) to Firestore
- Do not let Firestore errors crash the bot — all `database.py` calls must be wrapped in try/except