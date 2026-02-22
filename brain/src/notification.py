"""Proactive notification system (D-019).

Urgency (time-sensitive) != importance (content significance) != weight.
notification_outbox + delivery worker + Telegram. Quiet hours respected.

Channel routing:
  urgency >= threshold AND telegram_enabled -> push via Telegram
  importance >= threshold -> passive (injected in next /context/assemble)
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError

import asyncpg

from .config import (
    NOTIFICATION_DELIVERY_INTERVAL,
    NOTIFICATION_EXPIRY_HOURS,
    TELEGRAM_BOT_TOKEN_ENV,
)

logger = logging.getLogger("brain.notification")


class NotificationStore:
    """Manages the notification outbox."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def enqueue(
        self,
        agent_id: str,
        content: str,
        urgency: float,
        importance: float,
        source: str,
        source_memory_id: str | None = None,
        metadata: dict | None = None,
    ) -> int:
        """Add a notification to the outbox. Auto-routes to channel."""
        prefs = await self.get_preferences(agent_id)
        if not prefs.get("enabled", True):
            return -1

        # Route: high urgency + telegram -> push, otherwise passive
        channel = "passive"
        if (
            urgency >= prefs.get("urgency_threshold", 0.7)
            and prefs.get("telegram_enabled")
            and prefs.get("telegram_chat_id")
        ):
            channel = "telegram"

        row = await self.pool.fetchrow(
            """
            INSERT INTO notification_outbox
                (agent_id, content, urgency, importance, source,
                 source_memory_id, channel, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id
            """,
            agent_id, content, urgency, importance, source,
            source_memory_id, channel, metadata or {},
        )
        nid = row["id"]
        logger.info(
            "Notification %d enqueued [%s] channel=%s urgency=%.2f importance=%.2f",
            nid, agent_id, channel, urgency, importance,
        )
        return nid

    async def get_pending_push(self, limit: int = 10) -> list[dict]:
        """Fetch pending push notifications (telegram/webhook)."""
        rows = await self.pool.fetch(
            """
            SELECT o.*, p.telegram_chat_id, p.quiet_hours_start, p.quiet_hours_end
            FROM notification_outbox o
            LEFT JOIN notification_preferences p ON o.agent_id = p.agent_id
            WHERE o.status = 'pending'
              AND o.channel IN ('telegram', 'webhook')
              AND o.expires_at > NOW()
            ORDER BY o.urgency DESC, o.created_at
            LIMIT $1
            """,
            limit,
        )
        return [dict(r) for r in rows]

    async def get_pending_passive(self, agent_id: str, limit: int = 3) -> list[dict]:
        """Fetch pending passive notifications for context injection."""
        rows = await self.pool.fetch(
            """
            SELECT * FROM notification_outbox
            WHERE agent_id = $1
              AND status = 'pending'
              AND channel = 'passive'
              AND expires_at > NOW()
            ORDER BY importance DESC, created_at
            LIMIT $2
            """,
            agent_id, limit,
        )
        return [dict(r) for r in rows]

    async def mark_delivered(self, notification_id: int) -> None:
        await self.pool.execute(
            "UPDATE notification_outbox SET status = 'delivered', delivered_at = NOW() WHERE id = $1",
            notification_id,
        )

    async def mark_failed(self, notification_id: int, error: str) -> None:
        await self.pool.execute(
            """UPDATE notification_outbox
               SET status = 'failed', metadata = metadata || jsonb_build_object('error', $2::text)
               WHERE id = $1""",
            notification_id, error,
        )

    async def expire_old(self) -> int:
        """Expire notifications past their expiry time."""
        result = await self.pool.execute(
            "UPDATE notification_outbox SET status = 'expired' WHERE status = 'pending' AND expires_at <= NOW()"
        )
        count = int(result.split()[-1]) if result else 0
        if count > 0:
            logger.info("Expired %d old notifications", count)
        return count

    async def get_preferences(self, agent_id: str) -> dict:
        """Get notification preferences. Returns defaults if none set."""
        row = await self.pool.fetchrow(
            "SELECT * FROM notification_preferences WHERE agent_id = $1",
            agent_id,
        )
        if row:
            return dict(row)
        return {
            "agent_id": agent_id,
            "telegram_chat_id": None,
            "telegram_enabled": False,
            "quiet_hours_start": 23,
            "quiet_hours_end": 7,
            "urgency_threshold": 0.7,
            "importance_threshold": 0.5,
            "enabled": True,
        }

    async def set_preferences(self, agent_id: str, **kwargs) -> None:
        """Upsert notification preferences."""
        # Build SET clause from kwargs
        valid_keys = {
            "telegram_chat_id", "telegram_enabled",
            "quiet_hours_start", "quiet_hours_end",
            "urgency_threshold", "importance_threshold", "enabled",
        }
        filtered = {k: v for k, v in kwargs.items() if k in valid_keys}
        if not filtered:
            return

        # Upsert
        cols = ["agent_id"] + list(filtered.keys()) + ["updated_at"]
        vals = [agent_id] + list(filtered.values()) + [datetime.now(timezone.utc)]
        placeholders = [f"${i+1}" for i in range(len(vals))]
        update_parts = [
            f"{k} = EXCLUDED.{k}" for k in list(filtered.keys()) + ["updated_at"]
        ]
        await self.pool.execute(
            f"""
            INSERT INTO notification_preferences ({', '.join(cols)})
            VALUES ({', '.join(placeholders)})
            ON CONFLICT (agent_id) DO UPDATE SET {', '.join(update_parts)}
            """,
            *vals,
        )


class DeliveryWorker:
    """Background loop that delivers push notifications."""

    def __init__(self, pool: asyncpg.Pool, store: NotificationStore):
        self.pool = pool
        self.store = store
        self._running = False

    async def run(self, shutdown_event: asyncio.Event) -> None:
        """Main delivery loop."""
        self._running = True
        logger.info("Notification delivery worker started.")

        while not shutdown_event.is_set():
            try:
                # Expire old notifications
                await self.store.expire_old()

                # Deliver push notifications
                pending = await self.store.get_pending_push()
                for notif in pending:
                    if self._is_quiet_hours(notif):
                        continue
                    if notif["channel"] == "telegram":
                        chat_id = notif.get("telegram_chat_id")
                        if chat_id:
                            ok = await self._deliver_telegram(
                                chat_id, notif["content"], notif["agent_id"],
                            )
                            if ok:
                                await self.store.mark_delivered(notif["id"])
                            else:
                                await self.store.mark_failed(notif["id"], "telegram_send_failed")
            except Exception as e:
                logger.error("Delivery loop error: %s", e)

            try:
                await asyncio.wait_for(
                    shutdown_event.wait(),
                    timeout=NOTIFICATION_DELIVERY_INTERVAL,
                )
                break  # shutdown signaled
            except asyncio.TimeoutError:
                pass

        self._running = False
        logger.info("Notification delivery worker stopped.")

    async def _deliver_telegram(
        self, chat_id: str, content: str, agent_id: str,
    ) -> bool:
        """Send message via Telegram Bot API."""
        token = os.environ.get(TELEGRAM_BOT_TOKEN_ENV)
        if not token:
            logger.warning("No %s configured, skipping Telegram delivery", TELEGRAM_BOT_TOKEN_ENV)
            return False

        import json
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({
            "chat_id": chat_id,
            "text": f"[{agent_id}] {content}",
            "parse_mode": "Markdown",
        }).encode()

        try:
            req = Request(url, data=payload, headers={"Content-Type": "application/json"})
            response = await asyncio.to_thread(urlopen, req, timeout=10)
            if response.status == 200:
                logger.info("Telegram sent to %s for %s", chat_id, agent_id)
                return True
            logger.warning("Telegram API returned %d", response.status)
            return False
        except (URLError, OSError) as e:
            logger.warning("Telegram delivery failed: %s", e)
            return False

    @staticmethod
    def _is_quiet_hours(notif: dict) -> bool:
        """Check if current UTC hour is within quiet hours."""
        start = notif.get("quiet_hours_start", 23)
        end = notif.get("quiet_hours_end", 7)
        now_hour = datetime.now(timezone.utc).hour

        if start <= end:
            return start <= now_hour < end
        else:
            # Wraps midnight (e.g., 23-7)
            return now_hour >= start or now_hour < end

    @property
    def running(self) -> bool:
        return self._running
