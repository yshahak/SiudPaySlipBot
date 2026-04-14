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

    Returned dict may contain: employer_name, caregiver_name,
    vacation_balance (float), sick_balance (float).
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


async def add_to_balances(user_id: int, vacation_delta: Decimal, sick_delta: Decimal) -> None:
    """
    Increment the accumulated vacation and sick balances for this user.
    Uses Firestore Increment — initializes to 0 automatically on first write.
    """
    if _db is None:
        return
    try:
        await _get_db().collection(COLLECTION).document(str(user_id)).set(
            {
                "vacation_balance": firestore.Increment(float(vacation_delta)),
                "sick_balance": firestore.Increment(float(sick_delta)),
            },
            merge=True,
        )
    except Exception as exc:
        log.warning("add_to_balances(%s) failed: %s", user_id, exc)


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
