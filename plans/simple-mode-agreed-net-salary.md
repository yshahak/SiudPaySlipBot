# Plan: Simple Mode — Agreed Net Salary (Hidden Logic Architecture)

## Status: COMPLETE (as of April 2026)

All features below are implemented and tested. 110 tests pass.

---

## What was built

Simple Mode replaces the original per-deduction UX (housing region, health, extras, food) with a single agreed monthly net salary. The full deduction/gross infrastructure in `calculator.py`, `config.py`, and `pdf_generator.py` is **preserved with `# ARCHITECTURE NOTE` comments** for future Detailed Mode re-enablement.

### Core math

```
gross_base        = agreed_net_salary × (days_worked / 26)
vacation_accrued  = 1.16 × (active_days / days_in_month)  ← calendar ratio
sick_accrued      = 1.50 × (active_days / days_in_month)  ← calendar ratio
balance_to_pay    = gross_base + shabbat_addition + holiday_addition − advances
```

**Two ratios in play:**
- **Salary ratio** (`days_worked / 26`): counts only non-rest-day working days
- **Accrual ratio** (`active_days / days_in_month`): counts all calendar days employed (including rest days — the worker is in an employment relationship on those days)

---

## Implemented features (beyond original plan)

### `/setup` wizard (`ContractSetupForm`)

States: `agreed_net_salary → start_date → rest_day → employer_name → caregiver_name → _complete_setup()`

- Asks for agreed monthly net salary
- Asks for employment start date (for month-picker filtering and auto-detecting partial first month)
- **Asks for weekly rest day** (Friday / Saturday / Sunday) — drives both partial-day counting and shabbat question wording
- Completion screen shows settings summary + "📄 הפק תלוש עכשיו" button
- On `/start` with no contract → redirects to `/setup` (clean separation)

### Monthly payslip flow (`PayslipForm`)

States: `month_year → work_period → [days_worked] → [details_confirm | employer_name] → caregiver_name → passport → shabbat_days → holiday_days → advances → _show_confirm() → _generate_and_send()`

**Shabbat/holiday questions are active** (not bypassed). The agreed salary is the base; shabbat/holiday additions are on top.

**Fast-track flow**: if both employer and caregiver names are saved in Firestore, shows one confirmation card with [✅ כן, המשך] / [✏️ עריכת פרטים] instead of asking 3 questions sequentially. Passport is never stored (zero-data retention) — auto-skipped in fast-track.

### Partial-month day calculation (`calculator.py`)

`calculate_partial_days(partial_type, day, month, year, rest_day_weekday=5)`

- Counts **actual non-rest-day calendar days** in the period (not proportional formula)
- Full month always returns 26 regardless of rest day
- Returns `(active_days, days_worked)` — both values are used by the bot

### Social benefit accrual fix

`vacation_accrued` and `sick_accrued` use `active_days / days_in_month` (calendar ratio), not `days_worked / 26` (salary ratio). This matches Israeli labor law — accrual is based on duration of employment, not working days.

`PayslipInput.active_days: int = 0` — when 0, falls back to salary ratio (backward compat for tests that don't pass it).

### FSM architecture fixes

- `_run_start_flow(message, state, user_id)` extracted from `cmd_start` — user_id is passed explicitly so callback handlers (where `callback.message.from_user` is the bot) use `callback.from_user.id` instead.
- `global_error_handler` fixed to inject `Bot` as DI parameter (not `event.update.bot` which doesn't exist in aiogram v3).
- `handle_confirm` wraps `edit_reply_markup` in `try/except TelegramBadRequest`.

---

## File-by-file summary

### `calculator.py`
- `calculate_partial_days`: added `rest_day_weekday` parameter; counts actual calendar days
- `PayslipInput`: added `net_salary_override: Decimal = Decimal("0")` and `active_days: int = 0`
- `calculate()`: Simple Mode bypass for gross_base; calendar-ratio accrual when `active_days > 0`

### `config.py`
- `REST_DAY_OPTIONS: dict[str, tuple[str, int]]` — Friday/Saturday/Sunday with Hebrew labels and weekday ints
- `DEFAULT_REST_DAY = "saturday"`
- `rest_day_weekday(key)` and `rest_day_hebrew(key)` helpers
- All deduction cap constants preserved with `# ARCHITECTURE NOTE` comments

### `database.py`
- `upsert_contract` now writes `rest_day` alongside `agreed_net_salary` and `employment_start_date`
- `upsert_month_accrual` / `get_balances` for idempotent per-month accrual tracking (sum at read time, never double-count)

### `bot.py`
- `_run_start_flow(message, state, user_id)` — core start logic with explicit user_id
- `cmd_start` → calls `_run_start_flow(..., user_id=message.from_user.id)`
- `handle_setup_done_start` → calls `_run_start_flow(..., user_id=callback.from_user.id)`
- Fast-track: `_ask_details()`, `handle_details_use_saved`, `handle_details_edit`
- Rest day setup: `_ask_setup_rest_day()`, `handle_setup_rest_day()`
- Shabbat/holiday: `_ask_shabbat_days()`, `handle_shabbat_days/zero`, `_ask_holiday_days()`, `handle_holiday_days/zero`
- Both partial-month paths store `active_days` in FSM state
- `_build_payslip_input` passes `active_days` to `PayslipInput`
- `_complete_setup` shows "📄 הפק תלוש עכשיו" button after `/setup` wizard

### `tests/test_calculator.py`
- Updated day-count expectations for actual-calendar-day algorithm
- `TestRestDayVariants` (7 tests): Saturday/Friday/Sunday variants
- `TestRestDayConfig` (8 tests): config helpers and defaults
- `TestCalendarRatioAccrual` (6 tests): calendar vs salary ratio for accruals

### `tests/test_bot_helpers.py`
- Fixture includes `active_days`, `shabbat_days`, `holiday_days`, `rest_day`
- `ACCRUAL_RATIO = 19/30` (calendar) separate from `RATIO = 17/26` (salary)
- Tests for shabbat/holiday additions

### `tests/test_setup_done_flow.py` (new)
- 3 tests verifying `handle_setup_done_start` uses `callback.from_user.id`

---

## Re-enabling Detailed Mode

Search for `# ARCHITECTURE NOTE` in `bot.py` and `calculator.py`. Key stubs to restore:
- `PayslipForm.deductions`, `PayslipForm.deduction_edit` states
- `_deductions_keyboard()`, `handle_deductions_toggle()`, `handle_deduction_edit_input()`
- `_max_for_key()`, pro-rata deduction caps
- `ContractSetupForm` housing/health/extras questions
- `_build_payslip_input` deduction fields
- Remove `net_salary_override` Simple Mode branch in `calculate()`
