# SiudPaySlipBot — Implementation Plan & Work Summary

## What This Bot Does

A Telegram bot for Israeli employers of foreign caregivers (עובד זר בסיעוד). Runs a Hebrew-language
FSM conversation, calculates monthly salary per Israeli labor law, generates a formal Hebrew PDF
payslip, sends it, and immediately deletes all data. **Zero-Data Retention** is a core product
guarantee.

Community open-source tool. Primary users: private individuals with no payroll background.

---

## Legal Constants (April 2026)

Sourced from siud.pirsuma.com/min_2026/ and siud.pirsuma.com/calc_nikuyim/

| Constant | Value | Notes |
|---|---|---|
| Min wage | **6,443.85 ₪** | From April 1 2026 (was 6,247.67) |
| Shabbat/holiday rate | **439.73 ₪/day** | Scales proportionally with min wage |
| Housing deduction max | **192 ₪** | Requires written worker consent |
| Health deduction max | **169 ₪** | |
| Extras deduction max | **94 ₪** | |
| Food deduction max | **644 ₪** | ~10% of min wage; not pro-rated |
| Total deductions cap | **25%** of gross | Hard legal limit |
| Pocket money | **100 ₪/week** | Counts as salary |
| Employer pension | **6.5%** of gross base | Informational only |
| Employer severance | **6.0%** of gross base | Informational only |
| Vacation accrual | **1.16 days/month** | Pro-rated for partial months |
| Sick accrual | **1.50 days/month** | Pro-rated for partial months |

Historical wages are stored in `WAGE_HISTORY` in `config.py`. Adding a future wage period is a
one-line change.

---

## Architecture

```
SiudPaySlipBot/
├── config.py              # All legal constants + dynamic wage lookup
├── calculator.py          # Pure salary math — no I/O, no Telegram, no PDF
├── pdf_generator.py       # ReportLab PDF (Hebrew RTL, two-font architecture)
├── bot.py                 # aiogram v3 FSM — all conversation handlers
├── fonts/                 # NotoSansHebrew-Regular.ttf (git-ignored, downloaded at setup)
├── scripts/
│   ├── download_fonts.py  # One-time font download
│   └── generate_sample.py # Visual PDF inspection (does NOT delete the file)
├── tests/
│   ├── test_calculator.py  # 43 unit tests — pure math, no mocking
│   └── test_bot_helpers.py # 28 integration tests — FSM data → PayslipInput → calculate()
├── plans/PLAN.md          # This file
├── CLAUDE.md              # Developer guide for AI-assisted maintenance
├── Dockerfile
├── requirements.txt
└── .env.example
```

---

## Data Models

### `PayslipInput` (calculator.py)
All data collected from the FSM conversation. Fields: month, year, is_full_month, days_worked
(1–26), employer_name, caregiver_name, passport_number, shabbat_days, holiday_days,
deduction_housing/health/extras/food, pocket_money_weeks, advances.

### `PayslipResult` (calculator.py)
Fully calculated payslip ready for PDF generation. Includes all earnings, deductions, net pay,
employer contributions (informational), and social rights accrual.

---

## Calculation Rules

```
min_wage, shabbat_rate = get_wage_params(month, year)   # dynamic per period
ratio = days_worked / 26
gross_base = min_wage × ratio
pocket_money = pocket_money_weeks × 100
shabbat_pay = shabbat_days × shabbat_rate
holiday_pay = holiday_days × shabbat_rate
total_gross = gross_base + pocket_money + shabbat_pay + holiday_pay
total_deductions = housing + health + extras + food + advances
total_net = total_gross − total_deductions

employer_pension = gross_base × 6.5%    ← informational, NOT subtracted from net
employer_severance = gross_base × 6.0%  ← informational, NOT subtracted from net
vacation_accrued = 1.16 × ratio
sick_accrued = 1.50 × ratio
```

**Validation:** `total_deductions > 25% of total_gross` → raises `ValueError`.
Housing/health/extras deductions are pro-rated for partial months (× days/26).
Food deduction is NOT pro-rated (reflects actual food provided).

---

## FSM Conversation Flow (bot.py)

```
/start
  → month_year       (quick-picker: prev/current/next month, or MM/YYYY text)
  → work_period      (inline: full month / partial month)
  → partial_type     (inline: started mid-month / ended mid-month)  ← only if partial
  → partial_day      (which calendar day — auto-calculates days_worked)
  → employer_name    (optional — skip button available)
  → caregiver_name   (optional — skip button available)
  → passport         (optional — skip button available)
  → shabbat_days
  → holiday_days
  → pocket_money_weeks
  → advances
  → deductions       (inline multi-select with amounts shown inline; ✏️ to edit each)
  → confirm          (text summary of all values — confirm or restart)
  → _generate_and_send() → PDF → delete
```

### Partial month calculation
`calculate_partial_days(partial_type, day, month, year)` in `calculator.py`:
- `"started"`: active_days = days_in_month − day + 1
- `"ended"`: active_days = day
- days_worked = max(1, round(active_days / days_in_month × 26))

### Deductions keyboard (integrated amounts)
- Each deduction row shows its current amount inline when selected: `✅ מגורים (₪118.15) | ✏️`
- Default amounts are pro-rata maxima, pre-calculated on entering the deductions step
- ✏️ transforms the keyboard message into an edit prompt; after responding, keyboard restores
- "סיום" goes directly to the confirm summary — no separate per-deduction amount steps

---

## PDF Generation (pdf_generator.py)

**Library:** ReportLab + python-bidi + arabic-reshaper + NotoSansHebrew TTF

**Two-font architecture (critical — do not regress):**
- `NotoHebrew` — Hebrew glyphs + ₪ symbol
- `Helvetica` — digits, Latin, punctuation (ReportLab built-in)

`_mixed_markup(text)` splits strings into Hebrew/non-Hebrew runs and applies the correct font
per character. Numbers passed through `_h()` (bidi) alone will be invisible — always use
`_mixed_markup()` or `_amount_para()` for mixed strings.

**Layout:** title → employer/caregiver details → earnings table → deductions table →
net pay total → employer contributions section → footer (vacation/sick accrual + disclaimer).

---

## Zero-Data Retention (critical path)

```python
# bot.py _generate_and_send()
await state.clear()           # data leaves memory before PDF is created
pdf_path = generate_payslip_pdf(result)
try:
    await message.answer_document(FSInputFile(pdf_path), ...)
finally:
    os.remove(pdf_path)       # always runs — even if send fails
```

Never log personal data (names, passport numbers). Never cache `PayslipResult`.

---

## Known Issues Fixed During Development

| Issue | Fix |
|---|---|
| ₪ invisible in PDF | `_is_he()` now routes U+20AA (₪) to NotoHebrew |
| Bot silently stuck on errors | Added `@router.error()` global handler that logs + notifies user |
| `04/26` month input rejected | Regex now accepts 2-digit year → `2000 + int(year)` |
| Deduction amounts — separate step | Integrated into selection keyboard with inline ✏️ editing |

---

## Running Locally

```bash
pip install -r requirements.txt
python scripts/download_fonts.py   # first time only
python bot.py

# Visual PDF check
python scripts/generate_sample.py sample_payslip.pdf

# Tests (71 tests, all must pass)
pytest tests/ -v
```

---

## Docker

```bash
docker build -t payslipbot .
docker run --env-file .env payslipbot
```

The Dockerfile downloads the font at build time.