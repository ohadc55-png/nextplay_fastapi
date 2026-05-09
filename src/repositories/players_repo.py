"""Players + player_metrics + player_game_stats repositories.

All three are tenant-scoped via TeamScopedRepository. PlayerMetrics has an
`upsert` that mirrors v1.0-flask `db/__init__.py:989-1001`. We use a
select-then-insert-or-update pattern instead of `ON CONFLICT DO UPDATE`
so the same code works on Postgres and SQLite without dialect detection
(detecting the dialect from an AsyncSession requires touching `bind`,
which throws `MissingGreenlet` outside an active connection context).
The race window is in-process: a service composing this with a transaction
keeps it atomic at the DB level.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.players import Player, PlayerGameStat, PlayerMetric
from src.repositories.base_repository import TeamScopedRepository


class PlayersRepository(TeamScopedRepository[Player]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, Player)

    async def list_active(self, *, user_id: int | None, team_id: int | None) -> list[Player]:
        """Active roster ordered by jersey number. Mirrors `db/__init__.py:301`.
        Defensive: returns [] if both args missing (at-least-one-of)."""
        if user_id is None and team_id is None:
            return []
        stmt = select(Player).where(Player.active.is_(True))
        if user_id is not None:
            stmt = stmt.where(Player.user_id == user_id)
        if team_id is not None:
            stmt = stmt.where(Player.team_id == team_id)
        stmt = stmt.order_by(Player.number)
        return list((await self.session.execute(stmt)).scalars().all())


class PlayerMetricsRepository(TeamScopedRepository[PlayerMetric]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, PlayerMetric)

    async def get_for_player(self, player_id: int) -> PlayerMetric | None:
        """One row per player (UNIQUE(player_id))."""
        stmt = select(PlayerMetric).where(PlayerMetric.player_id == player_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def upsert(
        self,
        *,
        player_id: int,
        metrics: dict[str, Any],
        user_id: int | None,
        team_id: int | None,
    ) -> None:
        """Insert-or-update. Mirrors `db/__init__.py:989-1001`. Dialect-agnostic.

        Uses an UPDATE-first / INSERT-on-zero-rowcount pattern so we never
        round-trip an ORM-loaded row through Python (avoids the
        `MissingGreenlet` that triggers when ORM attribute mutation interacts
        with `JSONText` autoflush). Composes inside the surrounding
        transaction for atomicity at the DB level.
        """
        # Pass dict to JSONText (let the TypeDecorator encode) — pre-encoding
        # to a string would double-json-encode at bind time.
        update_stmt = (
            update(PlayerMetric)
            .where(PlayerMetric.player_id == player_id)
            .values(metrics_json=metrics, user_id=user_id, team_id=team_id)
        )
        result = await self.session.execute(update_stmt)
        if (result.rowcount or 0) > 0:
            await self.session.flush()
            return
        # No row to update — insert.
        new_row = PlayerMetric(
            player_id=player_id,
            user_id=user_id,
            team_id=team_id,
            metrics_json=metrics,
        )
        self.session.add(new_row)
        await self.session.flush()


class PlayerGameStatsRepository(TeamScopedRepository[PlayerGameStat]):
    def __init__(self, session: AsyncSession):
        super().__init__(session, PlayerGameStat)

    async def list_for_player(
        self, player_id: int, *, user_id: int, team_id: int, limit: int = 50
    ) -> list[PlayerGameStat]:
        """Most recent N games for a player. Tenant-scoped."""
        stmt = (
            select(PlayerGameStat)
            .where(
                PlayerGameStat.player_id == player_id,
                PlayerGameStat.user_id == user_id,
                PlayerGameStat.team_id == team_id,
            )
            .order_by(PlayerGameStat.game_date.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_entry(self, notebook_entry_id: int) -> list[PlayerGameStat]:
        """All player rows linked to a single Game Summary notebook entry."""
        stmt = select(PlayerGameStat).where(
            PlayerGameStat.notebook_entry_id == notebook_entry_id
        )
        return list((await self.session.execute(stmt)).scalars().all())


__all__ = [
    "PlayersRepository",
    "PlayerMetricsRepository",
    "PlayerGameStatsRepository",
]
