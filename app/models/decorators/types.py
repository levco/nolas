import logging
from enum import Enum
from typing import Any, TypeVar

from sqlalchemy import String
from sqlalchemy.types import TypeDecorator

EnumT = TypeVar("EnumT", bound=Enum)


class EnumStringType(TypeDecorator[EnumT]):
    impl = String(50)
    cache_ok = True

    def __init__(self, enum_class: type[EnumT], *args: Any, **kwargs: Any):
        super(EnumStringType, self).__init__(*args, **kwargs)
        self._enum_class = enum_class
        self._missing_fails_on_load = kwargs.get("missing_fails_on_load", True)
        self._logger = logging.getLogger(__name__)

    def process_bind_param(self, value: EnumT | str | None, dialect: Any) -> str | None:
        if value is not None:
            # There is a chance test factories may pass in a string OR relationship
            # joins in model using String would require the enum to be passed in as a string
            # This is a workaround to handle both cases.
            if isinstance(value, str):
                try:
                    value = self._enum_class[value]
                except KeyError:
                    self._logger.error(f"Invalid enum value: {value} for {self._enum_class}")
                    return None
            return value.name
        return None

    def process_result_value(self, name: str | None, dialect: Any) -> EnumT | None:
        if name is not None:
            try:
                return self._enum_class[name]
            except KeyError:
                if self._missing_fails_on_load:
                    raise ValueError(f"Invalid enum value: {name} for {self._enum_class}")
                self._logger.warning(f"Invalid enum value: {name} for {self._enum_class}, returning value as is")
                return name  # type: ignore
        return None
