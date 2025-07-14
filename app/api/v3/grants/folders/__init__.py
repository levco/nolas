from fastapi import APIRouter, Path
from fastapi.responses import JSONResponse

from app.api.payloads.error import APIError
from app.api.payloads.folders import Folder, FolderResponse

router = APIRouter()


@router.get(
    "/{folder_id}",
    response_model=FolderResponse,
    responses={
        400: {"model": APIError, "description": "Invalid grant"},
        404: {"model": APIError, "description": "Message not found"},
        500: {"model": APIError, "description": "Internal server error"},
    },
    summary="Get a specific folder",
    description="Gets a specific folder by ID for the specified grant",
)
async def get_folder(
    grant_id: str = Path(..., example="a3ec500d-126b-4532-a632-7808721b3732"),
    folder_id: str = Path(..., example="Sent"),
) -> FolderResponse | JSONResponse:
    # TODO: Implement this
    return FolderResponse(data=Folder(id=folder_id, name=folder_id, grant_id=grant_id))
