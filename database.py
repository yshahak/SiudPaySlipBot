"""
Firestore persistence layer for SiudPaySlipBot.

Stores employer_name, caregiver_name, and accumulated vacation/sick-day balances
per Telegram user_id. All functions degrade gracefully — if Firestore is
unavailable, they return None/False/silently so the bot continues working.

On Cloud Run, authentication uses the service account automatically.
For local development, run: gcloud auth application-default login
"""

import logging
from decimal import Decimal

from google.api_core.exceptions import NotFound
from google.cloud import firestore

log = logging.getLogger(__name__)

COLLECTION = "payslip_users"
_SKIPPED = "---"  # matches bot.py sentinel for skipped optional fields

_db: firestore.AsyncClient | None = None


def _get_db() -> firestore.AsyncClient:
    assert _db is not None, "init_db() was not called"
    return _db


async def init_db() -> None:
    """
    Initialize the Firestore async client.
    Must be called once at startup. If this raises, the bot continues without
    persistence — all other functions check for _db is None and return early.
    """
    global _db
    _db = firestore.AsyncClient()
    log.info("Firestore AsyncClient initialized.")


async def get_user(user_id: int) -> dict | None:
    """
    Return the stored data dict for this user, or None if not found / on error.

    Returned dict may contain: agreed_net_salary (float), employer_name,
    caregiver_name, vacation_balance (float), sick_balance (float),
    employment_start_date (str ISO), and legacy Detailed Mode fields.
    """
    if _db is None:
        return None
    try:
        doc = await _get_db().collection(COLLECTION).document(str(user_id)).get()
        return doc.to_dict() if doc.exists else None
    except Exception as exc:
        log.warning("get_user(%s) failed: %s", user_id, exc)
        return None


async def upsert_user(user_id: int, employer_name: str, caregiver_name: str) -> None:
    """
    Save employer and caregiver names for this user (merge=True so balances are preserved).
    Silently skips if either name is the _SKIPPED sentinel or Firestore is unavailable.
    """
    if _db is None:
        return
    if employer_name == _SKIPPED and caregiver_name == _SKIPPED:
        return
    try:
        data: dict = {}
        if employer_name != _SKIPPED:
            data["employer_name"] = employer_name
        if caregiver_name != _SKIPPED:
            data["caregiver_name"] = caregiver_name
        await _get_db().collection(COLLECTION).document(str(user_id)).set(data, merge=True)
    except Exception as exc:
        log.warning("upsert_user(%s) failed: %s", user_id, exc)


async def upsert_contract(
    user_id: int,
    agreed_net_salary: Decimal,
    employment_start_date: str | None = None,
    rest_day: str | None = None,
    # ARCHITECTURE NOTE (Detailed Mode fields — currently unused by Simple Mode):
    # These parameters are preserved for future re-enablement of itemized deductions.
    # In Simple Mode, the caller does not pass them (they stay None).
    base_housing: Decimal | None = None,
    base_health: Decimal | None = None,
    base_extras: Decimal | None = None,
    region: str | None = None,
    ownership: str | None = None,
) -> None:
    """
    Save the user's agreed monthly net salary, employment start date, and rest day.
    In Simple Mode, base_housing/health/extras/region/ownership are not collected
    but the parameters are kept for future Detailed Mode re-enablement.
    """
    if _db is None:
        return
    try:
        data: dict = {
            "agreed_net_salary": float(agreed_net_salary),
        }
        if employment_start_date is not None:
            data["employment_start_date"] = employment_start_date
        if rest_day is not None:
            data["rest_day"] = rest_day
        # ARCHITECTURE NOTE: Detailed Mode fields — write only when provided.
        if base_housing is not None:
            data["base_housing"] = float(base_housing)
        if base_health is not None:
            data["base_health"] = float(base_health)
        if base_extras is not None:
            data["base_extras"] = float(base_extras)
        if region is not None:
            data["contract_region"] = region
        if ownership is not None:
            data["contract_ownership"] = ownership
        await _get_db().collection(COLLECTION).document(str(user_id)).set(data, merge=True)
    except Exception as exc:
        log.warning("upsert_contract(%s) failed: %s", user_id, exc)


async def upsert_month_accrual(
    user_id: int, month_key: str, vacation: Decimal, sick: Decimal
) -> None:
    """
    Idempotently overwrite the vacation/sick accrual for one specific month.

    Regenerating the same payslip (e.g. to correct a mistake) just overwrites
    the same map key — no double-counting, and the latest calculation wins.

    month_key: "YYYY-MM" string, e.g. "2026-04"

    Uses update() with dot-notation so only this month's entry is touched.
    Falls back to set(merge=True) if the document doesn't exist yet.
    """
    if _db is None:
        return
    ref = _get_db().collection(COLLECTION).document(str(user_id))
    entry = {"vacation": float(vacation), "sick": float(sick)}
    try:
        await ref.update({f"monthly_accruals.{month_key}": entry})
    except NotFound:
        # Document doesn't exist yet (Firestore was unavailable during /setup).
        # set(merge=True) at the top level is safe here because monthly_accruals
        # is the only field we're writing.
        try:
            await ref.set({"monthly_accruals": {month_key: entry}}, merge=True)
        except Exception as exc:
            log.warning("upsert_month_accrual(%s, %s) set-fallback failed: %s", user_id, month_key, exc)
    except Exception as exc:
        log.warning("upsert_month_accrual(%s, %s) failed: %s", user_id, month_key, exc)


async def get_balances(user_id: int) -> tuple[float, float]:
    """
    Return (vacation_total, sick_total) computed by summing all entries in
    the monthly_accruals map. Each payslip generation overwrites its month's
    entry, so the sum always reflects the latest calculation per month.

    Returns (0.0, 0.0) if no data is found or on error.
    """
    if _db is None:
        return 0.0, 0.0
    try:
        doc = await _get_db().collection(COLLECTION).document(str(user_id)).get()
        if not doc.exists:
            return 0.0, 0.0
        accruals: dict = (doc.to_dict() or {}).get("monthly_accruals") or {}
        vacation = sum(v.get("vacation", 0.0) for v in accruals.values())
        sick     = sum(v.get("sick",     0.0) for v in accruals.values())
        return vacation, sick
    except Exception as exc:
        log.warning("get_balances(%s) failed: %s", user_id, exc)
        return 0.0, 0.0


async def delete_user(user_id: int) -> bool:
    """
    Delete all stored data for this user.
    Returns True if a document was deleted, False if not found or on error.
    """
    if _db is None:
        return False
    try:
        ref = _get_db().collection(COLLECTION).document(str(user_id))
        doc = await ref.get()
        if not doc.exists:
            return False
        await ref.delete()
        return True
    except Exception as exc:
        log.warning("delete_user(%s) failed: %s", user_id, exc)
        return False
