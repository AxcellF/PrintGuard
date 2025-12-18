from urllib.parse import urlparse
import urllib.request
import urllib.error
import asyncio
import logging
import json
from pywebpush import WebPushException, webpush

from ..models import Notification, SavedKey, SavedConfig
from ..utils.config import get_key, get_config
from ..utils.alert_utils import get_alert

def get_subscriptions():
    """Retrieve the list of current push notification subscriptions.

    Returns:
        list: A list of subscription dictionaries, each with at least an 'id' and 'endpoint'.
    """
    # pylint: disable=C0415
    from ..app import app
    return app.state.subscriptions

def remove_subscription(subscription_id = None, subscription = None):
    """Remove a subscription by ID or subscription object.

    Args:
        subscription_id (str, optional): The ID of the subscription to remove.
        subscription (dict, optional): The subscription object to remove.
    """
    # pylint: disable=C0415
    from ..app import app
    if subscription_id is not None:
        app.state.subscriptions = [
            sub for sub in app.state.subscriptions if sub.get('id') != subscription_id
        ]
    elif subscription is not None:
        app.state.subscriptions.remove(subscription)
    else:
        logging.error("No subscription ID or object provided to remove.")

async def send_defect_notification(alert_id):
    """Send a defect notification for a given alert ID to all subscribers.

    Args:
        alert_id (str): The ID of the alert for which to send a notification.
    """
    logging.debug("Attempting to send defect notification for alert ID: %s", alert_id)
    alert = get_alert(alert_id)
    if alert:
        logging.debug("Alert found for ID %s, preparing notification", alert_id)
        # pylint: disable=import-outside-toplevel
        from .camera_utils import get_camera_state
        camera_state = await get_camera_state(alert.camera_uuid)
        camera_nickname = camera_state.nickname if camera_state else alert.camera_uuid
        notification = Notification(
            title=f"Defect - Camera {camera_nickname}",
            body=f"Defect detected on camera {camera_nickname}",
        )
        subscriptions = get_subscriptions() or []
        logging.debug("Created notification object without image payload, sending to %d subscriptions",
                      len(subscriptions))
        send_notification(notification)
    else:
        logging.error("No alert found for ID: %s", alert_id)

async def send_home_assistant_notification(alert_id):
    """Send a notification to Home Assistant webhook if configured.

    Args:
        alert_id (str): The ID of the alert to send.
    """
    alert = get_alert(alert_id)
    if not alert:
        logging.warning("Cannot send HA notification: Alert %s not found", alert_id)
        return

    # pylint: disable=import-outside-toplevel
    from .camera_utils import get_camera_state
    camera_state = await get_camera_state(alert.camera_uuid)
    webhook_url = camera_state.home_assistant_webhook_url

    if not webhook_url:
        logging.debug("No Home Assistant webhook URL configured for camera %s", camera_state.nickname)
        return

    payload = {
        "event_type": "defect_detected",
        "camera_uuid": alert.camera_uuid,
        "camera_name": camera_state.nickname,
        "alert_id": alert.id,
        "countdown_time": alert.countdown_time,
        "countdown_action": alert.countdown_action,
        "timestamp": alert.timestamp,
        "message": alert.message
    }

    def _send_request():
        try:
            req = urllib.request.Request(
                webhook_url,
                data=json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json'}
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status >= 400:
                    logging.error("Home Assistant webhook failed with status: %s", response.status)
                else:
                    logging.debug("Home Assistant webhook sent successfully")
        except urllib.error.URLError as e:
            logging.error("Failed to send Home Assistant webhook: %s", e)
        except Exception as e:
            logging.error("Unexpected error sending Home Assistant webhook: %s", e)

    await asyncio.to_thread(_send_request)

def send_notification(notification: Notification):
    """Send a push notification to all current subscriptions.

    Args:
        notification (Notification): The notification object to send. Should have 'title' and 'body' fields at minimum.

    Returns:
        bool: True if at least one notification was sent successfully, False otherwise.
    """
    logging.debug("Starting notification send process")
    config = get_config()
    vapid_subject = config.get(SavedConfig.VAPID_SUBJECT, None)
    if not vapid_subject:
        logging.error("VAPID subject is not set in the configuration.")
        return False
    vapid_private_key = get_key(SavedKey.VAPID_PRIVATE_KEY)
    if not vapid_private_key:
        logging.error("VAPID private key is not set in the configuration.")
        return False
    subscriptions = get_subscriptions()
    logging.debug("VAPID configuration found. Subject: %s", vapid_subject)
    logging.debug("Number of subscriptions: %d", len(subscriptions))
    vapid_claims = {
        "sub": vapid_subject,
        "aud": None,
    }
    success_count = 0
    if not subscriptions:
        logging.warning("No push subscriptions available to send notifications")
        return False
    for i, sub in enumerate(subscriptions.copy()):
        logging.debug("Sending notification to subscription %d/%d",
                      i+1, len(subscriptions))
        try:
            endpoint = sub.get('endpoint', '')
            if not endpoint:
                logging.error("Subscription %d has no endpoint", i+1)
                continue
            parsed_endpoint = urlparse(endpoint)
            audience = f"{parsed_endpoint.scheme}://{parsed_endpoint.netloc}"
            aud_vapid_claims = dict(vapid_claims)
            aud_vapid_claims['aud'] = audience
            payload_dict = {
                'title': notification.title,
                'body': notification.body
            }
            data_payload = json.dumps(payload_dict)
            logging.debug("Sending to endpoint: %s", endpoint)
            webpush(
                subscription_info=sub,
                data=data_payload,
                vapid_private_key=vapid_private_key,
                vapid_claims=aud_vapid_claims
            )
            success_count += 1
            logging.debug("Successfully sent notification to subscription %d", i+1)
        except WebPushException as ex:
            logging.error("WebPush failed for subscription %d: %s", i+1, ex)
            if ex.response and ex.response.status_code == 410:
                remove_subscription(subscription=sub)
                logging.info("Subscription expired and removed: %s", sub.get('endpoint', 'unknown'))
            else:
                logging.error("Push failed: %s", ex)
        except Exception as e:
            logging.error("Unexpected error sending notification to subscription %d: %s", i+1, e)

    logging.debug("Notification send complete. Success count: %d/%d", success_count, len(subscriptions))
    return success_count > 0
