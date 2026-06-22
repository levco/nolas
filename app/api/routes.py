from fastapi import APIRouter

from app.api.v3.connect import router as connect_router
from app.api.v3.grants import router as grants_router
from app.api.v3.notifications import router as notifications_router
from app.api.v3.providers import router as providers_router

api_router = APIRouter()

api_router.include_router(connect_router, prefix="/connect", tags=["oauth2"])
api_router.include_router(grants_router, prefix="/grants", tags=["grants"])
api_router.include_router(providers_router, prefix="/providers", tags=["providers"])
api_router.include_router(notifications_router, prefix="/notifications", tags=["notifications"])
