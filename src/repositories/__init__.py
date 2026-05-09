"""Repository registry.

As concrete repositories land in subsequent batches they get re-exported here
so callers can `from src.repositories import UsersRepo` without remembering the
file each one lives in.
"""

from src.repositories.base_repository import BaseRepository, TeamScopedRepository

__all__ = [
    "BaseRepository",
    "TeamScopedRepository",
]
