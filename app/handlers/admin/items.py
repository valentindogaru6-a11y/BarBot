"""Administrator item creation, editing, moving, and deletion."""

import logging
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ForceReply, Message

from app.handlers.admin.common import (
    ADMIN_HEADER,
    actor_from_user,
    require_admin_callback,
    require_admin_message,
)
from app.handlers.admin.states import (
    CreateItemStates,
    EditItemStates,
    FullEditItemStates,
    MoveItemStates,
    TemplateConfirmationStates,
    TemplateCorrectionStates,
    TemplateCreateItemStates,
)
from app.handlers.common import edit_or_send_text
from app.keyboards.admin import (
    build_admin_confirm_keyboard,
    build_admin_item_missing_keyboard,
    build_admin_template_error_keyboard,
    build_admin_text_input_keyboard,
    build_admin_wizard_keyboard,
)
from app.keyboards.admin_callbacks import (
    AdminCancelCallback,
    AdminConfirmCallback,
    AdminItemCallback,
    AdminPaginationCallback,
)
from app.keyboards.admin_catalog import (
    build_admin_category_picker,
    build_admin_item_creation_modes,
    build_admin_item_actions,
    build_admin_items_keyboard,
    build_admin_placement_keyboard,
    build_created_item_keyboard,
)
from app.models.catalog_item import CatalogItem, MAX_STABLE_ID_LENGTH
from app.services.admin_service import (
    AdminInputError,
    AdminService,
    ObjectChangedError,
    parse_fields,
    parse_ingredients,
    parse_steps,
    parse_tags,
)
from app.services.auth_service import AuthService
from app.services.card_template import (
    CardTemplateError,
    ParsedItemUpdate,
    empty_item_template,
    parse_item_template,
    serialize_item_template,
)
from app.services.stable_id import (
    StableIdGenerationError,
    generate_stable_id,
)
from app.utils.formatting import format_item_card

router = Router(name=__name__)
logger = logging.getLogger(__name__)

TELEGRAM_TEXT_LIMIT = 4096


def _split_telegram_text(text: str) -> list[str]:
    """Split copyable text without truncating it or changing its contents."""

    if not text:
        return ["-"]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > TELEGRAM_TEXT_LIMIT:
        boundary = remaining.rfind("\n", 0, TELEGRAM_TEXT_LIMIT + 1)
        if boundary <= 0:
            boundary = TELEGRAM_TEXT_LIMIT
        else:
            boundary += 1
        chunks.append(remaining[:boundary])
        remaining = remaining[boundary:]
    chunks.append(remaining)
    return chunks


async def _send_copyable_text(
    message: Message,
    text: str,
    *,
    placeholder: str,
) -> None:
    """Send the entire raw value, then open a convenient reply composer."""

    chunks = _split_telegram_text(text)
    for index, chunk in enumerate(chunks):
        await message.answer(
            chunk,
            parse_mode=None,
            reply_markup=(
                build_admin_text_input_keyboard(text)
                if index == len(chunks) - 1
                else None
            ),
        )
    instruction = (
        "Текст длиннее лимита кнопки копирования. Используйте стандартное "
        "действие Telegram «Копировать», измените текст и отправьте обратно."
        if len(text) > 256
        else "Скопируйте текст, измените нужные строки и отправьте обратно."
    )
    await message.answer(
        instruction,
        reply_markup=ForceReply(input_field_placeholder=placeholder),
    )


def _serialize_item_field(item: CatalogItem, field: str) -> str:
    if field == "name":
        return item.name
    if field in {"short_description", "description"}:
        value = getattr(item, field)
        return value.strip() if value and value.strip() else "-"
    if field == "ingredients":
        return "\n".join(
            f"{entry.name} | {entry.amount} | {entry.unit}"
            for entry in item.ingredients
        ) or "-"
    if field == "fields":
        return "\n".join(
            f"{key} | {value}" for key, value in item.fields.items()
        ) or "-"
    if field == "steps":
        return "\n".join(item.steps) or "-"
    if field == "tags":
        return ", ".join(item.tags) or "-"
    raise ValueError(f"Unsupported editable field: {field}")


def _sub_id(value: str) -> str | None:
    return None if value == "_" else value


def _item_from_catalog(
    catalog,
    category_id: str,
    subcategory_id: str | None,
    item_id: str,
) -> CatalogItem:
    items = AdminService._items(catalog, category_id, subcategory_id)
    item = next((entry for entry in items if entry.id == item_id), None)
    if item is None:
        raise ObjectChangedError("Позиция уже удалена или изменена")
    return item


def _item_admin_text(item: CatalogItem) -> str:
    status = "активна" if item.is_active else "скрыто"
    photo = "есть" if item.photo_file_id else "нет"
    return (
        f"{ADMIN_HEADER}\n\n{format_item_card(item)}\n\n"
        f"<b>ID:</b> {item.id}\n"
        f"<b>Статус:</b> {status}\n"
        f"<b>Порядок:</b> {item.sort_order}\n"
        f"<b>Фото:</b> {photo}"
    )


async def show_item_editor(
    message: Message,
    service: AdminService,
    category_id: str,
    subcategory_id: str | None,
    item_id: str,
) -> bool:
    catalog = await service.get_catalog()
    try:
        item = _item_from_catalog(
            catalog,
            category_id,
            subcategory_id,
            item_id,
        )
    except ObjectChangedError:
        await edit_or_send_text(
            message,
            f"{ADMIN_HEADER}\n\n"
            "Позиция была удалена или перемещена и больше недоступна "
            "по этой ссылке.",
            build_admin_item_missing_keyboard(),
        )
        return False
    await edit_or_send_text(
        message,
        _item_admin_text(item),
        build_admin_item_actions(category_id, subcategory_id, item),
    )
    return True


async def _show_items(
    message: Message,
    service: AdminService,
    category_id: str,
    subcategory_id: str | None,
    *,
    page: int = 0,
    filter_mode: str = "a",
) -> None:
    catalog = await service.get_catalog()
    items = AdminService._items(catalog, category_id, subcategory_id)
    mode = {"a": "all", "v": "active", "h": "hidden"}.get(
        filter_mode,
        "all",
    )
    filtered = AdminService.filter_entities(items, mode)
    await edit_or_send_text(
        message,
        f"{ADMIN_HEADER}\n\n📇 Позиции:",
        build_admin_items_keyboard(
            category_id,
            subcategory_id,
            filtered,
            page,
            filter_mode,
        ),
    )


@router.callback_query(AdminItemCallback.filter(F.action.in_({"p", "q"})))
async def item_choose_placement(
    callback: CallbackQuery,
    callback_data: AdminItemCallback,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    catalog = await admin_service.get_catalog()
    category = AdminService._category(catalog, callback_data.category_id)
    action = "n" if callback_data.action == "q" else "l"
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            f"{ADMIN_HEADER}\n\nВыберите размещение:",
            build_admin_placement_keyboard(category, action=action),
        )


@router.callback_query(AdminItemCallback.filter(F.action == "l"))
async def item_list(
    callback: CallbackQuery,
    callback_data: AdminItemCallback,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    if isinstance(callback.message, Message):
        await _show_items(
            callback.message,
            admin_service,
            callback_data.category_id,
            _sub_id(callback_data.subcategory_id),
        )


@router.callback_query(AdminPaginationCallback.filter(F.entity == "i"))
async def item_page(
    callback: CallbackQuery,
    callback_data: AdminPaginationCallback,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    if isinstance(callback.message, Message):
        await _show_items(
            callback.message,
            admin_service,
            callback_data.category_id,
            _sub_id(callback_data.subcategory_id),
            page=callback_data.page,
            filter_mode=callback_data.filter_mode,
        )


@router.callback_query(AdminItemCallback.filter(F.action == "o"))
async def item_open(
    callback: CallbackQuery,
    callback_data: AdminItemCallback,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    if isinstance(callback.message, Message):
        await show_item_editor(
            callback.message,
            admin_service,
            callback_data.category_id,
            _sub_id(callback_data.subcategory_id),
            callback_data.item_id,
        )


@router.callback_query(AdminItemCallback.filter(F.action == "v"))
async def item_preview(
    callback: CallbackQuery,
    callback_data: AdminItemCallback,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    catalog = await admin_service.get_catalog()
    item = _item_from_catalog(
        catalog,
        callback_data.category_id,
        _sub_id(callback_data.subcategory_id),
        callback_data.item_id,
    )
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            f"{ADMIN_HEADER}\n\n👀 <b>Предпросмотр:</b>\n\n"
            f"{format_item_card(item)}",
            build_admin_item_actions(
                callback_data.category_id,
                _sub_id(callback_data.subcategory_id),
                item,
            ),
        )


@router.callback_query(AdminItemCallback.filter(F.action == "t"))
async def item_toggle(
    callback: CallbackQuery,
    callback_data: AdminItemCallback,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    try:
        await admin_service.toggle_item(
            actor_from_user(callback.from_user),
            callback_data.category_id,
            _sub_id(callback_data.subcategory_id),
            callback_data.item_id,
        )
        if isinstance(callback.message, Message):
            await show_item_editor(
                callback.message,
                admin_service,
                callback_data.category_id,
                _sub_id(callback_data.subcategory_id),
                callback_data.item_id,
            )
    except Exception:
        logger.exception("Could not toggle item")
        if isinstance(callback.message, Message):
            await callback.message.answer("⚠️ Не удалось сохранить изменения")


@router.callback_query(AdminItemCallback.filter(F.action == "d"))
async def item_delete_prompt(
    callback: CallbackQuery,
    callback_data: AdminItemCallback,
    auth_service: AuthService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            f"{ADMIN_HEADER}\n\n⚠️ Удалить позицию без возможности отмены?",
            build_admin_confirm_keyboard(
                action="di",
                category_id=callback_data.category_id,
                subcategory_id=callback_data.subcategory_id,
                item_id=callback_data.item_id,
            ),
        )


@router.callback_query(AdminConfirmCallback.filter(F.action == "di"))
async def item_delete_confirm(
    callback: CallbackQuery,
    callback_data: AdminConfirmCallback,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    try:
        await admin_service.delete_item(
            actor_from_user(callback.from_user),
            callback_data.category_id,
            _sub_id(callback_data.subcategory_id),
            callback_data.item_id,
            confirmed=True,
        )
    except Exception:
        logger.exception("Could not delete item")
        if isinstance(callback.message, Message):
            await callback.message.answer("⚠️ Не удалось сохранить изменения")
        return
    if isinstance(callback.message, Message):
        await _show_items(
            callback.message,
            admin_service,
            callback_data.category_id,
            _sub_id(callback_data.subcategory_id),
        )


@router.callback_query(AdminItemCallback.filter(F.action == "n"))
async def item_create_start(
    callback: CallbackQuery,
    callback_data: AdminItemCallback,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    await state.clear()
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            f"{ADMIN_HEADER}\n\nВыберите способ создания позиции:",
            build_admin_item_creation_modes(
                callback_data.category_id,
                _sub_id(callback_data.subcategory_id),
            ),
        )


@router.callback_query(AdminItemCallback.filter(F.action == "w"))
async def item_create_wizard_start(
    callback: CallbackQuery,
    callback_data: AdminItemCallback,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    """Start the unchanged step-by-step item creation wizard."""

    if not await require_admin_callback(callback, auth_service):
        return
    await state.set_state(CreateItemStates.name)
    await state.set_data(
        {
            "category_id": callback_data.category_id,
            "subcategory_id": _sub_id(callback_data.subcategory_id),
        }
    )
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            f"{ADMIN_HEADER}\n\nВведите название позиции:",
            build_admin_wizard_keyboard(False),
        )


@router.message(CreateItemStates.name)
async def create_item_name(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_message(message, auth_service):
        return
    value = (message.text or "").strip()
    if not value:
        await message.answer("Название не может быть пустым.")
        return
    data = await state.get_data()
    catalog = await admin_service.get_catalog()
    items = AdminService._items(
        catalog,
        data["category_id"],
        data.get("subcategory_id"),
    )
    try:
        stable_id = generate_stable_id(
            value,
            {item.id for item in items},
            MAX_STABLE_ID_LENGTH,
        )
    except StableIdGenerationError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(name=value, stable_id=stable_id)
    await state.set_state(CreateItemStates.short_description)
    await message.answer(
        "Введите краткое описание или -:",
        reply_markup=build_admin_wizard_keyboard(),
    )


@router.message(CreateItemStates.short_description)
async def create_item_short(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await require_admin_message(message, auth_service):
        return
    value = (message.text or "").strip()
    await state.update_data(short_description=None if value == "-" else value)
    await state.set_state(CreateItemStates.description)
    await message.answer(
        "Введите полное описание или -:",
        reply_markup=build_admin_wizard_keyboard(),
    )


@router.message(CreateItemStates.description)
async def create_item_description(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await require_admin_message(message, auth_service):
        return
    value = (message.text or "").strip()
    await state.update_data(description=None if value == "-" else value)
    await state.set_state(CreateItemStates.ingredients)
    await message.answer(
        "Введите ингредиенты: Название | количество | единица\n"
        "Одна строка — один ингредиент. Для пустого списка: -",
        reply_markup=build_admin_wizard_keyboard(),
    )


@router.message(CreateItemStates.ingredients)
async def create_item_ingredients(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await require_admin_message(message, auth_service):
        return
    try:
        ingredients = parse_ingredients(message.text or "")
    except AdminInputError as exc:
        await message.answer("⚠️ Ошибки:\n" + "\n".join(exc.errors))
        return
    await state.update_data(
        pending_value=[item.model_dump() for item in ingredients],
        pending_field="ingredients",
    )
    await state.set_state(CreateItemStates.ingredients_confirm)
    preview = "\n".join(
        f"• {item.name} — {item.amount} {item.unit}".rstrip()
        for item in ingredients
    ) or "Пустой список"
    await message.answer(
        f"Разобрано:\n{escape(preview)}\n\nПодтвердить?",
        reply_markup=build_admin_confirm_keyboard(action="ci"),
    )


async def _confirm_create_complex(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    field: str,
    next_state,
    prompt: str,
    auth_service: AuthService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    data = await state.get_data()
    if data.get("pending_field") != field:
        return
    await state.update_data(
        **{field: data.get("pending_value")},
        pending_value=None,
        pending_field=None,
    )
    await state.set_state(next_state)
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            prompt,
            build_admin_wizard_keyboard(),
        )


@router.callback_query(AdminConfirmCallback.filter(F.action == "ci"))
async def confirm_create_ingredients(
    callback: CallbackQuery,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    await _confirm_create_complex(
        callback,
        state,
        field="ingredients",
        next_state=CreateItemStates.fields,
        prompt="Введите характеристики: ключ | значение\nДля очистки: -",
        auth_service=auth_service,
    )


@router.message(CreateItemStates.fields)
async def create_item_fields(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await require_admin_message(message, auth_service):
        return
    try:
        fields = parse_fields(message.text or "")
    except AdminInputError as exc:
        await message.answer("⚠️ Ошибки:\n" + "\n".join(exc.errors))
        return
    await state.update_data(
        pending_value=fields,
        pending_field="fields",
    )
    await state.set_state(CreateItemStates.fields_confirm)
    preview = "\n".join(f"• {key}: {value}" for key, value in fields.items())
    await message.answer(
        f"Разобрано:\n{escape(preview or 'Пустой список')}\n\nПодтвердить?",
        reply_markup=build_admin_confirm_keyboard(action="cf"),
    )


@router.callback_query(AdminConfirmCallback.filter(F.action == "cf"))
async def confirm_create_fields(
    callback: CallbackQuery,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    await _confirm_create_complex(
        callback,
        state,
        field="fields",
        next_state=CreateItemStates.steps,
        prompt="Введите шаги приготовления, один шаг на строку, или -:",
        auth_service=auth_service,
    )


@router.message(CreateItemStates.steps)
async def create_item_steps(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await require_admin_message(message, auth_service):
        return
    steps = parse_steps(message.text or "")
    await state.update_data(pending_value=steps, pending_field="steps")
    await state.set_state(CreateItemStates.steps_confirm)
    preview = "\n".join(
        f"{number}. {step}" for number, step in enumerate(steps, 1)
    )
    await message.answer(
        f"Разобрано:\n{escape(preview or 'Пустой список')}\n\nПодтвердить?",
        reply_markup=build_admin_confirm_keyboard(action="csx"),
    )


@router.callback_query(AdminConfirmCallback.filter(F.action == "csx"))
async def confirm_create_steps(
    callback: CallbackQuery,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    await _confirm_create_complex(
        callback,
        state,
        field="steps",
        next_state=CreateItemStates.tags,
        prompt="Введите теги через запятую или -:",
        auth_service=auth_service,
    )


@router.message(CreateItemStates.tags)
async def create_item_tags(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await require_admin_message(message, auth_service):
        return
    tags = parse_tags(message.text or "")
    await state.update_data(pending_value=tags, pending_field="tags")
    await state.set_state(CreateItemStates.tags_confirm)
    await message.answer(
        f"Разобрано: {escape(', '.join(tags) or 'Пустой список')}\n\n"
        "Подтвердить?",
        reply_markup=build_admin_confirm_keyboard(action="ct"),
    )


@router.callback_query(AdminConfirmCallback.filter(F.action == "ct"))
async def confirm_create_tags(
    callback: CallbackQuery,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    await _confirm_create_complex(
        callback,
        state,
        field="tags",
        next_state=CreateItemStates.photo,
        prompt="Отправьте фотографию или - для пропуска:",
        auth_service=auth_service,
    )


@router.message(CreateItemStates.photo, F.text)
async def create_item_skip_photo(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await require_admin_message(message, auth_service):
        return
    if (message.text or "").strip() != "-":
        await message.answer("Отправьте фото или символ -.")
        return
    await state.update_data(photo_file_id=None)
    await state.set_state(CreateItemStates.sort_order)
    await message.answer("Введите целый sort_order:", reply_markup=build_admin_wizard_keyboard())


@router.message(CreateItemStates.sort_order)
async def create_item_sort(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await require_admin_message(message, auth_service):
        return
    try:
        value = int((message.text or "").strip())
    except ValueError:
        await message.answer("sort_order должен быть целым числом.")
        return
    await state.update_data(sort_order=value)
    await state.set_state(CreateItemStates.is_active)
    await message.answer(
        "Позиция активна? Введите да или нет:",
        reply_markup=build_admin_wizard_keyboard(),
    )


@router.message(CreateItemStates.is_active)
async def create_item_active(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await require_admin_message(message, auth_service):
        return
    value = (message.text or "").strip().casefold()
    if value not in {"да", "нет"}:
        await message.answer("Введите «да» или «нет».")
        return
    await state.update_data(is_active=value == "да")
    data = await state.get_data()
    item = _build_item(data)
    await state.set_state(CreateItemStates.confirm)
    await message.answer(
        f"{ADMIN_HEADER}\n\n👀 Предпросмотр:\n\n{format_item_card(item)}\n\n"
        f"<b>Технический ID:</b> {item.id}\n\n"
        "Сохранить позицию?",
        reply_markup=build_admin_confirm_keyboard(
            action="sv",
            category_id=data["category_id"],
            subcategory_id=data.get("subcategory_id") or "_",
        ),
    )


def _build_item(data: dict) -> CatalogItem:
    return CatalogItem(
        id=data["stable_id"],
        name=data["name"],
        short_description=data.get("short_description"),
        description=data.get("description"),
        photo_file_id=data.get("photo_file_id"),
        ingredients=data.get("ingredients", []),
        fields=data.get("fields", {}),
        steps=data.get("steps", []),
        tags=data.get("tags", []),
        sort_order=data["sort_order"],
        is_active=data["is_active"],
    )


@router.callback_query(AdminConfirmCallback.filter(F.action == "sv"))
async def create_item_save(
    callback: CallbackQuery,
    state: FSMContext,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    data = await state.get_data()
    try:
        catalog = await admin_service.get_catalog()
        items = AdminService._items(
            catalog,
            data["category_id"],
            data.get("subcategory_id"),
        )
        stable_id = generate_stable_id(
            data["name"],
            {item.id for item in items},
            MAX_STABLE_ID_LENGTH,
        )
        item = _build_item({**data, "stable_id": stable_id})
        await admin_service.create_item(
            actor_from_user(callback.from_user),
            data["category_id"],
            data.get("subcategory_id"),
            item,
        )
    except Exception:
        logger.exception("Could not create item")
        if isinstance(callback.message, Message):
            await callback.message.answer("⚠️ Не удалось сохранить изменения")
        return
    await state.clear()
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            f"{ADMIN_HEADER}\n\n✅ Изменения сохранены\n\n"
            f"{format_item_card(item)}",
            build_admin_item_actions(
                data["category_id"],
                data.get("subcategory_id"),
                item,
            ),
        )


async def _show_full_template_input(
    message: Message,
    template: str,
    *,
    creating: bool,
) -> None:
    action = "создания" if creating else "редактирования"
    await edit_or_send_text(
        message,
        f"{ADMIN_HEADER}\n\n"
        f"Скопируйте весь шаблон для {action} карточки, измените "
        "нужные значения и отправьте его одним сообщением. "
        "Фото и размещение редактируются отдельно.",
    )
    await _send_copyable_text(
        message,
        template,
        placeholder="Заполненный шаблон",
    )


@router.callback_query(AdminItemCallback.filter(F.action == "ea"))
async def item_full_edit_start(
    callback: CallbackQuery,
    callback_data: AdminItemCallback,
    state: FSMContext,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    """Show a populated template for an existing item."""

    if not await require_admin_callback(callback, auth_service):
        return
    catalog = await admin_service.get_catalog()
    try:
        item = _item_from_catalog(
            catalog,
            callback_data.category_id,
            _sub_id(callback_data.subcategory_id),
            callback_data.item_id,
        )
    except ObjectChangedError:
        if isinstance(callback.message, Message):
            await callback.message.answer(
                "Позиция уже удалена или перемещена."
            )
        return

    template = serialize_item_template(item)
    await state.set_state(FullEditItemStates.input)
    await state.set_data(
        {
            "template_mode": "edit",
            "category_id": callback_data.category_id,
            "subcategory_id": _sub_id(callback_data.subcategory_id),
            "item_id": callback_data.item_id,
            "raw_template": template,
        }
    )
    if isinstance(callback.message, Message):
        await _show_full_template_input(
            callback.message,
            template,
            creating=False,
        )


@router.callback_query(AdminItemCallback.filter(F.action == "k"))
async def item_template_create_start(
    callback: CallbackQuery,
    callback_data: AdminItemCallback,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    """Show an empty full-card template after placement was selected."""

    if not await require_admin_callback(callback, auth_service):
        return
    template = empty_item_template()
    await state.set_state(TemplateCreateItemStates.input)
    await state.set_data(
        {
            "template_mode": "create",
            "category_id": callback_data.category_id,
            "subcategory_id": _sub_id(callback_data.subcategory_id),
            "raw_template": template,
        }
    )
    if isinstance(callback.message, Message):
        await _show_full_template_input(
            callback.message,
            template,
            creating=True,
        )


async def _show_template_errors(
    message: Message,
    errors: list[str],
) -> None:
    error_text = "\n".join(f"• {error}" for error in errors)
    await message.answer(
        "⚠️ Шаблон содержит ошибки:\n\n" + error_text,
        parse_mode=None,
        reply_markup=build_admin_template_error_keyboard(),
    )


async def _process_item_template(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_message(message, auth_service):
        return
    raw = message.text or ""
    if raw.lstrip().startswith("/"):
        await message.answer(
            "Команда не записана в шаблон. Для выхода используйте /cancel."
        )
        return

    data = await state.get_data()
    try:
        parsed = parse_item_template(raw)
    except CardTemplateError as exc:
        await state.update_data(raw_template=raw, pending_update=None)
        await state.set_state(TemplateCorrectionStates.input)
        await _show_template_errors(message, exc.errors)
        return

    mode = data.get("template_mode")
    await state.update_data(
        raw_template=raw,
        pending_update=parsed.model_dump(mode="python"),
    )
    if mode == "edit":
        catalog = await admin_service.get_catalog()
        try:
            current = _item_from_catalog(
                catalog,
                data["category_id"],
                data.get("subcategory_id"),
                data["item_id"],
            )
        except ObjectChangedError:
            await message.answer(
                "Позиция уже удалена или перемещена. Изменения не сохранены."
            )
            return
        preview = parsed.build_item(
            item_id=current.id,
            photo_file_id=current.photo_file_id,
        )
        await state.set_state(TemplateConfirmationStates.edit)
        await message.answer(
            f"{ADMIN_HEADER}\n\n👀 <b>Предпросмотр полной карточки:</b>"
            f"\n\n{format_item_card(preview)}\n\n"
            "ID, фото и размещение останутся без изменений. Сохранить?",
            reply_markup=build_admin_confirm_keyboard(action="fu"),
        )
        return

    if mode != "create":
        await state.clear()
        await message.answer("Сценарий устарел. Начните создание заново.")
        return

    catalog = await admin_service.get_catalog()
    items = AdminService._items(
        catalog,
        data["category_id"],
        data.get("subcategory_id"),
    )
    try:
        stable_id = generate_stable_id(
            parsed.name,
            {item.id for item in items},
            MAX_STABLE_ID_LENGTH,
        )
    except StableIdGenerationError as exc:
        await state.set_state(TemplateCorrectionStates.input)
        await _show_template_errors(message, [str(exc)])
        return
    preview = parsed.build_item(item_id=stable_id)
    await state.set_state(TemplateConfirmationStates.create)
    await message.answer(
        f"{ADMIN_HEADER}\n\n👀 <b>Предпросмотр новой карточки:</b>"
        f"\n\n{format_item_card(preview)}\n\n"
        f"<b>Технический ID:</b> {preview.id}\n\nСохранить позицию?",
        reply_markup=build_admin_confirm_keyboard(action="fc"),
    )


@router.message(FullEditItemStates.input)
@router.message(TemplateCreateItemStates.input)
@router.message(TemplateCorrectionStates.input)
async def item_template_receive(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    await _process_item_template(
        message,
        state,
        auth_service,
        admin_service,
    )


@router.callback_query(AdminCancelCallback.filter(F.action == "r"))
async def item_template_retry(
    callback: CallbackQuery,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    """Show the rejected text again instead of losing administrator work."""

    if not await require_admin_callback(callback, auth_service):
        return
    if await state.get_state() != TemplateCorrectionStates.input.state:
        return
    data = await state.get_data()
    if isinstance(callback.message, Message):
        await _show_full_template_input(
            callback.message,
            data.get("raw_template") or empty_item_template(),
            creating=data.get("template_mode") == "create",
        )


@router.callback_query(AdminConfirmCallback.filter(F.action == "fu"))
async def item_full_edit_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    """Apply a fully validated update after a fresh permission check."""

    if not await require_admin_callback(callback, auth_service):
        return
    if await state.get_state() != TemplateConfirmationStates.edit.state:
        return
    data = await state.get_data()
    parsed = ParsedItemUpdate.model_validate(data["pending_update"])
    try:
        await admin_service.update_item(
            actor_from_user(callback.from_user),
            data["category_id"],
            data.get("subcategory_id"),
            data["item_id"],
            **parsed.item_changes(),
        )
    except Exception:
        logger.exception("Could not update item from full template")
        if isinstance(callback.message, Message):
            await callback.message.answer("⚠️ Не удалось сохранить изменения")
        return
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.answer("✅ Изменения сохранены")
        await show_item_editor(
            callback.message,
            admin_service,
            data["category_id"],
            data.get("subcategory_id"),
            data["item_id"],
        )


@router.callback_query(AdminConfirmCallback.filter(F.action == "fc"))
async def item_template_create_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    """Create a template item only after validation and confirmation."""

    if not await require_admin_callback(callback, auth_service):
        return
    if await state.get_state() != TemplateConfirmationStates.create.state:
        return
    data = await state.get_data()
    parsed = ParsedItemUpdate.model_validate(data["pending_update"])
    try:
        catalog = await admin_service.get_catalog()
        items = AdminService._items(
            catalog,
            data["category_id"],
            data.get("subcategory_id"),
        )
        stable_id = generate_stable_id(
            parsed.name,
            {item.id for item in items},
            MAX_STABLE_ID_LENGTH,
        )
        item = parsed.build_item(item_id=stable_id)
        await admin_service.create_item(
            actor_from_user(callback.from_user),
            data["category_id"],
            data.get("subcategory_id"),
            item,
        )
    except Exception:
        logger.exception("Could not create item from full template")
        if isinstance(callback.message, Message):
            await callback.message.answer("⚠️ Не удалось сохранить изменения")
        return
    await state.clear()
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            f"{ADMIN_HEADER}\n\n✅ Позиция создана\n\n"
            f"{format_item_card(item)}\n\nДобавить фотографию?",
            build_created_item_keyboard(
                data["category_id"],
                data.get("subcategory_id"),
                item.id,
            ),
        )


@router.callback_query(
    AdminItemCallback.filter(
        F.action.in_({"en", "es", "ed", "ei", "ef", "ep", "et"})
    )
)
async def item_edit_start(
    callback: CallbackQuery,
    callback_data: AdminItemCallback,
    state: FSMContext,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    fields = {
        "en": ("name", "Текущее название"),
        "es": ("short_description", "Текущее краткое описание"),
        "ed": ("description", "Текущее полное описание"),
        "ei": ("ingredients", "Текущий состав"),
        "ef": ("fields", "Текущие характеристики"),
        "ep": ("steps", "Текущее приготовление"),
        "et": ("tags", "Текущие теги"),
    }
    field, label = fields[callback_data.action]
    catalog = await admin_service.get_catalog()
    try:
        item = _item_from_catalog(
            catalog,
            callback_data.category_id,
            _sub_id(callback_data.subcategory_id),
            callback_data.item_id,
        )
    except ObjectChangedError:
        if isinstance(callback.message, Message):
            await callback.message.answer(
                "Позиция уже удалена или перемещена."
            )
        return
    current_text = _serialize_item_field(item, field)
    await state.set_state(EditItemStates.value)
    await state.set_data(
        {
            "category_id": callback_data.category_id,
            "subcategory_id": _sub_id(callback_data.subcategory_id),
            "item_id": callback_data.item_id,
            "field": field,
        }
    )
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            f"{ADMIN_HEADER}\n\n<b>{label}:</b>",
        )
        await _send_copyable_text(
            callback.message,
            current_text,
            placeholder="Новое значение",
        )


@router.message(EditItemStates.value)
async def item_edit_value(
    message: Message,
    state: FSMContext,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_message(message, auth_service):
        return
    data = await state.get_data()
    raw = message.text or ""
    field = data["field"]
    try:
        if field == "ingredients":
            value = [item.model_dump() for item in parse_ingredients(raw)]
        elif field == "fields":
            value = parse_fields(raw)
        elif field == "steps":
            value = parse_steps(raw)
        elif field == "tags":
            value = parse_tags(raw)
        elif field == "name":
            value = raw.strip()
            if not value:
                raise AdminInputError(["Название не может быть пустым"])
        else:
            value = None if raw.strip() == "-" else raw.strip()
    except AdminInputError as exc:
        await message.answer("⚠️ Ошибки:\n" + "\n".join(exc.errors))
        return

    if field in {"ingredients", "fields", "steps", "tags"}:
        await state.update_data(pending_value=value)
        await state.set_state(EditItemStates.confirm)
        await message.answer(
            f"Разобранное значение:\n{escape(str(value))}\n\nПодтвердить?",
            reply_markup=build_admin_confirm_keyboard(
                action="ui",
                category_id=data["category_id"],
                subcategory_id=data.get("subcategory_id") or "_",
                item_id=data["item_id"],
            ),
        )
        return
    await _save_item_field(message, state, admin_service, data, value)


async def _save_item_field(
    message: Message,
    state: FSMContext,
    admin_service: AdminService,
    data: dict,
    value,
) -> None:
    try:
        await admin_service.update_item(
            actor_from_user(message.from_user),
            data["category_id"],
            data.get("subcategory_id"),
            data["item_id"],
            **{data["field"]: value},
        )
    except Exception:
        logger.exception("Could not update item")
        await message.answer("⚠️ Не удалось сохранить изменения")
        return
    await state.clear()
    await message.answer("✅ Изменения сохранены")
    await show_item_editor(
        message,
        admin_service,
        data["category_id"],
        data.get("subcategory_id"),
        data["item_id"],
    )


@router.callback_query(AdminConfirmCallback.filter(F.action == "ui"))
async def item_edit_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    data = await state.get_data()
    try:
        await admin_service.update_item(
            actor_from_user(callback.from_user),
            data["category_id"],
            data.get("subcategory_id"),
            data["item_id"],
            **{data["field"]: data.get("pending_value")},
        )
    except Exception:
        logger.exception("Could not update complex item field")
        if isinstance(callback.message, Message):
            await callback.message.answer("⚠️ Не удалось сохранить изменения")
        return
    await state.clear()
    if isinstance(callback.message, Message):
        await show_item_editor(
            callback.message,
            admin_service,
            data["category_id"],
            data.get("subcategory_id"),
            data["item_id"],
        )


@router.callback_query(AdminItemCallback.filter(F.action == "m"))
async def item_move_start(
    callback: CallbackQuery,
    callback_data: AdminItemCallback,
    state: FSMContext,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    await state.set_state(MoveItemStates.category)
    await state.set_data(
        {
            "source_category_id": callback_data.category_id,
            "source_subcategory_id": _sub_id(callback_data.subcategory_id),
            "item_id": callback_data.item_id,
        }
    )
    catalog = await admin_service.get_catalog()
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            f"{ADMIN_HEADER}\n\nВыберите целевой раздел:",
            build_admin_category_picker(catalog.categories, "m"),
        )


@router.callback_query(AdminItemCallback.filter(F.action == "mc"))
async def item_move_or_list_category(
    callback: CallbackQuery,
    callback_data: AdminItemCallback,
    state: FSMContext,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    """Route category selection to move flow when its FSM is active."""

    if not await require_admin_callback(callback, auth_service):
        return
    if await state.get_state() != MoveItemStates.category.state:
        return
    data = await state.get_data()
    await state.update_data(target_category_id=callback_data.category_id)
    await state.set_state(MoveItemStates.placement)
    catalog = await admin_service.get_catalog()
    category = AdminService._category(catalog, callback_data.category_id)
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            f"{ADMIN_HEADER}\n\nВыберите целевое размещение:",
            build_admin_placement_keyboard(
                category,
                action="mv",
                item_id=data["item_id"],
            ),
        )


@router.callback_query(AdminItemCallback.filter(F.action == "mv"))
async def item_move_confirm_prompt(
    callback: CallbackQuery,
    callback_data: AdminItemCallback,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    await state.update_data(
        target_category_id=callback_data.category_id,
        target_subcategory_id=_sub_id(callback_data.subcategory_id),
    )
    await state.set_state(MoveItemStates.confirm)
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            f"{ADMIN_HEADER}\n\nПереместить позицию в выбранный раздел?",
            build_admin_confirm_keyboard(action="mi"),
        )


@router.callback_query(AdminConfirmCallback.filter(F.action == "mi"))
async def item_move_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    auth_service: AuthService,
    admin_service: AdminService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    data = await state.get_data()
    try:
        await admin_service.move_item(
            actor_from_user(callback.from_user),
            data["source_category_id"],
            data.get("source_subcategory_id"),
            data["item_id"],
            data["target_category_id"],
            data.get("target_subcategory_id"),
        )
    except Exception:
        logger.exception("Could not move item")
        if isinstance(callback.message, Message):
            await callback.message.answer("⚠️ Не удалось сохранить изменения")
        return
    await state.clear()
    if isinstance(callback.message, Message):
        await show_item_editor(
            callback.message,
            admin_service,
            data["target_category_id"],
            data.get("target_subcategory_id"),
            data["item_id"],
        )


@router.callback_query(AdminCancelCallback.filter(F.action == "b"))
async def item_wizard_back(
    callback: CallbackQuery,
    state: FSMContext,
    auth_service: AuthService,
) -> None:
    if not await require_admin_callback(callback, auth_service):
        return
    order = [
        CreateItemStates.name,
        CreateItemStates.short_description,
        CreateItemStates.description,
        CreateItemStates.ingredients,
        CreateItemStates.fields,
        CreateItemStates.steps,
        CreateItemStates.tags,
        CreateItemStates.photo,
        CreateItemStates.sort_order,
        CreateItemStates.is_active,
    ]
    current = await state.get_state()
    states = [entry.state for entry in order]
    if current not in states:
        return
    index = states.index(current)
    previous = order[max(0, index - 1)]
    await state.set_state(previous)
    if isinstance(callback.message, Message):
        await edit_or_send_text(
            callback.message,
            f"{ADMIN_HEADER}\n\n⬅️ Вернитесь к предыдущему значению и "
            "отправьте его заново.",
            build_admin_wizard_keyboard(index > 0),
        )
