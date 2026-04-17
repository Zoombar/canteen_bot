from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AdminStates(StatesGroup):
    waiting_bind_fio = State()
    waiting_add_fio = State()
    waiting_unlink_fio = State()
    waiting_deactivate_fio = State()
    waiting_delete_fio = State()
    waiting_delete_confirm = State()
