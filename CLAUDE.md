# CLAUDE.md — SiudPaySlipBot

## What this project is

A Telegram bot for Israeli employers of foreign caregivers (עובד זר בסיעוד). It runs a Hebrew-language FSM conversation, calculates monthly salary per Israeli labor law, generates a formal Hebrew PDF payslip, sends it, and immediately deletes all payroll data.

**Data retention policy (nuanced):**
- Passport numbers, salary figures, and PDF files → deleted immediately after sending (Zero-Data Retention)
- Employer name, caregiver name, vacation/sick accruals → stored in Firestore per user for UX convenience
- Users can delete their stored data with `/forget_me`

Community open-source tool. Primary users: private individuals with no payroll background. The bot must be self-explanatory in Hebrew.

---

## Module responsibilities (strict boundaries)

| File | Responsibility |
|---|---|
| `config.py` | All legal constants, dynamic wage lookup, rest-day helpers, paths, env vars |
| `calculator.py` | Pure math — no I/O, no Telegram, no PDF |
| `pdf_generator.py` | ReportLab PDF only — receives a `PayslipResult`, returns a temp file path |
| `bot.py` | aiogram v3 FSM, keyboards, user conversation, calls the other three |
| `database.py` | Firestore async layer — get/upsert/balance/delete per user |
| `scripts/download_fonts.py` | One-time font download; must work standalone |
| `scripts/generate_sample.py` | Visual PDF inspection; does NOT delete the file (unlike bot.py) |
| `tests/test_calculator.py` | Pure math unit tests |
| `tests/test_bot_helpers.py` | Integration tests for bot helper functions |
| `tests/test_setup_done_flow.py` | Tests for setup→start callback routing (user_id correctness) |

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

Passport numbers are never stored in Firestore. The fast-track flow (when names are saved) auto-skips the passport question — `_SKIPPED` sentinel is used throughout.

---

## Firestore persistence (database.py)

Stores per-user data in collection `payslip_users`, document key = `str(telegram_user_id)`.

**Document schema:**
```
agreed_net_salary:     float   # agreed monthly net salary from /setup (Simple Mode)
employer_name:         str     # last used employer name
caregiver_name:        str     # last used caregiver name
employment_start_date: str     # ISO "YYYY-MM-DD" — used for month-picker filtering
rest_day:              str     # "saturday" | "friday" | "sunday" (default: "saturday")
monthly_accruals:      map     # idempotent per-month accrual map
  "YYYY-MM":
    vacation: float            # vacation days accrued for that month (latest calculation)
    sick:     float            # sick days accrued for that month (latest calculation)
```

Balances are computed at read time by summing all `monthly_accruals` values — never stored as a flat total. Regenerating a payslip for the same month overwrites the same key, preventing double-counting.

Legacy fields that may exist in old documents (ignored by current code):
`vacation_balance`, `sick_balance`, `base_housing`, `base_health`, `base_extras`, `contract_region`, `contract_ownership`

**Key rules:**
- All functions degrade silently if Firestore is unavailable — bot still works without it
- `upsert_user` uses `merge=True` so it never overwrites other fields
- `upsert_contract` writes `agreed_net_salary`, `employment_start_date`, and `rest_day`
- `upsert_month_accrual` uses `update()` with dot-notation to touch only one month key; falls back to `set(merge=True)` on `NotFound` (new document)
- `get_balances` sums the `monthly_accruals` map — returns `(0.0, 0.0)` if empty or unavailable
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

## Calculation rules (Simple Mode — current)

**Simple Mode** is the active mode. The bot asks for an agreed monthly net salary instead of computing gross from legal minimums and itemizing deductions.

### Two ratios — do not confuse them

| Ratio | Formula | Used for |
|---|---|---|
| Salary ratio | `days_worked / 26` | `gross_base` |
| Accrual ratio | `active_days / days_in_month` | `vacation_accrued`, `sick_accrued` |

`days_worked` counts non-rest-day calendar days in the period. `active_days` counts all calendar days (including rest days). For a full month both ratios equal 1. For partial months they differ.

**Why different ratios?** Salary is paid for working days only. Social benefits accrue for every day the worker is in an employment relationship — rest days included. This matches Israeli labor law.

### Summary

- `gross_base = agreed_net_salary × (days_worked / 26)`
- `shabbat_addition = shabbat_days × shabbat_rate` (added on top of gross_base)
- `holiday_addition = holiday_days × shabbat_rate` (added on top)
- Advances are the only per-payslip deduction
- `net_pay = gross_base + shabbat_addition + holiday_addition − advances`
- `vacation_accrued = 1.16 × (active_days / days_in_month)`
- `sick_accrued = 1.50 × (active_days / days_in_month)`
- Employer pension and severance are informational — they do **not** reduce net pay
- The 25% deduction cap still applies (validation in calculator.py)

**Detailed Mode infrastructure** is preserved in the code but bypassed. All bypassed fields are marked with `# ARCHITECTURE NOTE` comments. See `plans/simple-mode-agreed-net-salary.md` for re-enablement instructions.

---

## Partial-month day calculation

`calculate_partial_days(partial_type, day, month, year, rest_day_weekday=5) → (active_days, days_worked)`

- `active_days`: count of calendar days in the period (e.g. Apr 12-30 = 19)
- `days_worked`: count of non-rest-day days in the period (e.g. 19 − 2 Saturdays = 17)
- Full month always returns `(days_in_month, 26)` regardless of rest day
- Both values are stored in FSM state and passed to `PayslipInput`

The rest day used for counting comes from the `rest_day` saved in Firestore / FSM state.

---

## FSM state order (bot.py) — Simple Mode

```
/start
  → if no saved contract → show "הקלד /setup להתחלת ההגדרה" (redirect, do NOT embed setup)
  → (Firestore lookup — loads agreed_net_salary + names + rest_day into FSM state)
  → month_year        (quick-picker or MM/YYYY / MM/YY text)
  → work_period       (inline: full / partial)
                      [if start month matches employment_start_date → auto partial, skip work_period]
  → days_worked       (only for explicit partial)
  → details_confirm   (fast-track: if both names saved → show summary card with [✅ כן, המשך] / [✏️ עריכת פרטים])
     OR employer_name → caregiver_name → passport  (if names not saved or user wants to edit)
  → shabbat_days      ("כמה ימי {rest_day_label} עבד/ה?" — 0-button available)
  → holiday_days      (0-button available)
  → advances          ("מקדמות / דמי כיס ששולמו במזומן" — 0-button available)
  → _show_confirm() → _generate_and_send()
      → upsert names to Firestore
      → upsert_month_accrual to Firestore
      → generate PDF → send → delete PDF
      → show updated vacation/sick balance
```

**`/setup` wizard** (`ContractSetupForm`):
```
agreed_net_salary → start_date → rest_day → employer_name → caregiver_name → _complete_setup()
  → shows settings summary + "📄 הפק תלוש עכשיו" button
```

**`/start` → `/setup` routing:**  `cmd_start` calls `_run_start_flow(message, state, user_id=message.from_user.id)`. The "הפק תלוש עכשיו" callback calls `_run_start_flow(..., user_id=callback.from_user.id)` — **never** `callback.message.from_user.id` (which would be the bot's own ID).

Commands available at any time: `/start` (restart), `/setup` (update settings), `/forget_me` (delete Firestore data)

The month picker shows prev/current/next month buttons (current pre-selected ✅), plus "📅 חודש אחר…" which falls back to text. Text input accepts `MM/YYYY` or `MM/YY` (2-digit year → `2000 + int(year)`).

Both the callback path and text path share `_confirm_month_and_proceed()` — keep them in sync. Both paths store both `active_days` and `days_worked` in FSM state.

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

# Tests (110 tests, all must pass)
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

- `pytest tests/ -v` must stay at 110/110
- After any `pdf_generator.py` change: run `generate_sample.py` and visually verify Hebrew RTL is correct and all numbers are visible
- After any `calculator.py` change: verify Simple Mode — `agreed_net=5989`, `days=26`, `advances=0` → `net=5989.00`; partial `days=17, active=19, month=Apr` → `vacation=1.16×19/30`
- After any `bot.py` FSM change: trace the full conversation — `/setup` → "📄 הפק תלוש" → full month → 0 advances → confirm → PDF
- After any `database.py` change: test graceful degradation (run without GCP credentials — bot must still generate PDFs)

---

## Things to avoid

- Do not persist passport numbers, salary details, or PDF files
- Do not cache `PayslipResult` or delay `state.clear()`
- Do not pass numeric strings through `_h()` alone — use `_mixed_markup()` or `_amount_para()`
- Do not hardcode a single minimum wage — always go through `get_wage_params(month, year)` (used for employer pension/severance even in Simple Mode)
- Do not subtract employer contributions from net pay
- Do not write `"---"` (the skipped-field sentinel) to Firestore
- Do not let Firestore errors crash the bot — all `database.py` calls must be wrapped in try/except
- Do not remove the `# ARCHITECTURE NOTE` code blocks in `bot.py` / `calculator.py` — they are the Detailed Mode infrastructure preserved for future use
- Do not set `net_salary_override` to a non-zero value in tests that are validating standard min-wage behavior (it would bypass the legal calculation)
- Do not use `active_days / days_in_month` for the salary/gross calculation — only for social benefit accrual
- Do not call `cmd_start(callback.message, state)` from callback handlers — use `_run_start_flow(..., user_id=callback.from_user.id)` instead (`callback.message.from_user` is the bot, not the user)
