"""
TDD: handle_setup_done_start must use callback.from_user.id (the real user)
     not callback.message.from_user.id (which is the bot's own user_id).

The bug: _handle_setup_done_start_ called cmd_start(callback.message, state).
cmd_start derives user_id from message.from_user.id.
For a CallbackQuery, callback.message is a Message the bot itself sent, so
callback.message.from_user is the BOT — a completely different user_id.
Result: database.get_user(bot_id) → None → "no contract" screen shown.

Fix: extract _run_start_flow(message, state, user_id) so the caller supplies
the correct id. handle_setup_done_start passes callback.from_user.id.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test_token_for_tests")


def _make_state(extra_data: dict | None = None) -> MagicMock:
    state = MagicMock()
    state.clear = AsyncMock()
    state.update_data = AsyncMock()
    state.set_state = AsyncMock()
    state.get_data = AsyncMock(return_value=extra_data or {})
    return state


def _make_message(user_id: int) -> MagicMock:
    msg = MagicMock()
    msg.from_user = MagicMock()
    msg.from_user.id = user_id
    msg.answer = AsyncMock()
    msg.edit_reply_markup = AsyncMock()
    return msg


def _make_callback(user_id: int, bot_id: int) -> MagicMock:
    cb = MagicMock()
    cb.answer = AsyncMock()
    cb.from_user = MagicMock()
    cb.from_user.id = user_id
    # callback.message is the message the BOT sent — from_user is the bot
    cb.message = _make_message(bot_id)
    return cb


class TestSetupDoneStartUsesCorrectUserId:
    """
    Verifies that handle_setup_done_start routes to the payslip flow using
    the real user's id, not the bot's id.
    """

    REAL_USER_ID = 111_000_111
    BOT_ID       = 999_999_999   # Different — simulates callback.message.from_user

    def _run(self, coro):
        return asyncio.run(coro)

    def test_database_lookup_uses_callback_from_user_id(self):
        """
        database.get_user must be called with the real user's id.
        If it is accidentally called with the bot id, the test fails — that
        would reproduce the bug.
        """
        from bot import handle_setup_done_start

        captured: list[int] = []

        async def mock_get_user(uid: int):
            captured.append(uid)
            # Return a valid contract so the flow proceeds past the gate
            return {
                "agreed_net_salary": 5989.0,
                "rest_day": "saturday",
                "employment_start_date": "2026-04-01",
            }

        cb    = _make_callback(self.REAL_USER_ID, self.BOT_ID)
        state = _make_state()

        async def run():
            with patch("database.get_user", side_effect=mock_get_user):
                await handle_setup_done_start(cb, state)

        self._run(run())

        assert captured, "database.get_user was never called"
        assert self.REAL_USER_ID in captured, (
            f"database.get_user was called with {captured} — "
            f"expected {self.REAL_USER_ID} (real user), "
            f"not {self.BOT_ID} (bot id). "
            "handle_setup_done_start must pass callback.from_user.id, "
            "not callback.message.from_user.id."
        )

    def test_no_contract_message_not_shown_when_contract_exists(self):
        """
        After /setup the user should land in the payslip month-picker,
        never see the 'הקלד /setup להתחלת ההגדרה' screen.
        """
        from bot import handle_setup_done_start

        async def mock_get_user(uid: int):
            if uid == self.REAL_USER_ID:
                return {
                    "agreed_net_salary": 5989.0,
                    "rest_day": "saturday",
                    "employment_start_date": "2026-04-01",
                }
            return None  # wrong id → no contract → would trigger the bug

        cb    = _make_callback(self.REAL_USER_ID, self.BOT_ID)
        state = _make_state()

        async def run():
            with patch("database.get_user", side_effect=mock_get_user):
                await handle_setup_done_start(cb, state)

        self._run(run())

        # Inspect all calls to cb.message.answer
        all_texts: list[str] = [
            str(call.args[0]) if call.args else str(call.kwargs.get("text", ""))
            for call in cb.message.answer.call_args_list
        ]
        for text in all_texts:
            assert "הקלד /setup" not in text, (
                f"'הקלד /setup' (no-contract screen) was sent to the user.\n"
                f"Answer texts: {all_texts}"
            )

    def test_state_set_to_month_year_after_setup(self):
        """
        The FSM should advance to PayslipForm.month_year, not stay cleared.
        """
        from bot import handle_setup_done_start
        from bot import PayslipForm

        async def mock_get_user(uid: int):
            if uid == self.REAL_USER_ID:
                return {"agreed_net_salary": 5989.0, "rest_day": "saturday"}
            return None

        cb    = _make_callback(self.REAL_USER_ID, self.BOT_ID)
        state = _make_state()

        async def run():
            with patch("database.get_user", side_effect=mock_get_user):
                await handle_setup_done_start(cb, state)

        self._run(run())

        state.set_state.assert_called_once_with(PayslipForm.month_year)
