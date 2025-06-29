"""
Grants API router - Parent router for grant-related endpoints.
"""

from fastapi import APIRouter

from .messages import router as messages_router

router = APIRouter()

# Include messages sub-router under grants
router.include_router(messages_router, prefix="/{grant_id}/messages", tags=["messages"])

# Future grant-related endpoints can be added here:
# @router.get("/{grant_id}")
# async def get_grant(grant_id: str):
#     """Get grant details"""
#     pass
#
# @router.get("/{grant_id}/calendar")
# async def get_calendar(grant_id: str):
#     """Get calendar for grant"""
#     pass
