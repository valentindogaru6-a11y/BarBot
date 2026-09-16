"""Telegram delivery, history, and recovery UI for warehouse XLSX reports."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from html import escape
from pathlib import Path

from aiogram import F, Router
from aiogram.types import CallbackQuery, FSInputFile, Message

from app.handlers.common import edit_or_send_text
from app.keyboards.warehouse import (
    report_card_keyboard,
    report_delivery_result_keyboard,
    report_history_keyboard,
    report_ready_keyboard,
    report_retry_keyboard,
)
from app.keyboards.warehouse_callbacks import WarehouseCallback
from app.services.access_service import AccessDeniedError
from app.services.warehouse.formatting import format_business_date
from app.services.warehouse.report_service import (
    WarehouseReportService,
    WarehouseReportsError,
)
from app.services.warehouse.delivery_service import WarehouseReportDeliveryService
from app.services.warehouse.correction_service import WarehouseCorrectionService
from app.services.warehouse.xlsx_report_service import WarehouseXlsxError

router = Router(name=__name__)
logger = logging.getLogger(__name__)


async def _thread(function, /, *args, **kwargs):
    return await asyncio.to_thread(function, *args, **kwargs)


def _safe_error(error: Exception) -> str:
    if isinstance(error, AccessDeniedError):
        return "Недостаточно прав."
    if isinstance(error, WarehouseXlsxError):
        return "Файл отчёта недоступен или повреждён. Обратитесь к администратору."
    if isinstance(error, WarehouseReportsError):
        return "Отчёт недоступен. Обновите раздел и попробуйте ещё раз."
    return "Не удалось сформировать отчёт. Попробуйте ещё раз."


def _ready_caption(report: dict) -> str:
    return (
        "✅ <b>Смена закрыта</b>\n\n"
        f"Списание: {report['writeoff_count']} итоговых позиций\n"
        f"Перемещение: {report['transfer_count']} итоговые позиции\n"
        f"Проработка: {report.get('development_count', 0)} позиций в "
        f"{report.get('development_group_count', 0)} проработках\n"
        f"Версия отчёта: {report['version']}"
    )


async def build_and_send_final_report(
    message: Message,
    service: WarehouseReportService,
    delivery_service: WarehouseReportDeliveryService,
    *,
    shift_id: int,
    actor_user_id: int,
    allow_empty: bool,
) -> bool:
    """Build outside the event loop and deliver, retaining a retry route."""

    await edit_or_send_text(message, "⏳ Формирую итоговый отчёт…")
    try:
        result = await _thread(
            service.build_final,
            shift_id,
            actor_user_id=actor_user_id,
            allow_empty=allow_empty,
        )
    except Exception as exc:
        logger.exception("Warehouse final report generation failed")
        await edit_or_send_text(
            message,
            _safe_error(exc),
            report_retry_keyboard(shift_id),
        )
        return False
    report = result["report"]
    artifact = result["artifact"]
    ready_keyboard = report_ready_keyboard(int(report["id"]))
    await edit_or_send_text(message, "✅ Смена закрыта. Отчёт сформирован.")
    actor_file_sent = True
    try:
        await message.answer_document(
            FSInputFile(
                artifact.path,
                filename=Path(artifact.relative_file_path).name,
            ),
            caption=_ready_caption(report),
            reply_markup=ready_keyboard,
        )
    except Exception:
        actor_file_sent = False
        logger.exception("Warehouse final report delivery failed")
        await edit_or_send_text(
            message,
            "✅ Смена закрыта, но файл не удалось отправить. "
            "Скачайте готовый отчёт повторно.",
            ready_keyboard,
        )
    try:
        delivery = await delivery_service.deliver(
            int(report["id"]), actor_user_id=actor_user_id
        )
    except Exception:
        logger.exception("Warehouse recipient delivery orchestration failed")
        await message.answer(
            "Смена закрыта, но отправку получателям не удалось выполнить. "
            "Откройте статусы доставки и повторите попытку.",
            reply_markup=report_delivery_result_keyboard(
                int(report["id"]), has_failed=True, can_send=True
            ),
        )
        return False
    context = await _thread(delivery_service.access.get_context, actor_user_id)
    if not delivery["deliveries"]:
        delivery_text = (
            "⚠️ Активных получателей нет. Автоматическая отправка не выполнялась."
        )
    else:
        delivery_text = (
            f"✅ Доставлено: {delivery['delivered']}\n"
            f"⚠️ Ошибка: {delivery['failed']}"
        )
        if delivery["pending"]:
            delivery_text += f"\n⏳ Не завершено: {delivery['pending']}"
    await message.answer(
        delivery_text,
        reply_markup=report_delivery_result_keyboard(
            int(report["id"]),
            has_failed=bool(delivery["failed"] or delivery["pending"]),
            can_manage_recipients=(
                context.can_manage_warehouse_report_recipients
            ),
            can_send=context.can_send_warehouse_reports,
        ),
    )
    return actor_file_sent and not delivery["failed"] and not delivery["pending"]


@router.callback_query(WarehouseCallback.filter(F.action == "df"))
async def report_draft(
    callback: CallbackQuery,
    warehouse_report_service: WarehouseReportService,
) -> None:
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    try:
        with tempfile.TemporaryDirectory(prefix="barbot-warehouse-draft-") as directory:
            result = await _thread(
                warehouse_report_service.draft,
                actor_user_id=callback.from_user.id,
                directory=directory,
            )
            caption = "Предварительный отчёт. Смена ещё не закрыта."
            if result["is_empty"]:
                caption += "\n\nВ текущей смене пока нет операций."
            await callback.message.answer_document(
                FSInputFile(result["path"], filename=Path(result["path"]).name),
                caption=caption,
            )
    except Exception as exc:
        logger.exception("Warehouse draft report failed")
        await callback.message.answer(_safe_error(exc))


async def _show_history(
    message: Message,
    service: WarehouseReportService,
    *,
    actor_user_id: int,
    page: int,
) -> None:
    reports, total = await _thread(
        service.history,
        actor_user_id=actor_user_id,
        page=max(page, 0),
    )
    text = "📁 <b>История отчётов</b>"
    if not reports:
        text += "\n\nГотовых отчётов пока нет."
    await edit_or_send_text(
        message,
        text,
        report_history_keyboard(
            reports,
            page=max(page, 0),
            total=total,
        ),
    )


@router.callback_query(WarehouseCallback.filter(F.action == "hl"))
async def report_history(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    warehouse_report_service: WarehouseReportService,
) -> None:
    try:
        if isinstance(callback.message, Message):
            await _show_history(
                callback.message,
                warehouse_report_service,
                actor_user_id=callback.from_user.id,
                page=callback_data.object_id,
            )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await callback.answer()


@router.callback_query(WarehouseCallback.filter(F.action == "hv"))
async def report_card(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    warehouse_report_service: WarehouseReportService,
    warehouse_delivery_service: WarehouseReportDeliveryService,
    warehouse_correction_service: WarehouseCorrectionService,
) -> None:
    try:
        report = await _thread(
            warehouse_report_service.report_card,
            callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        context = await _thread(
            warehouse_delivery_service.access.get_context,
            callback.from_user.id,
        )
        correction = (
            await _thread(
                warehouse_correction_service.report_entry,
                int(report["id"]),
                actor_user_id=callback.from_user.id,
            )
            if context.can_view_warehouse_corrections
            else {
                "correction_session_id": None,
                "active_session_id": None,
                "can_open": False,
                "shift_revision": 0,
                "correction_reason": None,
                "delivered_count": 0,
                "failed_count": 0,
                "pending_count": 0,
            }
        )
        version_kind = (
            "Исправленная версия"
            if report.get("correction_session_id") is not None
            else "Исходный отчёт"
        )
        is_correction = report.get("correction_session_id") is not None
        correction_details = ""
        if correction.get("correction_reason"):
            correction_details = (
                f"\nПричина исправления: "
                f"{escape(str(correction['correction_reason']))}"
            )
        delivery_details = ""
        if context.can_view_warehouse_corrections:
            delivery_details = (
                f"\nДоставка: ✅ {int(correction['delivered_count'])}, "
                f"⚠️ {int(correction['failed_count'])}, "
                f"⏳ {int(correction['pending_count'])}"
            )
        await edit_or_send_text(
            callback.message,
            f"📄 <b>Отчёт за "
            f"{format_business_date(str(report['business_date']))}</b>\n\n"
            f"Версия: v{int(report['version']):03d} — {version_kind}\n"
            "Статус: готов\n"
            f"Списание: {report['writeoff_count']} позиций\n"
            f"Перемещение: {report['transfer_count']} позиции\n"
            f"Проработка: {report.get('development_count', 0)} позиций в "
            f"{report.get('development_group_count', 0)} проработках"
            f"{correction_details}{delivery_details}",
            report_card_keyboard(
                int(report["id"]), return_page=callback_data.aux,
                can_send=context.can_send_warehouse_reports and not is_correction,
                correction_session_id=correction["active_session_id"],
                can_open_correction=bool(correction["can_open"]),
                shift_revision=int(correction["shift_revision"]),
                can_manage_correction=context.can_manage_warehouse_corrections,
                result_correction_session_id=(
                    int(correction["correction_session_id"])
                    if (
                        context.can_view_warehouse_corrections
                        and correction.get("correction_session_id") is not None
                    ) else None
                ),
            ),
        )


@router.callback_query(WarehouseCallback.filter(F.action == "hd"))
async def report_download(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    warehouse_report_service: WarehouseReportService,
) -> None:
    try:
        result = await _thread(
            warehouse_report_service.download,
            callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        report = result["report"]
        artifact = result["artifact"]
        await callback.message.answer_document(
            FSInputFile(
                artifact.path,
                filename=Path(artifact.relative_file_path).name,
            ),
            caption=(
                f"📄 Отчёт за "
                f"{format_business_date(str(report['business_date']))}, "
                f"версия {report['version']}."
            ),
            reply_markup=report_ready_keyboard(int(report["id"])),
        )


@router.callback_query(WarehouseCallback.filter(F.action == "fr"))
async def report_retry(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    warehouse_report_service: WarehouseReportService,
    warehouse_delivery_service: WarehouseReportDeliveryService,
) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await build_and_send_final_report(
            callback.message,
            warehouse_report_service,
            warehouse_delivery_service,
            shift_id=callback_data.object_id,
            actor_user_id=callback.from_user.id,
            allow_empty=False,
        )


@router.callback_query(WarehouseCallback.filter(F.action == "ds"))
async def report_delivery_retry(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    warehouse_delivery_service: WarehouseReportDeliveryService,
) -> None:
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    try:
        result = await warehouse_delivery_service.deliver(
            callback_data.object_id,
            actor_user_id=callback.from_user.id,
            retry_failed=True,
        )
    except Exception as exc:
        await callback.message.answer(_safe_error(exc))
        return
    await edit_or_send_text(
        callback.message,
        f"✅ Доставлено: {result['delivered']}\n"
        f"⚠️ Ошибка: {result['failed']}"
        + (f"\n⏳ Не завершено: {result['pending']}" if result["pending"] else ""),
        report_delivery_result_keyboard(
            callback_data.object_id,
            has_failed=bool(result["failed"] or result["pending"]),
            can_send=True,
        ),
    )


@router.callback_query(WarehouseCallback.filter(F.action == "da"))
async def report_delivery_send(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    warehouse_delivery_service: WarehouseReportDeliveryService,
) -> None:
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    try:
        result = await warehouse_delivery_service.deliver(
            callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.message.answer(_safe_error(exc))
        return
    text = (
        "⚠️ Активных получателей нет. Отправка не выполнялась."
        if not result["deliveries"]
        else f"✅ Доставлено: {result['delivered']}\n⚠️ Ошибка: {result['failed']}"
    )
    await edit_or_send_text(
        callback.message,
        text,
        report_delivery_result_keyboard(
            callback_data.object_id,
            has_failed=bool(result["failed"] or result["pending"]),
            can_send=True,
        ),
    )


@router.callback_query(WarehouseCallback.filter(F.action == "dh"))
async def report_delivery_history(
    callback: CallbackQuery,
    callback_data: WarehouseCallback,
    warehouse_delivery_service: WarehouseReportDeliveryService,
) -> None:
    try:
        result = await _thread(
            warehouse_delivery_service.history,
            callback_data.object_id,
            actor_user_id=callback.from_user.id,
        )
    except Exception as exc:
        await callback.answer(_safe_error(exc), show_alert=True)
        return
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    text = (
        "📨 <b>Доставка отчёта</b>\n\n"
        f"✅ Доставлено: {result['delivered']}\n"
        f"⚠️ Ошибка: {result['failed']}\n"
        f"⏳ Не завершено: {result['pending']}"
    )
    if result["can_view_details"] and result["details"]:
        labels = {
            "pending": "⏳ ожидает",
            "sending": "⏳ отправляется",
            "delivered": "✅ доставлено",
            "failed": "⚠️ ошибка",
        }
        text += "\n\n" + "\n".join(
            f"• {escape(str(row['recipient_title']))}: {labels[row['status']]}"
            for row in result["details"]
        )
    await edit_or_send_text(
        callback.message,
        text,
        report_delivery_result_keyboard(
            callback_data.object_id,
            has_failed=bool(result["failed"] or result["pending"]),
            can_send=warehouse_delivery_service.access.can_send_warehouse_reports(
                callback.from_user.id
            ),
        ),
    )


__all__ = ["build_and_send_final_report", "router"]
