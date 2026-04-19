from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AdminStates(StatesGroup):
    waiting_bind_fio = State()
