import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

spec = importlib.util.spec_from_file_location(
    "reader", Path(__file__).resolve().parents[1] / "backend/read_ring.py"
)
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)

ADDRESS = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-000000000002"


class ReaderTests(unittest.TestCase):
    def test_saved_ring_is_required_even_with_another_ring_nearby(self):
        devices = {OTHER: (SimpleNamespace(address=OTHER), None)}
        with self.assertRaises(ValueError):
            reader.choose_device(devices, ADDRESS)
        devices[ADDRESS] = (SimpleNamespace(address=ADDRESS), None)
        self.assertEqual(reader.choose_device(devices, ADDRESS).address, ADDRESS)
        with self.assertRaises(ValueError):
            reader.choose_device(devices, None)

    def test_private_app_directory_and_json_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "app"
            with patch.dict(os.environ, {"RING_BATTERY_DATA_DIR": str(root)}):
                self.assertEqual(reader.data_directory(), root)
                self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
                root.chmod(0o755)
                reader.data_directory()
                self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
                path = root / "test.json"
                path.with_suffix(".tmp").write_text("stale")
                path.with_suffix(".tmp").chmod(0o644)
                reader.private_json(path, {"mode": 1})
                self.assertEqual(json.loads(path.read_text()), {"mode": 1})
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertFalse(path.with_suffix(".tmp").exists())

    def test_credentials_accept_only_exact_key_and_uuid(self):
        self.assertEqual(
            reader.credentials({"key": "00" * 16, "address": ADDRESS}),
            (bytes(16), ADDRESS),
        )
        for value in (None, 0, "", "0" * 31, "0" * 33, "z" * 32, "00 " * 16):
            with self.subTest(key=value), self.assertRaises(reader.ReaderError):
                reader.credentials({"key": value, "address": ADDRESS})
        for value in (
            None,
            "",
            "neighbor",
            ADDRESS.replace("-", ""),
            "{" + ADDRESS + "}",
        ):
            with self.subTest(address=value), self.assertRaises(reader.ReaderError):
                reader.credentials({"key": "00" * 16, "address": value})

    def test_stdin_is_bounded_and_contains_one_object(self):
        valid = json.dumps({"key": "00" * 16, "address": ADDRESS}).encode()
        with patch.object(
            reader.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(valid))
        ):
            self.assertEqual(reader.read_input(("key", "address"))["address"], ADDRESS)
        for raw in (
            b"",
            b"[]",
            b"null",
            b"{}",
            valid + b"{}",
            b"\xff",
            b"{" + b"[" * 5000,
            b'{"key":1,"key":2,"address":null}',
            b" " * 65537,
        ):
            with (
                self.subTest(raw_length=len(raw)),
                patch.object(
                    reader.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw))
                ),
                self.assertRaises(reader.ReaderError),
            ):
                reader.read_input(("key", "address"))

    def test_self_test_does_not_need_data_or_radio(self):
        with (
            patch.object(
                reader, "data_directory", side_effect=AssertionError("data access")
            ),
            patch.object(
                reader.BleakScanner, "discover", side_effect=AssertionError("BLE")
            ),
        ):
            self.assertEqual(reader.self_test(), {"ok": True, "self_test": True})

    def test_live_frame_validation(self):
        valid = bytes.fromhex("2f082802000200005913")
        self.assertEqual(reader.live_ibi(valid), 857)
        self.assertIsNone(reader.live_ibi(valid[:-1]))
        self.assertIsNone(reader.live_ibi(bytes.fromhex("2f082802000200005903")))

    def test_hrv_requires_coverage_and_does_not_bridge_invalid_beats(self):
        result = reader.summarize([(i, 1000) for i in range(300)], 300)
        self.assertEqual(result["heart_rate"], 60)
        self.assertEqual(result["hrv_rmssd"], 0)
        sparse = reader.summarize([(i * 5, 1000) for i in range(60)], 300)
        self.assertIsNone(sparse["hrv_rmssd"])
        burst = reader.summarize([(i / 100, 1000) for i in range(240)], 300)
        self.assertIsNone(burst["hrv_rmssd"])
        self.assertLess(burst["coverage_percent"], 5)
        two_clusters = [(i, 1000) for i in range(151)] + [
            (299 + i / 1000, 1000) for i in range(89)
        ]
        self.assertIsNone(reader.summarize(two_clusters, 300)["hrv_rmssd"])
        invalid_gap = (
            [(i, 1000) for i in range(150)]
            + [(150, None)]
            + [(i, 1500) for i in range(151, 300)]
        )
        self.assertEqual(reader.summarize(invalid_gap, 300)["hrv_rmssd"], 0)
        self.assertIsNone(
            reader.summarize([(i, 1000) for i in range(30)], 30)["hrv_rmssd"]
        )

    def test_unusable_signal_is_rejected(self):
        with self.assertRaises(reader.ReaderError):
            reader.summarize([(0, None)], 300)


class AsyncReaderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        environment = patch.dict(os.environ, {"RING_BATTERY_DATA_DIR": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)

    async def test_scan_lists_all_rings_without_authentication_or_storage(self):
        devices = {
            address: (
                SimpleNamespace(address=address, name="Oura ring"),
                SimpleNamespace(local_name=None, rssi=-40),
            )
            for address in (ADDRESS, OTHER)
        }
        with (
            patch.object(reader, "discover", AsyncMock(return_value=devices)),
            patch.object(
                reader,
                "authenticate",
                AsyncMock(side_effect=AssertionError("authentication")),
            ),
            patch.object(
                reader, "data_directory", side_effect=AssertionError("data access")
            ),
        ):
            self.assertEqual(
                await reader.scan(),
                {
                    "ok": True,
                    "rings": [
                        {"address": ADDRESS, "name": "Oura ring", "rssi": -40},
                        {"address": OTHER, "name": "Oura ring", "rssi": -40},
                    ],
                },
            )

    async def test_failed_authentication_is_rejected(self):
        responses = [[{"auth_nonce": "00" * 15}], [{"auth_state": 1}]]
        with patch.object(
            reader, "exchange", AsyncMock(side_effect=responses)
        ) as exchange:
            with self.assertRaises(ValueError):
                await reader.authenticate(object(), bytes(16))
            self.assertEqual(exchange.await_count, 2)

    async def test_authentication_proves_challenge_with_selected_key(self):
        nonce = bytes(15)
        responses = [[{"auth_nonce": nonce.hex()}], [{"auth_state": 0}]]
        with patch.object(
            reader, "exchange", AsyncMock(side_effect=responses)
        ) as exchange:
            await reader.authenticate(object(), bytes(16))
            expected = reader.protocol.AES.new(
                bytes(16), reader.protocol.AES.MODE_ECB
            ).encrypt(nonce + b"\x01")
            self.assertEqual(
                exchange.await_args_list[1].args[1],
                reader.protocol.packet(0x2F, b"\x2d" + expected),
            )

    async def test_invalid_authentication_challenge_stops_before_proof(self):
        for response in ([], [{"auth_nonce": "00"}], [{"auth_nonce": "00" * 16}]):
            with (
                self.subTest(response=response),
                patch.object(
                    reader, "exchange", AsyncMock(return_value=response)
                ) as exchange,
            ):
                with self.assertRaises(reader.ReaderError):
                    await reader.authenticate(object(), bytes(16))
                self.assertEqual(exchange.await_count, 1)

    async def test_battery_authenticates_and_stores_no_key_or_peer(self):
        client_context = MagicMock()
        client = client_context.__aenter__.return_value
        devices = {ADDRESS: (SimpleNamespace(address=ADDRESS), None)}
        with (
            patch.object(reader, "discover", AsyncMock(return_value=devices)),
            patch.object(reader, "BleakClient", return_value=client_context),
            patch.object(reader, "authenticate", AsyncMock()) as authenticate,
            patch.object(
                reader, "exchange", AsyncMock(return_value=[{"battery_percent": 77}])
            ),
        ):
            result = await reader.read_ring(bytes(16), ADDRESS)
            self.assertEqual(result["battery_percent"], 77)
            authenticate.assert_awaited_once_with(client, bytes(16))
            self.assertEqual(list(self.root.iterdir()), [])

    async def test_other_ring_is_never_connected(self):
        with (
            patch.object(
                reader,
                "discover",
                AsyncMock(return_value={OTHER: (SimpleNamespace(address=OTHER), None)}),
            ),
            patch.object(reader, "BleakClient") as client,
        ):
            with self.assertRaises(reader.ReaderError):
                await reader.read_ring(bytes(16), ADDRESS)
            client.assert_not_called()

    async def test_failed_authentication_does_not_read_battery_or_change_mode(self):
        marker = self.root / "restore-mode.json"
        marker.write_text(json.dumps({"address": ADDRESS, "mode": 1}))
        with (
            patch.object(
                reader,
                "discover",
                AsyncMock(
                    return_value={ADDRESS: (SimpleNamespace(address=ADDRESS), None)}
                ),
            ),
            patch.object(reader, "BleakClient", return_value=MagicMock()),
            patch.object(
                reader,
                "authenticate",
                AsyncMock(
                    side_effect=reader.ReaderError("Ring authentication failed.")
                ),
            ),
            patch.object(reader, "exchange", AsyncMock()) as exchange,
            patch.object(reader, "set_mode", AsyncMock()) as set_mode,
        ):
            with self.assertRaises(reader.ReaderError):
                await reader.read_ring(bytes(16), ADDRESS)
            exchange.assert_not_awaited()
            set_mode.assert_not_awaited()
            self.assertTrue(marker.exists())

    async def test_recovery_precedes_battery_and_removes_only_confirmed_marker(self):
        marker = self.root / "restore-mode.json"
        marker.write_text(json.dumps({"address": ADDRESS, "mode": 2}))
        order = []

        async def restore(*args):
            order.append("restore")

        async def battery(*args):
            self.assertFalse(marker.exists())
            order.append("battery")
            return [{"battery_percent": 0}]

        with (
            patch.object(
                reader,
                "discover",
                AsyncMock(
                    return_value={ADDRESS: (SimpleNamespace(address=ADDRESS), None)}
                ),
            ),
            patch.object(reader, "BleakClient", return_value=MagicMock()),
            patch.object(reader, "authenticate", AsyncMock()),
            patch.object(
                reader, "set_mode", AsyncMock(side_effect=restore)
            ) as set_mode,
            patch.object(reader, "exchange", AsyncMock(side_effect=battery)),
        ):
            self.assertEqual(
                (await reader.read_ring(bytes(16), ADDRESS))["battery_percent"], 0
            )
            self.assertEqual(order, ["restore", "battery"])
            self.assertEqual(set_mode.await_args.args[1], 2)

    async def test_unsafe_recovery_marker_is_retained(self):
        marker = self.root / "restore-mode.json"
        for recovery in (
            {"address": OTHER, "mode": 1},
            {"address": ADDRESS, "mode": 3},
            {"address": ADDRESS, "mode": True},
            [],
        ):
            marker.write_text(json.dumps(recovery))
            with (
                self.subTest(recovery=recovery),
                patch.object(
                    reader,
                    "discover",
                    AsyncMock(
                        return_value={ADDRESS: (SimpleNamespace(address=ADDRESS), None)}
                    ),
                ),
                patch.object(reader, "BleakClient", return_value=MagicMock()),
                patch.object(reader, "authenticate", AsyncMock()),
                patch.object(reader, "set_mode", AsyncMock()) as set_mode,
                patch.object(reader, "exchange", AsyncMock()) as exchange,
            ):
                with self.assertRaises(reader.ReaderError):
                    await reader.read_ring(bytes(16), ADDRESS)
                self.assertTrue(marker.exists())
                set_mode.assert_not_awaited()
                exchange.assert_not_awaited()

    async def test_invalid_battery_readings_are_rejected(self):
        for frames in (
            [],
            [{"battery_percent": -1}],
            [{"battery_percent": 101}],
            [{"battery_percent": True}],
            [{"battery_percent": "50"}],
        ):
            with (
                self.subTest(frames=frames),
                patch.object(
                    reader,
                    "discover",
                    AsyncMock(
                        return_value={ADDRESS: (SimpleNamespace(address=ADDRESS), None)}
                    ),
                ),
                patch.object(reader, "BleakClient", return_value=MagicMock()),
                patch.object(reader, "authenticate", AsyncMock()),
                patch.object(reader, "exchange", AsyncMock(return_value=frames)),
                self.assertRaises(reader.ReaderError),
            ):
                await reader.read_ring(bytes(16), ADDRESS)

    async def test_cli_modes_pass_stdin_credentials_and_correct_duration(self):
        raw = json.dumps({"key": "00" * 16, "address": ADDRESS}).encode()
        for flags, duration in (
            ([], 0),
            (["--connect"], 0),
            (["--check-in"], 300),
            (["--probe"], 30),
        ):
            output = io.StringIO()
            with (
                self.subTest(flags=flags),
                patch.object(reader.sys, "argv", ["reader", *flags]),
                patch.object(
                    reader.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw))
                ),
                patch.object(reader.sys, "stdout", output),
                patch.object(
                    reader, "read_ring", AsyncMock(return_value={"ok": True})
                ) as read_ring,
            ):
                await reader.main()
                self.assertEqual(json.loads(output.getvalue()), {"ok": True})
                read_ring.assert_awaited_once_with(bytes(16), ADDRESS, duration)

    async def test_cli_never_echoes_secrets_from_external_errors(self):
        raw = json.dumps({"key": "00" * 16, "address": ADDRESS}).encode()
        for error in (
            ValueError("00" * 16),
            RuntimeError("00" * 16),
            reader.protocol.BleakError("00" * 16),
        ):
            output = io.StringIO()
            with (
                self.subTest(error=type(error)),
                patch.object(reader.sys, "argv", ["reader"]),
                patch.object(
                    reader.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw))
                ),
                patch.object(reader.sys, "stdout", output),
                patch.object(reader, "read_ring", AsyncMock(side_effect=error)),
            ):
                await reader.main()
                self.assertFalse(json.loads(output.getvalue())["ok"])
                self.assertNotIn("00" * 16, output.getvalue())

    async def test_cli_rejects_malformed_input_before_radio_access(self):
        for raw in (
            b"",
            b"[]",
            b" " * 65537,
            json.dumps({"key": "00" * 15, "address": ADDRESS}).encode(),
            json.dumps({"key": "00" * 16, "address": "another ring"}).encode(),
        ):
            output = io.StringIO()
            with (
                self.subTest(raw_length=len(raw)),
                patch.object(reader.sys, "argv", ["reader", "--connect"]),
                patch.object(
                    reader.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw))
                ),
                patch.object(reader.sys, "stdout", output),
                patch.object(
                    reader, "discover", AsyncMock(side_effect=AssertionError("BLE"))
                ),
            ):
                await reader.main()
                self.assertFalse(json.loads(output.getvalue())["ok"])
                self.assertNotIn("00" * 15, output.getvalue())

    async def test_scan_self_test_and_existing_backup_cli_need_no_input(self):
        for flag, result in (
            ("--scan", {"ok": True, "rings": []}),
            ("--self-test", {"ok": True, "self_test": True}),
            ("--existing-backup", {"ok": True, "key": "00" * 16}),
        ):
            output = io.StringIO()
            with (
                self.subTest(flag=flag),
                patch.object(reader.sys, "argv", ["reader", flag]),
                patch.object(reader.sys, "stdout", output),
                patch.object(reader, "read_input", side_effect=AssertionError("stdin")),
                patch.object(
                    reader, "scan", AsyncMock(return_value={"ok": True, "rings": []})
                ),
                patch.object(
                    reader,
                    "existing_backup_key",
                    return_value={"ok": True, "key": "00" * 16},
                ),
            ):
                await reader.main()
            self.assertEqual(json.loads(output.getvalue()), result)

    async def test_only_full_check_in_saves_summary(self):
        result = {"heart_rate": 60, "hrv_rmssd": None, "duration_seconds": 30}
        for duration in (30, 300):
            saved = self.root / "last-check-in.json"
            with (
                self.subTest(duration=duration),
                patch.object(
                    reader,
                    "discover",
                    AsyncMock(
                        return_value={ADDRESS: (SimpleNamespace(address=ADDRESS), None)}
                    ),
                ),
                patch.object(reader, "BleakClient", return_value=MagicMock()),
                patch.object(reader, "authenticate", AsyncMock()),
                patch.object(
                    reader, "check_in", AsyncMock(return_value=result)
                ) as check_in,
            ):
                self.assertEqual(
                    await reader.read_ring(bytes(16), ADDRESS, duration),
                    {"ok": True, **result},
                )
                self.assertEqual(
                    check_in.await_args.args[1:],
                    (duration, self.root / "restore-mode.json", ADDRESS),
                )
                self.assertEqual(saved.exists(), duration == 300)
                if saved.exists():
                    self.assertEqual(json.loads(saved.read_text()), result)

    async def test_unknown_command_is_json_error_without_stdin_or_radio(self):
        output = io.StringIO()
        with (
            patch.object(reader.sys, "argv", ["reader", "--reset"]),
            patch.object(reader.sys, "stdout", output),
            patch.object(reader, "read_input", side_effect=AssertionError("stdin")),
            patch.object(
                reader, "discover", AsyncMock(side_effect=AssertionError("BLE"))
            ),
        ):
            await reader.main()
        self.assertEqual(
            json.loads(output.getvalue()), {"ok": False, "error": "Unknown operation."}
        )

    async def test_check_in_cancellation_restores_original_mode(self):
        client = self.check_in_client()
        with tempfile.TemporaryDirectory() as directory:
            recovery = Path(directory) / "recovery.json"
            with (
                patch.object(
                    reader,
                    "exchange",
                    AsyncMock(return_value=[{"feature": 2, "feature_mode": 1}]),
                ),
                patch.object(reader, "set_mode", AsyncMock()) as set_mode,
                patch.object(
                    reader.asyncio,
                    "sleep",
                    AsyncMock(side_effect=reader.asyncio.CancelledError),
                ),
            ):
                with self.assertRaises(reader.asyncio.CancelledError):
                    await reader.check_in(client, 300, recovery, "ring")
                self.assertEqual(
                    [call.args[1] for call in set_mode.await_args_list], [1]
                )
                self.assertFalse(recovery.exists())

    async def test_failed_restoration_keeps_recovery_marker(self):
        client = self.check_in_client()
        with tempfile.TemporaryDirectory() as directory:
            recovery = Path(directory) / "recovery.json"
            with (
                patch.object(
                    reader,
                    "exchange",
                    AsyncMock(return_value=[{"feature": 2, "feature_mode": 1}]),
                ),
                patch.object(
                    reader,
                    "set_mode",
                    AsyncMock(side_effect=RuntimeError("disconnected")),
                ),
                patch.object(reader.asyncio, "sleep", AsyncMock()),
            ):
                with self.assertRaisesRegex(ValueError, "restoration is unconfirmed"):
                    await reader.check_in(client, 300, recovery, "ring")
                self.assertTrue(recovery.exists())

    async def test_failed_notification_cleanup_still_restores_mode(self):
        client = self.check_in_client()
        client.stop_notify.side_effect = RuntimeError("disconnected")
        recovery = self.root / "restore-mode.json"
        with (
            patch.object(
                reader,
                "exchange",
                AsyncMock(return_value=[{"feature": 2, "feature_mode": 2}]),
            ),
            patch.object(reader, "set_mode", AsyncMock()) as set_mode,
            patch.object(reader.asyncio, "sleep", AsyncMock()),
        ):
            with self.assertRaises(RuntimeError):
                await reader.check_in(client, 300, recovery, ADDRESS)
            self.assertEqual(set_mode.await_args.args[1], 2)
            self.assertFalse(recovery.exists())

    def check_in_client(self):
        client = SimpleNamespace(stop_notify=AsyncMock())

        async def start(_, receive):
            client.receive = receive

        async def write(*args, **kwargs):
            client.receive(None, bytes.fromhex("2f03230200"))

        client.start_notify = AsyncMock(side_effect=start)
        client.write_gatt_char = AsyncMock(side_effect=write)
        return client

    async def test_exchange_cancellation_stops_notifications(self):
        client = SimpleNamespace(
            start_notify=AsyncMock(),
            stop_notify=AsyncMock(),
            write_gatt_char=AsyncMock(),
        )
        with patch.object(
            reader.asyncio,
            "sleep",
            AsyncMock(side_effect=reader.asyncio.CancelledError),
        ):
            with self.assertRaises(reader.asyncio.CancelledError):
                await reader.exchange(client, b"\x0c\x00")
        client.stop_notify.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
