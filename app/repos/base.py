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

    @property
    def base_stmt(self) -> Select[tuple[ModelType]]:
        """Base select statement for the model."""
        return select(self._model)

    async def get(self, id: Any) -> ModelType | None:
        """Get a model by ID."""
        return cast(ModelType | None, await db.session.get(self._model, id))

    async def execute(self, query: Select[tuple[ModelType]]) -> ScalarResult[ModelType]:
        """Execute a query and return scalar results."""
        result = await db.session.execute(query)
        return cast(ScalarResult[ModelType], result.scalars())

    async def create(self, **kwargs: Any) -> ModelType:
        """Create a new model instance."""
        instance = self._model(**kwargs)
        db.session.add(instance)
        await db.session.flush()  # Flush to get the ID
        return instance

    async def delete(self, model: ModelType) -> None:
        """Delete a model instance."""
        await db.session.delete(model)

    async def commit(self) -> None:
        """Commit the current transaction."""
        await db.session.commit()

    async def rollback(self) -> None:
        """Rollback the current transaction."""
        await db.session.rollback()

    async def flush(self) -> None:
        """Flush the current session."""
        await db.session.flush()
