"""FSM states used only by user administration workflows."""

from aiogram.fsm.state import State, StatesGroup


class UserPickerStates(StatesGroup):
    waiting = State()


class UserSearchStates(StatesGroup):
    query = State()


class RoleChangeStates(StatesGroup):
    confirm = State()
