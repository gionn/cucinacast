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

ONVIF_HOST = os.environ.get("ONVIF_HOST")
ONVIF_PORT = int(os.environ.get("ONVIF_PORT", "80"))
ONVIF_USER = os.environ.get("ONVIF_USER")
ONVIF_PASS = os.environ.get("ONVIF_PASS")

DISCOVERY_TIMEOUT_SECONDS = 5

MOTION_TOPIC = "VideoSource/MotionAlarm"
OBJECT_CLASS_TOPIC = "ObjectDetection/Object"
DEBOUNCE_SECONDS = 30
SUBSCRIPTION_INTERVAL = datetime.timedelta(minutes=10)
PULL_TIMEOUT = datetime.timedelta(seconds=10)
PULL_MESSAGE_LIMIT = 10

_CLASS_KEYWORDS = {
    "human": "a person",
    "person": "a person",
    "face": "a person",
    "animal": "an animal",
    "pet": "an animal",
    "vehicle": "a vehicle",
    "car": "a vehicle",
}


def motion_detection_enabled():
    return bool(ONVIF_USER and ONVIF_PASS)


def describe_object(class_types):
    """Map a vendor-specific ONVIF ClassTypes string (e.g. "Human", "Animal") to
    a natural-language phrase, falling back to generic wording when absent/unrecognized."""
    lowered = (class_types or "").lower()
    for keyword, phrase in _CLASS_KEYWORDS.items():
        if keyword in lowered:
            return phrase
    return "someone"


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
    """Subscribe to the camera's events and await on_motion(description) for each
    debounced motion event, where description is a phrase like "a person"/"someone"."""
    host, port = (ONVIF_HOST, ONVIF_PORT) if ONVIF_HOST else discover_camera()
    camera = ONVIFCamera(host, port, ONVIF_USER, ONVIF_PASS)
    await camera.update_xaddrs()

    manager = await camera.create_pullpoint_manager(
        SUBSCRIPTION_INTERVAL, _on_subscription_lost
    )
    service = manager.get_service()

    last_announced = 0
    last_object_class = ""
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
                    if class_types:
                        last_object_class = class_types
                        logger.info("Object classified: %s", last_object_class)
                    continue

                if MOTION_TOPIC not in topic:
                    continue
                if not any(item.Value.lower() == "true" for item in simple_items):
                    continue
                now = time.monotonic()
                if now - last_announced < DEBOUNCE_SECONDS:
                    logger.info("Motion detected, debounced")
                    continue
                last_announced = now
                description = describe_object(last_object_class)
                last_object_class = ""
                logger.info("Motion detected (%s)", description)
                try:
                    await on_motion(description)
                except Exception:
                    logger.exception("on_motion callback failed")
    finally:
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
