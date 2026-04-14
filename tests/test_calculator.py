"""
Unit tests for calculator.py — Israeli Foreign Caregiver Payslip Engine.

Run with:
    pytest tests/test_calculator.py -v
"""

import os
import sys
from decimal import Decimal

import pytest

# Allow importing from project root without installation
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test_token_for_tests")

from calculator import PayslipInput, calculate, calculate_partial_days


# ── Fixtures / helpers ─────────────────────────────────────────────────────────

def make_input(**overrides) -> PayslipInput:
    """Return a PayslipInput with sensible defaults, overridden by kwargs."""
    defaults = dict(
        month=4,
        year=2026,
        is_full_month=True,
        days_worked=26,
        employer_name="ישראל ישראלי",
        caregiver_name="Maria Santos",
        passport_number="AB123456",
        shabbat_days=0,
        holiday_days=0,
        deduction_housing=Decimal("0"),
        deduction_health=Decimal("0"),
        deduction_extras=Decimal("0"),
        deduction_food=Decimal("0"),
        pocket_money_weeks=0,
        advances=Decimal("0"),
    )
    defaults.update(overrides)
    return PayslipInput(**defaults)


# ── Case A: Full month, April 2026 ────────────────────────────────────────────

class TestFullMonthApril2026:
    def test_minimum_wage_used(self):
        result = calculate(make_input())
        assert result.min_wage == Decimal("6443.85")

    def test_gross_base_equals_min_wage_for_full_month(self):
        result = calculate(make_input())
        assert result.gross_base == Decimal("6443.85")

    def test_net_pay_no_deductions(self):
        """With no deductions and no additions, net = gross."""
        result = calculate(make_input())
        assert result.total_net_pay == Decimal("6443.85")

    def test_net_pay_with_standard_deductions(self):
        """Housing + Health + Extras = 455 ILS → net = 6443.85 - 455 = 5988.85."""
        result = calculate(make_input(
            deduction_housing=Decimal("192"),
            deduction_health=Decimal("169"),
            deduction_extras=Decimal("94"),
        ))
        assert result.total_deductions == Decimal("455")
        assert result.total_net_pay == Decimal("5988.85")

    def test_employer_pension(self):
        """Employer pension = 6443.85 × 6.5% = 418.85 (rounded)."""
        result = calculate(make_input())
        assert result.employer_pension == Decimal("418.85")

    def test_employer_severance(self):
        """Employer severance = 6443.85 × 6.0% = 386.63 (rounded)."""
        result = calculate(make_input())
        assert result.employer_severance == Decimal("386.63")

    def test_employer_contributions_not_subtracted_from_net(self):
        """Employer contributions are informational — they don't reduce net pay."""
        result = calculate(make_input())
        assert result.total_net_pay == result.total_gross  # no deductions in this test
        assert result.employer_pension > 0
        # Net is NOT reduced by employer contributions
        assert result.total_net_pay != result.total_gross - result.employer_pension

    def test_shabbat_addition(self):
        """4 Shabbats × 439.73 = 1758.92 ILS."""
        result = calculate(make_input(shabbat_days=4))
        assert result.shabbat_rate == Decimal("439.73")
        assert result.shabbat_addition == Decimal("1758.92")

    def test_holiday_addition(self):
        """2 holidays × 439.73 = 879.46 ILS."""
        result = calculate(make_input(holiday_days=2))
        assert result.holiday_addition == Decimal("879.46")

    def test_pocket_money_4_weeks(self):
        """4 weeks × 100 ILS = 400 ILS counted as salary."""
        result = calculate(make_input(pocket_money_weeks=4))
        assert result.pocket_money_total == Decimal("400")
        assert result.total_gross == Decimal("6843.85")  # 6443.85 + 400

    def test_vacation_accrual_full_month(self):
        """Full month → 1.16 vacation days."""
        result = calculate(make_input())
        assert result.vacation_accrued == Decimal("1.16")

    def test_sick_accrual_full_month(self):
        """Full month → 1.50 sick days."""
        result = calculate(make_input())
        assert result.sick_accrued == Decimal("1.50")


# ── Case B: Partial month — 17/26 days ────────────────────────────────────────

class TestPartialMonth17Days:
    RATIO = Decimal("17") / Decimal("26")  # ≈ 0.653846…

    def test_gross_base_pro_rata(self):
        """Gross = 6443.85 × (17/26), rounded to 2dp."""
        result = calculate(make_input(days_worked=17, is_full_month=False))
        expected = (Decimal("6443.85") * self.RATIO).quantize(Decimal("0.01"))
        assert result.gross_base == expected

    def test_vacation_pro_rata(self):
        """Vacation = 1.16 × (17/26)."""
        result = calculate(make_input(days_worked=17, is_full_month=False))
        expected = (Decimal("1.16") * self.RATIO).quantize(Decimal("0.01"))
        assert result.vacation_accrued == expected

    def test_sick_pro_rata(self):
        """Sick = 1.50 × (17/26)."""
        result = calculate(make_input(days_worked=17, is_full_month=False))
        expected = (Decimal("1.50") * self.RATIO).quantize(Decimal("0.01"))
        assert result.sick_accrued == expected

    def test_employer_pension_pro_rata(self):
        """Employer pension is applied to the pro-rated gross base."""
        result = calculate(make_input(days_worked=17, is_full_month=False))
        expected_gross = (Decimal("6443.85") * self.RATIO).quantize(Decimal("0.01"))
        expected_pension = (expected_gross * Decimal("0.065")).quantize(Decimal("0.01"))
        assert result.employer_pension == expected_pension

    def test_working_days_recorded(self):
        result = calculate(make_input(days_worked=17, is_full_month=False))
        assert result.days_worked == 17
        assert result.working_days_in_month == 26


# ── Case C: Historical — March 2026 (pre-April wage) ─────────────────────────

class TestMarch2026HistoricalWage:
    def test_old_minimum_wage_applied(self):
        """March 2026 must use 6,247.67 ILS, not the April 2026 rate."""
        result = calculate(make_input(month=3, year=2026))
        assert result.min_wage == Decimal("6247.67")

    def test_gross_base_old_wage(self):
        result = calculate(make_input(month=3, year=2026))
        assert result.gross_base == Decimal("6247.67")

    def test_shabbat_rate_old_wage(self):
        """Shabbat rate for March 2026 must be 426.35 (pre-April value)."""
        result = calculate(make_input(month=3, year=2026))
        assert result.shabbat_rate == Decimal("426.35")

    def test_employer_pension_old_wage(self):
        """6247.67 × 6.5% = 406.10 (rounded)."""
        result = calculate(make_input(month=3, year=2026))
        assert result.employer_pension == Decimal("406.10")

    def test_april_2025_wage(self):
        """April 2025 should also use 6,247.67 (same wage period)."""
        result = calculate(make_input(month=4, year=2025))
        assert result.min_wage == Decimal("6247.67")


# ── Case D: Validation — 25% deduction cap ────────────────────────────────────

class TestDeductionValidation:
    def test_advances_exceeding_25pct_raises(self):
        """Advances alone of 5000 ILS on a 6443.85 gross exceeds 25% cap."""
        with pytest.raises(ValueError, match="25%"):
            calculate(make_input(advances=Decimal("5000")))

    def test_advances_at_exactly_25pct_passes(self):
        """Advances at exactly 25% of gross should not raise."""
        gross = Decimal("6443.85")
        max_advances = (gross * Decimal("0.25")).quantize(Decimal("0.01"))
        # Should not raise
        result = calculate(make_input(advances=max_advances))
        assert result.total_deductions == max_advances

    def test_housing_deduction_exceeds_max_raises(self):
        with pytest.raises(ValueError, match="מגורים"):
            calculate(make_input(deduction_housing=Decimal("300")))

    def test_health_deduction_exceeds_max_raises(self):
        with pytest.raises(ValueError, match="ביטוח רפואי"):
            calculate(make_input(deduction_health=Decimal("300")))

    def test_food_deduction_exceeds_max_raises(self):
        with pytest.raises(ValueError, match="כלכלה"):
            calculate(make_input(deduction_food=Decimal("700")))

    def test_invalid_days_worked_raises(self):
        with pytest.raises(ValueError):
            calculate(make_input(days_worked=0))

    def test_days_worked_over_26_raises(self):
        with pytest.raises(ValueError):
            calculate(make_input(days_worked=27))

    def test_negative_shabbat_days_raises(self):
        with pytest.raises(ValueError):
            calculate(make_input(shabbat_days=-1))


# ── Case E: Partial month — date-based day calculation ────────────────────────
#
# April 2026 = 30 days | March 2026 = 31 days | February 2026 = 28 days
#
# Formula:
#   started → active = days_in_month − day + 1
#   ended   → active = day
#   days_worked = max(1, round(active / days_in_month × 26))

class TestPartialDayCalculation:

    # ── "started" cases (April 2026 — 30 days) ────────────────────────────────

    def test_started_day1_is_full_month(self):
        """Starting on day 1 = full month = 26 days_worked."""
        active, dw = calculate_partial_days("started", 1, 4, 2026)
        assert active == 30
        assert dw == 26

    def test_started_day15_april(self):
        """Start day 15: 16 active days → round(16/30 × 26) = 14."""
        active, dw = calculate_partial_days("started", 15, 4, 2026)
        assert active == 16
        assert dw == 14

    def test_started_day16_april(self):
        """Start day 16: 15 active days → round(15/30 × 26) = 13."""
        active, dw = calculate_partial_days("started", 16, 4, 2026)
        assert active == 15
        assert dw == 13

    def test_started_last_day_clamps_to_1(self):
        """Starting on the last day of the month: 1 active day → days_worked = 1."""
        active, dw = calculate_partial_days("started", 30, 4, 2026)
        assert active == 1
        assert dw == 1

    # ── "ended" cases (April 2026 — 30 days) ──────────────────────────────────

    def test_ended_last_day_is_full_month(self):
        """Ending on the last day = full month = 26 days_worked."""
        active, dw = calculate_partial_days("ended", 30, 4, 2026)
        assert active == 30
        assert dw == 26

    def test_ended_day15_april(self):
        """End day 15: 15 active days → round(15/30 × 26) = 13."""
        active, dw = calculate_partial_days("ended", 15, 4, 2026)
        assert active == 15
        assert dw == 13

    def test_ended_day1_clamps_to_1(self):
        """Ending on day 1: 1 active day → days_worked = 1 (max floor)."""
        active, dw = calculate_partial_days("ended", 1, 4, 2026)
        assert active == 1
        assert dw == 1

    # ── February 2026 — 28 days ───────────────────────────────────────────────

    def test_started_day1_february(self):
        active, dw = calculate_partial_days("started", 1, 2, 2026)
        assert active == 28
        assert dw == 26

    def test_started_day15_february(self):
        """Start day 15, Feb (28 days): 14 active → round(14/28 × 26) = 13."""
        active, dw = calculate_partial_days("started", 15, 2, 2026)
        assert active == 14
        assert dw == 13

    def test_ended_day14_february(self):
        """End day 14, Feb (28 days): 14 active → round(14/28 × 26) = 13."""
        active, dw = calculate_partial_days("ended", 14, 2, 2026)
        assert active == 14
        assert dw == 13

    # ── March 2026 — 31 days ──────────────────────────────────────────────────

    def test_started_day16_march(self):
        """Start day 16, March (31 days): 16 active → round(16/31 × 26) = 13."""
        active, dw = calculate_partial_days("started", 16, 3, 2026)
        assert active == 16
        assert dw == 13

    def test_started_last_day_march_clamps_to_1(self):
        """Starting on day 31 of March: 1 active day → days_worked = 1."""
        active, dw = calculate_partial_days("started", 31, 3, 2026)
        assert active == 1
        assert dw == 1

    def test_ended_day16_march(self):
        """End day 16, March (31 days): 16 active → round(16/31 × 26) = 13."""
        active, dw = calculate_partial_days("ended", 16, 3, 2026)
        assert active == 16
        assert dw == 13
