import base64
from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import plistlib
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_reader import reader


class ImportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def database(self, name="assa.sqlite", values=(bytes(16),)):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as database:
            database.execute("CREATE TABLE ringconfiguration (auth_key BLOB)")
            database.executemany(
                "INSERT INTO ringconfiguration VALUES (?)",
                [(value,) for value in values],
            )
            database.commit()
        return path

    def backup(self, entries=None, flat=False, encrypted=False):
        root = self.root / "backup"
        root.mkdir()
        (root / "Manifest.plist").write_bytes(
            plistlib.dumps({"IsEncrypted": encrypted})
        )
        file_id = "0" * 40
        relative = "Documents/account/assa.sqlite"
        entries = (
            [(file_id, "AppDomain-com.ouraring.oura", relative)]
            if entries is None
            else entries
        )
        with closing(sqlite3.connect(root / "Manifest.db")) as manifest:
            manifest.execute(
                "CREATE TABLE Files (fileID TEXT, domain TEXT, relativePath TEXT)"
            )
            manifest.executemany("INSERT INTO Files VALUES (?, ?, ?)", entries)
            manifest.commit()
        stored = root / file_id if flat else root / file_id[:2] / file_id
        self.database(str(stored.relative_to(self.root)))
        return root

    def test_plain_key_file_stays_unchanged_and_creates_no_app_data(self):
        path = self.root / "ring-key.txt"
        path.write_text("\n" + "00" * 16 + "\n")
        before = path.read_bytes()
        with patch.object(
            reader, "data_directory", side_effect=AssertionError("data access")
        ):
            self.assertEqual(
                reader.extract_key(str(path)), {"ok": True, "key": "00" * 16}
            )
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_database_accepts_raw_and_base64_keys_in_declared_blob_column(self):
        for index, value in enumerate(
            (
                bytes(16),
                base64.b64encode(bytes(16)),
                base64.b64encode(bytes(16)).decode(),
            )
        ):
            with self.subTest(value_type=type(value)):
                path = self.database(f"database-{index}.sqlite", (value,))
                self.assertEqual(
                    reader.extract_key(str(path)), {"ok": True, "key": "00" * 16}
                )

    def test_duplicate_encodings_of_same_key_are_not_ambiguous(self):
        path = self.database(
            values=(None, bytes(16), base64.b64encode(bytes(16)).decode(), bytes(16))
        )
        self.assertEqual(reader.extract_key(str(path))["key"], "00" * 16)

    def test_database_rejects_multiple_distinct_keys(self):
        path = self.database(values=(bytes(16), bytes(range(16))))
        with self.assertRaisesRegex(reader.ReaderError, "Multiple distinct"):
            reader.extract_key(str(path))

    def test_database_rejects_invalid_key_encodings_and_lengths(self):
        for index, value in enumerate(
            (
                b"",
                bytes(15),
                bytes(17),
                "not-base64!",
                "\u2603",
                1,
                base64.b64encode(bytes(15)),
                base64.b64encode(bytes(17)),
                "00" * 16,
            )
        ):
            with self.subTest(index=index):
                path = self.database(f"invalid-{index}.sqlite", (value,))
                with self.assertRaises(reader.ReaderError):
                    reader.extract_key(str(path))

    def test_empty_database_has_no_key(self):
        path = self.database(values=(None,))
        with self.assertRaisesRegex(reader.ReaderError, "No ring authentication key"):
            reader.extract_key(str(path))

    def test_database_is_immutable_readonly_and_creates_no_journal(self):
        path = self.database()
        path.chmod(0o444)
        before = hashlib.sha256(path.read_bytes()).digest()
        original_connect = sqlite3.connect
        with patch.object(reader.sqlite3, "connect", wraps=original_connect) as connect:
            reader.extract_key(str(path))
        self.assertIn("mode=ro&immutable=1", connect.call_args.args[0])
        self.assertIs(connect.call_args.kwargs["uri"], True)
        self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), before)
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_database_cannot_load_extensions_from_a_view(self):
        path = self.root / "assa.sqlite"
        with closing(sqlite3.connect(path)) as database:
            database.execute(
                "CREATE VIEW ringconfiguration AS SELECT load_extension('unused-extension') AS auth_key"
            )
            database.commit()
        with self.assertRaises(reader.ReaderError):
            reader.extract_key(str(path))

    def test_invalid_plain_files_and_input_paths_are_rejected(self):
        for index, contents in enumerate(
            (
                b"",
                b"00",
                b"x" * 32,
                b"0" * 33,
                b"\xff" * 32,
                b"0" * 32 + b" " * 65536 + b"x",
            )
        ):
            path = self.root / f"bad-{index}"
            path.write_bytes(contents)
            with self.subTest(index=index), self.assertRaises(reader.ReaderError):
                reader.extract_key(str(path))
        for value in (None, 1, "", "\x00", str(self.root / "missing")):
            with self.subTest(path=value), self.assertRaises(reader.ReaderError):
                reader.extract_key(value)

    def test_sharded_backup_uses_only_the_correct_app_database(self):
        entries = [
            ("0" * 40, "AppDomain-com.ouraring.oura", "Documents/account/assa.sqlite"),
            ("invalid", "AppDomain-example.app", "Documents/assa.sqlite"),
            ("invalid", "AppDomain-com.ouraring.oura", "Library/assa.sqlite"),
            (
                "invalid",
                "AppDomain-com.ouraring.oura",
                "Documents/account/other.sqlite",
            ),
        ]
        root = self.backup(entries)
        before = {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).digest()
            for path in root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(reader.extract_key(str(root)), {"ok": True, "key": "00" * 16})
        after = {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).digest()
            for path in root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)

    def test_flat_backup_and_direct_documents_path_are_supported(self):
        root = self.backup(
            [("0" * 40, "AppDomain-com.ouraring.oura", "Documents/assa.sqlite")],
            flat=True,
        )
        self.assertEqual(reader.extract_key(str(root))["key"], "00" * 16)

    def test_encrypted_backup_is_rejected_before_database_access(self):
        root = self.root / "encrypted"
        root.mkdir()
        (root / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": True}))
        with (
            patch.object(
                reader.sqlite3, "connect", side_effect=AssertionError("database access")
            ),
            self.assertRaisesRegex(
                reader.ReaderError, "Encrypted Finder backups are not supported"
            ) as error,
        ):
            reader.extract_key(str(root))
        self.assertNotIn("disabl", str(error.exception).lower())

    def test_invalid_file_ids_are_rejected_without_traversal(self):
        root = self.backup(
            [("../outside", "AppDomain-com.ouraring.oura", "Documents/assa.sqlite")]
        )
        with self.assertRaisesRegex(reader.ReaderError, "invalid file identifier"):
            reader.extract_key(str(root))

    def test_backup_database_symlinks_cannot_escape_selected_folder(self):
        root = self.backup()
        stored = root / "00" / ("0" * 40)
        stored.unlink()
        outside = self.database("outside.sqlite")
        stored.symlink_to(outside)
        with self.assertRaisesRegex(reader.ReaderError, "inside the selected backup"):
            reader.extract_key(str(root))

    def test_multiple_backup_databases_with_different_keys_are_rejected(self):
        second_id = "1" * 40
        root = self.backup(
            [
                (
                    "0" * 40,
                    "AppDomain-com.ouraring.oura",
                    "Documents/first/assa.sqlite",
                ),
                (
                    second_id,
                    "AppDomain-com.ouraring.oura",
                    "Documents/second/assa.sqlite",
                ),
            ]
        )
        self.database(f"backup/11/{second_id}", (bytes(range(16)),))
        with self.assertRaisesRegex(reader.ReaderError, "Multiple distinct"):
            reader.extract_key(str(root))

    def test_missing_backup_database_is_not_created(self):
        root = self.root / "missing-backup"
        root.mkdir()
        with self.assertRaises(reader.ReaderError):
            reader.extract_key(str(root))
        self.assertEqual(list(root.iterdir()), [])


class ImportCLITests(unittest.IsolatedAsyncioTestCase):
    async def test_extract_key_returns_key_only_through_json_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "key.txt"
            path.write_text("00" * 16)
            raw = json.dumps({"path": str(path)}).encode()
            output = io.StringIO()
            with (
                patch.object(reader.sys, "argv", ["reader", "--extract-key"]),
                patch.object(
                    reader.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw))
                ),
                patch.object(reader.sys, "stdout", output),
                patch.object(
                    reader, "data_directory", side_effect=AssertionError("data access")
                ),
            ):
                await reader.main()
            self.assertEqual(
                json.loads(output.getvalue()), {"ok": True, "key": "00" * 16}
            )
            self.assertEqual(list(Path(directory).iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
