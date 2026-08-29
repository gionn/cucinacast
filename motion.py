"""Watch an ONVIF camera for motion events, no Telegram/TTS/casting dependency."""

import asyncio
import datetime
import logging
import os
import time
import urllib.parse

from onvif import ONVIFCamera
from wsdiscovery.discovery import ThreadedWSDiscovery as WSDiscovery

logger = logging.getLogger(__name__)

DISCOVERY_TIMEOUT_SECONDS = 5

MOTION_TOPIC = "VideoSource/MotionAlarm"
OBJECT_CLASS_TOPIC = "ObjectDetection/Object"
DEBOUNCE_SECONDS = 30
CLASSIFICATION_WAIT_SECONDS = 2
SUBSCRIPTION_INTERVAL = datetime.timedelta(minutes=10)
PULL_TIMEOUT = datetime.timedelta(seconds=10)
PULL_MESSAGE_LIMIT = 10

_CLASS_KEYWORDS = {
    "human": "person",
    "person": "person",
    "face": "person",
    "animal": "animal",
    "pet": "animal",
    "vehicle": "vehicle",
    "car": "vehicle",
}


def _onvif_host():
    return os.environ.get("ONVIF_HOST")


def _onvif_port():
    return int(os.environ.get("ONVIF_PORT", "80"))


def _onvif_user():
    return os.environ.get("ONVIF_USER")


def _onvif_pass():
    return os.environ.get("ONVIF_PASS")


def motion_detection_enabled():
    return bool(_onvif_user() and _onvif_pass())


def describe_object(class_types):
    """Map a vendor-specific ONVIF ClassTypes string (e.g. "Human", "Animal") to a
    language-neutral category ("person"/"animal"/"vehicle"), falling back to
    "unknown" when absent/unrecognized. Callers localize this into wording."""
    lowered = (class_types or "").lower()
    for keyword, category in _CLASS_KEYWORDS.items():
        if keyword in lowered:
            return category
    return "unknown"


def _on_subscription_lost():
    logger.warning("ONVIF pullpoint subscription lost, will be restarted automatically")


def discover_camera():
    """Find an ONVIF device on the local network via WS-Discovery and return (host, port)."""
    wsd = WSDiscovery()
    wsd.start()
    try:
        services = wsd.searchServices(timeout=DISCOVERY_TIMEOUT_SECONDS)
    finally:
        wsd.stop()

    for service in services:
        for xaddr in service.getXAddrs():
            if "onvif" not in xaddr.lower():
                continue
            parsed = urllib.parse.urlparse(xaddr)
            host, _, port = parsed.netloc.partition(":")
            logger.info("Discovered ONVIF device at %s (%s)", parsed.netloc, xaddr)
            return host, int(port) if port else 80

    raise RuntimeError("No ONVIF device found via WS-Discovery; set ONVIF_HOST manually")


async def watch_motion(on_motion):
    """Subscribe to the camera's events and await on_motion(category) for each
    debounced motion event, where category is "person"/"animal"/"vehicle"/"unknown"."""
    onvif_host = _onvif_host()
    host, port = (
        (onvif_host, _onvif_port()) if onvif_host else await asyncio.to_thread(discover_camera)
    )
    camera = ONVIFCamera(host, port, _onvif_user(), _onvif_pass())
    await camera.update_xaddrs()

    manager = await camera.create_pullpoint_manager(SUBSCRIPTION_INTERVAL, _on_subscription_lost)
    service = manager.get_service()

    last_announced = 0
    last_object_class = ""
    pending_tasks = set()

    async def _announce_after_delay():
        nonlocal last_object_class, last_announced
        # Classification often arrives after its motion event, not before.
        await asyncio.sleep(CLASSIFICATION_WAIT_SECONDS)
        category = describe_object(last_object_class)
        last_object_class = ""
        # Unclassified motion shouldn't block a real one from debouncing.
        if category != "unknown":
            last_announced = time.monotonic()
        logger.info("Motion detected (%s)", category)
        try:
            await on_motion(category)
        except Exception:
            logger.exception("on_motion callback failed")

    logger.info("Subscribed to ONVIF events, watching for motion...")
    try:
        while True:
            response = await service.PullMessages(
                {"Timeout": PULL_TIMEOUT, "MessageLimit": PULL_MESSAGE_LIMIT}
            )
            for message in response.NotificationMessage:
                topic = message.Topic._value_1
                data = message.Message._value_1.Data
                simple_items = data.SimpleItem
                items_repr = {item.Name: item.Value for item in simple_items}

                if OBJECT_CLASS_TOPIC in topic:
                    class_types = items_repr.get("ClassTypes", "")
                    # Only while an evaluation is pending, else it could leak into the next event.
                    if class_types and pending_tasks:
                        last_object_class = class_types
                        logger.info("Object classified: %s", last_object_class)
                    continue

                if MOTION_TOPIC not in topic:
                    continue
                if not any(item.Value.lower() == "true" for item in simple_items):
                    continue
                if time.monotonic() - last_announced < DEBOUNCE_SECONDS:
                    logger.info("Motion detected, debounced")
                    continue
                if pending_tasks:
                    continue
                task = asyncio.create_task(_announce_after_delay())
                pending_tasks.add(task)
                task.add_done_callback(pending_tasks.discard)
    finally:
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        await manager.shutdown()


async def run_forever(on_motion, retry_delay_seconds=30):
    """Run watch_motion() in a loop, restarting after a delay on any failure, so a
    transient camera/network problem doesn't take down the whole watch task."""
    while True:
        try:
            await watch_motion(on_motion)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Motion watch loop crashed, retrying in %ss", retry_delay_seconds)
        await asyncio.sleep(retry_delay_seconds)
