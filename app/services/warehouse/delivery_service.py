"""Authorized, idempotent orchestration of ready report deliveries."""

from __future__ import annotations

import asyncio
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any

from app.models.user import Role
from app.services.access_service import AccessDeniedError, AccessService
from app.services.warehouse.business_date_service import (
    BusinessDateService,
    Clock,
    utc_rfc3339,
)
from app.services.warehouse.report_service import WarehouseReportService
from app.services.warehouse.telegram_delivery import ReportSender
from app.storage.warehouse_delivery_database import WarehouseDeliveryDatabase


class WarehouseReportDeliveryService:
    STALE_SENDING_TIMEOUT = timedelta(minutes=15)

    def __init__(
        self,
        repository: WarehouseDeliveryDatabase,
        reports: WarehouseReportService,
        access: AccessService,
        sender: ReportSender,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.repository = repository
        self.reports = reports
        self.access = access
        self.sender = sender
        self.clock = clock

    def _now_datetime(self):
        return BusinessDateService(clock=self.clock).clock.now().astimezone(UTC)

    def _authorize_send(self, actor_user_id: int) -> None:
        self.access.require_warehouse_report_delivery(actor_user_id)

    def _authorize_report_send(self, report: dict[str, Any], actor_user_id: int) -> None:
        self._authorize_send(actor_user_id)
        if report.get("correction_session_id") is not None:
            self.access.require_warehouse_correction_management(actor_user_id)

    async def deliver(
        self,
        report_id: int,
        *,
        actor_user_id: int,
        retry_failed: bool = False,
        recipient_ids: tuple[int, ...] | None = None,
        caption: str | None = None,
        require_all_recipients_active: bool = False,
    ) -> dict[str, Any]:
        self._authorize_send(actor_user_id)
        ready = await asyncio.to_thread(
            self.reports.download, report_id, actor_user_id=actor_user_id
        )
        report = ready["report"]
        artifact = ready["artifact"]
        self._authorize_report_send(report, actor_user_id)
        now_value = self._now_datetime()
        deliveries = await asyncio.to_thread(
            self.repository.prepare_deliveries,
            report_id,
            actor_user_id=actor_user_id,
            now=utc_rfc3339(now_value),
            stale_before=utc_rfc3339(now_value - self.STALE_SENDING_TIMEOUT),
            retry_failed=retry_failed,
            authorize=lambda: self._authorize_report_send(report, actor_user_id),
            recipient_ids=recipient_ids,
            require_all_active=require_all_recipients_active,
        )
        for delivery in deliveries:
            attempt_time = utc_rfc3339(self._now_datetime())
            claimed = await asyncio.to_thread(
                self.repository.claim_delivery,
                int(delivery["id"]),
                allow_failed=retry_failed,
                actor_user_id=actor_user_id,
                now=attempt_time,
                authorize=lambda: self._authorize_report_send(report, actor_user_id),
            )
            if claimed is None:
                continue
            try:
                message_id = await self.sender.send_report(
                    chat_id=int(claimed["chat_id"]),
                    path=Path(artifact.path),
                    filename=Path(artifact.relative_file_path).name,
                    caption=caption or f"Складской отчёт за {report['business_date']}",
                )
            except Exception as exc:
                code = type(exc).__name__.casefold()[:100] or "telegram_error"
                await asyncio.to_thread(
                    self.repository.mark_failed,
                    int(claimed["id"]),
                    error_code=code,
                    error_text="Telegram не принял документ",
                    actor_user_id=actor_user_id,
                    now=utc_rfc3339(self._now_datetime()),
                )
            else:
                await asyncio.to_thread(
                    self.repository.mark_delivered,
                    int(claimed["id"]),
                    telegram_message_id=message_id,
                    actor_user_id=actor_user_id,
                    now=utc_rfc3339(self._now_datetime()),
                )
        history = await asyncio.to_thread(
            self.repository.deliveries_for_report, report_id
        )
        return {
            "report": report,
            "artifact": artifact,
            "deliveries": history,
            "delivered": sum(row["status"] == "delivered" for row in history),
            "failed": sum(row["status"] == "failed" for row in history),
            "pending": sum(row["status"] in {"pending", "sending"} for row in history),
        }

    async def deliver_scheduled(
        self, report_id: int, *, recipient_ids: tuple[int, ...], retry_failed: bool = True
    ) -> dict[str, Any]:
        """System path for v10 schedules; reuses v8 delivery rows and sender."""
        report = await asyncio.to_thread(self.reports.repository.get_report, report_id)
        if report is None or report["status"] != "ready":
            raise ValueError("Report is not ready")
        if report.get("correction_session_id") is not None:
            raise ValueError("Correction reports require explicit manual delivery")
        artifact = await asyncio.to_thread(self.reports.xlsx.verify_ready, report)
        now_value = self._now_datetime()
        deliveries = await asyncio.to_thread(
            self.repository.prepare_deliveries, report_id, actor_user_id=0,
            now=utc_rfc3339(now_value), stale_before=utc_rfc3339(now_value-self.STALE_SENDING_TIMEOUT),
            retry_failed=retry_failed, authorize=lambda: None, recipient_ids=recipient_ids,
        )
        for delivery in deliveries:
            now = utc_rfc3339(self._now_datetime())
            claimed = await asyncio.to_thread(self.repository.claim_delivery, int(delivery["id"]), allow_failed=retry_failed, actor_user_id=0, now=now, authorize=lambda: None)
            if claimed is None:
                continue
            try:
                message_id = await self.sender.send_report(chat_id=int(claimed["chat_id"]), path=Path(artifact.path), filename=Path(artifact.relative_file_path).name, caption=f"Складской отчёт за {report['business_date']}")
            except Exception as exc:
                await asyncio.to_thread(self.repository.mark_failed, int(claimed["id"]), error_code=type(exc).__name__.casefold()[:100] or "telegram_error", error_text="Telegram не принял документ", actor_user_id=0, now=utc_rfc3339(self._now_datetime()))
            else:
                await asyncio.to_thread(self.repository.mark_delivered, int(claimed["id"]), telegram_message_id=message_id, actor_user_id=0, now=utc_rfc3339(self._now_datetime()))
        history = await asyncio.to_thread(self.repository.deliveries_for_report, report_id)
        selected = [row for row in history if int(row["recipient_id"]) in recipient_ids]
        return {"report": report, "artifact": artifact, "deliveries": selected, "delivered": sum(row["status"] == "delivered" for row in selected), "failed": sum(row["status"] == "failed" for row in selected), "pending": sum(row["status"] in {"pending", "sending"} for row in selected)}

    def history(self, report_id: int, *, actor_user_id: int) -> dict[str, Any]:
        report = self.reports.report_card(report_id, actor_user_id=actor_user_id)
        context = self.access.get_context(actor_user_id)
        rows = self.repository.deliveries_for_report(report_id)
        can_view_details = context.role in {
            Role.SUPER_ADMIN, Role.ADMIN, Role.ACCOUNTANT,
        }
        if can_view_details:
            self.access.require_warehouse_delivery_history(actor_user_id)
        if not can_view_details:
            if (
                context.role is not Role.BARTENDER
                or report.get("closed_by") != actor_user_id
            ):
                raise AccessDeniedError("Недостаточно прав для истории доставки")
        return {
            "report": report,
            "details": rows if can_view_details else [],
            "can_view_details": can_view_details,
            "delivered": sum(row["status"] == "delivered" for row in rows),
            "failed": sum(row["status"] == "failed" for row in rows),
            "pending": sum(row["status"] in {"pending", "sending"} for row in rows),
        }


__all__ = ["WarehouseReportDeliveryService"]
