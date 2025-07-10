from fastapi import APIRouter

from app.api.v3.connect import router as connect_router
from app.api.v3.grants import router as grants_router

api_router = APIRouter()

api_router.include_router(connect_router, prefix="/connect", tags=["oauth2"])
api_router.include_router(grants_router, prefix="/grants", tags=["grants"])
