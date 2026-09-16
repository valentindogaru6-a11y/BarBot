"""FSM state groups for the warehouse UI only."""

from aiogram.fsm.state import State, StatesGroup


class WarehouseIntakeStates(StatesGroup):
    writeoff = State()
    transfer = State()
    development_title = State()
    development_body = State()
    missing_development_title = State()
    resolving = State()
    correcting_line = State()


class WarehouseOperationEditStates(StatesGroup):
    product = State()
    quantity = State()
    note = State()
    destination = State()
    development_title = State()
    confirm = State()
    delete = State()


class WarehouseClosingStates(StatesGroup):
    quantity = State()
    quantity_confirm = State()
    not_applicable_reason = State()


class WarehouseRequiredRuleStates(StatesGroup):
    create = State()
    question = State()
    mode = State()
    weekdays = State()
    limits = State()
    sort_order = State()


class WarehouseRecipientStates(StatesGroup):
    chat_id = State()
    title = State()


class WarehouseReminderStates(StatesGroup):
    chat_id = State()
    first = State()
    repeat = State()
    maximum = State()


class WarehouseScheduleStates(StatesGroup):
    daily_name = State()
    daily_delay = State()
    once_name = State()
    once_date = State()
    once_time = State()
    recipients = State()
    confirm = State()
