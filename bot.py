"""
Telegram Bot — Foreign Caregiver Payslip Generator.

Implements an aiogram v3 FSM conversation in Hebrew that collects monthly
payroll data, calculates the salary, generates a Hebrew PDF payslip, sends it,
and immediately deletes all data (Zero-Data Retention policy).
"""

import asyncio
import calendar
import logging
import os
import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

import config
import database
from calculator import PayslipInput, calculate, calculate_partial_days
from pdf_generator import generate_payslip_pdf

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

router = Router()


# ── Global error handler ───────────────────────────────────────────────────────

@router.error()
async def global_error_handler(event: ErrorEvent) -> None:
    """
    Catch any unhandled exception from a handler, log it, and notify the user.
    Without this, aiogram silently swallows exceptions — the user sees nothing
    and the bot appears stuck.
    """
    log.exception("Unhandled exception in handler: %s", event.exception)

    update = event.update
    chat_id: int | None = None

    if update.message:
        chat_id = update.message.chat.id
    elif update.callback_query:
        msg = update.callback_query.message
        if msg and hasattr(msg, "chat"):
            chat_id = msg.chat.id  # type: ignore[union-attr]

    if chat_id:
        try:
            # Re-use the bot instance from the running application
            bot: Bot = event.update.bot  # type: ignore[attr-defined]
            await bot.send_message(
                chat_id,
                "❌ אירעה שגיאה בלתי צפויה.\nאנא הזן /start להתחלה מחדש.",
            )
        except Exception:
            pass   # best-effort — don't let the error handler itself crash


# ── FSM States ─────────────────────────────────────────────────────────────────

class PayslipForm(StatesGroup):
    month_year = State()
    work_period = State()       # inline keyboard: full / partial
    partial_type = State()      # started mid-month or ended mid-month
    partial_day = State()       # which calendar day (1-31)
    employer_name = State()
    caregiver_name = State()
    passport = State()
    shabbat_days = State()
    holiday_days = State()
    pocket_money_weeks = State()
    advances = State()
    deductions = State()        # inline multi-select (amounts shown inline)
    deduction_edit = State()    # editing a single deduction amount in-place
    confirm = State()           # summary review before PDF generation


# ── Keyboard builders ──────────────────────────────────────────────────────────

def _month_picker_kb() -> InlineKeyboardMarkup:
    """
    Quick-select keyboard for the billing month.
    Shows previous, current (pre-selected ✅), and next month as buttons,
    plus a free-entry option for any other period.
    """
    today = date.today()

    def _add_months(d: date, n: int) -> date:
        month = d.month - 1 + n
        year = d.year + month // 12
        month = month % 12 + 1
        return date(year, month, 1)

    months = [_add_months(today, n) for n in (-1, 0, 1)]
    builder = InlineKeyboardBuilder()
    for i, m in enumerate(months):
        label = f"{config.HEBREW_MONTHS[m.month]} {m.year}"
        if i == 1:          # current month gets a checkmark
            label = f"✅ {label}"
        builder.button(text=label, callback_data=f"month:{m.month}:{m.year}")
    builder.button(text="📅 חודש אחר…", callback_data="month:other")
    builder.adjust(3, 1)    # 3 month buttons on row 1, "other" on row 2
    return builder.as_markup()


def _work_period_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ חודש מלא", callback_data="period:full")
    builder.button(text="⚠️ חודש חלקי", callback_data="period:partial")
    builder.adjust(2)
    return builder.as_markup()


def _partial_type_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="התחיל באמצע", callback_data="partial:started")
    builder.button(text="סיים באמצע", callback_data="partial:ended")
    builder.adjust(2)
    return builder.as_markup()


def _skip_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="המשך ללא מילוי ⏭️", callback_data="skip_field")
    return builder.as_markup()


# Placeholder stored when the user skips an optional personal-details field.
_SKIPPED = "---"

# Fixed deduction keys, labels, and their legal config maxima.
# Ordered as they appear in the conversation.
_DEDUCTION_META: dict[str, tuple[str, Decimal]] = {
    "housing": ("מגורים",        config.DEDUCTION_HOUSING_MAX),
    "health":  ("ביטוח רפואי",   config.DEDUCTION_HEALTH_MAX),
    "extras":  ("הוצאות נלוות",  config.DEDUCTION_EXTRAS_MAX),
}


def _deduction_edit_kb(max_amount: Decimal) -> InlineKeyboardMarkup:
    """Keyboard shown while editing a single deduction amount in-place."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"✅ ₪{max_amount:,.2f} (מקסימום מותר)",
        callback_data=f"ded_editpick:{max_amount}",
    )
    builder.button(text="↩️ ביטול", callback_data="ded_editcancel")
    builder.adjust(1)
    return builder.as_markup()


def _confirm_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ אשר והפק תלוש", callback_data="confirm:generate")
    builder.button(text="🔄 התחל מחדש",     callback_data="confirm:restart")
    builder.adjust(1)
    return builder.as_markup()


def _deductions_kb(sel: dict[str, bool], amounts: dict[str, str]) -> InlineKeyboardMarkup:
    """
    Multi-select deductions keyboard with amounts shown inline.

    Unselected row: single button "☐ <label>"
    Selected row:   two buttons — "✅ <label> (₪X.XX)" + "✏️"
    """
    rows: list[list[InlineKeyboardButton]] = []

    deduction_options = [
        ("housing", "מגורים"),
        ("health",  "ביטוח רפואי"),
        ("extras",  "הוצאות נלוות"),
        ("food",    "כלכלה"),
    ]

    for key, label in deduction_options:
        if sel.get(key):
            amount = Decimal(amounts.get(key, "0"))
            rows.append([
                InlineKeyboardButton(
                    text=f"✅ {label} (₪{amount:,.2f})",
                    callback_data=f"ded:{key}",
                ),
                InlineKeyboardButton(text="✏️", callback_data=f"ded_edit:{key}"),
            ])
        else:
            rows.append([
                InlineKeyboardButton(text=f"☐ {label}", callback_data=f"ded:{key}")
            ])

    rows.append([InlineKeyboardButton(text="🚫 אין ניכויים", callback_data="ded:none")])
    rows.append([InlineKeyboardButton(text="סיום ✔️", callback_data="ded:done")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _ask(message: Message, text: str, reply_markup=None) -> None:
    """Send a Hebrew message, optionally with a keyboard."""
    await message.answer(text, reply_markup=reply_markup)


async def _invalid(message: Message, prompt: str) -> None:
    """Reply to invalid input and re-ask."""
    await message.answer(f"⚠️ קלט לא תקין. {prompt}")


def _parse_decimal(text: str) -> Decimal | None:
    """Parse user-supplied decimal/integer, return None on failure."""
    try:
        return Decimal(text.strip().replace(",", "."))
    except InvalidOperation:
        return None


def _parse_non_negative_int(text: str) -> int | None:
    """Parse a non-negative integer, return None on failure."""
    try:
        val = int(text.strip())
        return val if val >= 0 else None
    except ValueError:
        return None


# ── /start ─────────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()

    user_id = message.from_user.id  # type: ignore[union-attr]
    saved_data: dict | None = None
    try:
        saved_data = await database.get_user(user_id)
    except Exception:
        log.warning("Firestore get_user failed for %s — continuing without saved data", user_id)
    await state.update_data(saved_data=saved_data, user_id=user_id)

    await message.answer(
        "👋 *ברוכים הבאים למחולל תלושי השכר*\n\n"
        "הבוט מסייע למעסיקי עובדי זר בסיעוד לחשב שכר ולהפיק תלוש שכר רשמי.\n\n"
        "🔒 *פרטיות:* מספרי דרכון ונתוני שכר נמחקים מיד. "
        "שמות ויתרות חופשה/מחלה נשמרים לנוחיותך. "
        "להסרת כל הנתונים: /forget\\_me\n\n"
        "לאיזה חודש להפיק את התלוש?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_month_picker_kb(),
    )
    await state.set_state(PayslipForm.month_year)


# ── /forget_me command ────────────────────────────────────────────────────────

@router.message(Command("forget_me"))
async def cmd_forget_me(message: Message) -> None:
    """Delete all stored data for this user from Firestore."""
    user_id = message.from_user.id  # type: ignore[union-attr]
    deleted = await database.delete_user(user_id)
    if deleted:
        await message.answer(
            "✅ כל הנתונים שלך נמחקו מהמערכת.\n"
            "שמות, יתרת חופשה ויתרת מחלה — הכל נמחק."
        )
    else:
        await message.answer("ℹ️ לא נמצאו נתונים שמורים עבורך.")


# ── State: month_year — quick-pick callback ────────────────────────────────────

@router.callback_query(PayslipForm.month_year, F.data.startswith("month:"))
async def handle_month_pick(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    parts = callback.data.split(":")   # "month:4:2026" or "month:other"

    if parts[1] == "other":
        await callback.message.edit_text(  # type: ignore[union-attr]
            "הזן חודש ושנה:\n_לדוגמה: 04/2026 או 04/26_",
            parse_mode=ParseMode.MARKDOWN,
        )
        return   # stay in month_year state — next message handled below

    month, year = int(parts[1]), int(parts[2])
    await _confirm_month_and_proceed(callback.message, state, month, year, edit=True)  # type: ignore[arg-type]


# ── State: month_year — free-text fallback (MM/YYYY or MM/YY) ─────────────────

@router.message(PayslipForm.month_year)
async def handle_month_year(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    # Accept MM/YYYY (e.g. 04/2026) or MM/YY shorthand (e.g. 04/26 → 2026)
    match = re.fullmatch(r"(\d{1,2})[/\-.](\d{2,4})", text)
    if not match:
        await _invalid(message, "נא להזין תאריך — לדוגמה: 04/2026 או 04/26")
        return
    month = int(match.group(1))
    year_raw = match.group(2)
    year = int(year_raw) if len(year_raw) == 4 else 2000 + int(year_raw)

    if not (1 <= month <= 12):
        await _invalid(message, "מספר חודש חייב להיות בין 1 ל-12.")
        return
    if year < 2020:
        await _invalid(message, "שנה לא תקינה. נא להזין שנה מ-2020 ואילך.")
        return

    await _confirm_month_and_proceed(message, state, month, year, edit=False)


async def _confirm_month_and_proceed(
    message: Message, state: FSMContext, month: int, year: int, *, edit: bool
) -> None:
    """Shared logic: save month/year, confirm to user, advance to work_period."""
    await state.update_data(month=month, year=year)
    month_name = config.HEBREW_MONTHS[month]
    confirm_text = f"📅 חודש: *{month_name} {year}*\n\nהאם העובד/ת עבד/ה חודש מלא?"

    if edit:
        await message.edit_text(confirm_text, parse_mode=ParseMode.MARKDOWN,  # type: ignore[union-attr]
                                reply_markup=_work_period_kb())
    else:
        await message.answer(confirm_text, parse_mode=ParseMode.MARKDOWN,
                             reply_markup=_work_period_kb())
    await state.set_state(PayslipForm.work_period)


# ── State: work_period (inline keyboard) ──────────────────────────────────────

@router.callback_query(PayslipForm.work_period, F.data.startswith("period:"))
async def handle_work_period(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    period = callback.data.split(":")[1]  # "full" or "partial"

    if period == "full":
        await state.update_data(days_worked=26)
        await callback.message.edit_text("✅ חודש מלא — 26 ימי עבודה.")  # type: ignore[union-attr]
        await _ask_employer_name(callback.message, state)  # type: ignore[arg-type]
    else:
        await callback.message.edit_text("⚠️ חודש חלקי.")  # type: ignore[union-attr]
        await callback.message.answer(  # type: ignore[union-attr]
            "האם העובד/ת *התחיל/ה* לעבוד באמצע החודש, או *סיים/ה* לעבוד באמצע החודש?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_partial_type_kb(),
        )
        await state.set_state(PayslipForm.partial_type)


# ── State: partial_type (inline keyboard: started / ended) ────────────────────

@router.callback_query(PayslipForm.partial_type, F.data.startswith("partial:"))
async def handle_partial_type(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    ptype = callback.data.split(":")[1]  # "started" or "ended"
    await state.update_data(partial_type=ptype)

    data = await state.get_data()
    month_name = config.HEBREW_MONTHS[data["month"]]
    days_in_month = calendar.monthrange(data["year"], data["month"])[1]

    if ptype == "started":
        question = (
            f"באיזה יום בחודש היה *יום העבודה הראשון* של העובד/ת?\n"
            f"_(הזן מספר בין 1 ל-{days_in_month} — חודש {month_name})_"
        )
    else:
        question = (
            f"באיזה יום בחודש היה *יום העבודה האחרון* של העובד/ת?\n"
            f"_(הזן מספר בין 1 ל-{days_in_month} — חודש {month_name})_"
        )

    await callback.message.edit_text(  # type: ignore[union-attr]
        "✅ " + ("התחיל/ה באמצע החודש." if ptype == "started" else "סיים/ה באמצע החודש.")
    )
    await callback.message.answer(question, parse_mode=ParseMode.MARKDOWN)  # type: ignore[union-attr]
    await state.set_state(PayslipForm.partial_day)


# ── State: partial_day ─────────────────────────────────────────────────────────

@router.message(PayslipForm.partial_day)
async def handle_partial_day(message: Message, state: FSMContext) -> None:
    val = _parse_non_negative_int(message.text or "")
    data = await state.get_data()
    month: int = data["month"]
    year: int = data["year"]
    days_in_month = calendar.monthrange(year, month)[1]

    if val is None or not (1 <= val <= days_in_month):
        await _invalid(message, f"נא להזין מספר יום תקין בין 1 ל-{days_in_month}.")
        return

    ptype: str = data["partial_type"]
    active_days, days_worked = calculate_partial_days(ptype, val, month, year)

    await state.update_data(days_worked=days_worked)
    month_name = config.HEBREW_MONTHS[month]
    await message.answer(
        f"✅ *{active_days} ימים פעילים* מתוך {days_in_month} ימי חודש {month_name} "
        f"= *{days_worked} ימי עבודה* מחושבים מתוך 26.",
        parse_mode=ParseMode.MARKDOWN,
    )
    await _ask_employer_name(message, state)


# ── State: employer_name ───────────────────────────────────────────────────────

async def _ask_employer_name(message: Message, state: FSMContext) -> None:
    """
    Prompt for the employer name. If Firestore has saved names for this user,
    shows an inline button to reuse them and displays the current balance.
    """
    data = await state.get_data()
    saved: dict | None = data.get("saved_data")
    await state.set_state(PayslipForm.employer_name)

    if saved and saved.get("employer_name", _SKIPPED) != _SKIPPED:
        employer = saved["employer_name"]
        caregiver = saved.get("caregiver_name", _SKIPPED)
        vac = saved.get("vacation_balance", 0.0)
        sick = saved.get("sick_balance", 0.0)
        caregiver_display = caregiver if caregiver != _SKIPPED else "לא הוזן"
        builder = InlineKeyboardBuilder()
        builder.button(
            text=f"⚡ פרטים קודמים: {employer} / {caregiver_display}",
            callback_data="use_saved",
        )
        builder.button(text="⏭️ דלג", callback_data="skip_field")
        builder.adjust(1)
        await message.answer(
            f"✅ *יתרה צבורה:* {vac:.2f} ימי חופשה | {sick:.2f} ימי מחלה\n\n"
            "מהו שם המעסיק?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=builder.as_markup(),
        )
    else:
        await message.answer("מהו שם המעסיק?", reply_markup=_skip_kb())


@router.callback_query(PayslipForm.employer_name, F.data == "skip_field")
async def skip_employer_name(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(employer_name=_SKIPPED)
    await callback.message.edit_text("⏭️ שם מעסיק — לא הוזן.")  # type: ignore[union-attr]
    await callback.message.answer("מהו שם המטפל/ת?", reply_markup=_skip_kb())  # type: ignore[union-attr]
    await state.set_state(PayslipForm.caregiver_name)


@router.callback_query(PayslipForm.employer_name, F.data == "use_saved")
async def handle_use_saved(callback: CallbackQuery, state: FSMContext) -> None:
    """Pre-fill employer + caregiver from Firestore and jump directly to passport."""
    await callback.answer()
    data = await state.get_data()
    saved: dict = data.get("saved_data") or {}
    employer_name = saved.get("employer_name", _SKIPPED)
    caregiver_name = saved.get("caregiver_name", _SKIPPED)
    await state.update_data(employer_name=employer_name, caregiver_name=caregiver_name)
    await callback.message.edit_text(  # type: ignore[union-attr]
        f"⚡ נטענו פרטים קודמים:\n"
        f"מעסיק: {employer_name} | מטפל/ת: {caregiver_name if caregiver_name != _SKIPPED else 'לא הוזן'}"
    )
    await callback.message.answer(  # type: ignore[union-attr]
        "מהו מספר הדרכון של המטפל/ת?",
        reply_markup=_skip_kb(),
    )
    await state.set_state(PayslipForm.passport)


@router.message(PayslipForm.employer_name)
async def handle_employer_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if len(name) < 2:
        await _invalid(message, "נא להזין שם תקין.")
        return
    await state.update_data(employer_name=name)
    await message.answer("מהו שם המטפל/ת?", reply_markup=_skip_kb())
    await state.set_state(PayslipForm.caregiver_name)


# ── State: caregiver_name ──────────────────────────────────────────────────────

@router.callback_query(PayslipForm.caregiver_name, F.data == "skip_field")
async def skip_caregiver_name(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(caregiver_name=_SKIPPED)
    await callback.message.edit_text("⏭️ שם מטפל/ת — לא הוזן.")  # type: ignore[union-attr]
    await callback.message.answer("מהו מספר הדרכון של המטפל/ת?", reply_markup=_skip_kb())  # type: ignore[union-attr]
    await state.set_state(PayslipForm.passport)


@router.message(PayslipForm.caregiver_name)
async def handle_caregiver_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if len(name) < 2:
        await _invalid(message, "נא להזין שם תקין.")
        return
    await state.update_data(caregiver_name=name)
    await message.answer("מהו מספר הדרכון של המטפל/ת?", reply_markup=_skip_kb())
    await state.set_state(PayslipForm.passport)


# ── State: passport ────────────────────────────────────────────────────────────

@router.callback_query(PayslipForm.passport, F.data == "skip_field")
async def skip_passport(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(passport=_SKIPPED)
    await callback.message.edit_text("⏭️ מספר דרכון — לא הוזן.")  # type: ignore[union-attr]
    data = await state.get_data()
    _, shabbat_rate = config.get_wage_params(data["month"], data["year"])
    await callback.message.answer(  # type: ignore[union-attr]
        f"כמה שבתות (ימי מנוחה שבועיים) עבד/ה העובד/ת החודש?\n"
        f"_(כל שבת = {shabbat_rate} ₪ — הזן 0 אם לא עבד/ה בשבתות)_",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(PayslipForm.shabbat_days)


@router.message(PayslipForm.passport)
async def handle_passport(message: Message, state: FSMContext) -> None:
    passport = (message.text or "").strip()
    if len(passport) < 3:
        await _invalid(message, "נא להזין מספר דרכון תקין.")
        return
    await state.update_data(passport=passport)

    data = await state.get_data()
    _, shabbat_rate = config.get_wage_params(data["month"], data["year"])
    await message.answer(
        f"כמה שבתות (ימי מנוחה שבועיים) עבד/ה העובד/ת החודש?\n"
        f"_(כל שבת = {shabbat_rate} ₪ — הזן 0 אם לא עבד/ה בשבתות)_",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(PayslipForm.shabbat_days)


# ── State: shabbat_days ────────────────────────────────────────────────────────

@router.message(PayslipForm.shabbat_days)
async def handle_shabbat_days(message: Message, state: FSMContext) -> None:
    val = _parse_non_negative_int(message.text or "")
    if val is None or val > 6:
        await _invalid(message, "נא להזין מספר שבתות בין 0 ל-6.")
        return
    await state.update_data(shabbat_days=val)

    data = await state.get_data()
    _, shabbat_rate = config.get_wage_params(data["month"], data["year"])
    await message.answer(
        f"כמה ימי חג עבד/ה העובד/ת החודש?\n"
        f"_(כל חג = {shabbat_rate} ₪ — הזן 0 אם לא עבד/ה בחגים)_",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(PayslipForm.holiday_days)


# ── State: holiday_days ────────────────────────────────────────────────────────

@router.message(PayslipForm.holiday_days)
async def handle_holiday_days(message: Message, state: FSMContext) -> None:
    val = _parse_non_negative_int(message.text or "")
    if val is None or val > 10:
        await _invalid(message, "נא להזין מספר ימי חג בין 0 ל-10.")
        return
    await state.update_data(holiday_days=val)
    await message.answer(
        "כמה שבועות שולמו דמי כיס (100 ₪ לשבוע) לעובד/ת?\n"
        "_(בדרך כלל 4. הזן 0 אם דמי כיס לא שולמו)_",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(PayslipForm.pocket_money_weeks)


# ── State: pocket_money_weeks ──────────────────────────────────────────────────

@router.message(PayslipForm.pocket_money_weeks)
async def handle_pocket_money(message: Message, state: FSMContext) -> None:
    val = _parse_non_negative_int(message.text or "")
    if val is None or val > 6:
        await _invalid(message, "נא להזין מספר שבועות בין 0 ל-6.")
        return
    await state.update_data(pocket_money_weeks=val)
    await message.answer(
        "האם שולמו *מקדמות נוספות* לשכר (מעבר לדמי הכיס) החודש?\n"
        "_(הזן את הסכום בשקלים, או 0 אם לא)_",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(PayslipForm.advances)


# ── State: advances ────────────────────────────────────────────────────────────

@router.message(PayslipForm.advances)
async def handle_advances(message: Message, state: FSMContext) -> None:
    val = _parse_decimal(message.text or "")
    if val is None or val < 0:
        await _invalid(message, "נא להזין סכום חיובי (לדוגמה: 200 או 0).")
        return
    await state.update_data(advances=str(val))

    # Pre-calculate pro-rata maxima for fixed deductions; set as default amounts
    data = await state.get_data()
    days_worked: int = data.get("days_worked", 26)
    ratio = Decimal(days_worked) / Decimal("26")
    pro_rata_max = {
        k: str((cfg_max * ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        for k, (_, cfg_max) in _DEDUCTION_META.items()
    }
    deductions_amounts = {**pro_rata_max, "food": "0"}

    sel = {"housing": False, "health": False, "extras": False, "food": False}
    await state.update_data(
        deductions_selected=sel,
        deductions_amounts=deductions_amounts,
        deductions_pro_rata_max=pro_rata_max,
    )

    await message.answer(
        "אילו *ניכויים מותרים* מנוכים מהשכר החודש?\n"
        "_(לחץ לסימון/ביטול. לחץ ✏️ לשינוי הסכום. לחץ סיום בסיום הבחירה)_\n\n"
        "⚠️ ניכויים מותרים דורשים הסכמת העובד בכתב (נספח ג׳ לחוזה).",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_deductions_kb(sel, deductions_amounts),
    )
    await state.set_state(PayslipForm.deductions)


# ── State: deductions (inline multi-select with inline amounts) ────────────────

@router.callback_query(PayslipForm.deductions, F.data.startswith("ded:"))
async def handle_deductions(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    key = callback.data.split(":")[1]

    data = await state.get_data()
    sel: dict[str, bool] = data.get("deductions_selected", {
        "housing": False, "health": False, "extras": False, "food": False
    })
    amounts: dict[str, str] = data.get("deductions_amounts", {})

    if key == "none":
        sel = {"housing": False, "health": False, "extras": False, "food": False}
        await state.update_data(deductions_selected=sel)
        await callback.message.edit_reply_markup(reply_markup=_deductions_kb(sel, amounts))  # type: ignore[union-attr]
        return

    if key == "done":
        await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
        await _show_confirm(callback.message, state)  # type: ignore[arg-type]
        return

    # Toggle the selected key — default amount pre-filled in deductions_amounts
    if key in sel:
        sel[key] = not sel[key]
        await state.update_data(deductions_selected=sel)
        await callback.message.edit_reply_markup(reply_markup=_deductions_kb(sel, amounts))  # type: ignore[union-attr]


# ── State: deductions — inline amount editing via ✏️ button ───────────────────

def _max_for_key(key: str, data: dict) -> Decimal:
    """Return the legal maximum amount for a deduction key given current FSM data."""
    if key == "food":
        return config.DEDUCTION_FOOD_MAX
    return Decimal(data["deductions_pro_rata_max"][key])


@router.callback_query(PayslipForm.deductions, F.data.startswith("ded_edit:"))
async def handle_deduction_edit_cb(callback: CallbackQuery, state: FSMContext) -> None:
    """User tapped ✏️ on a selected deduction — transform message into an edit prompt."""
    await callback.answer()
    key = callback.data.split(":")[1]
    data = await state.get_data()
    max_amount = _max_for_key(key, data)
    label, _ = _DEDUCTION_META.get(key, ("כלכלה", config.DEDUCTION_FOOD_MAX))

    await state.update_data(
        deduction_edit_key=key,
        deduction_edit_chat_id=callback.message.chat.id,  # type: ignore[union-attr]
        deduction_edit_msg_id=callback.message.message_id,  # type: ignore[union-attr]
    )
    await callback.message.edit_text(  # type: ignore[union-attr]
        f"✏️ *עריכת ניכוי — {label}*\n\n"
        f"הזן סכום חדש (מקסימום: ₪{max_amount:,.2f}):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_deduction_edit_kb(max_amount),
    )
    await state.set_state(PayslipForm.deduction_edit)


async def _finish_deduction_edit(state: FSMContext, amount: Decimal, bot: Bot) -> None:
    """Save the edited amount and restore the full deductions keyboard in-place."""
    data = await state.get_data()
    key: str = data["deduction_edit_key"]
    chat_id: int = data["deduction_edit_chat_id"]
    msg_id: int = data["deduction_edit_msg_id"]
    amounts: dict[str, str] = data["deductions_amounts"]
    amounts[key] = str(amount)
    sel: dict[str, bool] = data["deductions_selected"]

    await state.update_data(
        deductions_amounts=amounts,
        deduction_edit_key=None,
        deduction_edit_chat_id=None,
        deduction_edit_msg_id=None,
    )
    await bot.edit_message_text(
        chat_id=chat_id,
        message_id=msg_id,
        text=(
            "אילו *ניכויים מותרים* מנוכים מהשכר החודש?\n"
            "_(לחץ לסימון/ביטול. לחץ ✏️ לשינוי הסכום. לחץ סיום בסיום הבחירה)_\n\n"
            "⚠️ ניכויים מותרים דורשים הסכמת העובד בכתב (נספח ג׳ לחוזה)."
        ),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_deductions_kb(sel, amounts),
    )
    await state.set_state(PayslipForm.deductions)


@router.callback_query(PayslipForm.deduction_edit, F.data.startswith("ded_editpick:"))
async def handle_deduction_editpick(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    """User tapped the max-amount quick-pick while in edit mode."""
    await callback.answer()
    amount = Decimal(callback.data.split(":", 1)[1])
    await _finish_deduction_edit(state, amount, bot)


@router.callback_query(PayslipForm.deduction_edit, F.data == "ded_editcancel")
async def handle_deduction_editcancel(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    """User cancelled editing — restore the keyboard without changing the amount."""
    await callback.answer()
    data = await state.get_data()
    key: str = data["deduction_edit_key"]
    amounts: dict[str, str] = data["deductions_amounts"]
    await _finish_deduction_edit(state, Decimal(amounts.get(key, "0")), bot)


@router.message(PayslipForm.deduction_edit)
async def handle_deduction_edit_text(
    message: Message, state: FSMContext, bot: Bot
) -> None:
    """User typed a custom amount while in edit mode — validate then restore keyboard."""
    data = await state.get_data()
    key: str = data["deduction_edit_key"]
    label, _ = _DEDUCTION_META.get(key, ("כלכלה", config.DEDUCTION_FOOD_MAX))
    max_amount = _max_for_key(key, data)

    val = _parse_decimal(message.text or "")
    if val is None or val < 0:
        await _invalid(message, f"נא להזין סכום חיובי עד ₪{max_amount:,.2f}.")
        return
    if val > max_amount:
        await _invalid(
            message,
            f"הסכום ₪{val:,.2f} עולה על המקסימום המותר ({label}: ₪{max_amount:,.2f}).",
        )
        return

    await _finish_deduction_edit(state, val, bot)


# ── Helpers: build input, show confirm ────────────────────────────────────────

def _build_payslip_input(data: dict) -> PayslipInput:
    """Assemble PayslipInput from accumulated FSM state data."""
    sel: dict[str, bool] = data.get("deductions_selected", {})
    amounts: dict[str, str] = data.get("deductions_amounts", {})

    def _deduction(key: str) -> Decimal:
        if not sel.get(key):
            return Decimal("0")
        stored = amounts.get(key)
        return Decimal(stored) if stored is not None else Decimal("0")

    return PayslipInput(
        month=data["month"],
        year=data["year"],
        is_full_month=data["days_worked"] == 26,
        days_worked=data["days_worked"],
        employer_name=data.get("employer_name", _SKIPPED),
        caregiver_name=data.get("caregiver_name", _SKIPPED),
        passport_number=data.get("passport", _SKIPPED),
        shabbat_days=data["shabbat_days"],
        holiday_days=data["holiday_days"],
        deduction_housing=_deduction("housing"),
        deduction_health=_deduction("health"),
        deduction_extras=_deduction("extras"),
        deduction_food=_deduction("food"),
        pocket_money_weeks=data["pocket_money_weeks"],
        advances=Decimal(data.get("advances", "0")),
    )


async def _show_confirm(message: Message, state: FSMContext) -> None:
    """
    Calculate the payslip from current state, show a text summary, and ask
    the user to confirm before the PDF is generated.
    Catches calculation errors (e.g. 25% cap) here instead of at PDF time.
    """
    data = await state.get_data()

    try:
        result = calculate(_build_payslip_input(data))
    except ValueError as exc:
        await message.answer(f"❌ שגיאה בחישוב:\n{exc}\n\nהזן /start להתחלה מחדש.")
        await state.clear()
        return

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

    await message.answer(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_confirm_kb(),
    )
    await state.set_state(PayslipForm.confirm)


# ── State: confirm ─────────────────────────────────────────────────────────────

@router.callback_query(PayslipForm.confirm, F.data.startswith("confirm:"))
async def handle_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    action = callback.data.split(":")[1]
    await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]

    if action == "restart":
        await state.clear()
        await callback.message.answer(  # type: ignore[union-attr]
            "🔄 הנתונים נמחקו. לחץ /start להתחלה מחדש."
        )
        return

    # action == "generate"
    await _generate_and_send(callback.message, state)  # type: ignore[arg-type]


# ── PDF Generation + Send + Delete ─────────────────────────────────────────────

async def _generate_and_send(message: Message, state: FSMContext) -> None:
    """
    Build the PayslipInput from FSM state, generate the PDF, send it, then
    immediately delete the file and clear all state (Zero-Data Retention).
    Only called from handle_confirm — user has already reviewed the summary.
    """
    data = await state.get_data()
    payslip_input = _build_payslip_input(data)
    user_id: int = data.get("user_id", message.chat.id)

    # Clear FSM state immediately — data leaves memory before PDF is sent
    await state.clear()

    try:
        result = calculate(payslip_input)
    except ValueError as exc:
        await message.answer(f"❌ שגיאה בחישוב השכר:\n{exc}\n\nהזן /start להתחלה מחדש.")
        return

    # Fire DB updates in the background — parallel with PDF generation
    db_upsert = asyncio.create_task(
        database.upsert_user(user_id, result.employer_name, result.caregiver_name)
    )
    db_balances = asyncio.create_task(
        database.add_to_balances(user_id, result.vacation_accrued, result.sick_accrued)
    )

    await message.answer("⏳ מפיק את התלוש…")
    pdf_path: str | None = None
    try:
        pdf_path = generate_payslip_pdf(result)
        month_name = config.HEBREW_MONTHS[result.month]
        await message.answer_document(
            FSInputFile(pdf_path, filename=f"תלוש_שכר_{month_name}_{result.year}.pdf"),
            caption=(
                f"✅ *תלוש שכר — {month_name} {result.year}*\n"
                f"סה״כ לתשלום: *{result.total_net_pay:,.2f} ₪*\n\n"
                "🔒 נתוני שכר ודרכון נמחקו מהשרת."
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:
        log.exception("Failed to generate or send PDF: %s", exc)
        await message.answer("❌ אירעה שגיאה בהפקת התלוש. אנא נסה שוב עם /start")
    finally:
        if pdf_path and os.path.exists(pdf_path):
            os.remove(pdf_path)
            log.info("PDF deleted: %s", pdf_path)

    # Await DB tasks; log failures but do not crash
    db_results = await asyncio.gather(db_upsert, db_balances, return_exceptions=True)
    for i, r in enumerate(db_results):
        if isinstance(r, Exception):
            log.warning("DB task %d failed: %s", i, r)

    # Show updated balance (best-effort; skipped if Firestore unavailable)
    try:
        updated = await database.get_user(user_id)
        if updated:
            vac = updated.get("vacation_balance", 0.0)
            sick = updated.get("sick_balance", 0.0)
            await message.answer(
                f"✅ *יתרה עדכנית:* {vac:.2f} ימי חופשה | {sick:.2f} ימי מחלה",
                parse_mode=ParseMode.MARKDOWN,
            )
    except Exception:
        pass

    await message.answer("להפקת תלוש נוסף לחץ /start")


# ── Main ───────────────────────────────────────────────────────────────────────

async def main() -> None:
    bot = Bot(
        token=config.TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    try:
        await database.init_db()
        log.info("Firestore initialized.")
    except Exception as exc:
        log.warning("Firestore unavailable — running without persistence: %s", exc)

    if config.WEBHOOK_URL:
        webhook_url = f"{config.WEBHOOK_URL}/webhook"

        # Start HTTP server FIRST so Cloud Run's health check passes
        app = web.Application()
        SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
        setup_application(app, dp, bot=bot)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host="0.0.0.0", port=config.PORT)
        await site.start()
        log.info("Webhook server listening on port %d", config.PORT)

        # Register webhook with Telegram — non-fatal so a temporary/wrong URL
        # doesn't crash the server (we update WEBHOOK_URL once the real URL is known)
        try:
            await bot.set_webhook(webhook_url, allowed_updates=["message", "callback_query"])
            log.info("Webhook registered: %s", webhook_url)
        except Exception as exc:
            log.warning("Webhook registration failed (update WEBHOOK_URL env var): %s", exc)

        await asyncio.Event().wait()  # run forever until process is killed
    else:
        log.info("No WEBHOOK_URL set — starting polling (local dev mode).")
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
