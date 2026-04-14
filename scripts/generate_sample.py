"""
Standalone script to generate a sample payslip PDF for visual inspection.

Usage:
    python scripts/generate_sample.py [output_path]

The generated PDF is saved to `sample_payslip.pdf` (or the path you specify)
and is NOT deleted automatically — use it to verify Hebrew RTL rendering and
the Employer Contributions table layout before deploying.
"""

import os
import sys
import shutil

# Allow running from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "sample_script")

from decimal import Decimal
from calculator import PayslipInput, calculate
from pdf_generator import generate_payslip_pdf
import config


def main() -> None:
    output_path = sys.argv[1] if len(sys.argv) > 1 else "sample_payslip.pdf"

    # ── Realistic sample — April 2026 full month ───────────────────────────────
    # Full month, 4 Shabbats worked, 1 holiday, 4 weeks pocket money,
    # all standard deductions, plus food deduction.
    sample_input = PayslipInput(
        month=4,
        year=2026,
        is_full_month=True,
        days_worked=26,
        employer_name="ישראל ואסתר כהן",
        caregiver_name="Maria Santos",
        passport_number="PP-987654321",
        shabbat_days=4,
        holiday_days=1,
        deduction_housing=Decimal("192"),
        deduction_health=Decimal("169"),
        deduction_extras=Decimal("94"),
        deduction_food=Decimal("500"),    # כלכלה — below the 644 maximum
        pocket_money_weeks=4,
        advances=Decimal("0"),
    )

    result = calculate(sample_input)

    # Print a summary to stdout
    print("=" * 50)
    print(f"  תלוש שכר — {config.HEBREW_MONTHS[result.month]} {result.year}")
    print("=" * 50)
    print(f"  מעסיק:         {result.employer_name}")
    print(f"  מטפל/ת:        {result.caregiver_name}")
    print(f"  שכר מינימום:   {result.min_wage} ₪")
    print(f"  שכר בסיס:      {result.gross_base} ₪")
    print(f"  דמי כיס (4×100): {result.pocket_money_total} ₪")
    print(f"  שבתות (4×{result.shabbat_rate}): {result.shabbat_addition} ₪")
    print(f"  חגים  (1×{result.shabbat_rate}):  {result.holiday_addition} ₪")
    print(f"  סה\"כ ברוטו:    {result.total_gross} ₪")
    print(f"  סה\"כ ניכויים:  {result.total_deductions} ₪")
    print(f"  סה\"כ נטו:      {result.total_net_pay} ₪")
    print("-" * 50)
    print(f"  הפרשות מעסיק:")
    print(f"    פנסיה  (6.5%): {result.employer_pension} ₪")
    print(f"    פיצויים (6%): {result.employer_severance} ₪")
    print(f"    סה\"כ:         {result.employer_pension + result.employer_severance} ₪")
    print("-" * 50)
    print(f"  צבירת חופשה:   {result.vacation_accrued} ימים")
    print(f"  צבירת מחלה:    {result.sick_accrued} ימים")
    print("=" * 50)

    # Generate PDF
    tmp_path = generate_payslip_pdf(result)
    shutil.move(tmp_path, output_path)
    print(f"\n✅ PDF saved to: {os.path.abspath(output_path)}")
    print("   Open it to verify Hebrew RTL rendering and table layout.\n")


if __name__ == "__main__":
    main()
