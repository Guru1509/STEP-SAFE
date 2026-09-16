"""Pluggable notification services for STEP-SAFE.

Provides:
- NotificationProvider: Abstract base class for providers.
- InAppNotificationProvider: Stores notifications in SQLite for the web dashboard.
- FirebaseCloudMessagingProvider: Optional FCM HTTP v1 provider using Google
  service-account credentials (GOOGLE_APPLICATION_CREDENTIALS).
- NotificationService: Dispatches alerts to all registered providers.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("step_safe.notifications")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class NotificationProvider:
    """Abstract interface for all STEP-SAFE notification delivery channels."""

    name: str = "base"

    def send(self, recipient: dict[str, Any], title: str, message: str, data: dict[str, Any] | None = None) -> bool:
        """Deliver notification. Returns True if delivery succeeded."""
        raise NotImplementedError


class InAppNotificationProvider(NotificationProvider):
    """Core local provider: stores notifications in the SQLite database.

    Guarantees that the web dashboard notification center works 100% locally
    even when internet or third-party cloud providers are not available.
    """

    name: str = "in_app"

    def __init__(self, db_factory: Callable[[], Any]):
        self.db_factory = db_factory

    def send(self, recipient: dict[str, Any], title: str, message: str, data: dict[str, Any] | None = None) -> bool:
        user_id = recipient.get("id")
        if not user_id:
            return False

        data = data or {}
        event_id = data.get("event_id")
        notif_type = data.get("type", "SYSTEM")

        try:
            with self.db_factory() as conn:
                # INSERT OR IGNORE enforces the database-level UNIQUE(event_id, recipient_user_id, type)
                # ensuring at most one notification is recorded per event episode.
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO notifications
                    (event_id, recipient_user_id, type, title, message, status, sent_at)
                    VALUES (?, ?, ?, ?, ?, 'unread', ?)""",
                    (event_id, user_id, notif_type, title, message, utc_now()),
                )
                return cursor.rowcount > 0
        except Exception as exc:
            logger.error("InAppNotificationProvider failed for user %s: %s", user_id, exc)
            return False


class FirebaseCloudMessagingProvider(NotificationProvider):
    """Optional FCM HTTP v1 provider using Google service account credentials.

    Uses GOOGLE_APPLICATION_CREDENTIALS file path or FIREBASE_SERVICE_ACCOUNT_JSON.
    If credentials or the firebase_admin package are absent, this provider is
    gracefully disabled without affecting local in-app notifications.
    """

    name: str = "fcm_http_v1"

    def __init__(self, db_factory: Callable[[], Any]):
        self.db_factory = db_factory
        self.enabled = False
        self._firebase_app = None
        self._messaging = None

        cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        cred_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")

        if not cred_path and not cred_json:
            logger.info("FCM Provider: No service-account credentials configured. Push notifications disabled.")
            return

        try:
            import firebase_admin
            from firebase_admin import credentials, messaging

            if cred_path and Path(cred_path).is_file():
                cred = credentials.Certificate(cred_path)
            elif cred_json:
                cred = credentials.Certificate(json.loads(cred_json))
            else:
                logger.warning("FCM Provider: Credentials path '%s' not found.", cred_path)
                return

            # Initialize firebase_admin app if not already initialized
            try:
                self._firebase_app = firebase_admin.get_app("step_safe_fcm")
            except ValueError:
                self._firebase_app = firebase_admin.initialize_app(cred, name="step_safe_fcm")

            self._messaging = messaging
            self.enabled = True
            logger.info("FCM Provider: Successfully initialized with service-account credentials (HTTP v1).")
        except ImportError:
            logger.info("FCM Provider: firebase-admin package is not installed. Mobile push notifications disabled.")
        except Exception as exc:
            logger.warning("FCM Provider: Failed to initialize Firebase Admin SDK: %s", exc)

    def send(self, recipient: dict[str, Any], title: str, message: str, data: dict[str, Any] | None = None) -> bool:
        if not self.enabled or not self._messaging:
            return False

        user_id = recipient.get("id")
        if not user_id:
            return False

        # Query active push registration tokens for this recipient
        with self.db_factory() as conn:
            rows = conn.execute(
                "SELECT id, token FROM push_subscriptions WHERE user_id = ? AND active = 1",
                (user_id,),
            ).fetchall()

        if not rows:
            return False

        payload_data = {str(k): str(v) for k, v in (data or {}).items()}
        delivered_any = False

        for row in rows:
            sub_id = row["id"]
            token = row["token"]
            try:
                msg = self._messaging.Message(
                    notification=self._messaging.Notification(title=title, body=message),
                    data=payload_data,
                    token=token,
                )
                self._messaging.send(msg, app=self._firebase_app)
                delivered_any = True
            except self._messaging.UnregisteredError:
                logger.info("FCM token expired or unregistered. Deactivating subscription %s", sub_id)
                with self.db_factory() as conn:
                    conn.execute("UPDATE push_subscriptions SET active = 0 WHERE id = ?", (sub_id,))
            except Exception as exc:
                logger.warning("FCM push delivery failed for user %s: %s", user_id, exc)

        return delivered_any


class NotificationService:
    """Coordinates dispatching notifications across all active providers."""

    def __init__(self, db_factory: Callable[[], Any]):
        self.db_factory = db_factory
        self.providers: list[NotificationProvider] = [
            InAppNotificationProvider(db_factory),
            FirebaseCloudMessagingProvider(db_factory),
        ]

    def register_provider(self, provider: NotificationProvider) -> None:
        self.providers.append(provider)

    def dispatch(
        self,
        recipients: list[dict[str, Any]],
        title: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send notifications to a list of recipients across all active providers."""
        results = {"total_recipients": len(recipients), "deliveries": {}}

        for recipient in recipients:
            user_id = recipient.get("id")
            recipient_results = {}
            for provider in self.providers:
                try:
                    success = provider.send(recipient, title, message, data)
                    recipient_results[provider.name] = success
                except Exception as exc:
                    logger.error("Provider %s failed for recipient %s: %s", provider.name, user_id, exc)
                    recipient_results[provider.name] = False
            results["deliveries"][str(user_id)] = recipient_results

        return results
