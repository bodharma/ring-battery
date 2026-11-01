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
        home = patch.object(reader.Path, "home", return_value=self.root)
        home.start()
        self.addCleanup(home.stop)

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

    def backup(
        self,
        entries=None,
        flat=False,
        encrypted=False,
        name="backup",
        values=(bytes(16),),
    ):
        root = self.root / name
        root.mkdir(parents=True)
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
        self.database(str(stored.relative_to(self.root)), values)
        return root

    def local_backup(self, name="first", **kwargs):
        return self.backup(
            name=f"Library/Application Support/MobileSync/Backup/{name}", **kwargs
        )

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

    def test_imports_work_without_extension_loading_api(self):
        sources = [(self.database(), 1), (self.backup(), 2)]
        original_connect = sqlite3.connect

        def connect_without_extension_loading(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            return SimpleNamespace(execute=connection.execute, close=connection.close)

        for path, expected_connections in sources:
            with self.subTest(source=path.name):
                with patch.object(
                    reader.sqlite3,
                    "connect",
                    side_effect=connect_without_extension_loading,
                ) as connect:
                    self.assertEqual(
                        reader.extract_key(str(path)), {"ok": True, "key": "00" * 16}
                    )
                self.assertEqual(connect.call_count, expected_connections)
                for call in connect.call_args_list:
                    self.assertIn("mode=ro&immutable=1", call.args[0])
                    self.assertIs(call.kwargs["uri"], True)

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

    def test_existing_backup_discovers_one_key_without_writes(self):
        self.local_backup()
        before = {
            str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).digest()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        with patch.object(
            reader, "data_directory", side_effect=AssertionError("write")
        ):
            self.assertEqual(
                reader.existing_backup_key(), {"ok": True, "key": "00" * 16}
            )
        after = {
            str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).digest()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)

    def test_existing_backups_accept_duplicate_keys(self):
        self.local_backup("first")
        self.local_backup("second", values=(base64.b64encode(bytes(16)).decode(),))
        self.assertEqual(reader.existing_backup_key(), {"ok": True, "key": "00" * 16})

    def test_existing_backups_reject_distinct_keys(self):
        self.local_backup("first")
        self.local_backup("second", values=(bytes(range(16)),))
        with self.assertRaisesRegex(
            reader.ReaderError, "Multiple distinct.*Choose one"
        ):
            reader.existing_backup_key()

    def test_ambiguous_backup_cannot_be_skipped_for_another_valid_backup(self):
        self.local_backup("first")
        self.local_backup("second", values=(bytes(16), bytes(range(16))))
        with self.assertRaisesRegex(
            reader.ReaderError, "Multiple distinct.*Choose one"
        ):
            reader.existing_backup_key()

    def test_mixed_invalid_and_distinct_key_rows_cannot_select_another_backup(self):
        self.local_backup("first")
        self.local_backup("second", values=(bytes(16), "not-base64!", bytes(range(16))))
        with self.assertRaisesRegex(reader.ReaderError, "invalid ring key.*Choose"):
            reader.existing_backup_key()

    def test_missing_existing_backup_folder_is_reported_without_creation(self):
        with self.assertRaisesRegex(reader.ReaderError, "No existing local backups"):
            reader.existing_backup_key()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_existing_backup_without_oura_key_is_reported(self):
        self.local_backup(entries=[])
        with self.assertRaisesRegex(
            reader.ReaderError, "No ring key.*Advanced key import"
        ):
            reader.existing_backup_key()

    def test_existing_encrypted_backups_have_a_specific_error(self):
        self.local_backup(encrypted=True)
        with self.assertRaisesRegex(
            reader.ReaderError, "backups are encrypted"
        ) as error:
            reader.existing_backup_key()
        self.assertNotIn("disabl", str(error.exception).lower())
        self.assertNotIn(str(self.root), str(error.exception))

    def test_existing_backup_search_uses_readable_unencrypted_oura_backups(self):
        self.local_backup("first")
        self.local_backup("encrypted", encrypted=True)
        self.local_backup("other", entries=[])
        broken = self.local_backup("broken")
        (broken / "Manifest.db").write_bytes(b"invalid database")
        self.assertEqual(reader.existing_backup_key(), {"ok": True, "key": "00" * 16})

    def test_existing_backup_directory_permission_failure_is_safe(self):
        with (
            patch.object(
                reader.Path, "iterdir", side_effect=PermissionError("private")
            ),
            self.assertRaisesRegex(
                reader.ReaderError, "could not be read.*read access"
            ) as error,
        ):
            reader.existing_backup_key()
        self.assertNotIn("private", str(error.exception))

    def test_existing_backup_database_permission_failure_is_safe(self):
        self.local_backup()
        with (
            patch.object(
                reader.sqlite3, "connect", side_effect=PermissionError("private")
            ),
            self.assertRaisesRegex(
                reader.ReaderError, "could not be read.*read access"
            ) as error,
        ):
            reader.existing_backup_key()
        self.assertNotIn("private", str(error.exception))

    def test_existing_backup_search_refuses_more_than_32_candidates_before_import(self):
        root = self.root / "Library/Application Support/MobileSync/Backup"
        for index in range(33):
            child = root / str(index)
            child.mkdir(parents=True)
            (child / "Manifest.db").touch()
        with (
            patch.object(
                reader, "extract_key", side_effect=AssertionError("import")
            ) as extract,
            self.assertRaisesRegex(reader.ReaderError, "More than 32.*Choose one"),
        ):
            reader.existing_backup_key()
        extract.assert_not_called()

    def test_existing_backup_search_accepts_32_and_skips_unrelated_children(self):
        root = self.root / "Library/Application Support/MobileSync/Backup"
        for index in range(32):
            child = root / str(index)
            child.mkdir(parents=True)
            (child / "Manifest.db").touch()
        nested = root / "unrelated" / "nested"
        nested.mkdir(parents=True)
        (nested / "Manifest.db").touch()
        (root / "file").touch()
        with patch.object(
            reader, "extract_key", return_value={"ok": True, "key": "00" * 16}
        ) as extract:
            self.assertEqual(
                reader.existing_backup_key(), {"ok": True, "key": "00" * 16}
            )
        self.assertEqual(extract.call_count, 32)

    def test_existing_backup_search_skips_linked_folders_and_manifests(self):
        source = self.backup()
        root = self.root / "Library/Application Support/MobileSync/Backup"
        root.mkdir(parents=True)
        (root / "linked-folder").symlink_to(source, target_is_directory=True)
        child = root / "linked-manifest"
        child.mkdir()
        (child / "Manifest.db").symlink_to(source / "Manifest.db")
        original_is_dir = reader.Path.is_dir
        original_is_file = reader.Path.is_file

        def is_dir_without_following_links(path):
            self.assertFalse(path.is_symlink(), "A directory link target was probed.")
            return original_is_dir(path)

        def is_file_without_following_links(path):
            self.assertFalse(path.is_symlink(), "A file link target was probed.")
            return original_is_file(path)

        with (
            patch.object(reader.Path, "is_dir", new=is_dir_without_following_links),
            patch.object(reader.Path, "is_file", new=is_file_without_following_links),
            patch.object(
                reader, "extract_key", side_effect=AssertionError("linked import")
            ),
            self.assertRaisesRegex(reader.ReaderError, "No ring key"),
        ):
            reader.existing_backup_key()

    def test_existing_backup_search_rejects_redirected_ancestors_before_probing(self):
        for index, ancestor in enumerate(
            (
                "Library",
                "Library/Application Support",
                "Library/Application Support/MobileSync",
                "Library/Application Support/MobileSync/Backup",
            )
        ):
            home = self.root / f"home-{index}"
            target = self.root / f"target-{index}"
            target.mkdir()
            link = home / ancestor
            link.parent.mkdir(parents=True)
            link.symlink_to(target, target_is_directory=True)
            original_is_symlink = reader.Path.is_symlink

            def is_symlink_without_probing_target(path):
                self.assertFalse(
                    path != link and path.is_relative_to(link),
                    "A redirected ancestor was traversed before checking the link.",
                )
                return original_is_symlink(path)

            with (
                self.subTest(ancestor=ancestor),
                patch.object(reader.Path, "home", return_value=home),
                patch.object(
                    reader.Path, "is_symlink", new=is_symlink_without_probing_target
                ),
                patch.object(
                    reader.Path,
                    "iterdir",
                    side_effect=AssertionError("target enumeration"),
                ),
                self.assertRaisesRegex(
                    reader.ReaderError, "requires a local backup folder"
                ),
            ):
                reader.existing_backup_key()

    def test_explicit_manual_backup_import_can_use_a_linked_folder(self):
        source = self.backup()
        linked = self.root / "manual"
        linked.symlink_to(source, target_is_directory=True)
        self.assertEqual(
            reader.extract_key(str(linked)), {"ok": True, "key": "00" * 16}
        )


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
