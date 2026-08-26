#!/usr/bin/env python3
"""PoC: watch an ONVIF camera for motion events."""
import asyncio
import datetime
import logging
import os
import time
import urllib.parse

from lxml import etree
from dotenv import load_dotenv
from onvif import ONVIFCamera
from wsdiscovery.discovery import ThreadedWSDiscovery as WSDiscovery

load_dotenv()

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

ONVIF_HOST = os.environ.get("ONVIF_HOST")
ONVIF_PORT = int(os.environ.get("ONVIF_PORT", "80"))
ONVIF_USER = os.environ["ONVIF_USER"]
ONVIF_PASS = os.environ["ONVIF_PASS"]

DISCOVERY_TIMEOUT_SECONDS = 5

MOTION_TOPIC = "VideoSource/MotionAlarm"
OBJECT_CLASS_TOPIC = "ObjectDetection/Object"
DEBOUNCE_SECONDS = 30
SUBSCRIPTION_INTERVAL = datetime.timedelta(minutes=10)
PULL_TIMEOUT = datetime.timedelta(seconds=10)
PULL_MESSAGE_LIMIT = 10


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


async def watch_motion():
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
                element_items = getattr(data, "ElementItem", None) or []
                elements_xml = [
                    etree.tostring(item._value_1, pretty_print=True).decode()
                    for item in element_items
                ]
                logger.debug(
                    "Event topic=%s items=%s elements=%s", topic, items_repr, elements_xml
                )

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
                last_object_class = ""
                logger.info("Motion detected, would announce now")
    finally:
        await manager.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(watch_motion())
    except KeyboardInterrupt:
        logger.info("Stopped.")
