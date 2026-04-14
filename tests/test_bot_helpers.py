"""
Integration tests for bot.py helper functions.

Recreates the exact conversation from 13/04/2026 23:52 that caused the bot to get
stuck after asking for the housing deduction amount.

Scenario:
  - April 2026, partial month — started day 12
  - active days = 30 - 12 + 1 = 19  →  days_worked = round(19/30 × 26) = 16
  - employer: "מלכה", caregiver: skipped, passport: skipped
  - shabbat=0, holiday=0, pocket_money=0, advances=0
  - housing deduction selected; amount entered = 118.15 (pro-rata max)
  - no food deduction

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
from bot import _DEDUCTION_META, _SKIPPED, _build_payslip_input
from calculator import PayslipInput, calculate, calculate_partial_days


# ── Shared fixture ─────────────────────────────────────────────────────────────

def _make_fsm_data(**overrides) -> dict:
    """
    Build a dict that mirrors the aiogram FSM state at the moment the user
    has just entered the housing deduction amount and the bot is about to
    call _show_confirm().
    """
    _, days_worked = calculate_partial_days("started", 12, 4, 2026)  # → 16
    ratio = Decimal(days_worked) / Decimal("26")

    pro_rata_max = {
        k: str((cfg_max * ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        for k, (_, cfg_max) in _DEDUCTION_META.items()
    }
    housing_max = Decimal(pro_rata_max["housing"])

    data = {
        "month": 4,
        "year": 2026,
        "partial_type": "started",
        "days_worked": days_worked,
        "employer_name": "מלכה",
        "caregiver_name": _SKIPPED,
        "passport": _SKIPPED,
        "shabbat_days": 0,
        "holiday_days": 0,
        "pocket_money_weeks": 0,
        "advances": "0",
        "deductions_selected": {
            "housing": True,
            "health": False,
            "extras": False,
            "food": False,
        },
        "deductions_pro_rata_max": pro_rata_max,
        "deductions_amounts": {
            **pro_rata_max,          # housing=118.15, health=104.00, extras=57.85
            "food": "0",
        },
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
        assert dw == 16        # round(19/30 × 26) = round(16.47) = 16


# ── B: Verify pro-rata deduction maxima ───────────────────────────────────────

class TestProRataMaxima:
    RATIO = Decimal("16") / Decimal("26")

    def test_housing_pro_rata_max(self):
        expected = (config.DEDUCTION_HOUSING_MAX * self.RATIO).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        assert expected == Decimal("118.15")

    def test_health_pro_rata_max(self):
        expected = (config.DEDUCTION_HEALTH_MAX * self.RATIO).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        # 169 × 16/26 = 104.00 (exact)
        assert expected == Decimal("104.00")

    def test_extras_pro_rata_max(self):
        expected = (config.DEDUCTION_EXTRAS_MAX * self.RATIO).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        # 94 × 16/26 = 57.846… → 57.85
        assert expected == Decimal("57.85")

    def test_pro_rata_max_does_not_exceed_legal_max(self):
        ratio = Decimal("16") / Decimal("26")
        for key, (_, cfg_max) in _DEDUCTION_META.items():
            pro_rata = (cfg_max * ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            assert pro_rata <= cfg_max, f"{key}: {pro_rata} > {cfg_max}"


# ── C: _build_payslip_input with the stuck-scenario FSM data ──────────────────

class TestBuildPayslipInput:
    def test_days_worked(self):
        inp = _build_payslip_input(_make_fsm_data())
        assert inp.days_worked == 16

    def test_employer_name_preserved(self):
        inp = _build_payslip_input(_make_fsm_data())
        assert inp.employer_name == "מלכה"

    def test_skipped_caregiver_stored_as_placeholder(self):
        inp = _build_payslip_input(_make_fsm_data())
        assert inp.caregiver_name == _SKIPPED

    def test_skipped_passport_stored_as_placeholder(self):
        inp = _build_payslip_input(_make_fsm_data())
        assert inp.passport_number == _SKIPPED

    def test_housing_deduction_amount(self):
        inp = _build_payslip_input(_make_fsm_data())
        assert inp.deduction_housing == Decimal("118.15")

    def test_unselected_deductions_are_zero(self):
        inp = _build_payslip_input(_make_fsm_data())
        assert inp.deduction_health == Decimal("0")
        assert inp.deduction_extras == Decimal("0")
        assert inp.deduction_food == Decimal("0")

    def test_advances_zero(self):
        inp = _build_payslip_input(_make_fsm_data())
        assert inp.advances == Decimal("0")

    def test_pocket_money_zero(self):
        inp = _build_payslip_input(_make_fsm_data())
        assert inp.pocket_money_weeks == 0

    def test_is_partial_month(self):
        inp = _build_payslip_input(_make_fsm_data())
        assert inp.is_full_month is False


# ── D: calculate() with the stuck-scenario input ──────────────────────────────

class TestCalculateForScenario:
    def setup_method(self):
        self.inp = _build_payslip_input(_make_fsm_data())
        self.result = calculate(self.inp)

    def test_gross_base(self):
        expected = (Decimal("6443.85") * Decimal("16") / Decimal("26")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        assert self.result.gross_base == expected

    def test_total_gross_equals_gross_base_no_additions(self):
        # No pocket money, no shabbat, no holidays
        assert self.result.total_gross == self.result.gross_base

    def test_total_deductions(self):
        assert self.result.total_deductions == Decimal("118.15")

    def test_net_pay(self):
        assert self.result.total_net_pay == self.result.total_gross - Decimal("118.15")

    def test_deductions_under_25pct_cap(self):
        ratio = self.result.total_deductions / self.result.total_gross
        assert ratio <= Decimal("0.25")

    def test_employer_pension(self):
        expected = (self.result.gross_base * Decimal("0.065")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        assert self.result.employer_pension == expected

    def test_vacation_accrued(self):
        expected = (Decimal("1.16") * Decimal("16") / Decimal("26")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        assert self.result.vacation_accrued == expected


# ── E: Confirm-summary text builds without exceptions ─────────────────────────

class TestConfirmSummaryText:
    """
    Replicate the exact string-building logic from _show_confirm() and assert
    it produces a valid, non-empty summary string without raising any exception.
    This isolates formatting bugs from Telegram API issues.
    """

    def _build_summary(self) -> str:
        data = _make_fsm_data()
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

        lines.append("💰 *הכנסות:*")
        lines.append(f"  שכר בסיס: ₪{result.gross_base:,.2f}")
        if result.pocket_money_total:
            lines.append(f"  דמי כיס: ₪{result.pocket_money_total:,.2f}")
        if result.shabbat_addition:
            lines.append(f"  שבתות ({result.shabbat_days}): ₪{result.shabbat_addition:,.2f}")
        if result.holiday_addition:
            lines.append(f"  חגים ({result.holiday_days}): ₪{result.holiday_addition:,.2f}")
        lines.append(f"  *סה״כ ברוטו: ₪{result.total_gross:,.2f}*\n")

        if result.total_deductions > 0:
            lines.append("➖ *ניכויים:*")
            if result.deduction_housing:
                lines.append(f"  מגורים: ₪{result.deduction_housing:,.2f}")
            if result.deduction_health:
                lines.append(f"  ביטוח רפואי: ₪{result.deduction_health:,.2f}")
            if result.deduction_extras:
                lines.append(f"  הוצאות נלוות: ₪{result.deduction_extras:,.2f}")
            if result.deduction_food:
                lines.append(f"  כלכלה: ₪{result.deduction_food:,.2f}")
            if result.advances:
                lines.append(f"  מקדמות: ₪{result.advances:,.2f}")
            lines.append(f"  *סה״כ ניכויים: ₪{result.total_deductions:,.2f}*\n")

        lines.append(f"💳 *סה״כ לתשלום: ₪{result.total_net_pay:,.2f}*")

        return "\n".join(lines)

    def test_summary_builds_without_exception(self):
        summary = self._build_summary()
        assert len(summary) > 0

    def test_summary_contains_employer(self):
        assert "מלכה" in self._build_summary()

    def test_summary_contains_gross(self):
        # gross_base = 3965.45
        assert "3,965.45" in self._build_summary()

    def test_summary_contains_housing_deduction(self):
        assert "118.15" in self._build_summary()

    def test_summary_contains_net_pay(self):
        result = calculate(_build_payslip_input(_make_fsm_data()))
        expected = f"{result.total_net_pay:,.2f}"
        assert expected in self._build_summary()

    def test_no_unmatched_asterisks(self):
        """Unmatched * would cause Telegram Markdown parse errors."""
        summary = self._build_summary()
        assert summary.count("*") % 2 == 0, (
            f"Odd number of asterisks in summary — Markdown will fail:\n{summary}"
        )
