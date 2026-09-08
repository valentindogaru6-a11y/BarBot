"""Inline user-management keyboards and the temporary user picker."""

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestUsers,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.keyboards.admin_callbacks import AdminPanelCallback
from app.keyboards.callbacks import MainCallback
from app.keyboards.user_callbacks import (
    UserAdminCallback,
    UserListCallback,
    UserRoleCallback,
    UserViewCallback,
)
from app.models.user import Role, UserRecord
from app.utils.button_labels import user_button_label

ROLE_LABELS = {
    Role.SUPER_ADMIN: "👑 Super Admin",
    Role.ADMIN: "👑 Admin",
    Role.BARTENDER: "🍸 Bartender",
    Role.ACCOUNTANT: "🧾 Бухгалтер",
    Role.USER: "👤 User",
    Role.BLOCKED: "🚫 Blocked",
}


def build_user_management_keyboard(
    *,
    can_assign_admin: bool,
    can_assign_accountant: bool,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if can_assign_admin:
        builder.button(
            text="➕ Добавить администратора",
            callback_data=UserAdminCallback(action="pa"),
        )
    builder.button(
        text="➕ Добавить бармена",
        callback_data=UserAdminCallback(action="pb"),
    )
    if can_assign_accountant:
        builder.button(
            text="➕ Добавить бухгалтера",
            callback_data=UserAdminCallback(action="pc"),
        )
    for text, role in (
        ("👑 Администраторы", Role.ADMIN),
        ("🍸 Бармены", Role.BARTENDER),
        ("🧾 Бухгалтеры", Role.ACCOUNTANT),
        ("👤 Пользователи", Role.USER),
        ("🚫 Заблокированные", Role.BLOCKED),
    ):
        builder.button(
            text=text,
            callback_data=UserListCallback(role=role.value),
        )
    builder.button(
        text="🔎 Найти пользователя",
        callback_data=UserAdminCallback(action="search"),
    )
    builder.button(
        text="⬅️ Назад",
        callback_data=AdminPanelCallback(action="o"),
    )
    builder.button(
        text="🏠 Главное меню",
        callback_data=MainCallback(action="home"),
    )
    builder.adjust(1)
    return builder.as_markup()


def build_picker_reply_keyboard(request_id: int) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="👤 Выбрать пользователя",
                    request_users=KeyboardButtonRequestUsers(
                        request_id=request_id,
                        user_is_bot=False,
                        max_quantity=1,
                        request_name=True,
                        request_username=True,
                    ),
                )
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
        is_persistent=False,
    )


def build_picker_inline_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="📋 Выбрать из пользователей бота",
        callback_data=UserAdminCallback(action="known"),
    )
    builder.button(
        text="❌ Отмена",
        callback_data=UserAdminCallback(action="cancel"),
    )
    builder.adjust(1)
    return builder.as_markup()


def build_user_cancel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="❌ Отмена",
        callback_data=UserAdminCallback(action="menu"),
    )
    return builder.as_markup()


def build_users_list_keyboard(
    users: list[tuple[UserRecord, Role]],
    *,
    role: str,
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for user, _effective_role in users:
        builder.button(
            text=user_button_label(user.display_name, user.username),
            callback_data=UserViewCallback(
                user_id=user.telegram_user_id,
                role=role,
                page=page,
            ),
        )
    builder.adjust(1)
    if total_pages > 1:
        page_buttons = []
        if page > 0:
            page_buttons.append(
                InlineKeyboardButton(
                    text="◀️",
                    callback_data=UserListCallback(
                        role=role,
                        page=page - 1,
                    ).pack(),
                )
            )
        page_buttons.append(
            InlineKeyboardButton(
                text=f"{page + 1}/{total_pages}",
                callback_data=UserListCallback(role=role, page=page).pack(),
            )
        )
        if page < total_pages - 1:
            page_buttons.append(
                InlineKeyboardButton(
                    text="▶️",
                    callback_data=UserListCallback(
                        role=role,
                        page=page + 1,
                    ).pack(),
                )
            )
        builder.row(*page_buttons)
    builder.button(
        text="⬅️ К управлению пользователями",
        callback_data=UserAdminCallback(action="menu"),
    )
    return builder.as_markup()


def build_user_card_keyboard(
    *,
    target_id: int,
    current_role: Role,
    allowed_roles: list[Role],
    back_role: str,
    back_page: int,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if current_role is Role.BLOCKED and Role.USER in allowed_roles:
        builder.button(
            text="✅ Разблокировать",
            callback_data=UserRoleCallback(
                action="ask",
                user_id=target_id,
                role=Role.USER.value,
            ),
        )
    else:
        labels = {
            Role.ADMIN: "🛠 Назначить Admin",
            Role.BARTENDER: "🍸 Назначить Bartender",
            Role.ACCOUNTANT: "🧾 Назначить бухгалтера",
            Role.USER: "👤 Сделать User",
            Role.BLOCKED: "🚫 Заблокировать",
        }
        for role in allowed_roles:
            builder.button(
                text=labels[role],
                callback_data=UserRoleCallback(
                    action="ask",
                    user_id=target_id,
                    role=role.value,
                ),
            )
    builder.button(
        text="⬅️ Назад",
        callback_data=UserListCallback(role=back_role, page=back_page),
    )
    builder.adjust(1)
    return builder.as_markup()


def build_role_confirmation_keyboard(
    target_id: int,
    role: Role,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Подтвердить",
        callback_data=UserRoleCallback(
            action="confirm",
            user_id=target_id,
            role=role.value,
        ),
    )
    builder.button(
        text="❌ Отмена",
        callback_data=UserAdminCallback(action="menu"),
    )
    builder.adjust(2)
    return builder.as_markup()
