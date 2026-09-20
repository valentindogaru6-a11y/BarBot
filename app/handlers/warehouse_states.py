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
    new_product_name = State()
    new_product_unit = State()
    new_product_confirm = State()


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
    edit_name = State()
    edit_delay = State()
    edit_recipients = State()
    edit_once_date = State()
    edit_once_time = State()
    edit_once_occurrence = State()
    edit_confirm = State()


class WarehouseCorrectionStates(StatesGroup):
    open_reason = State()
    open_confirm = State()
    add_product = State()
    add_quantity = State()
    add_destination = State()
    add_development_title = State()
    add_note = State()
    add_confirm = State()
    edit_product = State()
    edit_product_quantity = State()
    edit_quantity = State()
    edit_note = State()
    edit_destination = State()
    edit_development_title = State()
    edit_confirm = State()
    delete_reason = State()
    delete_confirm = State()
    restore_reason = State()
    restore_confirm = State()
    cancel_session = State()
    finalize_confirm = State()
    recipient_selection = State()
    delivery_confirm = State()
    retry_confirm = State()
