"""Aggregate router for the separated administrator interface."""

from aiogram import Router

from app.handlers.admin import (
    categories,
    common,
    items,
    photos,
    subcategories,
    users,
)

router = Router(name=__name__)
router.include_router(common.router)
router.include_router(users.router)
router.include_router(categories.router)
router.include_router(subcategories.router)
router.include_router(items.router)
router.include_router(photos.router)

__all__ = ["router"]
