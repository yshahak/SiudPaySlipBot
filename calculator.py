"""
Salary calculation engine for foreign caregivers in Israel.

This module is pure math — no I/O, no Telegram, no PDF.
All monetary values use Decimal to avoid floating-point errors.
"""

import calendar as _calendar
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

import config


def _round2(value: Decimal) -> Decimal:
    """Round to 2 decimal places using standard rounding."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calculate_partial_days(
    partial_type: str, day: int, month: int, year: int
) -> tuple[int, int]:
    """
    Convert a partial-month start/end date into (active_calendar_days, days_worked_out_of_26).

    partial_type:
        "started" — `day` is the first calendar day the worker was present.
        "ended"   — `day` is the last calendar day the worker was present.

    Returns a tuple so callers can show the user the active-day count alongside
    the computed days_worked.  days_worked is clamped to a minimum of 1.
    """
    days_in_month = _calendar.monthrange(year, month)[1]
    if partial_type == "started":
        active_days = days_in_month - day + 1
    else:
        active_days = day
    days_worked = max(1, round(active_days / days_in_month * 26))
    return active_days, days_worked


@dataclass
class PayslipInput:
    """All data collected from the user via the Telegram FSM."""
    month: int
    year: int
    is_full_month: bool
    days_worked: int             # 1–26; always 26 for a full month
    employer_name: str
    caregiver_name: str
    passport_number: str
    shabbat_days: int            # days worked on Shabbat (יום מנוחה שבועי)
    holiday_days: int            # days worked on national holidays (חגים)
    deduction_housing: Decimal   # 0 if not applicable this month
    deduction_health: Decimal    # 0 if not applicable
    deduction_extras: Decimal    # 0 if not applicable
    deduction_food: Decimal      # 0 if not applicable (כלכלה)
    pocket_money_weeks: int      # number of weeks דמי כיס (100 ₪/week) were paid
    advances: Decimal            # additional advances beyond pocket money


@dataclass
class PayslipResult:
    """Fully calculated payslip — ready to be passed to pdf_generator."""

    # ── Period & identifiers ──────────────────────────────────────────────────
    month: int
    year: int
    employer_name: str
    caregiver_name: str
    passport_number: str
    days_worked: int
    working_days_in_month: int  # always 26
    shabbat_days: int
    holiday_days: int

    # ── Earnings ──────────────────────────────────────────────────────────────
    min_wage: Decimal            # applicable minimum wage for this period
    gross_base: Decimal          # min_wage × (days_worked / 26)
    pocket_money_total: Decimal  # pocket_money_weeks × 100
    shabbat_addition: Decimal    # shabbat_days × shabbat_rate
    holiday_addition: Decimal    # holiday_days × shabbat_rate
    shabbat_rate: Decimal        # rate used this period
    total_gross: Decimal         # gross_base + pocket_money + shabbat + holiday

    # ── Deductions ────────────────────────────────────────────────────────────
    deduction_housing: Decimal
    deduction_health: Decimal
    deduction_extras: Decimal
    deduction_food: Decimal
    advances: Decimal
    total_deductions: Decimal

    # ── Net pay ───────────────────────────────────────────────────────────────
    total_net_pay: Decimal       # total_gross − total_deductions

    # ── Employer contributions (informational — not subtracted from net) ───────
    employer_pension: Decimal    # 6.5% of gross_base
    employer_severance: Decimal  # 6.0% of gross_base

    # ── Social rights accrual (proportional to days worked) ───────────────────
    vacation_accrued: Decimal    # 1.16 × ratio
    sick_accrued: Decimal        # 1.50 × ratio


def calculate(data: PayslipInput) -> PayslipResult:
    """
    Apply Israeli labor law rules to compute a complete payslip.

    Raises ValueError if any input violates legal constraints.
    """
    # ── Validate inputs ───────────────────────────────────────────────────────
    if not (1 <= data.days_worked <= 26):
        raise ValueError(f"ימי עבודה חייבים להיות בין 1 ל-26, קיבלנו: {data.days_worked}")
    if data.shabbat_days < 0 or data.holiday_days < 0:
        raise ValueError("מספר שבתות/חגים לא יכול להיות שלילי")
    if data.pocket_money_weeks < 0 or data.pocket_money_weeks > 6:
        raise ValueError("מספר שבועות דמי כיס חייב להיות בין 0 ל-6")
    if data.advances < 0:
        raise ValueError("מקדמות לא יכולות להיות שליליות")

    # ── Look up the correct wage for this period ──────────────────────────────
    min_wage, shabbat_rate = config.get_wage_params(data.month, data.year)

    # ── Base salary (pro-rata for partial months) ─────────────────────────────
    ratio = Decimal(data.days_worked) / config.WORKING_DAYS_MONTH
    gross_base = _round2(min_wage * ratio)

    # ── Additions ─────────────────────────────────────────────────────────────
    pocket_money_total = Decimal(data.pocket_money_weeks) * config.POCKET_MONEY_WEEKLY
    shabbat_addition = _round2(Decimal(data.shabbat_days) * shabbat_rate)
    holiday_addition = _round2(Decimal(data.holiday_days) * shabbat_rate)

    total_gross = gross_base + pocket_money_total + shabbat_addition + holiday_addition

    # ── Deductions ────────────────────────────────────────────────────────────
    # Validate each deduction does not exceed the legal maximum
    if data.deduction_housing > config.DEDUCTION_HOUSING_MAX:
        raise ValueError(f"ניכוי מגורים ({data.deduction_housing} ₪) עולה על המקסימום ({config.DEDUCTION_HOUSING_MAX} ₪)")
    if data.deduction_health > config.DEDUCTION_HEALTH_MAX:
        raise ValueError(f"ניכוי ביטוח רפואי ({data.deduction_health} ₪) עולה על המקסימום ({config.DEDUCTION_HEALTH_MAX} ₪)")
    if data.deduction_extras > config.DEDUCTION_EXTRAS_MAX:
        raise ValueError(f"ניכוי הוצאות נלוות ({data.deduction_extras} ₪) עולה על המקסימום ({config.DEDUCTION_EXTRAS_MAX} ₪)")
    if data.deduction_food > config.DEDUCTION_FOOD_MAX:
        raise ValueError(f"ניכוי כלכלה ({data.deduction_food} ₪) עולה על המקסימום ({config.DEDUCTION_FOOD_MAX} ₪)")

    total_deductions = (
        data.deduction_housing
        + data.deduction_health
        + data.deduction_extras
        + data.deduction_food
        + data.advances
    )

    # Warn via exception if total deductions exceed legal 25% cap
    if total_gross > 0 and (total_deductions / total_gross) > config.DEDUCTION_TOTAL_MAX_PCT:
        raise ValueError(
            f"סך הניכויים ({total_deductions} ₪) עולה על 25% מהשכר הגולמי ({total_gross} ₪). "
            f"המקסימום המותר: {_round2(total_gross * config.DEDUCTION_TOTAL_MAX_PCT)} ₪"
        )

    total_net_pay = total_gross - total_deductions

    # ── Employer contributions (informational) ─────────────────────────────────
    employer_pension = _round2(gross_base * config.EMPLOYER_PENSION_PCT)
    employer_severance = _round2(gross_base * config.EMPLOYER_SEVERANCE_PCT)

    # ── Social rights (proportional to days worked) ───────────────────────────
    vacation_accrued = _round2(config.VACATION_DAYS_PER_MONTH * ratio)
    sick_accrued = _round2(config.SICK_DAYS_PER_MONTH * ratio)

    return PayslipResult(
        month=data.month,
        year=data.year,
        employer_name=data.employer_name,
        caregiver_name=data.caregiver_name,
        passport_number=data.passport_number,
        days_worked=data.days_worked,
        working_days_in_month=int(config.WORKING_DAYS_MONTH),
        shabbat_days=data.shabbat_days,
        holiday_days=data.holiday_days,
        min_wage=min_wage,
        gross_base=gross_base,
        pocket_money_total=pocket_money_total,
        shabbat_addition=shabbat_addition,
        holiday_addition=holiday_addition,
        shabbat_rate=shabbat_rate,
        total_gross=total_gross,
        deduction_housing=data.deduction_housing,
        deduction_health=data.deduction_health,
        deduction_extras=data.deduction_extras,
        deduction_food=data.deduction_food,
        advances=data.advances,
        total_deductions=total_deductions,
        total_net_pay=total_net_pay,
        employer_pension=employer_pension,
        employer_severance=employer_severance,
        vacation_accrued=vacation_accrued,
        sick_accrued=sick_accrued,
    )
