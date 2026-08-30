"""Bluetooth presence tracking (who's home) via hcitool/bluetoothctl, no
Telegram/TTS/casting dependency."""

import asyncio
import logging
import os
import re
import time
from contextlib import suppress

import storage_bluetooth

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 600
MISS_THRESHOLD = 3
DISCOVERY_TIMEOUT_SECONDS = 15
PROBE_TIMEOUT_SECONDS = 8
PAIR_TIMEOUT_SECONDS = 60

_HCI_DEVICE_RE = re.compile(r"^hci\d+\s+([0-9A-F:]+)$", re.MULTILINE)
_NEW_DEVICE_RE = re.compile(r"\[NEW\] Device ([0-9A-F:]+)(?:\s+(.*))?$", re.MULTILINE)
_PASSKEY_RE = re.compile(r"(?:Confirm passkey|Enter passkey) (\d+)")

SUDO_PREFIX = () if os.environ.get("PRESENCE_NO_SUDO") else ("sudo",)


def bluetooth_available():
    """True if the local adapter is present (hcitool dev lists hci0). sudo is
    required for hcitool/bluetoothctl; the service user has passwordless sudo."""
    try:
        out = _run_checked((*SUDO_PREFIX, "hcitool", "dev"))
    except Exception:
        logger.exception("Failed to check bluetooth adapter")
        return False
    return bool(_HCI_DEVICE_RE.search(out))


def _run_checked(cmd, timeout=15, **kwargs):
    """Run a subprocess synchronously (hcitool/bluetoothctl are blocking CLI
    tools) and return stdout; raise on nonzero exit."""
    import subprocess

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kwargs)  # noqa: S603
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


async def _run_async(cmd, timeout=PROBE_TIMEOUT_SECONDS):
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"{' '.join(cmd)} timed out")
    if proc.returncode != 0:
        raise RuntimeError(
            f"{' '.join(cmd)} failed ({proc.returncode}): {stderr.decode(errors='replace').strip()}"
        )
    return stdout.decode(errors="replace")


async def discover_devices(timeout_seconds=DISCOVERY_TIMEOUT_SECONDS):
    """Run a temporary bluetoothctl discovery scan and return discovered devices
    as a list of {"mac", "name"} dicts."""
    out = await _run_async(
        (*SUDO_PREFIX, "bluetoothctl", "--timeout", str(timeout_seconds), "scan", "on"),
        timeout=timeout_seconds + 10,
    )
    devices = []
    for match in _NEW_DEVICE_RE.finditer(out):
        mac = match.group(1).upper()
        name = match.group(2) or ""
        devices.append({"mac": mac, "name": name})
    seen = set()
    unique = []
    for device in devices:
        if device["mac"] not in seen:
            seen.add(device["mac"])
            unique.append(device)
    return unique


async def _pair_interactive(mac, on_prompt):
    """Pair with a device using an interactive bluetoothctl session. on_prompt
    is called with a passkey/confirm prompt and must return a reply (or None to
    abort); returns True on successful pairing, False on failure/abort."""
    cmd = (*SUDO_PREFIX, "bluetoothctl")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    start = time.monotonic()
    paired = False
    try:
        await _feed(proc, "agent on")
        await _feed(proc, "default-agent")
        await _feed(proc, f"pair {mac}")
        while True:
            if time.monotonic() - start > PAIR_TIMEOUT_SECONDS:
                await _feed(proc, "quit")
                raise TimeoutError(f"Pairing with {mac} timed out")
            line = await _read_line(proc, timeout=5)
            if line is None:
                break
            logger.info("bluetoothctl: %s", line.strip())
            if "Pairing successful" in line:
                paired = True
                break
            if "Failed to pair" in line or "Device not available" in line:
                break
            if "Attempting to pair" in line or "Pairing" in line:
                continue
            passkey_match = _PASSKEY_RE.search(line)
            if passkey_match:
                passkey = passkey_match.group(1)
                reply = await on_prompt(passkey)
                if reply is None:
                    await _feed(proc, "cancel")
                    return False
                await _feed(proc, reply)
    except (asyncio.TimeoutError, BrokenPipeError):
        logger.exception("Pairing session with %s failed", mac)
    finally:
        with suppress(Exception):
            proc.stdin.close()
        await proc.wait()
    return paired


async def _feed(proc, text):
    if proc.stdin is None:
        return
    proc.stdin.write(f"{text}\n".encode())
    await proc.stdin.drain()


async def _read_line(proc, timeout):
    if proc.stdout is None:
        return None
    try:
        return await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
    except asyncio.TimeoutError:
        return None


async def pair_device(mac, on_prompt):
    """Pair + trust a device. Returns (ok: bool, message: str)."""
    mac = storage_bluetooth.normalize_mac(mac)
    try:
        success = await _pair_interactive(mac, on_prompt)
    except TimeoutError as exc:
        return False, str(exc)
    if not success:
        return False, "Pairing failed or was aborted"
    try:
        await _run_async((*SUDO_PREFIX, "bluetoothctl", "trust", mac), timeout=10)
    except Exception:
        logger.exception("Failed to trust %s", mac)
    return True, "Paired and trusted"


def _probe(mac):
    """Probe a single device via hcitool name (paging). Works for any powered-on
    phone whether or not it's paired/discoverable. Returns True if present."""
    try:
        out = _run_checked((*SUDO_PREFIX, "hcitool", "name", mac), timeout=PROBE_TIMEOUT_SECONDS)
    except Exception:
        return False
    return out.strip() != ""


def _apply_miss(device):
    """Apply one absent observation to a device's strike count and return the
    (new_home, miss_count, flipped) tuple. A home device flips to away only
    after MISS_THRESHOLD consecutive misses; an already-away device stays away
    and keeps counting (so it can flip home again only on a sighting)."""
    miss_count = device["miss_count"] + 1
    home = device["home"]
    flipped = False
    if home and miss_count >= MISS_THRESHOLD:
        home = False
        flipped = True
    return home, miss_count, flipped


async def check_presence():
    """Probe all registered devices once and return a list of transition dicts
    {"nickname", "mac", "home"} for devices whose home/away state flipped."""
    transitions = []
    devices = storage_bluetooth.list_devices()
    if not devices:
        return transitions
    for device in devices:
        mac = device["mac"]
        present = await asyncio.to_thread(_probe, mac)
        now = time.time()
        if present:
            home, miss_count = True, 0
        else:
            home, miss_count, _ = _apply_miss(device)
        if home and not device["home"]:
            transitions.append({"nickname": device["nickname"], "mac": mac, "home": True})
        elif not home and device["home"]:
            transitions.append({"nickname": device["nickname"], "mac": mac, "home": False})
        last_seen = now if present else device["last_seen"]
        storage_bluetooth.set_device_state(mac, home, miss_count, last_seen)
    return transitions


async def run_forever(on_transition, poll_interval_seconds=POLL_INTERVAL_SECONDS):
    """Poll presence every poll_interval_seconds and await
    on_transition(transition) for each home/away flip. Restarts on failure so a
    transient bluetooth hiccup can't kill the watch task."""
    while True:
        try:
            if not storage_bluetooth.list_devices():
                await asyncio.sleep(poll_interval_seconds)
                continue
            transitions = await check_presence()
            for transition in transitions:
                try:
                    await on_transition(transition)
                except Exception:
                    logger.exception("on_transition callback failed")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Presence check crashed, retrying in %ss", poll_interval_seconds)
        await asyncio.sleep(poll_interval_seconds)
