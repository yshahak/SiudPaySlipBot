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
from aiogram.exceptions import TelegramBadRequest
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
async def global_error_handler(event: ErrorEvent, bot: Bot) -> None:
    """
    Catch any unhandled exception from a handler, log it, and notify the user.
    Without this, aiogram silently swallows exceptions — the user sees nothing
    and the bot appears stuck.

    Bot is injected by aiogram's DI system — do NOT use event.update.bot
    (that attribute does not exist in aiogram v3).
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
    details_confirm = State()   # fast-track: confirm saved employer+caregiver in one step
    employer_name = State()
    caregiver_name = State()
    passport = State()
    shabbat_days = State()      # days worked on Shabbat (added on top of base salary)
    holiday_days = State()      # days worked on national holidays
    # ARCHITECTURE NOTE (Simple Mode): pocket_money_weeks, deductions, and deduction_edit
    # states are bypassed. All are set to 0 automatically.
    # Re-enable these states for Detailed Mode when per-item deduction UI is needed.
    advances = State()          # cash advances already paid this month
    confirm = State()           # summary review before PDF generation


class ContractSetupForm(StatesGroup):
    """One-time (or /setup-triggered) flow to collect persistent caregiver/contract config."""
    review           = State()   # /setup only: show current values + confirm update
    agreed_net_salary = State()  # monthly net salary agreed in the contract
    rest_day         = State()   # weekly rest day: friday / saturday / sunday
    # ARCHITECTURE NOTE (Simple Mode): region, ownership_type, housing, health, extras
    # states are bypassed. Re-enable for Detailed Mode when per-item deduction setup
    # is needed.
    start_date     = State()   # employment start date (DD/MM/YYYY or MM/YYYY)
    employer_name  = State()   # optional: employer name for payslips
    caregiver_name = State()   # optional: caregiver name for payslips


# ── Keyboard builders ──────────────────────────────────────────────────────────

def _month_picker_kb(employment_start: date | None = None) -> InlineKeyboardMarkup:
    """
    Quick-select keyboard for the billing month.
    Shows previous, current (pre-selected ✅), and next month as buttons,
    plus a free-entry option for any other period.
    Months before employment_start are hidden from the quick-pick row.
    """
    today = date.today()

    def _add_months(d: date, n: int) -> date:
        m = d.month - 1 + n
        y = d.year + m // 12
        return date(y, m % 12 + 1, 1)

    months = [(_add_months(today, n), n == 0) for n in (-1, 0, 1)]
    builder = InlineKeyboardBuilder()
    shown = 0
    for m, is_current in months:
        if employment_start and (m.year, m.month) < (employment_start.year, employment_start.month):
            continue
        label = f"{config.HEBREW_MONTHS[m.month]} {m.year}"
        if is_current:
            label = f"✅ {label}"
        builder.button(text=label, callback_data=f"month:{m.month}:{m.year}")
        shown += 1
    builder.button(text="📅 חודש אחר…", callback_data="month:other")
    builder.adjust(shown, 1)
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

# ARCHITECTURE NOTE (Detailed Mode — Simple Mode bypass):
# _DEDUCTION_META, _deduction_edit_kb(), _region_kb(), _ownership_kb(), and
# _deductions_kb() are part of the per-item deduction UI infrastructure.
# They are not used in Simple Mode (the bot asks only for agreed net salary and
# cash advances). Re-enable / restore these for Detailed Mode.

def _ownership_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🏠 בבעלות המעסיק", callback_data="ownership:owned")
    builder.button(text="🔑 שכורה",          callback_data="ownership:rented")
    builder.adjust(2)
    return builder.as_markup()


def _keep_btn(display: str, key: str) -> InlineKeyboardMarkup:
    """Single-button keyboard for keeping the current value of a setup field."""
    builder = InlineKeyboardBuilder()
    builder.button(text=f"⚡ השאר {display}", callback_data=f"setup_keep:{key}")
    builder.adjust(1)
    return builder.as_markup()


def _start_date_year_kb(current_year: int | None = None) -> InlineKeyboardMarkup:
    """Year-selection keyboard for employment start date (2019 → current year)."""
    today = date.today()
    years = list(range(2019, today.year + 1))
    builder = InlineKeyboardBuilder()
    for y in years:
        label = f"✅ {y}" if y == current_year else str(y)
        builder.button(text=label, callback_data=f"setup_date:year:{y}")
    builder.button(text="✏️ הזן ידנית", callback_data="setup_date:manual")
    builder.adjust(4)
    return builder.as_markup()


def _start_date_month_kb(year: int, current_month: int | None = None) -> InlineKeyboardMarkup:
    """Month-selection keyboard shown after a year is picked."""
    builder = InlineKeyboardBuilder()
    for m in range(1, 13):
        label = f"✅ {config.HEBREW_MONTHS[m]}" if m == current_month else config.HEBREW_MONTHS[m]
        builder.button(text=label, callback_data=f"setup_date:month:{year}:{m}")
    builder.button(text="🔙 חזרה לשנה", callback_data="setup_date:back_to_year")
    builder.adjust(3, 3, 3, 3, 1)
    return builder.as_markup()


def _start_date_day_kb(
    year: int, month: int, current_day: int | None = None
) -> InlineKeyboardMarkup:
    """Day-of-month keyboard shown after a month is picked (7 days per row)."""
    days_in_month = calendar.monthrange(year, month)[1]
    builder = InlineKeyboardBuilder()
    for d in range(1, days_in_month + 1):
        label = f"✅ {d}" if d == current_day else str(d)
        builder.button(text=label, callback_data=f"setup_date:day:{year}:{month}:{d}")
    builder.button(text="🔙 חזרה לחודש", callback_data=f"setup_date:back_to_month:{year}")
    builder.adjust(7)
    return builder.as_markup()


def _setup_edit_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ עדכן הגדרות", callback_data="setup:edit")
    builder.button(text="✅ הכל תקין",     callback_data="setup:ok")
    builder.adjust(2)
    return builder.as_markup()


def _confirm_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ אשר והפק תלוש", callback_data="confirm:generate")
    builder.button(text="🔄 התחל מחדש",     callback_data="confirm:restart")
    builder.adjust(1)
    return builder.as_markup()



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

async def _run_start_flow(message: Message, state: FSMContext, user_id: int) -> None:
    """
    Core /start logic.  Accepts an explicit user_id so it can be called from
    both the /start command handler (where user_id = message.from_user.id) and
    from callback handlers (where message is a bot-sent Message whose from_user
    is the bot itself, not the real user — callback.from_user.id must be passed
    instead).
    """
    await state.clear()

    saved_data: dict | None = None
    try:
        saved_data = await database.get_user(user_id)
    except Exception:
        log.warning("Firestore get_user failed for %s — continuing without saved data", user_id)

    # Simple Mode: the agreed monthly net salary is the setup gate
    agreed_net_salary = saved_data.get("agreed_net_salary") if saved_data else None
    has_contract = agreed_net_salary is not None

    # Load employment start date for month-picker filtering
    employment_start_iso: str | None = saved_data.get("employment_start_date") if saved_data else None
    employment_start: date | None = None
    if employment_start_iso:
        try:
            employment_start = date.fromisoformat(employment_start_iso)
        except ValueError:
            pass

    await state.update_data(
        saved_data=saved_data,
        user_id=user_id,
        entry_point="start",
        agreed_net_salary=str(agreed_net_salary) if agreed_net_salary is not None else None,
        employment_start_date=employment_start_iso,
    )

    if not has_contract:
        await state.clear()
        await message.answer(
            "👋 *ברוכים הבאים למחולל תלושי השכר!*\n\n"
            "לפני הפקת תלוש ראשון, יש להגדיר את פרטי החוזה.\n\n"
            "הקלד /setup להתחלת ההגדרה.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await message.answer(
        "👋 *ברוכים הבאים למחולל תלושי השכר*\n\n"
        "הבוט מסייע למעסיקי עובדי זר בסיעוד לחשב שכר ולהפיק תלוש שכר רשמי.\n\n"
        "💾 ההגדרות שלך שמורות — לצפייה ועדכון: /setup\n\n"
        "🔒 *פרטיות:* מספרי דרכון ונתוני שכר נמחקים מיד. "
        "שמות ויתרות חופשה/מחלה נשמרים לנוחיותך. "
        "להסרת כל הנתונים: /forget\\_me\n\n"
        "לאיזה חודש להפיק את התלוש?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_month_picker_kb(employment_start),
    )
    await state.set_state(PayslipForm.month_year)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await _run_start_flow(message, state, user_id=message.from_user.id)  # type: ignore[union-attr]


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


# ── /setup command + ContractSetupForm flow ───────────────────────────────────

def _setup_summary(saved: dict) -> str:
    """Build the human-readable settings card shown by /setup."""
    agreed_net = saved.get("agreed_net_salary")
    net_display = f"₪{agreed_net:,.2f}" if agreed_net is not None else "לא הוגדר"

    start_iso = saved.get("employment_start_date")
    if start_iso:
        try:
            d = date.fromisoformat(start_iso)
            start_display = f"{d.day:02d}/{d.month:02d}/{d.year}"
        except ValueError:
            start_display = "לא תקין"
    else:
        start_display = "לא הוגדר"

    employer  = saved.get("employer_name",  _SKIPPED)
    caregiver = saved.get("caregiver_name", _SKIPPED)
    employer_display  = employer  if employer  != _SKIPPED else "לא הוגדר"
    caregiver_display = caregiver if caregiver != _SKIPPED else "לא הוגדר"

    rest_day = saved.get("rest_day", config.DEFAULT_REST_DAY)
    rest_day_display = config.rest_day_hebrew(rest_day)

    return (
        "⚙️ *הגדרות*\n\n"
        f"👤 מעסיק: {employer_display}\n"
        f"👤 מטפל/ת: {caregiver_display}\n"
        f"💰 שכר נטו חודשי מוסכם: {net_display}\n"
        f"📅 תחילת העסקה: {start_display}\n"
        f"🗓️ יום מנוחה שבועי: {rest_day_display}"
    )


@router.message(Command("setup"))
async def cmd_setup(message: Message, state: FSMContext) -> None:
    """Show the settings summary card; let the user edit all fields at once."""
    await state.clear()
    user_id = message.from_user.id  # type: ignore[union-attr]
    saved_data: dict | None = None
    try:
        saved_data = await database.get_user(user_id)
    except Exception:
        pass

    await state.update_data(
        user_id=user_id,
        entry_point="setup_cmd",
        saved_data=saved_data,
    )

    if saved_data and saved_data.get("agreed_net_salary") is not None:
        await message.answer(
            _setup_summary(saved_data),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_setup_edit_kb(),
        )
        await state.set_state(ContractSetupForm.review)
        return

    await _ask_agreed_net_salary(message, state)


@router.callback_query(ContractSetupForm.review, F.data == "setup:edit")
async def handle_setup_edit(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
    await _ask_agreed_net_salary(callback.message, state)  # type: ignore[arg-type]


@router.callback_query(ContractSetupForm.review, F.data == "setup:ok")
async def handle_setup_ok(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("👍")
    await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
    await state.clear()


@router.callback_query(F.data == "setup_done:start")
async def handle_setup_done_start(callback: CallbackQuery, state: FSMContext) -> None:
    """'הפק תלוש עכשיו' button shown after /setup completes — jumps straight into /start.

    Must pass callback.from_user.id explicitly.  callback.message is the message
    the bot sent, so callback.message.from_user is the BOT — using it would cause
    a Firestore lookup on the bot's user_id and falsely show "no contract".
    """
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
    await _run_start_flow(
        callback.message,  # type: ignore[arg-type]
        state,
        user_id=callback.from_user.id,
    )


async def _ask_agreed_net_salary(message: Message, state: FSMContext) -> None:
    """Entry point for ContractSetupForm — ask for the agreed monthly net salary."""
    data = await state.get_data()
    saved: dict = data.get("saved_data") or {}
    current = saved.get("agreed_net_salary")
    current_hint = f" _(נוכחי: ₪{current:,.2f})_" if current is not None else ""
    keep_kb = _keep_btn(f"₪{current:,.2f}", "agreed_net_salary") if current is not None else None
    await state.set_state(ContractSetupForm.agreed_net_salary)
    await message.answer(
        f"📋 *הגדרה קצרה לפני שמתחילים*\n\n"
        "מה *השכר הנטו החודשי המוסכם* — הסכום שהמטפל/ת מקבל/ת ביד לחודש מלא?\n"
        f"_(הזן את הסכום בשקלים, לדוגמה: 5989){current_hint}_\n\n"
        "אפשר לעדכן בכל עת עם /setup.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keep_kb,
    )


@router.message(ContractSetupForm.agreed_net_salary)
async def handle_agreed_net_salary(message: Message, state: FSMContext) -> None:
    val = _parse_decimal(message.text or "")
    if val is None or val <= 0:
        await message.answer("נא להזין סכום חיובי (לדוגמה: 5989).")
        return
    await state.update_data(contract_agreed_net=str(val))
    await _ask_setup_rest_day(message, state)


@router.callback_query(ContractSetupForm.agreed_net_salary, F.data == "setup_keep:agreed_net_salary")
async def keep_agreed_net_salary(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
    data = await state.get_data()
    saved: dict = data.get("saved_data") or {}
    val = Decimal(str(saved.get("agreed_net_salary", "0")))
    await state.update_data(contract_agreed_net=str(val))
    await _ask_setup_rest_day(callback.message, state)  # type: ignore[arg-type]


# ── Setup: rest_day ────────────────────────────────────────────────────────────

def _rest_day_kb(current: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, (label, _) in config.REST_DAY_OPTIONS.items():
        mark = " ✅" if key == current else ""
        builder.button(text=f"{label}{mark}", callback_data=f"setup_rest:{key}")
    builder.adjust(3)
    return builder.as_markup()


async def _ask_setup_rest_day(message: Message, state: FSMContext) -> None:
    await state.set_state(ContractSetupForm.rest_day)
    data = await state.get_data()
    saved: dict = data.get("saved_data") or {}
    current = saved.get("rest_day", config.DEFAULT_REST_DAY)
    await message.answer(
        "🗓️ *מהו יום המנוחה השבועי של המטפל/ת?*\n"
        "_(יום זה לא נספר בחישוב ימי העבודה ומשולם בתוספת בנפרד)_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_rest_day_kb(current),
    )


@router.callback_query(ContractSetupForm.rest_day, F.data.startswith("setup_rest:"))
async def handle_setup_rest_day(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    key = callback.data.split(":")[1]  # type: ignore[union-attr]
    label = config.rest_day_hebrew(key)
    await state.update_data(contract_rest_day=key)
    await callback.message.edit_text(  # type: ignore[union-attr]
        f"🗓️ יום מנוחה: *{label}*", parse_mode=ParseMode.MARKDOWN
    )
    await _ask_setup_start_date(callback.message, state)  # type: ignore[arg-type]


# ARCHITECTURE NOTE (Detailed Mode — currently bypassed in Simple Mode):
# The following region/ownership/housing/health/extras handlers were removed
# as part of the Simple Mode simplification. The FSM states (ContractSetupForm.region,
# .ownership_type, .housing, .health, .extras) and associated keyboards (_region_kb,
# _ownership_kb, _deductions_kb) are also preserved as comments above.
# Re-enable all of the above when implementing Detailed Mode.

_REGION_LABELS: dict[str, str] = {
    "tel_aviv":  "תל אביב",
    "jerusalem": "ירושלים",
    "center":    "מרכז / חיפה",
    "south":     "דרום",
    "north":     "צפון",
}


async def _ask_setup_start_date(message: Message, state: FSMContext) -> None:
    """Show the year-picker keyboard for employment start date."""
    await state.set_state(ContractSetupForm.start_date)
    data = await state.get_data()
    saved: dict = data.get("saved_data") or {}
    current_start = saved.get("employment_start_date")

    current_year: int | None = None
    current_hint = ""
    keep_kb_row: list | None = None

    if current_start:
        try:
            d = date.fromisoformat(current_start)
            current_year = d.year
            current_hint = f" _(נוכחי: {d.day:02d}/{d.month:02d}/{d.year})_"
            keep_kb_row = [("setup_keep:start_date", f"⚡ השאר {d.day:02d}/{d.month:02d}/{d.year}")]
        except ValueError:
            pass

    builder = InlineKeyboardBuilder()
    today = date.today()
    for y in range(2019, today.year + 1):
        label = f"✅ {y}" if y == current_year else str(y)
        builder.button(text=label, callback_data=f"setup_date:year:{y}")
    builder.button(text="✏️ הזן ידנית", callback_data="setup_date:manual")
    if keep_kb_row:
        builder.button(text=keep_kb_row[0][1], callback_data=keep_kb_row[0][0])
    builder.adjust(4)

    await message.answer(
        f"📅 *מתי התחיל/ה המטפל/ת לעבוד?*{current_hint}\n"
        "בחר שנה:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=builder.as_markup(),
    )


@router.callback_query(ContractSetupForm.start_date, F.data.startswith("setup_date:year:"))
async def handle_setup_year_pick(callback: CallbackQuery, state: FSMContext) -> None:
    year = int(callback.data.split(":")[2])
    await callback.answer()
    # Check if we already have a saved start date to pre-select the month
    data = await state.get_data()
    saved: dict = data.get("saved_data") or {}
    current_start = saved.get("employment_start_date")
    current_month: int | None = None
    if current_start:
        try:
            d = date.fromisoformat(current_start)
            if d.year == year:
                current_month = d.month
        except ValueError:
            pass
    await callback.message.edit_text(  # type: ignore[union-attr]
        f"📅 שנה: *{year}* — בחר חודש:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_start_date_month_kb(year, current_month),
    )


@router.callback_query(ContractSetupForm.start_date, F.data.startswith("setup_date:month:"))
async def handle_setup_month_pick(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")   # "setup_date:month:YYYY:M"
    year, month = int(parts[2]), int(parts[3])
    await callback.answer()
    month_name = config.HEBREW_MONTHS[month]
    data = await state.get_data()
    saved: dict = data.get("saved_data") or {}
    current_start = saved.get("employment_start_date")
    current_day: int | None = None
    if current_start:
        try:
            d = date.fromisoformat(current_start)
            if d.year == year and d.month == month:
                current_day = d.day
        except ValueError:
            pass
    await callback.message.edit_text(  # type: ignore[union-attr]
        f"📅 *{month_name} {year}* — בחר יום:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_start_date_day_kb(year, month, current_day),
    )


@router.callback_query(ContractSetupForm.start_date, F.data.startswith("setup_date:day:"))
async def handle_setup_day_pick(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")   # "setup_date:day:YYYY:M:D"
    year, month, day = int(parts[2]), int(parts[3]), int(parts[4])
    try:
        emp_start = date(year, month, day)
    except ValueError:
        await callback.answer("תאריך לא תקין", show_alert=True)
        return
    if emp_start > date.today():
        await callback.answer("⚠️ תאריך לא יכול להיות בעתיד", show_alert=True)
        return
    await callback.answer()
    month_name = config.HEBREW_MONTHS[month]
    await callback.message.edit_text(  # type: ignore[union-attr]
        f"📅 תחילת העסקה: *{day:02d} {month_name} {year}*",
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.update_data(contract_start_date=emp_start.isoformat())
    await _ask_setup_employer_name(callback.message, state)  # type: ignore[arg-type]


@router.callback_query(ContractSetupForm.start_date, F.data.startswith("setup_date:back_to_month:"))
async def handle_setup_back_to_month(callback: CallbackQuery, state: FSMContext) -> None:
    year = int(callback.data.split(":")[2])
    await callback.answer()
    data = await state.get_data()
    saved: dict = data.get("saved_data") or {}
    current_start = saved.get("employment_start_date")
    current_month: int | None = None
    if current_start:
        try:
            d = date.fromisoformat(current_start)
            if d.year == year:
                current_month = d.month
        except ValueError:
            pass
    await callback.message.edit_text(  # type: ignore[union-attr]
        f"📅 שנה: *{year}* — בחר חודש:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_start_date_month_kb(year, current_month),
    )


@router.callback_query(ContractSetupForm.start_date, F.data == "setup_date:back_to_year")
async def handle_setup_back_to_year(callback: CallbackQuery, state: FSMContext) -> None:
    """Return from month picker to year picker."""
    await callback.answer()
    data = await state.get_data()
    saved: dict = data.get("saved_data") or {}
    current_start = saved.get("employment_start_date")
    current_year: int | None = None
    if current_start:
        try:
            current_year = date.fromisoformat(current_start).year
        except ValueError:
            pass
    await callback.message.edit_text(  # type: ignore[union-attr]
        "📅 *מתי התחיל/ה המטפל/ת לעבוד?*\nבחר שנה:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_start_date_year_kb(current_year),
    )


@router.callback_query(ContractSetupForm.start_date, F.data == "setup_date:manual")
async def handle_setup_date_manual(callback: CallbackQuery, state: FSMContext) -> None:
    """Switch to free-text input for employment start date."""
    await callback.answer()
    await callback.message.edit_text(  # type: ignore[union-attr]
        "📅 הזן תאריך — לדוגמה: 15/03/2025 או 03/2025"
    )


@router.message(ContractSetupForm.start_date)
async def handle_contract_start_date(message: Message, state: FSMContext) -> None:
    """Parse employment start date (DD/MM/YYYY or MM/YYYY), persist all setup data."""
    text = (message.text or "").strip()

    # Forgiving date parser — tries DD/MM/YYYY variants first, then MM/YYYY (day=1)
    sep = r"[/\-.]"
    emp_start: date | None = None
    # DD[sep]MM[sep]YY or DD[sep]MM[sep]YYYY
    m_full = re.fullmatch(rf"(\d{{1,2}}){sep}(\d{{1,2}}){sep}(\d{{2,4}})", text)
    if m_full:
        d_raw, mo_raw, y_raw = m_full.group(1), m_full.group(2), m_full.group(3)
        year = int(y_raw) if len(y_raw) == 4 else 2000 + int(y_raw)
        try:
            emp_start = date(year, int(mo_raw), int(d_raw))
        except ValueError:
            pass
    # MM[sep]YYYY (day defaults to 1)
    if emp_start is None:
        m_month = re.fullmatch(rf"(\d{{1,2}}){sep}(\d{{4}})", text)
        if m_month:
            try:
                emp_start = date(int(m_month.group(2)), int(m_month.group(1)), 1)
            except ValueError:
                pass

    if emp_start is None:
        await message.answer(
            "לא הצלחתי לקרוא את התאריך. נסה שוב, למשל: 01/05/2026"
        )
        return

    if emp_start > date.today():
        await message.answer("⚠️ תאריך תחילת העסקה לא יכול להיות בעתיד.")
        return

    await state.update_data(contract_start_date=emp_start.isoformat())
    await _ask_setup_employer_name(message, state)


async def _complete_setup(message: Message, state: FSMContext, emp_start: date) -> None:
    """Persist all collected setup data and finish the wizard."""
    employment_start_iso = emp_start.isoformat()
    data = await state.get_data()
    agreed_net_salary = Decimal(data["contract_agreed_net"])
    user_id: int = data["user_id"]
    entry_point: str = data.get("entry_point", "start")

    employer_name  = data.get("setup_employer_name",  _SKIPPED) or _SKIPPED
    caregiver_name = data.get("setup_caregiver_name", _SKIPPED) or _SKIPPED

    rest_day: str = data.get("contract_rest_day") or config.DEFAULT_REST_DAY

    try:
        await database.upsert_contract(
            user_id,
            agreed_net_salary=agreed_net_salary,
            employment_start_date=employment_start_iso,
            rest_day=rest_day,
        )
    except Exception as exc:
        log.warning("upsert_contract failed: %s", exc)

    if employer_name != _SKIPPED or caregiver_name != _SKIPPED:
        try:
            await database.upsert_user(user_id, employer_name, caregiver_name)
        except Exception as exc:
            log.warning("upsert_user (setup) failed: %s", exc)

    await state.update_data(
        agreed_net_salary=str(agreed_net_salary),
        employment_start_date=employment_start_iso,
        rest_day=rest_day,
    )

    if entry_point == "setup_cmd":
        # Build summary from the values we just saved (before clearing state)
        summary_data: dict = {
            "agreed_net_salary":     float(agreed_net_salary),
            "employment_start_date": employment_start_iso,
            "employer_name":         employer_name,
            "caregiver_name":        caregiver_name,
            "rest_day":              rest_day,
        }
        await state.clear()
        await message.answer(
            "✅ *ההגדרות עודכנו!*\n\n" + _setup_summary(summary_data),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📄 הפק תלוש עכשיו", callback_data="setup_done:start"),
            ]]),
        )
        return

    # entry_point == "start" — continue to payslip flow
    await message.answer(
        "✅ ההגדרות נשמרו!\n\n"
        "👋 *ברוכים הבאים למחולל תלושי השכר*\n\n"
        "הבוט מסייע למעסיקי עובדי זר בסיעוד לחשב שכר ולהפיק תלוש שכר רשמי.\n\n"
        "🔒 *פרטיות:* מספרי דרכון ונתוני שכר נמחקים מיד. "
        "שמות ויתרות חופשה/מחלה נשמרים לנוחיותך. "
        "להסרת כל הנתונים: /forget\\_me\n\n"
        "לאיזה חודש להפיק את התלוש?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_month_picker_kb(emp_start),
    )
    await state.set_state(PayslipForm.month_year)


# ── ContractSetupForm — "keep current value" quick-tap callbacks ──────────────


@router.callback_query(ContractSetupForm.start_date, F.data == "setup_keep:start_date")
async def keep_start_date(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
    data = await state.get_data()
    saved: dict = data.get("saved_data") or {}
    start_iso = saved.get("employment_start_date", "")
    await state.update_data(contract_start_date=start_iso)
    await _ask_setup_employer_name(callback.message, state)  # type: ignore[arg-type]


# ── ContractSetupForm — employer / caregiver name steps ───────────────────────

def _setup_name_kb(current: str | None, key: str) -> InlineKeyboardMarkup:
    """
    Keyboard for optional name fields in setup.
    Shows a keep button if a value is already stored, plus a skip button.
    """
    builder = InlineKeyboardBuilder()
    if current and current != _SKIPPED:
        builder.button(text=f"⚡ השאר \"{current}\"", callback_data=f"setup_keep:{key}")
    builder.button(text="⏭️ דלג (השאר ריק)", callback_data=f"setup_skip:{key}")
    builder.adjust(1)
    return builder.as_markup()


async def _ask_setup_employer_name(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    saved: dict = data.get("saved_data") or {}
    current = saved.get("employer_name", _SKIPPED)
    current_hint = f" _(נוכחי: {current})_" if current and current != _SKIPPED else ""
    await state.set_state(ContractSetupForm.employer_name)
    await message.answer(
        f"👤 *שם המעסיק*{current_hint}\n"
        "_(שם שיופיע בתלוש — ניתן לדלג אם לא רלוונטי)_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_setup_name_kb(current if current != _SKIPPED else None, "employer_name"),
    )


@router.message(ContractSetupForm.employer_name)
async def handle_setup_employer_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("נא להזין שם, או לחץ על אחד הכפתורים למטה.")
        return
    await state.update_data(setup_employer_name=name)
    await _ask_setup_caregiver_name(message, state)


@router.callback_query(ContractSetupForm.employer_name, F.data == "setup_keep:employer_name")
async def keep_setup_employer_name(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
    data = await state.get_data()
    saved: dict = data.get("saved_data") or {}
    name = saved.get("employer_name", _SKIPPED)
    await state.update_data(setup_employer_name=name)
    await _ask_setup_caregiver_name(callback.message, state)  # type: ignore[arg-type]


@router.callback_query(ContractSetupForm.employer_name, F.data == "setup_skip:employer_name")
async def skip_setup_employer_name(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
    await state.update_data(setup_employer_name=_SKIPPED)
    await _ask_setup_caregiver_name(callback.message, state)  # type: ignore[arg-type]


async def _ask_setup_caregiver_name(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    saved: dict = data.get("saved_data") or {}
    current = saved.get("caregiver_name", _SKIPPED)
    current_hint = f" _(נוכחי: {current})_" if current and current != _SKIPPED else ""
    await state.set_state(ContractSetupForm.caregiver_name)
    await message.answer(
        f"👤 *שם המטפל/ת*{current_hint}\n"
        "_(שם שיופיע בתלוש — ניתן לדלג אם לא רלוונטי)_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_setup_name_kb(current if current != _SKIPPED else None, "caregiver_name"),
    )


@router.message(ContractSetupForm.caregiver_name)
async def handle_setup_caregiver_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("נא להזין שם, או לחץ על אחד הכפתורים למטה.")
        return
    await state.update_data(setup_caregiver_name=name)
    data = await state.get_data()
    emp_start = date.fromisoformat(data["contract_start_date"])
    await _complete_setup(message, state, emp_start)


@router.callback_query(ContractSetupForm.caregiver_name, F.data == "setup_keep:caregiver_name")
async def keep_setup_caregiver_name(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
    data = await state.get_data()
    saved: dict = data.get("saved_data") or {}
    name = saved.get("caregiver_name", _SKIPPED)
    await state.update_data(setup_caregiver_name=name)
    emp_start = date.fromisoformat(data["contract_start_date"])
    await _complete_setup(callback.message, state, emp_start)  # type: ignore[arg-type]


@router.callback_query(ContractSetupForm.caregiver_name, F.data == "setup_skip:caregiver_name")
async def skip_setup_caregiver_name(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
    await state.update_data(setup_caregiver_name=_SKIPPED)
    data = await state.get_data()
    emp_start = date.fromisoformat(data["contract_start_date"])
    await _complete_setup(callback.message, state, emp_start)  # type: ignore[arg-type]


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


@router.callback_query(PayslipForm.month_year, F.data.startswith("month:early_confirm:"))
async def handle_early_month_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """User confirmed proceeding with a month that precedes employment start."""
    await callback.answer()
    parts = callback.data.split(":")   # "month:early_confirm:{m}:{y}"
    month, year = int(parts[2]), int(parts[3])
    await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
    await _confirm_month_and_proceed(callback.message, state, month, year, edit=False)  # type: ignore[arg-type]


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

    # Soft warning if month is before employment start
    data = await state.get_data()
    start_iso = data.get("employment_start_date")
    if start_iso:
        try:
            emp_start = date.fromisoformat(start_iso)
            if (year, month) < (emp_start.year, emp_start.month):
                builder = InlineKeyboardBuilder()
                builder.button(text="כן, המשך ➡️", callback_data=f"month:early_confirm:{month}:{year}")
                builder.button(text="🔙 בחר חודש אחר", callback_data="month:other")
                builder.adjust(2)
                await message.answer(
                    f"⚠️ החודש שנבחר ({month:02d}/{year}) הוא לפני תחילת ההעסקה "
                    f"({emp_start.month:02d}/{emp_start.year}). ממשיכים בכל זאת?",
                    reply_markup=builder.as_markup(),
                )
                return
        except ValueError:
            pass

    await _confirm_month_and_proceed(message, state, month, year, edit=False)


async def _confirm_month_and_proceed(
    message: Message, state: FSMContext, month: int, year: int, *, edit: bool
) -> None:
    """
    Save month/year and advance.

    If the selected month is the employment start month and the worker started
    after day 1, auto-fill the partial-month details and skip the work_period /
    partial_day questions entirely.
    """
    await state.update_data(month=month, year=year)
    month_name = config.HEBREW_MONTHS[month]

    # Auto-detect first partial month from employment start date
    data = await state.get_data()
    saved: dict = data.get("saved_data") or {}
    _rest_day_key = saved.get("rest_day") or data.get("rest_day") or config.DEFAULT_REST_DAY
    _rest_weekday = config.rest_day_weekday(_rest_day_key)

    start_iso = data.get("employment_start_date")
    if start_iso:
        try:
            emp_start = date.fromisoformat(start_iso)
            if emp_start.year == year and emp_start.month == month and emp_start.day > 1:
                active_days, days_worked = calculate_partial_days(
                    "started", emp_start.day, month, year, _rest_weekday
                )
                await state.update_data(
                    partial_type="started",
                    days_worked=days_worked,
                    active_days=active_days,
                )
                days_in_month = calendar.monthrange(year, month)[1]
                auto_text = (
                    f"📅 חודש: *{month_name} {year}*\n\n"
                    f"✅ חודש חלקי — המטפל/ת התחיל/ה ב-{emp_start.day:02d}/{month:02d}/{year}.\n"
                    f"{active_days} ימים פעילים מתוך {days_in_month} = *{days_worked} ימי עבודה* מחושבים."
                )
                if edit:
                    try:
                        await message.edit_text(auto_text, parse_mode=ParseMode.MARKDOWN)  # type: ignore[union-attr]
                    except TelegramBadRequest:
                        pass
                else:
                    await message.answer(auto_text, parse_mode=ParseMode.MARKDOWN)
                await _ask_details(message, state)
                return
        except ValueError:
            pass

    confirm_text = f"📅 חודש: *{month_name} {year}*\n\nהאם העובד/ת עבד/ה חודש מלא?"
    if edit:
        try:
            await message.edit_text(confirm_text, parse_mode=ParseMode.MARKDOWN,  # type: ignore[union-attr]
                                    reply_markup=_work_period_kb())
        except TelegramBadRequest:
            pass  # message already has this content (stale callback replay) — ignore
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
        await _ask_details(callback.message, state)  # type: ignore[arg-type]
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
    saved_d: dict = data.get("saved_data") or {}
    _rdk = saved_d.get("rest_day") or data.get("rest_day") or config.DEFAULT_REST_DAY
    active_days, days_worked = calculate_partial_days(ptype, val, month, year, config.rest_day_weekday(_rdk))

    await state.update_data(days_worked=days_worked, active_days=active_days)
    month_name = config.HEBREW_MONTHS[month]
    await message.answer(
        f"✅ *{active_days} ימים פעילים* מתוך {days_in_month} ימי חודש {month_name} "
        f"= *{days_worked} ימי עבודה* מחושבים מתוך 26.",
        parse_mode=ParseMode.MARKDOWN,
    )
    await _ask_details(message, state)


# ── Fast-track: confirm saved details in one step ──────────────────────────────

async def _ask_details(message: Message, state: FSMContext) -> None:
    """
    If both employer_name and caregiver_name are saved, show a single confirmation
    prompt so the user can skip all three sequential questions in one tap.
    Falls back to the sequential flow if either name is missing.
    """
    data = await state.get_data()
    saved: dict = data.get("saved_data") or {}
    employer = saved.get("employer_name", _SKIPPED)
    caregiver = saved.get("caregiver_name", _SKIPPED)

    if employer and employer != _SKIPPED and caregiver and caregiver != _SKIPPED:
        await state.set_state(PayslipForm.details_confirm)
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ כן, המשך", callback_data="details:use_saved")
        builder.button(text="✏️ עריכת פרטים", callback_data="details:edit")
        builder.adjust(1)
        await message.answer(
            f"📋 *פרטים שמורים במערכת:*\n"
            f"מעסיק: {employer}\n"
            f"מטפל/ת: {caregiver}\n\n"
            "האם להשתמש בפרטים אלו לתלוש הנוכחי?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=builder.as_markup(),
        )
    else:
        await _ask_employer_name(message, state)


@router.callback_query(PayslipForm.details_confirm, F.data == "details:use_saved")
async def handle_details_use_saved(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    saved: dict = data.get("saved_data") or {}
    employer = saved.get("employer_name", _SKIPPED)
    caregiver = saved.get("caregiver_name", _SKIPPED)
    await state.update_data(
        employer_name=employer,
        caregiver_name=caregiver,
        passport=_SKIPPED,  # passport is never stored (zero-data retention)
    )
    await callback.message.edit_text(  # type: ignore[union-attr]
        f"📋 מעסיק: *{employer}* | מטפל/ת: *{caregiver}*",
        parse_mode=ParseMode.MARKDOWN,
    )
    await _ask_shabbat_days(callback.message, state)  # type: ignore[arg-type]


@router.callback_query(PayslipForm.details_confirm, F.data == "details:edit")
async def handle_details_edit(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
    except TelegramBadRequest:
        pass
    await _ask_employer_name(callback.message, state)  # type: ignore[arg-type]


# ── State: employer_name ───────────────────────────────────────────────────────

async def _ask_employer_name(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    saved: dict = data.get("saved_data") or {}
    await state.set_state(PayslipForm.employer_name)

    saved_employer = saved.get("employer_name", _SKIPPED)
    builder = InlineKeyboardBuilder()
    if saved_employer and saved_employer != _SKIPPED:
        builder.button(text=f'✅ "{saved_employer}"', callback_data="employer_use_saved")
    builder.button(text="⏭️ ללא שם מעסיק", callback_data="skip_field")
    builder.adjust(1)

    hint = (
        f"\n_(שמור: \"{saved_employer}\" — לחץ לשימוש, או הקלד שם אחר)_"
        if saved_employer and saved_employer != _SKIPPED else ""
    )
    await message.answer(
        f"👤 *שם המעסיק לתלוש?*{hint}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=builder.as_markup(),
    )


@router.callback_query(PayslipForm.employer_name, F.data == "employer_use_saved")
async def handle_employer_use_saved(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    saved: dict = data.get("saved_data") or {}
    name = saved.get("employer_name", _SKIPPED)
    await state.update_data(employer_name=name)
    await callback.message.edit_text(f"👤 מעסיק: *{name}*", parse_mode=ParseMode.MARKDOWN)  # type: ignore[union-attr]
    await _ask_caregiver_name(callback.message, state)  # type: ignore[arg-type]


@router.callback_query(PayslipForm.employer_name, F.data == "skip_field")
async def skip_employer_name(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(employer_name=_SKIPPED)
    await callback.message.edit_text("👤 מעסיק: לא יצוין בתלוש.")  # type: ignore[union-attr]
    await _ask_caregiver_name(callback.message, state)  # type: ignore[arg-type]


@router.message(PayslipForm.employer_name)
async def handle_employer_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if len(name) < 2:
        await _invalid(message, "נא להזין שם תקין.")
        return
    await state.update_data(employer_name=name)
    await _ask_caregiver_name(message, state)


# ── State: caregiver_name ──────────────────────────────────────────────────────

async def _ask_caregiver_name(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    saved: dict = data.get("saved_data") or {}
    await state.set_state(PayslipForm.caregiver_name)

    saved_caregiver = saved.get("caregiver_name", _SKIPPED)
    builder = InlineKeyboardBuilder()
    if saved_caregiver and saved_caregiver != _SKIPPED:
        builder.button(text=f'✅ "{saved_caregiver}"', callback_data="caregiver_use_saved")
    builder.button(text="⏭️ ללא שם מטפל/ת", callback_data="skip_field")
    builder.adjust(1)

    hint = (
        f"\n_(שמור: \"{saved_caregiver}\" — לחץ לשימוש, או הקלד שם אחר)_"
        if saved_caregiver and saved_caregiver != _SKIPPED else ""
    )
    await message.answer(
        f"👤 *שם המטפל/ת לתלוש?*{hint}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=builder.as_markup(),
    )


@router.callback_query(PayslipForm.caregiver_name, F.data == "caregiver_use_saved")
async def handle_caregiver_use_saved(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    saved: dict = data.get("saved_data") or {}
    name = saved.get("caregiver_name", _SKIPPED)
    await state.update_data(caregiver_name=name)
    await callback.message.edit_text(f"👤 מטפל/ת: *{name}*", parse_mode=ParseMode.MARKDOWN)  # type: ignore[union-attr]
    await _ask_passport(callback.message, state)  # type: ignore[arg-type]


@router.callback_query(PayslipForm.caregiver_name, F.data == "skip_field")
async def skip_caregiver_name(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(caregiver_name=_SKIPPED)
    await callback.message.edit_text("👤 מטפל/ת: לא יצוין בתלוש.")  # type: ignore[union-attr]
    await _ask_passport(callback.message, state)  # type: ignore[arg-type]


@router.message(PayslipForm.caregiver_name)
async def handle_caregiver_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if len(name) < 2:
        await _invalid(message, "נא להזין שם תקין.")
        return
    await state.update_data(caregiver_name=name)
    await _ask_passport(message, state)


# ── State: passport ────────────────────────────────────────────────────────────

async def _ask_passport(message: Message, state: FSMContext) -> None:
    await state.set_state(PayslipForm.passport)
    await message.answer("מהו מספר הדרכון של המטפל/ת?", reply_markup=_skip_kb())


@router.callback_query(PayslipForm.passport, F.data == "skip_field")
async def skip_passport(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(passport=_SKIPPED)
    await callback.message.edit_text("⏭️ מספר דרכון — לא הוזן.")  # type: ignore[union-attr]
    await _ask_shabbat_days(callback.message, state)  # type: ignore[arg-type]


@router.message(PayslipForm.passport)
async def handle_passport(message: Message, state: FSMContext) -> None:
    passport = (message.text or "").strip()
    if len(passport) < 3:
        await _invalid(message, "נא להזין מספר דרכון תקין.")
        return
    await state.update_data(passport=passport)
    await _ask_shabbat_days(message, state)


# ── State: shabbat_days ────────────────────────────────────────────────────────

def _zero_kb(callback_data: str, label: str = "✅ לא עבד/ה (0)") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=label, callback_data=callback_data)
    return builder.as_markup()


async def _ask_shabbat_days(message: Message, state: FSMContext) -> None:
    await state.set_state(PayslipForm.shabbat_days)
    data = await state.get_data()
    saved_s: dict = data.get("saved_data") or {}
    _rdk = saved_s.get("rest_day") or data.get("rest_day") or config.DEFAULT_REST_DAY
    rest_label = config.rest_day_hebrew(_rdk)
    await message.answer(
        f"כמה ימי *{rest_label}* עבד/ה המטפל/ת החודש?\n_(שכר יום מנוחה מתווסף לשכר היסוד)_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_zero_kb("shabbat:zero"),
    )


@router.callback_query(PayslipForm.shabbat_days, F.data == "shabbat:zero")
async def handle_shabbat_zero(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
    except TelegramBadRequest:
        pass
    await state.update_data(shabbat_days="0")
    await _ask_holiday_days(callback.message, state)  # type: ignore[arg-type]


@router.message(PayslipForm.shabbat_days)
async def handle_shabbat_days(message: Message, state: FSMContext) -> None:
    val = (message.text or "").strip()
    if not val.isdigit() or int(val) < 0:
        await _invalid(message, "נא להזין מספר שלם חיובי (לדוגמה: 0, 1, 2).")
        return
    await state.update_data(shabbat_days=val)
    await _ask_holiday_days(message, state)


# ── State: holiday_days ────────────────────────────────────────────────────────

async def _ask_holiday_days(message: Message, state: FSMContext) -> None:
    await state.set_state(PayslipForm.holiday_days)
    await message.answer(
        "כמה *ימי חג* עבד/ה המטפל/ת החודש?\n_(שכר חג מתווסף לשכר היסוד)_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_zero_kb("holiday:zero"),
    )


@router.callback_query(PayslipForm.holiday_days, F.data == "holiday:zero")
async def handle_holiday_zero(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
    except TelegramBadRequest:
        pass
    await state.update_data(holiday_days="0")
    await _ask_advances(callback.message, state)  # type: ignore[arg-type]


@router.message(PayslipForm.holiday_days)
async def handle_holiday_days(message: Message, state: FSMContext) -> None:
    val = (message.text or "").strip()
    if not val.isdigit() or int(val) < 0:
        await _invalid(message, "נא להזין מספר שלם חיובי (לדוגמה: 0, 1, 2).")
        return
    await state.update_data(holiday_days=val)
    await _ask_advances(message, state)


# ARCHITECTURE NOTE (Simple Mode — Detailed Mode bypass):
# pocket_money_weeks question is skipped.
# Re-enable when implementing Detailed Mode.


async def _ask_advances(message: Message, state: FSMContext) -> None:
    await state.set_state(PayslipForm.advances)
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ לא שולמו מקדמות (0)", callback_data="advances:zero")
    await message.answer(
        "האם שולמו *מקדמות או דמי כיס במזומן* במהלך החודש?\n"
        "_(הזן סכום בשקלים, או לחץ על הכפתור אם לא שולם כלום)_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=builder.as_markup(),
    )


# ── State: advances ────────────────────────────────────────────────────────────

@router.callback_query(PayslipForm.advances, F.data == "advances:zero")
async def handle_advances_zero(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
    except TelegramBadRequest:
        pass
    await state.update_data(advances="0")
    await _show_confirm(callback.message, state)  # type: ignore[arg-type]


@router.message(PayslipForm.advances)
async def handle_advances(message: Message, state: FSMContext) -> None:
    val = _parse_decimal(message.text or "")
    if val is None or val < 0:
        await _invalid(message, "נא להזין סכום חיובי (לדוגמה: 200).")
        return
    await state.update_data(advances=str(val))
    # ARCHITECTURE NOTE (Simple Mode): deductions multi-select is bypassed.
    # Advances are the only deduction collected. Re-enable deductions screen
    # for Detailed Mode.
    await _show_confirm(message, state)


# ARCHITECTURE NOTE (Detailed Mode — Simple Mode bypass):
# The deductions multi-select flow (PayslipForm.deductions, PayslipForm.deduction_edit)
# and all associated handlers (handle_deductions, _max_for_key, handle_deduction_edit_cb,
# _finish_deduction_edit, handle_deduction_prorata_pick, handle_deduction_editpick,
# handle_deduction_editcancel, handle_deduction_edit_text) have been removed for Simple Mode.
# In Simple Mode the user is only asked for cash advances (PayslipForm.advances),
# which then goes directly to _show_confirm().
# Re-enable all of the above for Detailed Mode.


# ── Helpers: build input, show confirm ────────────────────────────────────────

def _build_payslip_input(data: dict) -> PayslipInput:
    """Assemble PayslipInput from accumulated FSM state data."""
    return PayslipInput(
        month=data["month"],
        year=data["year"],
        is_full_month=data["days_worked"] == 26,
        days_worked=data["days_worked"],
        employer_name=data.get("employer_name", _SKIPPED),
        caregiver_name=data.get("caregiver_name", _SKIPPED),
        passport_number=data.get("passport", _SKIPPED),
        shabbat_days=int(data.get("shabbat_days") or 0),
        holiday_days=int(data.get("holiday_days") or 0),
        # ARCHITECTURE NOTE (Simple Mode): pocket_money_weeks and individual
        # deductions are bypassed — set to 0. Re-enable for Detailed Mode.
        pocket_money_weeks=0,
        deduction_housing=Decimal("0"),
        deduction_health=Decimal("0"),
        deduction_extras=Decimal("0"),
        deduction_food=Decimal("0"),
        advances=Decimal(data.get("advances", "0")),
        # net_salary_override: agreed monthly net salary from /setup.
        # Causes calculator to use this as the pro-rata basis instead of legal min wage.
        net_salary_override=Decimal(data.get("agreed_net_salary") or "0"),
        # active_days: calendar days employed — used for social-benefit accrual ratio.
        # 0 for full months (calculator uses ratio=1.0); set by partial-month handlers.
        active_days=int(data.get("active_days") or 0),
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

    lines.append("💰 *שכר:*")
    lines.append(f"  שכר יסוד: ₪{result.gross_base:,.2f}")
    if result.shabbat_addition > 0:
        _rdk2 = (data.get("saved_data") or {}).get("rest_day") or data.get("rest_day") or config.DEFAULT_REST_DAY
        rest_label = config.rest_day_hebrew(_rdk2)
        lines.append(f"  תוספת {rest_label} ({result.shabbat_days} ימים): ₪{result.shabbat_addition:,.2f}")
    if result.holiday_addition > 0:
        lines.append(f"  תוספת חג ({result.holiday_days} ימים): ₪{result.holiday_addition:,.2f}")
    if result.shabbat_addition > 0 or result.holiday_addition > 0:
        lines.append(f"  *סה״כ שכר: ₪{result.total_gross:,.2f}*")

    if result.advances > 0:
        lines.append(f"\n➖ *ניכויים:*")
        lines.append(f"  מקדמות: ₪{result.advances:,.2f}")
        lines.append(f"  *סה״כ ניכויים: ₪{result.total_deductions:,.2f}*\n")

    lines.append(f"\n💳 *יתרה לתשלום בהעברה: ₪{result.total_net_pay:,.2f}*")

    lines.append(f"\n📅 *צבירה לתלוש זה בלבד:*")
    lines.append(f"  חופשה: +{result.vacation_accrued:.2f} ימים")
    lines.append(f"  מחלה: +{result.sick_accrued:.2f} ימים")

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
    try:
        await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
    except TelegramBadRequest:
        pass  # buttons already gone — safe to continue

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
        database.upsert_month_accrual(
            user_id,
            f"{result.year}-{result.month:02d}",
            result.vacation_accrued,
            result.sick_accrued,
        )
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
        vac, sick = await database.get_balances(user_id)
        if vac or sick:
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
