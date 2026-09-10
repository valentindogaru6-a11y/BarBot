"""FSM state groups for the warehouse UI only."""

from aiogram.fsm.state import State, StatesGroup


class WarehouseIntakeStates(StatesGroup):
    writeoff = State()
    transfer = State()
    resolving = State()
    correcting_line = State()


class WarehouseOperationEditStates(StatesGroup):
    product = State()
    quantity = State()
    note = State()
    destination = State()
    confirm = State()
    delete = State()
