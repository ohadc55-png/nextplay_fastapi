"""IP geolocation cache.

`ip_geo_cache` caches lookups against ipinfo.io. PK is the IP itself.

Origin: `backend/admin/routes.py` (inline) + `backend/migrations/add_ip_geo_cache.py`.
"""

from __future__ import annotations

from sqlalchemy import Float, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base


class IpGeoCache(Base):
    __tablename__ = "ip_geo_cache"

    ip: Mapped[str] = mapped_column(Text, primary_key=True)
    country_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    country_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    city: Mapped[str | None] = mapped_column(Text, nullable=True)
    region: Mapped[str | None] = mapped_column(Text, nullable=True)
    lat: Mapped[float | None] = mapped_column(Float(precision=24), nullable=True)  # REAL in prod
    lon: Mapped[float | None] = mapped_column(Float(precision=24), nullable=True)  # REAL in prod
    resolved_at: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = ["IpGeoCache"]
