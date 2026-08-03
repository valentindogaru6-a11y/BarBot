"""Separate FSM state groups for administrator workflows."""

from aiogram.fsm.state import State, StatesGroup


class CreateCategoryStates(StatesGroup):
    name = State()
    emoji = State()
    description = State()
    sort_order = State()
    confirm = State()


class EditCategoryStates(StatesGroup):
    value = State()


class CreateSubcategoryStates(StatesGroup):
    name = State()
    emoji = State()
    description = State()
    sort_order = State()
    confirm = State()


class EditSubcategoryStates(StatesGroup):
    value = State()


class CreateItemStates(StatesGroup):
    name = State()
    short_description = State()
    description = State()
    ingredients = State()
    ingredients_confirm = State()
    fields = State()
    fields_confirm = State()
    steps = State()
    steps_confirm = State()
    tags = State()
    tags_confirm = State()
    photo = State()
    photo_confirm = State()
    sort_order = State()
    is_active = State()
    confirm = State()


class EditItemStates(StatesGroup):
    value = State()
    confirm = State()


class FullEditItemStates(StatesGroup):
    input = State()


class TemplateCreateItemStates(StatesGroup):
    input = State()


class TemplateCorrectionStates(StatesGroup):
    input = State()


class TemplateConfirmationStates(StatesGroup):
    edit = State()
    create = State()


class ReplacePhotoStates(StatesGroup):
    photo = State()
    confirm = State()


class MoveItemStates(StatesGroup):
    category = State()
    placement = State()
    confirm = State()


class ComplexInputConfirmStates(StatesGroup):
    confirm = State()
