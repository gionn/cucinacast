"""Bluetooth presence tracking (who's home) via hcitool/bluetoothctl, no
Telegram/TTS/casting dependency."""

import asyncio
import logging
import re
import time
from contextlib import suppress

import storage_bluetooth

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 600
MISS_THRESHOLD = 3
DISCOVERY_TIMEOUT_SECONDS = 15
PROBE_TIMEOUT_SECONDS = 8
PROBE_ATTEMPTS = 3
PROBE_RETRY_DELAY_SECONDS = 1
PAIR_TIMEOUT_SECONDS = 180
PASSKEY_CONFIRM_TIMEOUT_SECONDS = 90

_HCI_DEVICE_RE = re.compile(r"^\s*hci\d+\s+([0-9A-F:]+)$", re.MULTILINE)
_NEW_DEVICE_RE = re.compile(r"\[NEW\] Device ([0-9A-F:]+)(?:\s+(.*))?$", re.MULTILINE)
_DEVICE_NAME_RE = re.compile(r"\[CHG\] Device ([0-9A-F:]+) (?:Name|Alias): (.+)$", re.MULTILINE)
_PASSKEY_RE = re.compile(r"(?:Confirm passkey|Enter passkey) (\d+)")
_ANSI_RE = re.compile(r"\x1b\[[?0-9;]*[a-zA-Z]")


def _strip_ansi(text):
    """Strip ANSI escape sequences (colors, cursor movement, private modes)
    and the SOH/STX control bytes bluetoothctl wraps around colored tokens, so
    the plain-text regexes below match and logs stay readable."""
    return _ANSI_RE.sub("", text.replace("\x01", "").replace("\x02", "").replace("\r", ""))


def bluetooth_available():
    """True if a usable adapter is present. hcitool runs unprivileged: on
    Debian the HCI socket is accessible once the user is in the 'bluetooth'
    group, and bluetoothctl talks over the BlueZ DBus API."""
    try:
        out = _run_checked(("hcitool", "dev"))
    except Exception as exc:
        logger.warning(
            "Couldn't query the Bluetooth adapter: %s. If this host has "
            "Bluetooth, make sure hcitool is installed and the service user is "
            "in the 'bluetooth' group (sudo usermod -aG bluetooth <user>).",
            exc,
        )
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
    out = _strip_ansi(
        await _run_async(
            ("bluetoothctl", "--timeout", str(timeout_seconds), "scan", "on"),
            timeout=timeout_seconds + 10,
        )
    )
    devices = {}
    for match in _NEW_DEVICE_RE.finditer(out):
        mac = match.group(1).upper()
        name = (match.group(2) or "").strip()
        # bluetoothctl echoes the MAC (dash-separated) as the name until the
        # device reports one; treat that as nameless.
        if name.replace("-", ":").upper() == mac:
            name = ""
        devices.setdefault(mac, {"mac": mac, "name": name})
    for match in _DEVICE_NAME_RE.finditer(out):
        mac = match.group(1).upper()
        device = devices.setdefault(mac, {"mac": mac, "name": ""})
        if match.group(2):
            device["name"] = match.group(2).strip()
    return list(devices.values())


async def _pair_interactive(mac, on_prompt):
    """Pair with a device using an interactive bluetoothctl session. on_prompt
    is called with a passkey/confirm prompt and must return a reply (or None to
    abort); returns (paired: bool, outcome: str)."""
    proc = await asyncio.create_subprocess_exec(
        "bluetoothctl",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    start = time.monotonic()
    paired = False
    outcome = "unknown"
    prompted = False
    passkey = None
    try:
        await _feed(proc, "agent on")
        await _feed(proc, "default-agent")
        await _feed(proc, f"pair {mac}")
        while True:
            remaining = PAIR_TIMEOUT_SECONDS - (time.monotonic() - start)
            if remaining <= 0:
                outcome = "timed out"
                break
            line = await _read_line(proc, timeout=min(5, remaining))
            if line is None:
                continue
            if not line:
                outcome = "bluetoothctl exited before pairing finished"
                break
            line = _strip_ansi(line.decode(errors="replace")).strip()
            logger.info("bluetoothctl: %s", " ".join(line.split()))
            if "Pairing successful" in line or "Bonded: yes" in line:
                paired = True
                outcome = "success"
                break
            if "Failed to pair" in line or "Device not available" in line:
                outcome = "bluetoothctl reported failure"
                break
            passkey_match = _PASSKEY_RE.search(line)
            if passkey_match:
                # bluetoothctl re-prints the pending prompt on every redraw
                # line (interleaved with [DEL]/[CHG] events, plus the echo of
                # our reply), so only act on the first occurrence per passkey.
                if prompted and passkey_match.group(1) == passkey:
                    continue
                passkey = passkey_match.group(1)
                prompted = True
                try:
                    reply = await asyncio.wait_for(
                        on_prompt(passkey), timeout=PASSKEY_CONFIRM_TIMEOUT_SECONDS
                    )
                except asyncio.TimeoutError:
                    outcome = "timed out waiting for user confirmation"
                    break
                if reply is None:
                    outcome = "aborted by user"
                    break
                await _feed(proc, reply)
    except (BrokenPipeError, ConnectionResetError):
        outcome = "bluetoothctl connection lost"
    finally:
        await _stop_interactive(proc)
    logger.info("Pairing session with %s finished: %s", mac, outcome)
    return paired, outcome


async def _stop_interactive(proc):
    """Send quit and wait for bluetoothctl to exit, killing it if it won't, so
    a hung session can't leak a process that holds the BlueZ agent."""
    with suppress(Exception):
        proc.stdin.write(b"quit\n")
        await proc.stdin.drain()
        proc.stdin.close()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        proc.kill()
        with suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=5)


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
    success, outcome = await _pair_interactive(mac, on_prompt)
    if not success:
        logger.info("Pairing with %s failed: %s", mac, outcome)
        return False, outcome
    try:
        await _run_async(("bluetoothctl", "trust", mac), timeout=10)
    except Exception:
        logger.exception("Failed to trust %s", mac)
        return True, "Paired but could not be trusted"
    return True, "Paired and trusted"


def _probe(mac):
    """Probe a single device via hcitool name (paging). Works for any powered-on
    phone whether or not it's paired/discoverable. Retries since classic BT
    paging is flaky — a page can come back empty for no good reason. Returns
    True if present."""
    for attempt in range(PROBE_ATTEMPTS):
        out = ""
        try:
            out = _run_checked(("hcitool", "name", mac), timeout=PROBE_TIMEOUT_SECONDS)
        except Exception:  # noqa: S110 - a failed page is expected, that's why we retry
            pass
        if out.strip():
            return True
        if attempt < PROBE_ATTEMPTS - 1:
            time.sleep(PROBE_RETRY_DELAY_SECONDS)
    return False


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


def _collapse_transitions(transitions):
    """Collapse a poll cycle's transitions to at most one per nickname, so a
    person with several devices doesn't get one notification per device. When a
    nickname has both a home and an away transition in the same cycle, prefer
    the home one (a single device in range means the person is reachable)."""
    by_nickname = {}
    for transition in transitions:
        nickname = transition["nickname"]
        current = by_nickname.get(nickname)
        if current is None or (transition["home"] and not current["home"]):
            by_nickname[nickname] = transition
    return list(by_nickname.values())


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
            for transition in _collapse_transitions(transitions):
                try:
                    await on_transition(transition)
                except Exception:
                    logger.exception("on_transition callback failed")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Presence check crashed, retrying in %ss", poll_interval_seconds)
        await asyncio.sleep(poll_interval_seconds)
