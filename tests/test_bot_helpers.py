"""
Integration tests for bot.py helper functions — Simple Mode.

Recreates the exact conversation scenario from 13/04/2026 that first exposed
an FSM issue (partial month, started day 12, employer "מלכה").

ARCHITECTURE NOTE: This test file was updated for Simple Mode. The original
version tested the deduction multi-select flow (housing/health/extras/food).
That flow is currently bypassed — the bot now asks only for the agreed monthly
net salary (in /setup) and cash advances (per payslip). The Detailed Mode
infrastructure is preserved in bot.py / calculator.py for future re-enablement.

Run with:
    pytest tests/test_bot_helpers.py -v
"""

import os
import sys
from decimal import ROUND_HALF_UP, Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test_token_for_tests")

import config
from bot import _SKIPPED, _build_payslip_input
from calculator import PayslipInput, calculate, calculate_partial_days

# Agreed net salary used throughout this scenario (₪/month)
_AGREED_NET = Decimal("5989")


# ── Shared fixture ─────────────────────────────────────────────────────────────

def _make_fsm_data(**overrides) -> dict:
    """
    Build a dict mirroring the aiogram FSM state just before _show_confirm().

    Scenario: April 2026, partial month — started day 12.
      active days = 30 - 12 + 1 = 19
      Saturdays in Apr 12-30: 18th, 25th = 2 → days_worked = 19 - 2 = 17
    """
    active_days, days_worked = calculate_partial_days("started", 12, 4, 2026)  # → 19, 17

    data = {
        "month": 4,
        "year": 2026,
        "partial_type": "started",
        "active_days": active_days,
        "days_worked": days_worked,
        "employer_name": "מלכה",
        "caregiver_name": _SKIPPED,
        "passport": _SKIPPED,
        "shabbat_days": "0",
        "holiday_days": "0",
        "rest_day": "saturday",
        "agreed_net_salary": str(_AGREED_NET),
        "advances": "0",
    }
    data.update(overrides)
    return data


# ── A: Verify the partial-month day calculation ────────────────────────────────

class TestPartialDayForScenario:
    def test_started_day12_april_active_days(self):
        active, _ = calculate_partial_days("started", 12, 4, 2026)
        assert active == 19   # 30 - 12 + 1

    def test_started_day12_april_days_worked(self):
        _, dw = calculate_partial_days("started", 12, 4, 2026)
        assert dw == 17        # Apr 12-30: 19 calendar days - 2 Saturdays (18th, 25th) = 17


# ── B: Verify deduction cap constants still exist (Detailed Mode infrastructure) ──
# ARCHITECTURE NOTE: These tests validate that the legal cap constants are still
# present in config.py and hold the correct April 2026 values. They are used by
# Detailed Mode — do not remove them even though Simple Mode bypasses them.

class TestDeductionCapConstants:
    RATIO = Decimal("17") / Decimal("26")

    def test_housing_cap_exists_and_correct(self):
        assert config.DEDUCTION_HOUSING_MAX == Decimal("192.81")

    def test_health_cap_exists_and_correct(self):
        assert config.DEDUCTION_HEALTH_MAX == Decimal("169")

    def test_extras_cap_exists_and_correct(self):
        assert config.DEDUCTION_EXTRAS_MAX == Decimal("94")

    def test_housing_pro_rata_max(self):
        expected = (config.DEDUCTION_HOUSING_MAX * self.RATIO).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        assert expected == Decimal("126.07")

    def test_health_pro_rata_max(self):
        expected = (config.DEDUCTION_HEALTH_MAX * self.RATIO).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        assert expected == Decimal("110.50")

    def test_extras_pro_rata_max(self):
        expected = (config.DEDUCTION_EXTRAS_MAX * self.RATIO).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        assert expected == Decimal("61.46")


# ── C: _build_payslip_input — Simple Mode FSM data ────────────────────────────

class TestBuildPayslipInput:
    def test_days_worked(self):
        inp = _build_payslip_input(_make_fsm_data())
        assert inp.days_worked == 17

    def test_employer_name_preserved(self):
        inp = _build_payslip_input(_make_fsm_data())
        assert inp.employer_name == "מלכה"

    def test_skipped_caregiver_stored_as_placeholder(self):
        inp = _build_payslip_input(_make_fsm_data())
        assert inp.caregiver_name == _SKIPPED

    def test_skipped_passport_stored_as_placeholder(self):
        inp = _build_payslip_input(_make_fsm_data())
        assert inp.passport_number == _SKIPPED

    def test_net_salary_override_set(self):
        """Simple Mode: net_salary_override carries the agreed net salary."""
        inp = _build_payslip_input(_make_fsm_data())
        assert inp.net_salary_override == _AGREED_NET

    def test_all_individual_deductions_are_zero(self):
        """Simple Mode: all itemized deduction fields are bypassed → 0."""
        inp = _build_payslip_input(_make_fsm_data())
        assert inp.deduction_housing == Decimal("0")
        assert inp.deduction_health == Decimal("0")
        assert inp.deduction_extras == Decimal("0")
        assert inp.deduction_food == Decimal("0")

    def test_advances_zero(self):
        inp = _build_payslip_input(_make_fsm_data())
        assert inp.advances == Decimal("0")

    def test_advances_non_zero(self):
        inp = _build_payslip_input(_make_fsm_data(advances="300"))
        assert inp.advances == Decimal("300")

    def test_shabbat_days_zero_by_default(self):
        inp = _build_payslip_input(_make_fsm_data())
        assert inp.shabbat_days == 0

    def test_shabbat_days_non_zero(self):
        inp = _build_payslip_input(_make_fsm_data(shabbat_days="2"))
        assert inp.shabbat_days == 2

    def test_holiday_days_non_zero(self):
        inp = _build_payslip_input(_make_fsm_data(holiday_days="1"))
        assert inp.holiday_days == 1

    def test_pocket_money_always_zero(self):
        """pocket_money_weeks is bypassed in Simple Mode — always 0."""
        inp = _build_payslip_input(_make_fsm_data())
        assert inp.pocket_money_weeks == 0

    def test_is_partial_month(self):
        inp = _build_payslip_input(_make_fsm_data())
        assert inp.is_full_month is False


# ── D: calculate() with Simple Mode input ─────────────────────────────────────

class TestCalculateForScenario:
    RATIO         = Decimal("17") / Decimal("26")   # salary ratio (working days)
    ACCRUAL_RATIO = Decimal("19") / Decimal("30")   # accrual ratio (calendar days, Apr 12-30)

    def setup_method(self):
        self.inp = _build_payslip_input(_make_fsm_data())
        self.result = calculate(self.inp)

    def test_gross_base_from_agreed_net(self):
        """gross_base = agreed_net_salary × (days_worked / 26)."""
        expected = (_AGREED_NET * self.RATIO).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        assert self.result.gross_base == expected

    def test_total_gross_equals_gross_base_no_additions(self):
        """No shabbat/pocket_money → total_gross == gross_base."""
        assert self.result.total_gross == self.result.gross_base

    def test_no_deductions(self):
        """No individual deductions selected, no advances → total_deductions = 0."""
        assert self.result.total_deductions == Decimal("0")

    def test_net_pay_equals_gross_when_no_advances(self):
        assert self.result.total_net_pay == self.result.total_gross

    def test_net_pay_with_advances(self):
        inp = _build_payslip_input(_make_fsm_data(advances="200"))
        result = calculate(inp)
        assert result.total_net_pay == result.total_gross - Decimal("200")

    def test_deductions_under_25pct_cap(self):
        if self.result.total_gross > 0:
            ratio = self.result.total_deductions / self.result.total_gross
            assert ratio <= Decimal("0.25")

    def test_employer_pension(self):
        expected = (self.result.gross_base * Decimal("0.065")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        assert self.result.employer_pension == expected

    def test_vacation_accrued(self):
        """vacation uses calendar ratio (active_days/days_in_month = 19/30), not salary ratio."""
        expected = (Decimal("1.16") * self.ACCRUAL_RATIO).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        assert self.result.vacation_accrued == expected

    def test_shabbat_addition_two_days(self):
        """2 rest days worked → shabbat_addition = 2 × shabbat_rate."""
        from calculator import calculate as _calc
        inp = _build_payslip_input(_make_fsm_data(shabbat_days="2"))
        result = _calc(inp)
        _, shabbat_rate = __import__("config").get_wage_params(4, 2026)
        expected = (Decimal("2") * shabbat_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        assert result.shabbat_addition == expected

    def test_total_gross_includes_shabbat_addition(self):
        """total_gross = gross_base + shabbat_addition when rest days > 0."""
        from calculator import calculate as _calc
        inp = _build_payslip_input(_make_fsm_data(shabbat_days="2"))
        result = _calc(inp)
        assert result.total_gross == result.gross_base + result.shabbat_addition

    def test_holiday_addition_one_day(self):
        """1 holiday worked → holiday_addition = 1 × shabbat_rate."""
        from calculator import calculate as _calc
        inp = _build_payslip_input(_make_fsm_data(holiday_days="1"))
        result = _calc(inp)
        _, shabbat_rate = __import__("config").get_wage_params(4, 2026)
        expected = shabbat_rate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        assert result.holiday_addition == expected


# ── E: Confirm-summary text builds without exceptions ─────────────────────────

class TestConfirmSummaryText:
    """
    Replicate the _show_confirm() string-building logic and assert it produces
    a valid, non-empty summary string without raising any exception.
    """

    def _build_summary(self, **fsm_overrides) -> str:
        data = _make_fsm_data(**fsm_overrides)
        result = calculate(_build_payslip_input(data))

        month_name = config.HEBREW_MONTHS[result.month]
        days_label = (
            f"{result.days_worked}/26 ימים"
            if result.days_worked < 26
            else "חודש מלא (26 ימים)"
        )

        lines: list[str] = [f"📋 *סיכום התלוש — {month_name} {result.year}*\n"]

        if data.get("employer_name", _SKIPPED) != _SKIPPED:
            lines.append(f"👤 מעסיק: {data['employer_name']}")
        if data.get("caregiver_name", _SKIPPED) != _SKIPPED:
            lines.append(f"👤 מטפל/ת: {data['caregiver_name']}")
        lines.append(f"📆 ימי עבודה: {days_label}\n")

        lines.append("💰 *שכר:*")
        lines.append(f"  שכר יסוד: ₪{result.gross_base:,.2f}")
        if result.shabbat_addition > 0:
            lines.append(f"  תוספת שבת ({result.shabbat_days} ימים): ₪{result.shabbat_addition:,.2f}")
        if result.holiday_addition > 0:
            lines.append(f"  תוספת חג ({result.holiday_days} ימים): ₪{result.holiday_addition:,.2f}")
        if result.shabbat_addition > 0 or result.holiday_addition > 0:
            lines.append(f"  *סה״כ שכר: ₪{result.total_gross:,.2f}*")

        if result.advances > 0:
            lines.append(f"\n➖ *ניכויים:*")
            lines.append(f"  מקדמות: ₪{result.advances:,.2f}")
            lines.append(f"  *סה״כ ניכויים: ₪{result.total_deductions:,.2f}*\n")

        lines.append(f"\n💳 *יתרה לתשלום בהעברה: ₪{result.total_net_pay:,.2f}*")

        return "\n".join(lines)

    def test_summary_builds_without_exception(self):
        assert len(self._build_summary()) > 0

    def test_summary_contains_employer(self):
        assert "מלכה" in self._build_summary()

    def test_summary_contains_gross_base(self):
        result = calculate(_build_payslip_input(_make_fsm_data()))
        assert f"{result.gross_base:,.2f}" in self._build_summary()

    def test_summary_shows_advances_when_nonzero(self):
        assert "300.00" in self._build_summary(advances="300")

    def test_summary_no_advances_section_when_zero(self):
        summary = self._build_summary(advances="0")
        assert "מקדמות" not in summary

    def test_net_pay_in_summary(self):
        result = calculate(_build_payslip_input(_make_fsm_data()))
        assert f"{result.total_net_pay:,.2f}" in self._build_summary()

    def test_no_unmatched_asterisks(self):
        """Unmatched * would cause Telegram Markdown parse errors."""
        summary = self._build_summary()
        assert summary.count("*") % 2 == 0, (
            f"Odd number of asterisks in summary — Markdown will fail:\n{summary}"
        )
