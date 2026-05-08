"""Aggregated model registry.

Importing `src.models` registers every ORM class with `Base.metadata`.
Alembic's env.py imports `from src.models import *` so autogenerate sees them.

Add new models to the imports below as they land in subsequent batches.
"""

from src.models.auth import AuditLog, AuthToken, RefreshToken, SocialAccount
from src.models.clubs import Club, InviteCode
from src.models.coach import CoachPreference, Feedback
from src.models.players import Player, PlayerGameStat, PlayerMetric
from src.models.teams import TeamProfile
from src.models.users import User

__all__ = [
    # Identity / auth
    "User",
    "SocialAccount",
    "RefreshToken",
    "AuthToken",
    "AuditLog",
    # B2B / billing
    "Club",
    "InviteCode",
    # Teams & roster
    "TeamProfile",
    "Player",
    "PlayerMetric",
    "PlayerGameStat",
    # Coach personalization
    "CoachPreference",
    "Feedback",
]
