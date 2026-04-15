"""
Configuration and Israel labor law constants for the Foreign Caregiver Payslip Bot.

All monetary values use Decimal for precision.
To add a new minimum wage period, append one entry to WAGE_HISTORY.
"""

import os
from datetime import date
from decimal import Decimal

from dotenv import load_dotenv

load_dotenv()

# ── Telegram ───────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]

# ── Cloud Run / Webhook ────────────────────────────────────────────────────────
# Set WEBHOOK_URL to enable webhook mode (required for Cloud Run).
# Leave unset for local polling mode.
WEBHOOK_URL: str | None = os.environ.get("WEBHOOK_URL")  # e.g. "https://my-service.run.app"
PORT: int = int(os.environ.get("PORT", "8080"))

# ── Labor Law: Minimum Wage History ───────────────────────────────────────────
# Each entry: (effective_from, min_monthly_gross, shabbat_holiday_daily_rate)
# Source: siud.pirsuma.com/min_2026/, Israeli Ministry of Labor official data.
# NOTE: Pre-2025 values are approximate. Verify before generating historical payslips.
WAGE_HISTORY: list[tuple[date, Decimal, Decimal]] = [
    (date(2023, 4, 1), Decimal("5571.75"), Decimal("391.89")),  # Approx — verify if needed
    (date(2025, 4, 1), Decimal("6247.67"), Decimal("426.35")),  # Confirmed
    (date(2026, 4, 1), Decimal("6443.85"), Decimal("439.73")),  # Confirmed (current)
]


def get_wage_params(month: int, year: int) -> tuple[Decimal, Decimal]:
    """
    Return (min_monthly_gross, shabbat_holiday_daily_rate) for the given period.

    Finds the most recent WAGE_HISTORY entry whose effective date is on or before
    the first day of the requested month. Adding future wage changes to WAGE_HISTORY
    is the only change required to keep this bot current.
    """
    target = date(year, month, 1)
    min_wage, shabbat_rate = WAGE_HISTORY[0][1], WAGE_HISTORY[0][2]
    for effective_date, mw, sr in WAGE_HISTORY:
        if target >= effective_date:
            min_wage, shabbat_rate = mw, sr
    return min_wage, shabbat_rate


# ── Housing Deduction Caps by Region — April 2026 (employer-owned property) ───
# ARCHITECTURE NOTE: These constants are part of the Detailed Mode infrastructure.
# Simple Mode bypasses individual deductions by asking for the agreed monthly net
# salary instead, and passes 0 for all deduction fields.
# Do NOT remove — required for future Detailed Mode re-enablement.
# Source: siud.pirsuma.com/calc_nikuyim/
# These are the "owned by employer" caps. Rented property caps are exactly 2×.
HOUSING_CAPS_OWNED: dict[str, Decimal] = {
    "tel_aviv":   Decimal("289.18"),
    "jerusalem":  Decimal("254.31"),
    "center":     Decimal("192.81"),
    "south":      Decimal("171.40"),
    "north":      Decimal("157.71"),
}
HOUSING_CAPS_RENTED: dict[str, Decimal] = {
    k: v * 2 for k, v in HOUSING_CAPS_OWNED.items()
}

# ── Permitted Deduction Maximums (reflect April 2026 values) ──────────────────
# Source: siud.pirsuma.com/calc_nikuyim/
# All deductions require written worker consent (Appendix C of standard contract).
# DEDUCTION_HOUSING_MAX is the fallback/default (Center region, owned) used when
# no region has been configured yet.
DEDUCTION_HOUSING_MAX = HOUSING_CAPS_OWNED["center"]  # ₪192.81 — Center / Haifa
DEDUCTION_HEALTH_MAX = Decimal("169")      # ביטוח רפואי
DEDUCTION_EXTRAS_MAX = Decimal("94")       # הוצאות נלוות
DEDUCTION_FOOD_MAX = Decimal("644")        # כלכלה — 10% of April 2026 min wage
DEDUCTION_TOTAL_MAX_PCT = Decimal("0.25")  # Total deductions ≤ 25% of total gross

# ── Employer Contribution Rates (הפרשות מעסיק) ────────────────────────────────
EMPLOYER_PENSION_PCT = Decimal("0.065")    # 6.5% פנסיה מעסיק
EMPLOYER_SEVERANCE_PCT = Decimal("0.06")   # 6.0% פיצויים מעסיק

# ── Social Rights Accrual (per month, full month basis) ───────────────────────
VACATION_DAYS_PER_MONTH = Decimal("1.16")  # 14 net vacation days ÷ 12
SICK_DAYS_PER_MONTH = Decimal("1.50")      # 18 sick days ÷ 12

# ── Calculation Basis ──────────────────────────────────────────────────────────
WORKING_DAYS_MONTH = Decimal("26")         # 6 days/week × 4.33 weeks
POCKET_MONEY_WEEKLY = Decimal("100")       # Standard דמי כיס per week (counts as salary)

# ── Weekly Rest Day ───────────────────────────────────────────────────────────
# Maps rest_day key (stored in Firestore) → (Hebrew label, Python weekday number).
# Python weekday(): 0=Monday … 4=Friday, 5=Saturday, 6=Sunday.
# Default is Saturday, matching the Israeli legal default for foreign caregivers.
REST_DAY_OPTIONS: dict[str, tuple[str, int]] = {
    "friday":   ("שישי",   4),
    "saturday": ("שבת",    5),
    "sunday":   ("ראשון",  6),
}
DEFAULT_REST_DAY = "saturday"


def rest_day_weekday(rest_day: str) -> int:
    """Return the Python weekday number for the given rest_day key."""
    return REST_DAY_OPTIONS.get(rest_day, REST_DAY_OPTIONS[DEFAULT_REST_DAY])[1]


def rest_day_hebrew(rest_day: str) -> str:
    """Return the Hebrew label for the given rest_day key."""
    return REST_DAY_OPTIONS.get(rest_day, REST_DAY_OPTIONS[DEFAULT_REST_DAY])[0]


# ── Hebrew Month Names ─────────────────────────────────────────────────────────
HEBREW_MONTHS: dict[int, str] = {
    1: "ינואר", 2: "פברואר", 3: "מרץ", 4: "אפריל",
    5: "מאי", 6: "יוני", 7: "יולי", 8: "אוגוסט",
    9: "ספטמבר", 10: "אוקטובר", 11: "נובמבר", 12: "דצמבר",
}

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONTS_DIR = os.path.join(BASE_DIR, "fonts")
HEBREW_FONT_PATH = os.path.join(FONTS_DIR, "NotoSansHebrew-Regular.ttf")
