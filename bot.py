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


class ContractSetupForm(StatesGroup):
    """One-time (or /setup-triggered) flow to collect persistent caregiver/contract config."""
    review         = State()   # /setup only: show current values + confirm update
    region         = State()   # inline: region of residence
    ownership_type = State()   # inline: employer-owned vs rented
    housing        = State()
    health         = State()
    extras         = State()
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

# Fixed deduction keys, labels, and their legal config maxima.
# Ordered as they appear in the conversation.
_DEDUCTION_META: dict[str, tuple[str, Decimal]] = {
    "housing": ("מגורים",        config.DEDUCTION_HOUSING_MAX),
    "health":  ("ביטוח רפואי",   config.DEDUCTION_HEALTH_MAX),
    "extras":  ("הוצאות נלוות",  config.DEDUCTION_EXTRAS_MAX),
}


def _deduction_edit_kb(
    max_amount: Decimal, prorata_amount: Decimal | None = None
) -> InlineKeyboardMarkup:
    """Keyboard shown while editing a single deduction amount in-place."""
    builder = InlineKeyboardBuilder()
    if prorata_amount is not None:
        builder.button(
            text=f"⚡ השתמש בסכום היחסי: ₪{prorata_amount:,.2f}",
            callback_data="ded_editpick:prorata",
        )
    builder.button(
        text=f"✅ ₪{max_amount:,.2f} (מקסימום מותר)",
        callback_data=f"ded_editpick:{max_amount}",
    )
    builder.button(text="↩️ ביטול", callback_data="ded_editcancel")
    builder.adjust(1)
    return builder.as_markup()


def _region_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="תל אביב",          callback_data="region:tel_aviv")
    builder.button(text="ירושלים",           callback_data="region:jerusalem")
    builder.button(text="מרכז / חיפה",      callback_data="region:center")
    builder.button(text="דרום",             callback_data="region:south")
    builder.button(text="צפון",             callback_data="region:north")
    builder.adjust(2, 2, 1)
    return builder.as_markup()


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

    base_housing = saved_data.get("base_housing") if saved_data else None
    base_health  = saved_data.get("base_health")  if saved_data else None
    base_extras  = saved_data.get("base_extras")  if saved_data else None
    has_contract = base_housing is not None

    # Reconstruct the regional housing cap for use in handle_advances / deduction edit
    contract_region    = saved_data.get("contract_region")    if saved_data else None
    contract_ownership = saved_data.get("contract_ownership") if saved_data else None
    if contract_region and contract_ownership:
        caps = config.HOUSING_CAPS_OWNED if contract_ownership == "owned" else config.HOUSING_CAPS_RENTED
        housing_cap = str(caps.get(contract_region, config.DEDUCTION_HOUSING_MAX))
    else:
        housing_cap = str(config.DEDUCTION_HOUSING_MAX)

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
        base_housing=base_housing,
        base_health=base_health,
        base_extras=base_extras,
        housing_cap=housing_cap,
        employment_start_date=employment_start_iso,
    )

    if not has_contract:
        await _ask_contract_housing(message, state)
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
    region_key      = saved.get("contract_region", "")
    ownership       = saved.get("contract_ownership", "")
    region_label    = _REGION_LABELS.get(region_key, "לא הוגדר")
    ownership_label = (
        "בבעלות המעסיק" if ownership == "owned" else
        "שכורה"          if ownership == "rented" else "לא הוגדר"
    )
    caps = config.HOUSING_CAPS_OWNED if ownership == "owned" else config.HOUSING_CAPS_RENTED
    cap  = caps.get(region_key)
    cap_str = f" _(תקרה: ₪{cap:,.2f})_" if cap else ""

    b_housing = saved.get("base_housing")
    b_health  = saved.get("base_health")
    b_extras  = saved.get("base_extras")
    if b_housing is not None:
        deductions_lines = (
            f"   🏠 דיור: ₪{b_housing:.2f}\n"
            f"   🏥 ביטוח רפואי: ₪{b_health:.2f}\n"
            f"   📦 ציוד: ₪{b_extras:.2f}"
        )
    else:
        deductions_lines = "   לא הוגדרו"

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

    return (
        "⚙️ *הגדרות*\n\n"
        f"👤 מעסיק: {employer_display}\n"
        f"👤 מטפל/ת: {caregiver_display}\n"
        f"📍 אזור: {region_label}\n"
        f"🏠 דירה: {ownership_label}{cap_str}\n"
        "💰 ניכויים לחודש מלא:\n"
        f"{deductions_lines}\n"
        f"📅 תחילת העסקה: {start_display}"
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
        base_housing=None,
        base_health=None,
        base_extras=None,
    )

    if saved_data and saved_data.get("base_housing") is not None:
        await message.answer(
            _setup_summary(saved_data),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_setup_edit_kb(),
        )
        await state.set_state(ContractSetupForm.review)
        return

    await _ask_contract_housing(message, state)


@router.callback_query(ContractSetupForm.review, F.data == "setup:edit")
async def handle_setup_edit(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
    await _ask_contract_housing(callback.message, state)  # type: ignore[arg-type]


@router.callback_query(ContractSetupForm.review, F.data == "setup:ok")
async def handle_setup_ok(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("👍")
    await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
    await state.clear()


async def _ask_contract_housing(message: Message, state: FSMContext) -> None:
    """Entry point for ContractSetupForm — first ask the region."""
    data = await state.get_data()
    saved: dict = data.get("saved_data") or {}
    current_region = saved.get("contract_region", "")
    current_hint = (
        f"_(אזור נוכחי: {_REGION_LABELS[current_region]})_\n\n"
        if current_region in _REGION_LABELS else ""
    )
    await state.set_state(ContractSetupForm.region)
    await message.answer(
        "📋 *הגדרה קצרה לפני שמתחילים*\n\n"
        "כדי שהבוט יוכל לחשב עבורך אוטומטית את סכומי הניכויים בחודשים חלקיים, "
        "אני צריך לדעת באיזה אזור המטפל/ת גר/ה ועובד/ת.\n\n"
        "אפשר לשנות את הערכים האלה בכל עת עם /setup.\n\n"
        f"📍 *באיזה אזור מתבצעת העבודה?*\n{current_hint}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_region_kb(),
    )


_REGION_LABELS: dict[str, str] = {
    "tel_aviv":  "תל אביב",
    "jerusalem": "ירושלים",
    "center":    "מרכז / חיפה",
    "south":     "דרום",
    "north":     "צפון",
}


@router.callback_query(ContractSetupForm.region, F.data.startswith("region:"))
async def handle_contract_region(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    region = callback.data.split(":")[1]
    await state.update_data(contract_region=region)
    label = _REGION_LABELS.get(region, region)
    await callback.message.edit_text(f"📍 אזור: *{label}*", parse_mode=ParseMode.MARKDOWN)  # type: ignore[union-attr]

    data = await state.get_data()
    saved: dict = data.get("saved_data") or {}
    current_ownership = saved.get("contract_ownership", "")
    ownership_hint = ""
    if current_ownership in ("owned", "rented"):
        current_ownership_label = "בבעלות המעסיק" if current_ownership == "owned" else "שכורה"
        ownership_hint = f"\n_(סוג נוכחי: {current_ownership_label})_"

    await callback.message.answer(  # type: ignore[union-attr]
        f"🏠 *הדירה שבה המטפל/ת גר/ה* — שייכת למי?{ownership_hint}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_ownership_kb(),
    )
    await state.set_state(ContractSetupForm.ownership_type)


@router.callback_query(ContractSetupForm.ownership_type, F.data.startswith("ownership:"))
async def handle_contract_ownership(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    ownership = callback.data.split(":")[1]   # "owned" or "rented"
    data = await state.get_data()
    region: str = data["contract_region"]

    caps = config.HOUSING_CAPS_OWNED if ownership == "owned" else config.HOUSING_CAPS_RENTED
    housing_cap: Decimal = caps[region]

    await state.update_data(contract_ownership=ownership, housing_cap=str(housing_cap))

    ownership_label = "בבעלות המעסיק" if ownership == "owned" else "שכורה"
    await callback.message.edit_text(  # type: ignore[union-attr]
        f"🏠 דירה: *{ownership_label}*", parse_mode=ParseMode.MARKDOWN
    )
    saved: dict = data.get("saved_data") or {}
    current_housing = saved.get("base_housing")
    housing_current_hint = (
        f" _(נוכחי: ₪{current_housing:.2f})_" if current_housing is not None else ""
    )
    keep_kb = _keep_btn(f"₪{current_housing:.2f}", "housing") if current_housing is not None else None
    await callback.message.answer(  # type: ignore[union-attr]
        f"🏠 *דיור* — כמה ₪ מנוכה לחודש מלא?{housing_current_hint}\n"
        f"_(תקרה חוקית לאזור זה: ₪{housing_cap:,.2f}. הזן 0 אם לא מנוכה דיור)_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keep_kb,
    )
    await state.set_state(ContractSetupForm.housing)


@router.message(ContractSetupForm.housing)
async def handle_contract_housing(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    housing_cap = Decimal(data.get("housing_cap") or str(config.DEDUCTION_HOUSING_MAX))
    val = _parse_decimal(message.text or "")
    if val is None or val < 0:
        await message.answer("נא להזין סכום חיובי.")
        return
    if val > housing_cap:
        await message.answer(
            f"שים לב: הסכום שהזנת גבוה מהתקרה החוקית לאזור זה (₪{housing_cap:,.2f} לחודש מלא).\n"
            "אנא הזן את הסכום שמופיע בחוזה:"
        )
        return
    await state.update_data(contract_housing=str(val))
    await state.set_state(ContractSetupForm.health)
    saved: dict = data.get("saved_data") or {}
    current_health = saved.get("base_health")
    health_current_hint = (
        f" _(נוכחי: ₪{current_health:.2f})_" if current_health is not None else ""
    )
    keep_kb = _keep_btn(f"₪{current_health:.2f}", "health") if current_health is not None else None
    await message.answer(
        f"🏥 *ביטוח רפואי* — כמה ₪ מנוכה לחודש מלא?{health_current_hint}\n"
        f"_(מקסימום חוקי: ₪{config.DEDUCTION_HEALTH_MAX:,.2f}. הזן 0 אם לא מנוכה ביטוח רפואי)_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keep_kb,
    )


@router.message(ContractSetupForm.health)
async def handle_contract_health(message: Message, state: FSMContext) -> None:
    val = _parse_decimal(message.text or "")
    if val is None or val < 0:
        await message.answer("נא להזין סכום חיובי.")
        return
    if val > config.DEDUCTION_HEALTH_MAX:
        await message.answer(
            f"שים לב: הסכום שהזנת גבוה מהמותר בחוק לניכוי ביטוח רפואי (₪{config.DEDUCTION_HEALTH_MAX:,.2f} לחודש מלא).\n"
            "אנא הזן את הסכום שמופיע בחוזה:"
        )
        return
    await state.update_data(contract_health=str(val))
    await state.set_state(ContractSetupForm.extras)
    data = await state.get_data()
    saved_now: dict = data.get("saved_data") or {}
    current_extras = saved_now.get("base_extras")
    extras_current_hint = (
        f" _(נוכחי: ₪{current_extras:.2f})_" if current_extras is not None else ""
    )
    keep_kb = _keep_btn(f"₪{current_extras:.2f}", "extras") if current_extras is not None else None
    await message.answer(
        f"📦 *ציוד והוצאות נוספות* — כמה ₪ מנוכה לחודש מלא?{extras_current_hint}\n"
        f"_(מקסימום חוקי: ₪{config.DEDUCTION_EXTRAS_MAX:,.2f}. הזן 0 אם לא מנוכה)_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keep_kb,
    )


@router.message(ContractSetupForm.extras)
async def handle_contract_extras(message: Message, state: FSMContext) -> None:
    val = _parse_decimal(message.text or "")
    if val is None or val < 0:
        await message.answer("נא להזין סכום חיובי.")
        return
    if val > config.DEDUCTION_EXTRAS_MAX:
        await message.answer(
            f"שים לב: הסכום שהזנת גבוה מהמותר בחוק לניכוי זה (₪{config.DEDUCTION_EXTRAS_MAX:,.2f} לחודש מלא).\n"
            "אנא הזן את הסכום שמופיע בחוזה:"
        )
        return

    await state.update_data(contract_extras=str(val))
    await _ask_setup_start_date(message, state)


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
    base_housing = Decimal(data["contract_housing"])
    base_health  = Decimal(data["contract_health"])
    base_extras  = Decimal(data["contract_extras"])
    user_id: int = data["user_id"]
    entry_point: str = data.get("entry_point", "start")

    employer_name  = data.get("setup_employer_name",  _SKIPPED) or _SKIPPED
    caregiver_name = data.get("setup_caregiver_name", _SKIPPED) or _SKIPPED

    try:
        await database.upsert_contract(
            user_id, base_housing, base_health, base_extras,
            region=data.get("contract_region"),
            ownership=data.get("contract_ownership"),
            employment_start_date=employment_start_iso,
        )
    except Exception as exc:
        log.warning("upsert_contract failed: %s", exc)

    if employer_name != _SKIPPED or caregiver_name != _SKIPPED:
        try:
            await database.upsert_user(user_id, employer_name, caregiver_name)
        except Exception as exc:
            log.warning("upsert_user (setup) failed: %s", exc)

    await state.update_data(
        base_housing=float(base_housing),
        base_health=float(base_health),
        base_extras=float(base_extras),
        employment_start_date=employment_start_iso,
    )

    if entry_point == "setup_cmd":
        # Build summary from the values we just saved (before clearing state)
        summary_data: dict = {
            "contract_region":      data.get("contract_region"),
            "contract_ownership":   data.get("contract_ownership"),
            "base_housing":         float(base_housing),
            "base_health":          float(base_health),
            "base_extras":          float(base_extras),
            "employment_start_date": employment_start_iso,
            "employer_name":        employer_name,
            "caregiver_name":       caregiver_name,
        }
        await state.clear()
        await message.answer(
            "✅ *ההגדרות עודכנו!*\n\n" + _setup_summary(summary_data) + "\n\n"
            "בפעם הבאה שתפיק תלוש (/start), הבוט ישתמש בהגדרות החדשות.",
            parse_mode=ParseMode.MARKDOWN,
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

@router.callback_query(ContractSetupForm.housing, F.data == "setup_keep:housing")
async def keep_housing(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
    data = await state.get_data()
    saved: dict = data.get("saved_data") or {}
    val = Decimal(str(saved.get("base_housing", "0")))
    await state.update_data(contract_housing=str(val))
    await state.set_state(ContractSetupForm.health)
    current_health = saved.get("base_health")
    keep_kb = _keep_btn(f"₪{current_health:.2f}", "health") if current_health is not None else None
    await callback.message.answer(  # type: ignore[union-attr]
        f"🏥 *ביטוח רפואי* — כמה ₪ מנוכה לחודש מלא?"
        + (f" _(נוכחי: ₪{current_health:.2f})_" if current_health is not None else "") + "\n"
        f"_(מקסימום חוקי: ₪{config.DEDUCTION_HEALTH_MAX:,.2f}. הזן 0 אם לא מנוכה ביטוח רפואי)_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keep_kb,
    )


@router.callback_query(ContractSetupForm.health, F.data == "setup_keep:health")
async def keep_health(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
    data = await state.get_data()
    saved: dict = data.get("saved_data") or {}
    val = Decimal(str(saved.get("base_health", "0")))
    await state.update_data(contract_health=str(val))
    await state.set_state(ContractSetupForm.extras)
    current_extras = saved.get("base_extras")
    keep_kb = _keep_btn(f"₪{current_extras:.2f}", "extras") if current_extras is not None else None
    await callback.message.answer(  # type: ignore[union-attr]
        f"📦 *ציוד והוצאות נוספות* — כמה ₪ מנוכה לחודש מלא?"
        + (f" _(נוכחי: ₪{current_extras:.2f})_" if current_extras is not None else "") + "\n"
        f"_(מקסימום חוקי: ₪{config.DEDUCTION_EXTRAS_MAX:,.2f}. הזן 0 אם לא מנוכה)_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keep_kb,
    )


@router.callback_query(ContractSetupForm.extras, F.data == "setup_keep:extras")
async def keep_extras(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
    data = await state.get_data()
    saved: dict = data.get("saved_data") or {}
    val = Decimal(str(saved.get("base_extras", "0")))
    await state.update_data(contract_extras=str(val))
    await _ask_setup_start_date(callback.message, state)  # type: ignore[arg-type]


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
    start_iso = data.get("employment_start_date")
    if start_iso:
        try:
            emp_start = date.fromisoformat(start_iso)
            if emp_start.year == year and emp_start.month == month and emp_start.day > 1:
                active_days, days_worked = calculate_partial_days("started", emp_start.day, month, year)
                await state.update_data(
                    partial_type="started",
                    days_worked=days_worked,
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
                await _ask_employer_name(message, state)
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
    data = await state.get_data()
    saved: dict = data.get("saved_data") or {}
    await state.set_state(PayslipForm.employer_name)

    # Show accumulated balance as its own message if non-zero
    vac  = saved.get("vacation_balance", 0.0) or 0.0
    sick = saved.get("sick_balance",     0.0) or 0.0
    if vac or sick:
        await message.answer(
            f"💾 *יתרה צבורה:* {vac:.2f} ימי חופשה | {sick:.2f} ימי מחלה",
            parse_mode=ParseMode.MARKDOWN,
        )

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
    await _ask_shabbat(callback.message, state)  # type: ignore[arg-type]


@router.message(PayslipForm.passport)
async def handle_passport(message: Message, state: FSMContext) -> None:
    passport = (message.text or "").strip()
    if len(passport) < 3:
        await _invalid(message, "נא להזין מספר דרכון תקין.")
        return
    await state.update_data(passport=passport)
    await _ask_shabbat(message, state)


async def _ask_shabbat(message: Message, state: FSMContext) -> None:
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

    # Pre-calculate pro-rata maxima for fixed deductions.
    # Housing uses the region-specific cap when available, else falls back to config default.
    data = await state.get_data()
    days_worked: int = data.get("days_worked", 26)
    ratio = Decimal(days_worked) / Decimal("26")
    housing_cap = Decimal(data.get("housing_cap") or str(config.DEDUCTION_HOUSING_MAX))
    cfg_maxima: dict[str, Decimal] = {
        "housing": housing_cap,
        "health":  config.DEDUCTION_HEALTH_MAX,
        "extras":  config.DEDUCTION_EXTRAS_MAX,
    }
    pro_rata_max = {
        k: str((cap * ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        for k, cap in cfg_maxima.items()
    }

    # Use contract base values (from /setup) when available — pro-rate them.
    # Fall back to the legal pro-rata max when no contract value is stored.
    def _amount(base_key: str, cap: Decimal) -> str:
        base = data.get(base_key)
        if base is not None and float(base) > 0:
            return str(
                (Decimal(str(base)) * ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            )
        return str((cap * ratio).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    deductions_amounts = {
        "housing": _amount("base_housing", housing_cap),
        "health":  _amount("base_health",  config.DEDUCTION_HEALTH_MAX),
        "extras":  _amount("base_extras",  config.DEDUCTION_EXTRAS_MAX),
        "food": "0",
    }

    # Pre-select any deduction whose contract base is set and non-zero.
    def _selected(base_key: str) -> bool:
        v = data.get(base_key)
        return v is not None and float(v) > 0

    sel = {
        "housing": _selected("base_housing"),
        "health":  _selected("base_health"),
        "extras":  _selected("base_extras"),
        "food": False,
    }
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

    # Build pro-rata hint for partial months with a stored contract value
    prorata_suggested: Decimal | None = None
    prorata_hint = ""
    days_worked: int = data.get("days_worked", 26)
    base_val = data.get(f"base_{key}")  # e.g. "base_housing"

    if key in _DEDUCTION_META and base_val is not None and float(base_val) > 0 and days_worked < 26:
        prorata_suggested = (
            Decimal(str(base_val)) * Decimal(days_worked) / Decimal(26)
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        prorata_hint = (
            f"\n\n💡 לפי החוזה שלך הניכוי הוא ₪{float(base_val):,.2f}. "
            f"מכיוון שהעובד עבד החודש יחס של {days_worked}/26 ימי עבודה, "
            f"הסכום היחסי המותר לניכוי הוא ₪{prorata_suggested:,.2f}."
        )

    await state.update_data(
        deduction_edit_key=key,
        deduction_edit_chat_id=callback.message.chat.id,  # type: ignore[union-attr]
        deduction_edit_msg_id=callback.message.message_id,  # type: ignore[union-attr]
        deduction_prorata_suggested=str(prorata_suggested) if prorata_suggested is not None else None,
    )
    await callback.message.edit_text(  # type: ignore[union-attr]
        f"✏️ *עריכת ניכוי — {label}*\n\n"
        f"הזן סכום חדש (מקסימום: ₪{max_amount:,.2f}):{prorata_hint}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_deduction_edit_kb(max_amount, prorata_suggested),
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


@router.callback_query(PayslipForm.deduction_edit, F.data == "ded_editpick:prorata")
async def handle_deduction_prorata_pick(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    """User tapped the pro-rata quick-fill button — use the stored suggested amount."""
    await callback.answer()
    data = await state.get_data()
    suggested_str = data.get("deduction_prorata_suggested") or "0"
    await _finish_deduction_edit(state, Decimal(suggested_str), bot)


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
