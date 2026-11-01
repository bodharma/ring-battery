"""Local, authenticated battery reads and explicitly started resting check-ins."""

import asyncio
import base64
import binascii
from contextlib import closing
import json
import math
import os
from pathlib import Path
import plistlib
import re
import signal
import sqlite3
import sys
import time
from uuid import UUID

VENDOR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "vendor"
sys.path.insert(0, str(VENDOR))
import oura_protocol as protocol  # noqa: E402 — vendored module path must be set first.
from bleak import BleakClient, BleakScanner  # noqa: E402


class ReaderError(ValueError):
    """An error whose fixed message is safe to return to the app."""


def data_directory():
    path = Path(
        os.environ.get(
            "RING_BATTERY_DATA_DIR",
            Path.home() / "Library/Application Support/RingBattery",
        )
    )
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def private_json(path, value):
    temporary = path.with_suffix(".tmp")
    with open(
        temporary, "w", opener=lambda name, flags: os.open(name, flags, 0o600)
    ) as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream)
    temporary.replace(path)


def read_input(fields):
    raw = sys.stdin.buffer.read(65537)
    if len(raw) > 65536:
        raise ReaderError("Input exceeds the 64 KiB limit.")
    try:

        def unique_object(pairs):
            result = {}
            for name, value in pairs:
                if name in result:
                    raise ReaderError("Input contains duplicate fields.")
                result[name] = value
            return result

        value = json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ReaderError("Provide one valid JSON object on standard input.") from exc
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ReaderError("Input fields do not match this operation.")
    return value


def validate_address(value):
    if not isinstance(value, str) or len(value) != 36:
        raise ReaderError("Select a ring with a valid Bluetooth UUID.")
    try:
        address = str(UUID(value)).upper()
    except ValueError as exc:
        raise ReaderError("Select a ring with a valid Bluetooth UUID.") from exc
    if value.upper() != address:
        raise ReaderError("Select a ring with a valid Bluetooth UUID.")
    return address


def credentials(value):
    key = value["key"]
    if not isinstance(key, str) or re.fullmatch(r"[0-9a-fA-F]{32}", key) is None:
        raise ReaderError(
            "The ring key must contain exactly 32 hexadecimal characters."
        )
    return bytes.fromhex(key), validate_address(value["address"])


def database_keys(path):
    try:
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        ) as database:
            database.enable_load_extension(False)
            database.execute("PRAGMA query_only=ON")
            database.execute("PRAGMA trusted_schema=OFF")
            keys = set()
            for (value,) in database.execute(
                "SELECT auth_key FROM ringconfiguration WHERE auth_key IS NOT NULL"
            ):
                if isinstance(value, bytes) and len(value) == 16:
                    key = value
                elif isinstance(value, (str, bytes)):
                    try:
                        key = base64.b64decode(value, validate=True)
                    except (ValueError, binascii.Error) as exc:
                        raise ReaderError(
                            "The database contains an invalid ring key."
                        ) from exc
                else:
                    raise ReaderError("The database contains an invalid ring key.")
                if len(key) != 16:
                    raise ReaderError("The database contains an invalid ring key.")
                keys.add(key)
                if len(keys) > 1:
                    raise ReaderError(
                        "Multiple distinct ring keys were found. Select a file for one ring."
                    )
            return keys
    except sqlite3.Error as exc:
        raise ReaderError(
            "The selected database could not be read as an Oura key database."
        ) from exc


def extract_key(path):
    if not isinstance(path, str) or not path or "\x00" in path:
        raise ReaderError(
            "Select a key file, Oura database, or unencrypted Finder backup folder."
        )
    path = Path(path).expanduser()
    try:
        if path.is_dir():
            manifest_plist = path / "Manifest.plist"
            if manifest_plist.exists():
                with manifest_plist.open("rb") as stream:
                    raw_manifest = stream.read(1048577)
                if len(raw_manifest) > 1048576:
                    raise ReaderError("The backup manifest is too large.")
                metadata = plistlib.loads(raw_manifest)
                if (
                    not isinstance(metadata, dict)
                    or type(metadata.get("IsEncrypted", False)) is not bool
                ):
                    raise ReaderError("The backup manifest is invalid.")
                if metadata.get("IsEncrypted"):
                    raise ReaderError(
                        "Encrypted Finder backups are not supported. Import a key file or Oura database instead."
                    )
            with closing(
                sqlite3.connect(
                    (path / "Manifest.db").resolve().as_uri() + "?mode=ro&immutable=1",
                    uri=True,
                )
            ) as manifest:
                manifest.enable_load_extension(False)
                manifest.execute("PRAGMA query_only=ON")
                manifest.execute("PRAGMA trusted_schema=OFF")
                entries = manifest.execute(
                    "SELECT fileID, relativePath FROM Files WHERE domain=? AND relativePath LIKE 'Documents/%'",
                    ("AppDomain-com.ouraring.oura",),
                ).fetchall()
            keys = set()
            for file_id, relative in entries:
                if (
                    not isinstance(relative, str)
                    or Path(relative).name != "assa.sqlite"
                ):
                    continue
                if (
                    not isinstance(file_id, str)
                    or re.fullmatch(r"[0-9a-fA-F]{40}", file_id) is None
                ):
                    raise ReaderError("The backup contains an invalid file identifier.")
                stored = path / file_id[:2] / file_id
                if not stored.is_file():
                    stored = path / file_id
                if not stored.resolve().is_relative_to(path.resolve()):
                    raise ReaderError(
                        "The backup database must be inside the selected backup folder."
                    )
                keys.update(database_keys(stored))
                if len(keys) > 1:
                    raise ReaderError(
                        "Multiple distinct ring keys were found. Select a file for one ring."
                    )
        else:
            with path.open("rb") as stream:
                header = stream.read(16)
                if header == b"SQLite format 3\x00":
                    keys = database_keys(path)
                else:
                    content = header + stream.read(65521)
                    if (
                        len(content) > 65536
                        or re.fullmatch(rb"[0-9a-fA-F]{32}", content.strip()) is None
                    ):
                        raise ReaderError(
                            "The key file must contain exactly 32 hexadecimal characters."
                        )
                    keys = {bytes.fromhex(content.strip().decode("ascii"))}
        if not keys:
            raise ReaderError(
                "No ring authentication key was found in the selected source."
            )
        return {"ok": True, "key": keys.pop().hex()}
    except (
        OSError,
        sqlite3.Error,
        plistlib.InvalidFileException,
        OverflowError,
    ) as exc:
        raise ReaderError("The selected key source could not be read.") from exc


def choose_device(devices, address):
    address = validate_address(address)
    matches = [
        item[0] for item in devices.values() if item[0].address.upper() == address
    ]
    if not matches:
        raise ReaderError(
            "Your selected ring is not advertising nearby. Try again shortly."
        )
    return matches[0]


async def discover():
    return await BleakScanner.discover(
        timeout=10, return_adv=True, service_uuids=[protocol.OURA_SERVICE]
    )


async def scan():
    devices = await discover()
    return {
        "ok": True,
        "rings": [
            {
                "address": validate_address(device.address),
                "name": advertisement.local_name or device.name or "Oura ring",
                "rssi": advertisement.rssi,
            }
            for device, advertisement in devices.values()
        ],
    }


def parsed_frames(responses):
    return [
        parsed
        for response in responses
        if (parsed := protocol.parse_packet(response)).get("length_ok")
    ]


async def exchange(client, request):
    responses = []
    await client.start_notify(
        protocol.OURA_NOTIFY, lambda _, data: responses.append(bytes(data))
    )
    try:
        await client.write_gatt_char(protocol.OURA_WRITE, request, response=True)
        await asyncio.sleep(1.5)
    finally:
        await asyncio.wait_for(client.stop_notify(protocol.OURA_NOTIFY), 3)
    return parsed_frames(responses)


async def authenticate(client, key):
    nonce_frames = await exchange(client, protocol.COMMANDS["auth_nonce"].request)
    nonce = next(
        (bytes.fromhex(p["auth_nonce"]) for p in nonce_frames if "auth_nonce" in p),
        None,
    )
    if nonce is None or len(nonce) != 15:
        raise ReaderError("Ring authentication did not return a valid challenge.")
    proof = protocol.AES.new(key, protocol.AES.MODE_ECB).encrypt(
        protocol.pad(nonce, 16)
    )
    frames = await exchange(client, protocol.packet(0x2F, bytes([0x2D]) + proof))
    states = [p["auth_state"] for p in frames if "auth_state" in p]
    if not states or states[-1] != 0:
        raise ReaderError("Ring authentication failed. No ring settings were changed.")


def feature_mode(frames):
    modes = [p["feature_mode"] for p in frames if p.get("feature") == 2]
    if not modes or modes[-1] not in (0, 1, 2):
        raise ReaderError(
            "The ring already has a live session, or its HR mode is unavailable."
        )
    return modes[-1]


async def set_mode(client, mode):
    frames = await exchange(client, bytes([0x2F, 3, 0x22, 2, mode]))
    if not any(
        p.get("feature_set_mode") == 2 and p.get("feature_set_mode_result") == 0
        for p in frames
    ):
        raise ReaderError("The ring did not confirm the heart-rate mode change.")


def live_ibi(frame):
    if len(frame) != 10 or frame[:4] != bytes([0x2F, 8, 0x28, 2]) or frame[4] != 0:
        return None
    word = int.from_bytes(frame[8:10], "little")
    interval = word & 0xFFF
    return interval if word >> 12 == 1 and 300 <= interval <= 2000 else None


def summarize(samples, duration):
    valid = [(stamp, ibi) for stamp, ibi in samples if ibi is not None]
    intervals = [ibi for _, ibi in valid]
    if not intervals:
        raise ReaderError(
            "No valid beats received. Wear the ring and keep your hand still."
        )
    covered = 0.0
    previous_end = float("-inf")
    for stamp, interval in valid:
        covered += max(0, stamp - max(previous_end, stamp - interval / 1000))
        previous_end = stamp
    coverage = min(1.0, covered / duration)
    differences = []
    for (previous_time, previous), (stamp, interval) in zip(samples, samples[1:]):
        if (
            previous is not None
            and interval is not None
            and 0.3 <= stamp - previous_time <= 2.5
        ):
            differences.append((interval - previous) ** 2)
    # shortcut: signal coverage is a coarse quality gate, validate against ECG before clinical use.
    quality_ok = duration >= 300 and coverage >= 0.8 and len(differences) >= 150
    return {
        "heart_rate": round(60000 / (sum(intervals) / len(intervals))),
        "hrv_rmssd": round(math.sqrt(sum(differences) / len(differences)), 1)
        if quality_ok
        else None,
        "valid_beats": len(intervals),
        "coverage_percent": round(coverage * 100),
        "duration_seconds": duration,
        "measured_at": time.time(),
    }


async def check_in(client, duration, recovery_path, address):
    original = feature_mode(
        await exchange(client, protocol.COMMANDS["feature_daytime_hr"].request)
    )
    private_json(recovery_path, {"address": address, "mode": original})
    samples = []
    acknowledgement = asyncio.get_running_loop().create_future()

    def receive(_, data):
        frame = bytes(data)
        parsed = protocol.parse_packet(frame)
        if (
            parsed.get("length_ok")
            and parsed.get("feature_set_mode") == 2
            and not acknowledgement.done()
        ):
            acknowledgement.set_result(parsed.get("feature_set_mode_result"))
        if len(frame) >= 4 and frame[:4] == bytes([0x2F, 8, 0x28, 2]):
            samples.append((time.monotonic(), live_ibi(frame)))

    try:
        await client.start_notify(protocol.OURA_NOTIFY, receive)
        try:
            await client.write_gatt_char(
                protocol.OURA_WRITE, bytes([0x2F, 3, 0x22, 2, 3]), response=True
            )
            if await asyncio.wait_for(acknowledgement, 5) != 0:
                raise ReaderError("The ring declined live heart-rate measurement.")
            await asyncio.sleep(duration)
        finally:
            await asyncio.wait_for(client.stop_notify(protocol.OURA_NOTIFY), 3)
    finally:

        async def restore():
            try:
                await asyncio.wait_for(set_mode(client, original), 6)
                recovery_path.unlink(missing_ok=True)
            except Exception as exc:
                raise ReaderError(
                    "HR mode restoration is unconfirmed. Refresh battery to retry restoration."
                ) from exc

        cleanup = asyncio.create_task(restore())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise
    return summarize(samples, duration)


async def read_ring(key, address, duration=0):
    root = data_directory()
    recovery_path = root / "restore-mode.json"
    devices = await discover()
    device = choose_device(devices, address)
    async with BleakClient(device, timeout=12) as client:
        await authenticate(client, key)
        if recovery_path.exists():
            recovery = json.loads(recovery_path.read_text())
            if (
                not isinstance(recovery, dict)
                or validate_address(recovery.get("address")) != address
                or type(recovery.get("mode")) is not int
                or recovery["mode"] not in (0, 1, 2)
            ):
                raise ReaderError("Saved HR recovery state needs inspection.")
            await set_mode(client, recovery["mode"])
            recovery_path.unlink()
        if duration:
            result = await check_in(client, duration, recovery_path, device.address)
            if duration == 300:
                private_json(root / "last-check-in.json", result)
            return {"ok": True, **result}
        frames = await exchange(client, protocol.COMMANDS["battery"].request)
        values = [p["battery_percent"] for p in frames if "battery_percent" in p]
        if not values or type(values[-1]) is not int or not 0 <= values[-1] <= 100:
            raise ReaderError("The ring did not return a valid battery reading.")
        return {"ok": True, "battery_percent": values[-1], "measured_at": time.time()}


def self_test():
    if sys.platform == "darwin":
        import bleak.backends.corebluetooth.client  # noqa: F401
        import bleak.backends.corebluetooth.scanner  # noqa: F401
    assert (
        protocol.AES.new(bytes(16), protocol.AES.MODE_ECB).encrypt(bytes(16)).hex()
        == "66e94bd4ef8a2c3b884cfa59ca342b2e"
    )
    assert protocol.COMMANDS["battery"].request == b"\x0c\x00"
    assert live_ibi(bytes.fromhex("2f082802000200005913")) == 857
    return {"ok": True, "self_test": True}


async def main():
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    cancel_requested = False

    def cancel_once():
        nonlocal cancel_requested
        if not cancel_requested:
            cancel_requested = True
            task.cancel()

    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, cancel_once)
    try:
        operation = sys.argv[1:]
        if operation == ["--self-test"]:
            result = self_test()
        elif operation == ["--scan"]:
            result = await asyncio.wait_for(scan(), 45)
        elif operation == ["--extract-key"]:
            result = extract_key(read_input(("path",))["path"])
        elif operation in ([], ["--connect"], ["--check-in"], ["--probe"]):
            key, address = credentials(read_input(("key", "address")))
            duration = (
                300
                if operation == ["--check-in"]
                else 30
                if operation == ["--probe"]
                else 0
            )
            result = await asyncio.wait_for(
                read_ring(key, address, duration), 45 + duration
            )
        else:
            raise ReaderError("Unknown operation.")
    except asyncio.CancelledError:
        result = {"ok": False, "cancelled": True, "error": "Check-in cancelled."}
    except Exception as exc:
        # Third-party exceptions can contain keys or private paths; only return our fixed messages.
        message = (
            str(exc)
            if isinstance(exc, ReaderError)
            else "The operation failed. Check your selection and try again."
        )
        if (
            isinstance(exc, protocol.BleakError)
            and "not authorized" in str(exc).lower()
        ):
            message = (
                "Allow Ring Battery Bluetooth access in System Settings, then Refresh."
            )
        result = {"ok": False, "error": message}
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    os.umask(0o077)
    asyncio.run(main())
