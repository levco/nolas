"""
Grants API router - Parent router for grant-related endpoints.
"""

from fastapi import APIRouter

from .attachments import router as attachments_router
from .folders import router as folders_router
from .messages import router as messages_router

router = APIRouter()

# Include sub-routers under grants
router.include_router(attachments_router, prefix="/{grant_id}/attachments", tags=["attachments"])
router.include_router(messages_router, prefix="/{grant_id}/messages", tags=["messages"])
router.include_router(folders_router, prefix="/{grant_id}/folders", tags=["folders"])
