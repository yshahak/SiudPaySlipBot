"""
PDF payslip generator using ReportLab.

Root cause of original rendering failure:
  The OpenMapTiles NotoSansHebrew font is a map-tile SUBSET — it contains Hebrew
  glyphs only, with no Latin, digit, or ₪ glyphs. All numbers were invisible.

Fix strategy (two-font approach):
  - _HE_FONT  (NotoHebrew):   Hebrew characters + ₪ symbol
  - _ASCII_FONT (Helvetica):  Digits, Latin letters, punctuation

All Paragraphs use _mixed_markup() which splits any string into per-character
font runs, so mixed Hebrew/digit labels (e.g. "שבתות 4 ימים") render correctly.

The caller is responsible for deleting the returned temp file (zero-data-retention).
"""

import html
import os
import tempfile
from decimal import Decimal

from bidi.algorithm import get_display
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

import config
from calculator import PayslipResult

# ── Font registration ──────────────────────────────────────────────────────────

_HE_FONT = "NotoHebrew"      # for Hebrew glyphs + ₪
_ASCII_FONT = "Helvetica"    # built-in; covers digits, Latin, punctuation
_ASCII_BOLD = "Helvetica-Bold"

_FONT_REGISTERED = False


def _ensure_font() -> None:
    global _FONT_REGISTERED
    if _FONT_REGISTERED:
        return
    if not os.path.exists(config.HEBREW_FONT_PATH):
        raise FileNotFoundError(
            f"Hebrew font not found: {config.HEBREW_FONT_PATH}\n"
            "Run: python scripts/download_fonts.py"
        )
    pdfmetrics.registerFont(TTFont(_HE_FONT, config.HEBREW_FONT_PATH))
    _FONT_REGISTERED = True


# ── Text helpers ───────────────────────────────────────────────────────────────

def _h(text: str) -> str:
    """Apply the Unicode Bidirectional Algorithm. Use only on Hebrew strings."""
    return get_display(text)


def _is_he(char: str) -> bool:
    # Hebrew block U+0590–U+05FF, plus ₪ (U+20AA) which lives in NotoHebrew
    return "\u0590" <= char <= "\u05FF" or char == "₪"


def _mixed_markup(text: str, ascii_font: str = _ASCII_FONT) -> str:
    """
    Split *text* into contiguous Hebrew / non-Hebrew character runs and wrap
    each in the appropriate <font> tag for ReportLab's Paragraph parser.

      Hebrew chars  → _HE_FONT   (NotoHebrew  — has Hebrew glyphs)
      Everything else → ascii_font (Helvetica — has digits, Latin, ₪, punctuation)

    All text is HTML-escaped before wrapping so < > & are safe.
    """
    if not text:
        return ""
    runs: list[tuple[bool, list[str]]] = []
    for ch in text:
        he = _is_he(ch)
        if not runs or runs[-1][0] != he:
            runs.append((he, []))
        runs[-1][1].append(ch)

    parts: list[str] = []
    for he, chars in runs:
        font = _HE_FONT if he else ascii_font
        content = html.escape("".join(chars))
        parts.append(f'<font name="{font}">{content}</font>')
    return "".join(parts)


# ── Paragraph factories ────────────────────────────────────────────────────────

def _he_para(
    text: str,
    size: int = 10,
    bold: bool = False,
    align: int = TA_RIGHT,
    color=colors.black,
) -> Paragraph:
    """
    Hebrew paragraph with automatic per-character font switching.
    Applies bidi first, then wraps runs in NotoHebrew / Helvetica tags.
    Digits and ASCII within Hebrew labels render via Helvetica → always visible.
    """
    ascii_font = _ASCII_BOLD if bold else _ASCII_FONT
    bidi_text = _h(text)
    markup = _mixed_markup(bidi_text, ascii_font=ascii_font)
    style = ParagraphStyle(
        f"he_{size}_{align}_{bold}",
        fontName=_HE_FONT,
        fontSize=size,
        leading=size + 5,
        alignment=align,
        textColor=color,
    )
    return Paragraph(markup, style)


def _amount_para(
    amount: Decimal,
    size: int = 10,
    bold: bool = False,
    color=colors.black,
) -> Paragraph:
    """
    Monetary amount: ₪ in NotoHebrew (has the glyph), number in Helvetica.
    Left-aligned so it sits at the left edge of the amount column.
    """
    ascii_font = _ASCII_BOLD if bold else _ASCII_FONT
    formatted = f"{amount:,.2f}"
    # ₪ literal in NotoHebrew, digits in Helvetica/Bold
    markup = f'<font name="{_HE_FONT}">₪</font><font name="{ascii_font}">{formatted}</font>'
    style = ParagraphStyle(
        f"amt_{size}_{bold}",
        fontName=ascii_font,
        fontSize=size,
        leading=size + 5,
        alignment=TA_LEFT,
        textColor=color,
    )
    return Paragraph(markup, style)


def _value_para(text: str, size: int = 10) -> Paragraph:
    """
    Smart value paragraph for the info table.
    Hebrew employer names → right-aligned NotoHebrew.
    Latin caregiver names / passport numbers → left-aligned Helvetica.
    """
    if any(_is_he(c) for c in text):
        return _he_para(text, size=size, align=TA_RIGHT)
    else:
        style = ParagraphStyle(
            f"val_ascii_{size}",
            fontName=_ASCII_FONT,
            fontSize=size,
            leading=size + 5,
            alignment=TA_LEFT,
        )
        return Paragraph(html.escape(text), style)


# ── Table builder ──────────────────────────────────────────────────────────────

# Usable page width = 210mm − 2×15mm margin = 180mm
_COL_AMOUNT = 70 * mm
_COL_LABEL = 110 * mm

# Dark header colour shared across all sections
_HEADER_COLOR = colors.HexColor("#2C3E50")
_TOTAL_ROW_COLOR = colors.HexColor("#D5D8DC")
_ALT_ROW_COLORS = [colors.white, colors.HexColor("#F8F9FA")]
_GRID_COLOR = colors.HexColor("#BDC3C7")


def _section_table(
    header: str,
    rows: list[tuple[str, Decimal]],
    highlight_last: bool = False,
    employer_style: bool = False,
) -> Table:
    """
    2-column section table.

    Layout (left → right on the page, reads right → left in Hebrew):
      [  Amount (₪ x,xxx.xx)  |  Hebrew label  ]

    - Column 0: amount — Helvetica, LEFT-aligned
    - Column 1: label  — NotoHebrew, RIGHT-aligned
    - Row 0: header spanning both columns, centered, white on dark background
    """
    header_color = colors.HexColor("#7F8C8D") if employer_style else _HEADER_COLOR

    header_para = _he_para(header, size=11, align=TA_CENTER, color=colors.white)
    data: list[list] = [[header_para, ""]]  # row 0 — will SPAN

    for i, (label, amount) in enumerate(rows):
        is_total = (i == len(rows) - 1) and highlight_last
        data.append([
            _amount_para(amount, bold=is_total),
            _he_para(label, bold=is_total),
        ])

    tbl = Table(data, colWidths=[_COL_AMOUNT, _COL_LABEL])

    row_count = len(data)
    style_cmds = [
        # Header row
        ("SPAN",             (0, 0), (1, 0)),
        ("BACKGROUND",       (0, 0), (1, 0), header_color),
        ("ALIGN",            (0, 0), (1, 0), "CENTER"),
        ("TOPPADDING",       (0, 0), (1, 0), 7),
        ("BOTTOMPADDING",    (0, 0), (1, 0), 7),
        # Data rows
        ("ROWBACKGROUNDS",   (0, 1), (1, -1), _ALT_ROW_COLORS),
        ("ALIGN",            (0, 1), (0, -1), "LEFT"),   # amounts: LEFT
        ("ALIGN",            (1, 1), (1, -1), "RIGHT"),  # labels:  RIGHT
        ("VALIGN",           (0, 0), (1, -1), "MIDDLE"),
        ("TOPPADDING",       (0, 1), (1, -1), 5),
        ("BOTTOMPADDING",    (0, 1), (1, -1), 5),
        ("LEFTPADDING",      (0, 0), (1, -1), 8),
        ("RIGHTPADDING",     (0, 0), (1, -1), 8),
        ("GRID",             (0, 0), (1, -1), 0.5, _GRID_COLOR),
    ]

    if highlight_last and row_count > 1:
        style_cmds += [
            ("BACKGROUND",   (0, -1), (1, -1), _TOTAL_ROW_COLOR),
            ("LINEABOVE",    (0, -1), (1, -1), 1.5, _HEADER_COLOR),
        ]

    tbl.setStyle(TableStyle(style_cmds))
    return tbl


# ── Main entry point ───────────────────────────────────────────────────────────

def generate_payslip_pdf(result: PayslipResult) -> str:
    """
    Generate a Hebrew payslip PDF and return its temporary file path.
    The caller MUST delete the file after use (zero-data-retention).
    """
    _ensure_font()

    fd, pdf_path = tempfile.mkstemp(suffix=".pdf", prefix="payslip_")
    os.close(fd)

    month_name = config.HEBREW_MONTHS[result.month]
    period_label = f"{month_name} {result.year}"

    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=A4,
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=f"תלוש שכר {period_label}",
    )

    story = []

    # ── Title ──────────────────────────────────────────────────────────────────
    story.append(_he_para(f"תלוש שכר חודשי — {period_label}", size=18, align=TA_CENTER))
    story.append(Spacer(1, 5 * mm))

    # ── Info table — Employer / Caregiver ─────────────────────────────────────
    # Each row: [value0, label0, value1, label1]
    # Rightmost col (3) = label, col 2 = value, col 1 = label, col 0 = value
    days_label = (
        f"{result.days_worked}/{result.working_days_in_month} ימי עבודה"
        if result.days_worked < result.working_days_in_month
        else "חודש מלא"
    )

    info_label_style = ParagraphStyle(
        "info_lbl", fontName=_HE_FONT, fontSize=9, leading=13,
        alignment=TA_RIGHT, textColor=colors.HexColor("#7F8C8D")
    )

    def _info_label(text: str) -> Paragraph:
        return Paragraph(_h(text), info_label_style)

    info_data = [
        [
            _value_para(result.employer_name),
            _info_label("מעסיק:"),
            _value_para(result.caregiver_name),
            _info_label("שם המטפל/ת:"),
        ],
        [
            _value_para(result.passport_number),
            _info_label("מספר דרכון:"),
            _he_para(days_label),
            _info_label("תקופת עבודה:"),
        ],
    ]

    info_tbl = Table(
        info_data,
        colWidths=[50 * mm, 30 * mm, 58 * mm, 42 * mm],
    )
    info_tbl.setStyle(TableStyle([
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN",         (0, 0), (0, -1), "LEFT"),   # value col 0
        ("ALIGN",         (1, 0), (1, -1), "RIGHT"),  # label col 1
        ("ALIGN",         (2, 0), (2, -1), "LEFT"),   # value col 2
        ("ALIGN",         (3, 0), (3, -1), "RIGHT"),  # label col 3
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
        ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#F0F3F4")),
        ("BOX",           (0, 0), (-1, -1), 0.5, _GRID_COLOR),
        ("INNERGRID",     (0, 0), (-1, -1), 0.5, _GRID_COLOR),
    ]))
    story.append(info_tbl)
    story.append(Spacer(1, 5 * mm))

    # ── Section 1: Earnings ────────────────────────────────────────────────────
    base_label = (
        f"שכר בסיס יחסי ({result.days_worked}/{result.working_days_in_month} ימים)"
        if result.days_worked < result.working_days_in_month
        else "שכר בסיס (חודש מלא)"
    )
    earnings: list[tuple[str, Decimal]] = [(base_label, result.gross_base)]

    if result.pocket_money_total > 0:
        weeks = int(result.pocket_money_total / 100)
        earnings.append((f"דמי כיס ({weeks} שבועות × ₪100)", result.pocket_money_total))

    if result.shabbat_addition > 0:
        earnings.append((
            f"שבתות ({result.shabbat_days} × ₪{result.shabbat_rate:,.2f})",
            result.shabbat_addition,
        ))

    if result.holiday_addition > 0:
        earnings.append((
            f"חגים ({result.holiday_days} × ₪{result.shabbat_rate:,.2f})",
            result.holiday_addition,
        ))

    earnings.append(("סה״כ שכר ברוטו", result.total_gross))
    story.append(_section_table("שכר ותוספות", earnings, highlight_last=True))
    story.append(Spacer(1, 4 * mm))

    # ── Section 2: Deductions ──────────────────────────────────────────────────
    deductions: list[tuple[str, Decimal]] = []
    if result.deduction_housing > 0:
        deductions.append(("ניכוי מגורים", result.deduction_housing))
    if result.deduction_health > 0:
        deductions.append(("ניכוי ביטוח רפואי", result.deduction_health))
    if result.deduction_extras > 0:
        deductions.append(("הוצאות נלוות", result.deduction_extras))
    if result.deduction_food > 0:
        deductions.append(("כלכלה", result.deduction_food))
    if result.advances > 0:
        deductions.append(("מקדמות", result.advances))

    if deductions:
        deductions.append(("סה״כ ניכויים", result.total_deductions))
        story.append(_section_table("ניכויים", deductions, highlight_last=True))
        story.append(Spacer(1, 4 * mm))

    # ── Net Pay row (prominent) ────────────────────────────────────────────────
    net_tbl = Table(
        [[
            _amount_para(result.total_net_pay, size=14, bold=True, color=colors.white),
            _he_para("סה״כ לתשלום בהעברה בנקאית", size=13, bold=True,
                     align=TA_RIGHT, color=colors.white),
        ]],
        colWidths=[_COL_AMOUNT, _COL_LABEL],
    )
    net_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), _HEADER_COLOR),
        ("ALIGN",         (0, 0), (0, -1), "LEFT"),
        ("ALIGN",         (1, 0), (1, -1), "RIGHT"),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
        ("BOX",           (0, 0), (-1, -1), 1.5, colors.HexColor("#1A252F")),
    ]))
    story.append(net_tbl)
    story.append(Spacer(1, 5 * mm))

    # ── Section 3: Employer Contributions (informational) ─────────────────────
    employer_total = result.employer_pension + result.employer_severance
    employer_rows: list[tuple[str, Decimal]] = [
        ("פנסיה מעסיק (6.5%)", result.employer_pension),
        ("פיצויים מעסיק (6%)", result.employer_severance),
        ("סה״כ הפרשות מעסיק", employer_total),
    ]
    story.append(_section_table(
        "הפרשות מעסיק — אינן מנוכות מהשכר",
        employer_rows,
        highlight_last=True,
        employer_style=True,
    ))
    story.append(Spacer(1, 5 * mm))

    # ── Footer: Social rights accrual ─────────────────────────────────────────
    footer_style = ParagraphStyle(
        "footer", fontName=_HE_FONT, fontSize=9, leading=14,
        alignment=TA_RIGHT, textColor=colors.HexColor("#444444"),
    )
    disclaimer_style = ParagraphStyle(
        "disc", fontName=_HE_FONT, fontSize=8, leading=12,
        alignment=TA_RIGHT, textColor=colors.grey, spaceBefore=3,
    )

    def _footer_line(prefix: str, value: Decimal, suffix: str) -> Paragraph:
        """Mixed-font footer: Hebrew label + Helvetica number."""
        markup = (
            f'<font name="{_HE_FONT}">{_h(prefix)}</font>'
            f'<font name="{_ASCII_FONT}"> {str(value)} </font>'
            f'<font name="{_HE_FONT}">{_h(suffix)}</font>'
        )
        return Paragraph(markup, footer_style)

    story.append(_footer_line("צבירת חופשה חודשית:", result.vacation_accrued, "ימים"))
    story.append(_footer_line("צבירת מחלה חודשית:", result.sick_accrued, "ימים"))
    story.append(Paragraph(
        _h("* מסמך זה הופק אוטומטית. אין בו כדי להחליף ייעוץ משפטי."),
        disclaimer_style,
    ))

    doc.build(story)
    return pdf_path
