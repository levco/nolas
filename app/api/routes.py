from fastapi import APIRouter

from app.api.v3.grants import router as grants_router

api_router = APIRouter()

api_router.include_router(grants_router, prefix="/grants", tags=["grants"])
