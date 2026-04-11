from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AdminStates(StatesGroup):
    waiting_add_fio = State()
    waiting_unlink_fio = State()
    waiting_deactivate_fio = State()
