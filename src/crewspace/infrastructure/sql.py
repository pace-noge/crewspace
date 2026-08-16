"""Small SQLAlchemy Core adapter used by repository implementations."""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import CursorResult, Row
from sqlalchemy.ext.asyncio import AsyncConnection


class MappingRow:
    """Stable mapping row independent of SQLAlchemy or driver internals."""

    def __init__(self, row: Row[Any]) -> None:
        self._values = dict(row._mapping)
        self._sequence = tuple(row)

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return self._sequence[key]
        return self._values[key]

    def keys(self):
        return self._values.keys()


class AsyncResult:
    def __init__(self, result: CursorResult[Any]) -> None:
        self._result = result

    @property
    def rowcount(self) -> int:
        return self._result.rowcount

    async def fetchone(self) -> MappingRow | None:
        row = self._result.fetchone()
        return MappingRow(row) if row is not None else None

    async def fetchall(self) -> list[MappingRow]:
        return [MappingRow(row) for row in self._result.fetchall()]


class SqlAlchemyConnection:
    """AsyncConnection facade with the repository's compact execute API.

    Existing repository SQL uses DB-API qmark parameters. Statements are converted
    to SQLAlchemy named binds, so the same repository implementation works with
    SQLite and PostgreSQL drivers.
    """

    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    @property
    def dialect_name(self) -> str:
        return self._connection.dialect.name

    async def execute(
        self, statement: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> AsyncResult:
        sql, bound = _bind(statement, params)
        result = await self._connection.execute(text(sql), bound)
        return AsyncResult(result)

    async def executemany(
        self, statement: str, param_sets: Iterable[Sequence[Any] | Mapping[str, Any]]
    ) -> AsyncResult:
        values = list(param_sets)
        if not values:
            return await self.execute("SELECT 1 WHERE 0=1")
        sql, first = _bind(statement, values[0])
        bound = [first]
        for params in values[1:]:
            _, item = _bind(statement, params)
            bound.append(item)
        result = await self._connection.execute(text(sql), bound)
        return AsyncResult(result)

    async def executescript(self, script: str) -> None:
        for statement in (part.strip() for part in script.split(";")):
            if statement:
                await self.execute(statement)

    async def commit(self) -> None:
        await self._connection.commit()

    async def rollback(self) -> None:
        await self._connection.rollback()


def _bind(
    statement: str, params: Sequence[Any] | Mapping[str, Any]
) -> tuple[str, Mapping[str, Any]]:
    if isinstance(params, Mapping):
        return statement, params
    values = tuple(params)
    if not values:
        return statement, {}
    parts = statement.split("?")
    if len(parts) - 1 != len(values):
        raise ValueError("SQL parameter count does not match qmark placeholders")
    chunks = [parts[0]]
    bound: dict[str, Any] = {}
    for index, value in enumerate(values):
        name = f"p{index}"
        chunks.extend((f":{name}", parts[index + 1]))
        bound[name] = value
    return "".join(chunks), bound
