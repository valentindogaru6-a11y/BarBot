"""Telegram delivery, history, and recovery UI for warehouse XLSX reports."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from aiogram import F, Router
from aiogram.types import CallbackQuery, FSInputFile, Message

from app.handlers.common import edit_or_send_text
from app.keyboards.warehouse import (
    report_card_keyboard,
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
        f"Версия отчёта: {report['version']}"
    )


async def build_and_send_final_report(
    message: Message,
    service: WarehouseReportService,
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
        logger.exception("Warehouse final report delivery failed")
        await edit_or_send_text(
            message,
            "✅ Смена закрыта, но файл не удалось отправить. "
            "Скачайте готовый отчёт повторно.",
            ready_keyboard,
        )
        return False
    return True


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
        await edit_or_send_text(
            callback.message,
            f"📄 <b>Отчёт за "
            f"{format_business_date(str(report['business_date']))}</b>\n\n"
            f"Версия: {report['version']}\n"
            "Статус: готов\n"
            f"Списание: {report['writeoff_count']} позиций\n"
            f"Перемещение: {report['transfer_count']} позиции",
            report_card_keyboard(
                int(report["id"]), return_page=callback_data.aux
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
) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await build_and_send_final_report(
            callback.message,
            warehouse_report_service,
            shift_id=callback_data.object_id,
            actor_user_id=callback.from_user.id,
            allow_empty=False,
        )


__all__ = ["build_and_send_final_report", "router"]
