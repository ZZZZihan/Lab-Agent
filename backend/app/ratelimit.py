"""Redis fixed-window rate limits for login and chat."""

from __future__ import annotations

import redis.asyncio as redis

from app.config import Settings
from app.logging_conf import get_logger

log = get_logger("ratelimit")


class RateLimiter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.redis = redis.from_url(settings.redis_url, decode_responses=True)

    async def allow(self, bucket: str, limit: int, window_seconds: int) -> bool:
        if not self.settings.rate_limit_enabled:
            return True
        try:
            count = await self.redis.incr(bucket)
            if count == 1:
                await self.redis.expire(bucket, window_seconds)
            return int(count) <= limit
        except redis.RedisError:
            log.warning("rate_limit_backend_unavailable", env=self.settings.app_env)
            return self.settings.app_env != "prod"

    async def ping(self) -> bool:
        try:
            return bool(await self.redis.ping())
        except redis.RedisError:
            return False

    async def close(self) -> None:
        await self.redis.aclose()
