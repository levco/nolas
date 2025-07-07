from typing import Any, Generic, TypeVar, cast

from fastapi_async_sqlalchemy import db
from sqlalchemy import ScalarResult, select
from sqlalchemy.sql.selectable import Select

from app.models.base import Base

ModelType = TypeVar("ModelType", bound=Base)


class BaseRepo(Generic[ModelType]):
    """Simplified base repository using fastapi_async_sqlalchemy directly."""

    def __init__(self, model: type[ModelType]) -> None:
        self._model = model
        self._db = db

    @property
    def base_stmt(self) -> Select[tuple[ModelType]]:
        """Base select statement for the model."""
        return select(self._model)

    async def get(self, id: Any) -> ModelType | None:
        """Get a model by ID."""
        return cast(ModelType | None, await self._db.session.get(self._model, id))

    async def execute(self, query: Select[tuple[ModelType]]) -> ScalarResult[ModelType]:
        """Execute a query and return scalar results."""
        result = await self._db.session.execute(query)
        return cast(ScalarResult[ModelType], result.scalars())

    async def add(self, model: ModelType, commit: bool = False) -> None:
        """Add a model instance."""
        self._db.session.add(model)
        if commit:
            await self.commit()
        else:
            await self.flush()

    async def delete(self, model: ModelType) -> None:
        """Delete a model instance."""
        await self._db.session.delete(model)

    async def commit(self) -> None:
        """Commit the current transaction."""
        await self._db.session.commit()

    async def rollback(self) -> None:
        """Rollback the current transaction."""
        await self._db.session.rollback()

    async def flush(self) -> None:
        """Flush the current session."""
        await self._db.session.flush()
