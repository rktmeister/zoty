import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from zoty import db


class FakeResponse:
    def __init__(self, *, status_code: int, url: str):
        self.status_code = status_code
        self.url = url


class FakeHttpError(Exception):
    def __init__(self, message: str, *, status_code: int, url: str):
        super().__init__(message)
        self.response = FakeResponse(status_code=status_code, url=url)


class DbTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db = db._ZOTERO_DB
        self.original_storage = db._ZOTERO_STORAGE
        self.original_sidecar_root = db._SIDECAR_ROOT
        self.original_zot = db._zot

        db._ZOTERO_DB = Path(self.temp_dir.name) / "zotero.sqlite"
        db._ZOTERO_STORAGE = Path(self.temp_dir.name) / "storage"
        db._SIDECAR_ROOT = Path(self.temp_dir.name) / "sidecar"
        db._ZOTERO_STORAGE.mkdir(parents=True, exist_ok=True)
        db._zot = None

        with closing(sqlite3.connect(db._ZOTERO_DB)) as conn:
            conn.executescript(
                """
                CREATE TABLE collections (
                    collectionID INTEGER PRIMARY KEY,
                    key TEXT NOT NULL,
                    name TEXT NOT NULL
                );
                """
            )
            conn.executemany(
                "INSERT INTO collections(collectionID, key, name) VALUES (?, ?, ?)",
                [
                    (1, "COLL123", "Valid Collection"),
                    (2, "COLL456", "Secondary Collection"),
                ],
            )
            conn.commit()

    def tearDown(self):
        db._ZOTERO_DB = self.original_db
        db._ZOTERO_STORAGE = self.original_storage
        db._SIDECAR_ROOT = self.original_sidecar_root
        db._zot = self.original_zot
        with db._index_lock:
            db._search_state = None
            db._refresh_in_progress = False
            db._refresh_requested = False
        self.temp_dir.cleanup()

    def _paper_item(self):
        return {
            "data": {
                "key": "PARENT1",
                "itemType": "preprint",
                "title": "Example Paper",
                "creators": [{"firstName": "Jane", "lastName": "Example"}],
                "date": "2026-03-10",
                "dateAdded": "2026-03-10 10:00:00",
                "DOI": "10.1000/example",
                "url": "https://example.org/paper",
                "tags": [{"tag": "chemistry"}],
                "collections": ["COLL123"],
                "abstractNote": "Example abstract.",
            }
        }

    def _creator_dicts(self, count: int) -> list[dict[str, str]]:
        return [
            {"firstName": f"Author{index + 1}", "lastName": "Example"}
            for index in range(count)
        ]

    def _install_search_state(self, docs, *, parents=None):
        if parents is None:
            parents = {
                "PARENT1": {
                    "key": "PARENT1",
                    "dateModified": "2026-03-10 10:00:00",
                    "itemType": "preprint",
                    "title": "Example Paper",
                    "abstract": "Example abstract.",
                    "creators": ["Jane Example"],
                    "collections": ["COLL123"],
                    "tags": ["chemistry"],
                    "date": "2026-03-10",
                    "DOI": "10.1000/example",
                    "url": "https://example.org/paper",
                }
            }

        with closing(db._connect_manifest(writable=True)) as conn:
            db._initialize_manifest(conn)
            for parent_item_id, parent in enumerate(parents.values(), start=1):
                db._upsert_parent(
                    conn,
                    db._ParentRecord(
                        parent_key=parent["key"],
                        parent_item_id=parent_item_id,
                        item_version=1,
                        date_modified=parent["dateModified"],
                        item_type=parent["itemType"],
                        title=parent["title"],
                        abstract=parent["abstract"],
                        creators=list(parent["creators"]),
                        collections=list(parent["collections"]),
                        tags=list(parent["tags"]),
                        date=parent["date"],
                        doi=parent["DOI"],
                        url=parent["url"],
                        metadata_hash=f"hash-{parent['key']}",
                    ),
                )
            for doc, _score in docs:
                db._insert_doc(conn, doc)
            db._set_meta(conn, "last_source_fingerprint", "fingerprint-1")
            db._set_meta(conn, "last_refresh_status", "ready")
            conn.commit()

        state = db._load_search_state()
        self.assertIsNotNone(state)
        db._search_state = state


class AttachmentPathsTests(DbTestCase):
    def setUp(self):
        super().setUp()

        with closing(sqlite3.connect(db._ZOTERO_DB)) as conn:
            conn.executescript(
                """
                CREATE TABLE items (
                    itemID INTEGER PRIMARY KEY,
                    key TEXT NOT NULL,
                    dateAdded TEXT NOT NULL
                );
                CREATE TABLE itemAttachments (
                    itemID INTEGER PRIMARY KEY,
                    parentItemID INT,
                    linkMode INT,
                    contentType TEXT,
                    path TEXT
                );
                CREATE TABLE fields (
                    fieldID INTEGER PRIMARY KEY,
                    fieldName TEXT NOT NULL
                );
                CREATE TABLE itemDataValues (
                    valueID INTEGER PRIMARY KEY,
                    value TEXT UNIQUE
                );
                CREATE TABLE itemData (
                    itemID INTEGER NOT NULL,
                    fieldID INTEGER NOT NULL,
                    valueID INTEGER NOT NULL
                );
                """
            )
            conn.execute(
                "INSERT INTO items(itemID, key, dateAdded) VALUES (?, ?, ?)",
                (1, "PARENT1", "2026-03-10 10:00:00"),
            )
            conn.execute(
                "INSERT INTO items(itemID, key, dateAdded) VALUES (?, ?, ?)",
                (2, "ATTACH1", "2026-03-10 10:01:00"),
            )
            conn.execute(
                "INSERT INTO fields(fieldID, fieldName) VALUES (?, ?)",
                (1, "title"),
            )
            conn.execute(
                "INSERT INTO itemDataValues(valueID, value) VALUES (?, ?)",
                (1, "Attached PDF"),
            )
            conn.execute(
                "INSERT INTO itemData(itemID, fieldID, valueID) VALUES (?, ?, ?)",
                (2, 1, 1),
            )
            conn.execute(
                """INSERT INTO itemAttachments(itemID, parentItemID, linkMode, contentType, path)
                   VALUES (?, ?, ?, ?, ?)""",
                (2, 1, 0, "application/pdf", "storage:paper.pdf"),
            )
            conn.commit()

    def test_get_item_attachments_returns_slim_metadata_without_filepaths(self):
        attachments = db._get_item_attachments("PARENT1")

        self.assertEqual(
            attachments,
            [
                {
                    "key": "ATTACH1",
                    "title": "Attached PDF",
                    "contentType": "application/pdf",
                    "linkMode": "imported_file",
                }
            ],
        )

    def test_get_item_attachments_by_parent_logs_and_falls_back_on_failure(self):
        stderr = io.StringIO()

        with (
            patch("zoty.db._open_zotero_db", side_effect=RuntimeError("boom")),
            patch("sys.stderr", new=stderr),
        ):
            attachments_by_parent = db._get_item_attachments_by_parent(["PARENT1"])

        self.assertEqual(attachments_by_parent, {"PARENT1": []})
        self.assertIn("zoty: failed to load attachment metadata for PARENT1: boom", stderr.getvalue())

    def test_get_item_attachment_count_logs_and_falls_back_on_failure(self):
        stderr = io.StringIO()

        with (
            patch("zoty.db._open_zotero_db", side_effect=RuntimeError("boom")),
            patch("sys.stderr", new=stderr),
        ):
            count = db._get_item_attachment_count("PARENT1")

        self.assertEqual(count, 0)
        self.assertIn("zoty: failed to count attachments for PARENT1: boom", stderr.getvalue())

    def test_get_item_attachment_counts_logs_and_falls_back_on_failure(self):
        stderr = io.StringIO()

        with (
            patch("zoty.db._open_zotero_db", side_effect=RuntimeError("boom")),
            patch("sys.stderr", new=stderr),
        ):
            counts = db._get_item_attachment_counts(["PARENT1", " PARENT2 ", "PARENT1"])

        self.assertEqual(counts, {"PARENT1": 0, "PARENT2": 0})
        self.assertIn("zoty: failed to count attachments for PARENT1, PARENT2: boom", stderr.getvalue())

    def test_format_link_mode_maps_known_and_unknown_values(self):
        self.assertEqual(db._format_link_mode(0), "imported_file")
        self.assertEqual(db._format_link_mode(1), "imported_url")
        self.assertEqual(db._format_link_mode(2), "linked_file")
        self.assertEqual(db._format_link_mode(3), "linked_url")
        self.assertEqual(db._format_link_mode(99), "unknown(99)")
        self.assertEqual(db._format_link_mode(None), "unknown")

    def test_get_item_includes_slim_attachment_metadata(self):
        zot = Mock()
        zot.item.return_value = self._paper_item()

        with patch("zoty.db._get_zot", return_value=zot):
            result = json.loads(db.get_item("PARENT1"))

        self.assertEqual(result["key"], "PARENT1")
        self.assertEqual(result["attachment_count"], 1)
        self.assertEqual(
            result["attachments"][0],
            {
                "key": "ATTACH1",
                "title": "Attached PDF",
                "contentType": "application/pdf",
                "linkMode": "imported_file",
            },
        )
        self.assertEqual(result["attachments"][0]["contentType"], "application/pdf")
        self.assertEqual(
            result["collections"],
            [{"key": "COLL123", "name": "Valid Collection"}],
        )
        self.assertNotIn(str(db._ZOTERO_STORAGE), json.dumps(result))

    def test_get_item_normalizes_item_key_to_uppercase(self):
        zot = Mock()
        zot.item.return_value = self._paper_item()

        with patch("zoty.db._get_zot", return_value=zot):
            json.loads(db.get_item(" parent1 "))

        zot.item.assert_called_once_with("PARENT1")

    def test_get_item_truncates_very_long_creator_lists(self):
        zot = Mock()
        item = self._paper_item()
        item["data"]["creators"] = self._creator_dicts(18)
        zot.item.return_value = item

        with patch("zoty.db._get_zot", return_value=zot):
            result = json.loads(db.get_item("PARENT1"))

        self.assertEqual(len(result["creators"]), db._DETAIL_VIEW_MAX_CREATORS + 1)
        self.assertEqual(result["creators"][0], "Author1 Example")
        self.assertEqual(result["creators"][-1], "... and 3 more")
        self.assertEqual(result["collections"], [{"key": "COLL123", "name": "Valid Collection"}])

    def test_get_item_batches_truncate_creator_lists_more_aggressively(self):
        def fetch_side_effect(item_key):
            item = self._paper_item()
            item["data"]["key"] = item_key
            item["data"]["creators"] = self._creator_dicts(18)
            return item

        with (
            patch("zoty.db._fetch_item_detail", side_effect=fetch_side_effect),
            patch(
                "zoty.db._get_item_attachments_by_parent",
                return_value={"PARENT1": [], "PARENT2": []},
            ),
        ):
            result = json.loads(db.get_item(item_keys=["parent1", "parent2"]))

        self.assertEqual(result["item_keys"], ["PARENT1", "PARENT2"])
        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["items"][0]["creators"]), db._LIST_VIEW_MAX_CREATORS + 1)
        self.assertEqual(result["items"][0]["creators"][-1], "... and 13 more")
        self.assertEqual(result["items"][0]["collections"], [{"key": "COLL123", "name": "Valid Collection"}])

    def test_get_item_rejects_empty_item_key(self):
        with patch("zoty.db._get_zot") as get_zot_mock:
            result = json.loads(db.get_item(""))

        self.assertEqual(result, {"error": "Provide item_key"})
        get_zot_mock.assert_not_called()

    def test_get_item_rejects_whitespace_only_item_key(self):
        with patch("zoty.db._get_zot") as get_zot_mock:
            result = json.loads(db.get_item("   "))

        self.assertEqual(result, {"error": "Provide item_key"})
        get_zot_mock.assert_not_called()

    def test_get_item_supports_single_key_via_item_keys_without_changing_shape(self):
        zot = Mock()
        zot.item.return_value = self._paper_item()

        with patch("zoty.db._get_zot", return_value=zot):
            result = json.loads(db.get_item(item_keys=[" parent1 "]))

        self.assertEqual(result["key"], "PARENT1")
        self.assertNotIn("items", result)
        zot.item.assert_called_once_with("PARENT1")

    def test_get_item_requires_at_least_one_key_for_batch_mode(self):
        with patch("zoty.db._get_zot") as get_zot_mock:
            result = json.loads(db.get_item(item_keys=[]))

        self.assertEqual(
            result,
            {
                "error": "Provide item_key or item_keys",
                "items": [],
                "total": 0,
            },
        )
        get_zot_mock.assert_not_called()

    def test_get_item_returns_structured_error_skeleton_for_fetch_failure(self):
        zot = Mock()
        zot.item.side_effect = RuntimeError("boom")

        with patch("zoty.db._get_zot", return_value=zot):
            result = json.loads(db.get_item("PARENT1"))

        self.assertEqual(
            result,
            {
                "key": "PARENT1",
                "error": "Failed to fetch item PARENT1: boom",
            },
        )

    def test_get_item_sanitizes_http_not_found_errors(self):
        zot = Mock()
        zot.item.side_effect = FakeHttpError(
            "GET http://localhost:23119/api/users/0/items/PARENT1 Code 404 Response: not found",
            status_code=404,
            url="http://localhost:23119/api/users/0/items/PARENT1",
        )

        with patch("zoty.db._get_zot", return_value=zot):
            result = json.loads(db.get_item("PARENT1"))

        self.assertEqual(
            result,
            {
                "key": "PARENT1",
                "error": "Item PARENT1 was not found",
            },
        )
        self.assertNotIn("localhost", json.dumps(result))

    def test_get_item_sanitizes_stringified_http_not_found_errors(self):
        zot = Mock()
        zot.item.side_effect = RuntimeError(
            (
                "Code: 404 URL: http://localhost:23119/api/users/0/items/PARENT1 "
                "Method: GET Response:"
            )
        )

        with patch("zoty.db._get_zot", return_value=zot):
            result = json.loads(db.get_item("PARENT1"))

        self.assertEqual(
            result,
            {
                "key": "PARENT1",
                "error": "Item PARENT1 was not found",
            },
        )
        self.assertNotIn("localhost", json.dumps(result))

    def test_get_zot_creates_only_one_client_under_concurrent_first_access(self):
        barrier = threading.Barrier(8)
        created_clients = []
        results = [None] * 8
        errors = []

        def construct(*_args, **_kwargs):
            client = object()
            created_clients.append(client)
            time.sleep(0.05)
            return client

        def worker(index):
            try:
                barrier.wait(timeout=1)
                results[index] = db._get_zot()
            except BaseException as exc:  # pragma: no cover - surfaced by assertions below
                errors.append(exc)

        with patch("zoty.db.zotero.Zotero", side_effect=construct):
            threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

        self.assertEqual(errors, [])
        self.assertEqual(len(created_clients), 1)
        self.assertTrue(all(result is not None for result in results))
        self.assertEqual({id(result) for result in results}, {id(created_clients[0])})

    def test_get_item_returns_multiple_items_and_partial_errors(self):
        zot = Mock()

        def item_side_effect(item_key):
            if item_key == "BADKEY":
                raise RuntimeError("missing item")

            item = self._paper_item()
            item["data"]["key"] = item_key
            item["data"]["title"] = f"{item_key} title"
            item["data"]["abstractNote"] = f"{item_key} abstract"
            return item

        zot.item.side_effect = item_side_effect

        with patch("zoty.db._get_zot", return_value=zot):
            result = json.loads(db.get_item(item_keys=["parent1", "badkey", "parent2"]))

        self.assertEqual(result["item_keys"], ["PARENT1", "BADKEY", "PARENT2"])
        self.assertEqual(result["requested"], 3)
        self.assertEqual(result["total"], 2)
        self.assertEqual([item["key"] for item in result["items"]], ["PARENT1", "PARENT2"])
        self.assertEqual(result["items"][0]["collections"], [{"key": "COLL123", "name": "Valid Collection"}])
        self.assertEqual(
            result["errors"],
            [
                {
                    "key": "BADKEY",
                    "error": "Failed to fetch item BADKEY: missing item",
                }
            ],
        )

    def test_get_item_fetches_multiple_items_concurrently_and_batches_attachments(self):
        barrier = threading.Barrier(2)

        def fetch_side_effect(item_key):
            barrier.wait(timeout=1)
            item = self._paper_item()
            item["data"]["key"] = item_key
            return item

        with (
            patch("zoty.db._fetch_item_detail", side_effect=fetch_side_effect) as fetch_mock,
            patch(
                "zoty.db._get_item_attachments_by_parent",
                return_value={"GOOD1": [], "GOOD2": []},
            ) as attachments_mock,
        ):
            result = json.loads(db.get_item(item_keys=["good1", "good2"]))

        self.assertEqual(result["total"], 2)
        self.assertNotIn("errors", result)
        self.assertEqual(fetch_mock.call_count, 2)
        self.assertEqual(attachments_mock.call_count, 1)
        attachments_mock.assert_called_once_with(["GOOD1", "GOOD2"])
        self.assertEqual(result["items"][0]["collections"], [{"key": "COLL123", "name": "Valid Collection"}])

    def test_list_collections_returns_structured_error_skeleton_for_fetch_failure(self):
        with patch("zoty.db._get_zot", side_effect=RuntimeError("boom")):
            result = json.loads(db.list_collections())

        self.assertEqual(result["collections"], [])
        self.assertEqual(result["total"], 0)
        self.assertIn("Failed to fetch collections: boom", result["error"])

    def test_search_includes_attachment_count(self):
        attachment_doc = {
            "doc_id": "chunk:ATTACH1:0",
            "parent_key": "PARENT1",
            "attachment_key": "ATTACH1",
            "doc_kind": "attachment_chunk",
            "chunk_index": 0,
            "char_start": 0,
            "char_end": 42,
            "token_count": 6,
            "text": "example body text that matches the query",
            "text_hash": "hash-1",
        }
        self._install_search_state([(attachment_doc, 2.5)])

        result = json.loads(db.search("example"))

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["key"], "PARENT1")
        self.assertEqual(result["items"][0]["attachment_count"], 1)
        self.assertEqual(result["items"][0]["collections"], [{"key": "COLL123", "name": "Valid Collection"}])
        self.assertNotIn("attachments", result["items"][0])
        self.assertEqual(result["items"][0]["snippet_attachment_key"], "ATTACH1")

    def test_search_batches_attachment_detail_lookups_for_multiple_results(self):
        with closing(sqlite3.connect(db._ZOTERO_DB)) as conn:
            conn.execute(
                "INSERT INTO items(itemID, key, dateAdded) VALUES (?, ?, ?)",
                (3, "PARENT2", "2026-03-10 10:02:00"),
            )
            conn.execute(
                "INSERT INTO items(itemID, key, dateAdded) VALUES (?, ?, ?)",
                (4, "ATTACH2", "2026-03-10 10:03:00"),
            )
            conn.execute(
                "INSERT INTO itemDataValues(valueID, value) VALUES (?, ?)",
                (2, "Attached EPUB"),
            )
            conn.execute(
                "INSERT INTO itemData(itemID, fieldID, valueID) VALUES (?, ?, ?)",
                (4, 1, 2),
            )
            conn.execute(
                """INSERT INTO itemAttachments(itemID, parentItemID, linkMode, contentType, path)
                   VALUES (?, ?, ?, ?, ?)""",
                (4, 3, 1, "application/epub+zip", "storage:book.epub"),
            )
            conn.commit()

        parents = {
            "PARENT1": {
                "key": "PARENT1",
                "dateModified": "2026-03-10 10:00:00",
                "itemType": "preprint",
                "title": "Example Paper",
                "abstract": "Example abstract.",
                "creators": ["Jane Example"],
                "collections": ["COLL123"],
                "tags": ["chemistry"],
                "date": "2026-03-10",
                "DOI": "10.1000/example",
                "url": "https://example.org/paper",
            },
            "PARENT2": {
                "key": "PARENT2",
                "dateModified": "2026-03-11 10:00:00",
                "itemType": "preprint",
                "title": "Second Paper",
                "abstract": "Second abstract.",
                "creators": ["John Example"],
                "collections": ["COLL456"],
                "tags": ["biology"],
                "date": "2026-03-11",
                "DOI": "",
                "url": "",
            },
        }
        docs = [
            ({
                "doc_id": "meta:PARENT1",
                "parent_key": "PARENT1",
                "attachment_key": "",
                "doc_kind": "metadata",
                "chunk_index": 0,
                "char_start": 0,
                "char_end": 20,
                "token_count": 3,
                "text": "example result one",
                "text_hash": "hash-1",
            }, 4.0),
            ({
                "doc_id": "meta:PARENT2",
                "parent_key": "PARENT2",
                "attachment_key": "",
                "doc_kind": "metadata",
                "chunk_index": 0,
                "char_start": 0,
                "char_end": 20,
                "token_count": 3,
                "text": "example result two",
                "text_hash": "hash-2",
            }, 3.5),
        ]
        self._install_search_state(docs, parents=parents)

        with patch("zoty.db._open_zotero_db", wraps=db._open_zotero_db) as open_db_mock:
            result = json.loads(db.search("example", limit=2, include_attachments=True))

        self.assertEqual(result["total"], 2)
        items_by_key = {row["key"]: row for row in result["items"]}
        self.assertEqual(set(items_by_key), {"PARENT1", "PARENT2"})
        self.assertEqual(
            [items_by_key[key]["attachment_count"] for key in ("PARENT1", "PARENT2")],
            [1, 1],
        )
        self.assertEqual(
            items_by_key["PARENT1"]["attachments"][0],
            {
                "key": "ATTACH1",
                "title": "Attached PDF",
                "contentType": "application/pdf",
                "linkMode": "imported_file",
            },
        )
        self.assertEqual(
            items_by_key["PARENT2"]["attachments"][0],
            {
                "key": "ATTACH2",
                "title": "Attached EPUB",
                "contentType": "application/epub+zip",
                "linkMode": "imported_url",
            },
        )
        self.assertEqual(open_db_mock.call_count, 2)
        self.assertNotIn(str(db._ZOTERO_STORAGE), json.dumps(result))
        self.assertEqual(open_db_mock.call_count, 2)


class CollectionItemTests(DbTestCase):
    def setUp(self):
        super().setUp()
        with closing(sqlite3.connect(db._ZOTERO_DB)) as conn:
            conn.executescript(
                """
                CREATE TABLE items (
                    itemID INTEGER PRIMARY KEY,
                    key TEXT NOT NULL,
                    dateAdded TEXT NOT NULL
                );
                CREATE TABLE itemAttachments (
                    itemID INTEGER PRIMARY KEY,
                    parentItemID INT,
                    linkMode INT,
                    contentType TEXT,
                    path TEXT
                );
                CREATE TABLE fields (
                    fieldID INTEGER PRIMARY KEY,
                    fieldName TEXT NOT NULL
                );
                CREATE TABLE itemDataValues (
                    valueID INTEGER PRIMARY KEY,
                    value TEXT UNIQUE
                );
                CREATE TABLE itemData (
                    itemID INTEGER NOT NULL,
                    fieldID INTEGER NOT NULL,
                    valueID INTEGER NOT NULL
                );
                """
            )
            conn.executemany(
                "INSERT INTO items(itemID, key, dateAdded) VALUES (?, ?, ?)",
                [
                    (1, "ITEM1", "2026-03-10 10:00:00"),
                    (2, "ATTACH1", "2026-03-10 10:01:00"),
                ],
            )
            conn.execute("INSERT INTO fields(fieldID, fieldName) VALUES (?, ?)", (1, "title"))
            conn.execute("INSERT INTO itemDataValues(valueID, value) VALUES (?, ?)", (1, "Collection PDF"))
            conn.execute("INSERT INTO itemData(itemID, fieldID, valueID) VALUES (?, ?, ?)", (2, 1, 1))
            conn.execute(
                """INSERT INTO itemAttachments(itemID, parentItemID, linkMode, contentType, path)
                   VALUES (?, ?, ?, ?, ?)""",
                (2, 1, 0, "application/pdf", "storage:collection.pdf"),
            )
            conn.commit()

    def test_list_collection_items_returns_structured_error_for_invalid_key(self):
        zot = Mock()
        zot.collections.return_value = [
            {"data": {"key": "COLL123", "name": "Valid Collection"}, "meta": {"numItems": 4}}
        ]

        with patch("zoty.db._get_zot", return_value=zot):
            result = json.loads(db.list_collection_items("missing"))

        self.assertEqual(result["collection_key"], "MISSING")
        self.assertFalse(result["collection_found"])
        self.assertEqual(result["items"], [])
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["requested_limit"], 25)
        self.assertEqual(result["applied_limit"], db._LIST_RESULT_LIMIT_CAP)
        self.assertEqual(result["limit_cap"], db._LIST_RESULT_LIMIT_CAP)
        self.assertFalse(result["limit_capped"])
        self.assertIn("not found", result["error"])
        zot.collection_items.assert_not_called()

    def test_list_collection_items_returns_structured_filtered_items_for_valid_key(self):
        zot = Mock()
        zot.collections.return_value = [
            {"data": {"key": "COLL123", "name": "Valid Collection"}, "meta": {"numItems": 4}}
        ]
        zot.collection_items.return_value = [
            {
                "data": {
                    "key": "ITEM1",
                    "itemType": "preprint",
                    "title": "First Paper",
                    "creators": [{"firstName": "Jane", "lastName": "Example"}],
                    "date": "2026-03-10",
                    "DOI": "10.1000/one",
                    "url": "https://example.org/one",
                    "tags": [{"tag": "chemistry"}],
                    "collections": ["COLL123"],
                    "abstractNote": "First abstract.",
                }
            },
            {
                "data": {
                    "key": "ITEM2",
                    "itemType": "preprint",
                    "title": "Wrong Collection",
                    "creators": [{"firstName": "John", "lastName": "Example"}],
                    "date": "2026-03-11",
                    "DOI": "10.1000/two",
                    "url": "https://example.org/two",
                    "tags": [],
                    "collections": ["OTHER"],
                    "abstractNote": "Second abstract.",
                }
            },
            {
                "data": {
                    "key": "ATTACH1",
                    "itemType": "attachment",
                    "title": "Attachment",
                    "creators": [],
                    "date": "",
                    "DOI": "",
                    "url": "",
                    "tags": [],
                    "collections": ["COLL123"],
                    "abstractNote": "",
                }
            },
            {
                "data": {
                    "key": "ANNOT1",
                    "itemType": "annotation",
                    "title": "Annotation",
                    "creators": [],
                    "date": "",
                    "DOI": "",
                    "url": "",
                    "tags": [],
                    "collections": ["COLL123"],
                    "abstractNote": "",
                }
            },
        ]

        with patch("zoty.db._get_zot", return_value=zot):
            result = json.loads(db.list_collection_items("coll123", limit=5))

        self.assertEqual(result["collection_key"], "COLL123")
        self.assertTrue(result["collection_found"])
        self.assertEqual(result["total"], 4)
        self.assertEqual(result["returned_count"], 1)
        self.assertEqual(result["requested_limit"], 5)
        self.assertEqual(result["applied_limit"], 5)
        self.assertEqual(result["limit_cap"], db._LIST_RESULT_LIMIT_CAP)
        self.assertFalse(result["limit_capped"])
        self.assertEqual([row["key"] for row in result["items"]], ["ITEM1"])
        self.assertEqual(result["items"][0]["title"], "First Paper")
        self.assertEqual(result["items"][0]["attachment_count"], 1)
        self.assertEqual(result["items"][0]["collections"], [{"key": "COLL123", "name": "Valid Collection"}])
        self.assertNotIn("attachments", result["items"][0])
        zot.collection_items.assert_called_once_with("COLL123", limit=25, start=0)

    def test_list_collection_items_caps_requested_limit_and_reports_metadata(self):
        zot = Mock()
        zot.collections.return_value = [
            {"data": {"key": "COLL123", "name": "Valid Collection"}, "meta": {"numItems": 1}}
        ]
        zot.collection_items.return_value = [
            {
                "data": {
                    "key": "ITEM1",
                    "itemType": "preprint",
                    "title": "First Paper",
                    "creators": [{"firstName": "Jane", "lastName": "Example"}],
                    "date": "2026-03-10",
                    "DOI": "10.1000/one",
                    "url": "https://example.org/one",
                    "tags": [{"tag": "chemistry"}],
                    "collections": ["COLL123"],
                    "abstractNote": "First abstract.",
                }
            }
        ]

        with patch("zoty.db._get_zot", return_value=zot):
            result = json.loads(db.list_collection_items("coll123", limit=999))

        self.assertEqual(result["requested_limit"], 999)
        self.assertEqual(result["applied_limit"], db._LIST_RESULT_LIMIT_CAP)
        self.assertEqual(result["limit_cap"], db._LIST_RESULT_LIMIT_CAP)
        self.assertTrue(result["limit_capped"])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["returned_count"], 1)
        zot.collection_items.assert_called_once_with("COLL123", limit=75, start=0)

    def test_list_collection_items_preserves_zero_requested_limit_and_skips_item_fetch(self):
        zot = Mock()
        zot.collections.return_value = [
            {"data": {"key": "COLL123", "name": "Valid Collection"}, "meta": {"numItems": 1}}
        ]

        with patch("zoty.db._get_zot", return_value=zot):
            result = json.loads(db.list_collection_items("coll123", limit=0))

        self.assertEqual(result["collection_key"], "COLL123")
        self.assertTrue(result["collection_found"])
        self.assertEqual(result["items"], [])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["returned_count"], 0)
        self.assertEqual(result["requested_limit"], 0)
        self.assertEqual(result["applied_limit"], 0)
        self.assertEqual(result["limit_cap"], db._LIST_RESULT_LIMIT_CAP)
        self.assertFalse(result["limit_capped"])
        zot.collection_items.assert_not_called()

    def test_list_collection_items_truncates_long_creator_lists(self):
        zot = Mock()
        zot.collections.return_value = [
            {"data": {"key": "COLL123", "name": "Valid Collection"}, "meta": {"numItems": 1}}
        ]
        zot.collection_items.return_value = [
            {
                "data": {
                    "key": "ITEM1",
                    "itemType": "preprint",
                    "title": "First Paper",
                    "creators": self._creator_dicts(7),
                    "date": "2026-03-10",
                    "DOI": "10.1000/one",
                    "url": "https://example.org/one",
                    "tags": [{"tag": "chemistry"}],
                    "collections": ["COLL123"],
                    "abstractNote": "First abstract.",
                }
            },
        ]

        with patch("zoty.db._get_zot", return_value=zot):
            result = json.loads(db.list_collection_items("coll123", limit=5))

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["returned_count"], 1)
        self.assertEqual(
            result["items"][0]["creators"],
            [
                "Author1 Example",
                "Author2 Example",
                "Author3 Example",
                "Author4 Example",
                "Author5 Example",
                "... and 2 more",
            ],
        )

    def test_list_collection_items_fetches_past_skipped_first_page(self):
        zot = Mock()
        zot.collections.return_value = [
            {"data": {"key": "COLL123", "name": "Valid Collection"}, "meta": {"numItems": 1}}
        ]
        skipped_page = [
            {
                "data": {
                    "key": f"ATTACH{index}",
                    "itemType": "attachment",
                    "collections": ["COLL123"],
                }
            }
            for index in range(25)
        ]
        parent_item = {
            "data": {
                "key": "ITEM1",
                "itemType": "preprint",
                "title": "First Paper",
                "creators": [{"firstName": "Jane", "lastName": "Example"}],
                "date": "2026-03-10",
                "DOI": "10.1000/one",
                "url": "https://example.org/one",
                "tags": [{"tag": "chemistry"}],
                "collections": ["COLL123"],
                "abstractNote": "First abstract.",
            }
        }

        def collection_items_side_effect(collection_key, **kwargs):
            self.assertEqual(collection_key, "COLL123")
            if kwargs["start"] == 0:
                return skipped_page
            if kwargs["start"] == 25:
                return [parent_item]
            return []

        zot.collection_items.side_effect = collection_items_side_effect

        with patch("zoty.db._get_zot", return_value=zot):
            result = json.loads(db.list_collection_items("coll123", limit=1))

        self.assertEqual([row["key"] for row in result["items"]], ["ITEM1"])
        self.assertEqual(result["returned_count"], 1)


class RecentItemsLimitTests(DbTestCase):
    def test_get_recent_items_truncates_long_creator_lists(self):
        zot = Mock()
        item = self._paper_item()
        item["data"]["creators"] = self._creator_dicts(8)
        zot.top.return_value = [
            {"data": {"itemType": "preprint"}},
            {"data": {"itemType": "note"}},
        ]
        zot.everything.return_value = [
            {"data": {"itemType": "preprint"}},
            {"data": {"itemType": "note"}},
        ]
        zot.items.return_value = [item]

        with patch("zoty.db._get_zot", return_value=zot):
            result = json.loads(db.get_recent_items(limit=1))

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["returned_count"], 1)
        self.assertEqual(result["requested_limit"], 1)
        self.assertEqual(result["applied_limit"], 1)
        self.assertEqual(result["limit_cap"], db._LIST_RESULT_LIMIT_CAP)
        self.assertFalse(result["limit_capped"])
        self.assertEqual(
            result["items"][0]["creators"],
            [
                "Author1 Example",
                "Author2 Example",
                "Author3 Example",
                "Author4 Example",
                "Author5 Example",
                "... and 3 more",
            ],
        )

    def test_get_recent_items_caps_requested_limit_and_reports_metadata(self):
        zot = Mock()
        zot.top.return_value = [
            {"data": {"itemType": "preprint"}},
        ]
        zot.everything.return_value = [
            {"data": {"itemType": "preprint"}},
        ]
        zot.items.return_value = [self._paper_item()]

        with patch("zoty.db._get_zot", return_value=zot):
            result = json.loads(db.get_recent_items(limit=999))

        self.assertEqual(result["requested_limit"], 999)
        self.assertEqual(result["applied_limit"], db._LIST_RESULT_LIMIT_CAP)
        self.assertEqual(result["limit_cap"], db._LIST_RESULT_LIMIT_CAP)
        self.assertTrue(result["limit_capped"])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["returned_count"], 1)
        zot.items.assert_called_once_with(
            limit=75,
            start=0,
            sort="dateAdded",
            direction="desc",
        )

    def test_get_recent_items_fetches_past_skipped_first_page(self):
        zot = Mock()
        zot.top.return_value = [
            {"data": {"itemType": "preprint"}},
            {"data": {"itemType": "attachment"}},
        ]
        zot.everything.return_value = [
            {"data": {"itemType": "preprint"}},
            {"data": {"itemType": "attachment"}},
        ]
        skipped_page = [
            {"data": {"key": f"ATTACH{index}", "itemType": "attachment"}}
            for index in range(25)
        ]
        parent_item = self._paper_item()

        def items_side_effect(**kwargs):
            if kwargs["start"] == 0:
                return skipped_page
            if kwargs["start"] == 25:
                return [parent_item]
            return []

        zot.items.side_effect = items_side_effect

        with patch("zoty.db._get_zot", return_value=zot):
            result = json.loads(db.get_recent_items(limit=1))

        self.assertEqual([row["key"] for row in result["items"]], ["PARENT1"])
        self.assertEqual(result["returned_count"], 1)

    def test_get_recent_items_preserves_zero_requested_limit_and_reports_total(self):
        zot = Mock()
        zot.top.return_value = [
            {"data": {"itemType": "preprint"}},
            {"data": {"itemType": "note"}},
        ]
        zot.everything.return_value = [
            {"data": {"itemType": "preprint"}},
            {"data": {"itemType": "note"}},
        ]

        with patch("zoty.db._get_zot", return_value=zot):
            result = json.loads(db.get_recent_items(limit=0))

        self.assertEqual(result["items"], [])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["returned_count"], 0)
        self.assertEqual(result["requested_limit"], 0)
        self.assertEqual(result["applied_limit"], 0)
        self.assertEqual(result["limit_cap"], db._LIST_RESULT_LIMIT_CAP)
        self.assertFalse(result["limit_capped"])
        zot.items.assert_not_called()

    def test_get_recent_items_returns_structured_error_skeleton_for_fetch_failure(self):
        with patch("zoty.db._get_zot", side_effect=RuntimeError("boom")):
            result = json.loads(db.get_recent_items(limit=1))

        self.assertEqual(result["items"], [])
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["returned_count"], 0)
        self.assertEqual(result["requested_limit"], 1)
        self.assertEqual(result["applied_limit"], 1)
        self.assertEqual(result["limit_cap"], db._LIST_RESULT_LIMIT_CAP)
        self.assertFalse(result["limit_capped"])
        self.assertIn("Failed to fetch recent items: boom", result["error"])


class ParentRecordDateNormalizationTests(DbTestCase):
    def setUp(self):
        super().setUp()
        with closing(sqlite3.connect(db._ZOTERO_DB)) as conn:
            conn.executescript(
                """
                CREATE TABLE items (
                    itemID INTEGER PRIMARY KEY,
                    key TEXT NOT NULL,
                    version INTEGER,
                    dateModified TEXT,
                    itemTypeID INTEGER NOT NULL,
                    libraryID INTEGER
                );
                CREATE TABLE itemTypesCombined (
                    itemTypeID INTEGER PRIMARY KEY,
                    typeName TEXT NOT NULL
                );
                CREATE TABLE deletedItems (
                    itemID INTEGER PRIMARY KEY
                );
                CREATE TABLE fields (
                    fieldID INTEGER PRIMARY KEY,
                    fieldName TEXT NOT NULL
                );
                CREATE TABLE itemDataValues (
                    valueID INTEGER PRIMARY KEY,
                    value TEXT UNIQUE
                );
                CREATE TABLE itemData (
                    itemID INTEGER NOT NULL,
                    fieldID INTEGER NOT NULL,
                    valueID INTEGER NOT NULL
                );
                CREATE TABLE itemCreators (
                    itemID INTEGER NOT NULL,
                    creatorID INTEGER NOT NULL,
                    orderIndex INTEGER NOT NULL
                );
                CREATE TABLE creators (
                    creatorID INTEGER PRIMARY KEY,
                    firstName TEXT,
                    lastName TEXT,
                    fieldMode INTEGER
                );
                CREATE TABLE collectionItems (
                    collectionID INTEGER NOT NULL,
                    itemID INTEGER NOT NULL,
                    orderIndex INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS collections (
                    collectionID INTEGER PRIMARY KEY,
                    key TEXT NOT NULL,
                    name TEXT NOT NULL
                );
                CREATE TABLE itemTags (
                    itemID INTEGER NOT NULL,
                    tagID INTEGER NOT NULL
                );
                CREATE TABLE tags (
                    tagID INTEGER PRIMARY KEY,
                    name TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """INSERT INTO itemTypesCombined(itemTypeID, typeName)
                   VALUES (?, ?)""",
                (1, "preprint"),
            )
            conn.execute(
                """INSERT INTO items(itemID, key, version, dateModified, itemTypeID, libraryID)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (1, "PARENT1", 3, "2026-03-10 10:00:00", 1, 1),
            )
            conn.executemany(
                "INSERT INTO fields(fieldID, fieldName) VALUES (?, ?)",
                [
                    (1, "title"),
                    (2, "date"),
                ],
            )
            conn.executemany(
                "INSERT INTO itemDataValues(valueID, value) VALUES (?, ?)",
                [
                    (1, "Normalized Date Paper"),
                    (2, "2025-09-17 2025-09-17"),
                ],
            )
            conn.executemany(
                "INSERT INTO itemData(itemID, fieldID, valueID) VALUES (?, ?, ?)",
                [
                    (1, 1, 1),
                    (1, 2, 2),
                ],
            )
            conn.commit()

    def test_normalize_item_date_collapses_duplicate_iso_dates_only(self):
        self.assertEqual(db._normalize_item_date("2025-09-17 2025-09-17"), "2025-09-17")
        self.assertEqual(db._normalize_item_date(" 2025-09-17   2025-09-17 "), "2025-09-17")
        self.assertEqual(db._normalize_item_date("2025-09-17 2025-10-01"), "2025-09-17 2025-10-01")
        self.assertEqual(db._normalize_item_date("Spring 2025 Spring 2025"), "Spring 2025 Spring 2025")

    def test_fetch_parent_records_normalizes_duplicated_date_field(self):
        parents = db._fetch_parent_records()

        self.assertEqual(parents["PARENT1"].date, "2025-09-17")
        self.assertEqual(parents["PARENT1"].title, "Normalized Date Paper")


class AttachmentRecordFetchTests(DbTestCase):
    def setUp(self):
        super().setUp()
        with closing(sqlite3.connect(db._ZOTERO_DB)) as conn:
            conn.executescript(
                """
                CREATE TABLE items (
                    itemID INTEGER PRIMARY KEY,
                    key TEXT NOT NULL,
                    version INTEGER
                );
                CREATE TABLE itemAttachments (
                    itemID INTEGER PRIMARY KEY,
                    parentItemID INT,
                    linkMode INT,
                    contentType TEXT,
                    path TEXT,
                    storageModTime INT,
                    storageHash TEXT,
                    lastProcessedModificationTime INT
                );
                CREATE TABLE fulltextItems (
                    itemID INTEGER PRIMARY KEY,
                    indexedPages INT,
                    totalPages INT,
                    indexedChars INT,
                    totalChars INT,
                    version INT
                );
                """
            )
            conn.executemany(
                "INSERT INTO items(itemID, key, version) VALUES (?, ?, ?)",
                [
                    (1, "PARENT1", 3),
                    (2, "ATTACH1", 4),
                    (3, "STANDALONE", 5),
                ],
            )
            conn.executemany(
                """INSERT INTO itemAttachments(
                       itemID, parentItemID, linkMode, contentType, path,
                       storageModTime, storageHash, lastProcessedModificationTime
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (2, 1, 0, "application/pdf", "storage:paper.pdf", None, "", None),
                    (3, None, 0, "application/pdf", "storage:standalone.pdf", None, "", None),
                ],
            )
            conn.execute(
                """INSERT INTO fulltextItems(
                       itemID, indexedPages, totalPages, indexedChars, totalChars, version
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (2, None, None, None, 1, 28),
            )
            conn.commit()

    def test_fetch_attachment_records_ignores_standalone_attachments(self):
        parents = {
            "PARENT1": db._ParentRecord(
                parent_key="PARENT1",
                parent_item_id=1,
                item_version=3,
                date_modified="2026-03-10 10:00:00",
                item_type="preprint",
                title="Parent Paper",
                abstract="",
                creators=[],
                collections=[],
                tags=[],
                date="2026-03-10",
                doi="",
                url="",
                metadata_hash="parent-hash",
            )
        }

        attachments = db._fetch_attachment_records(parents)

        self.assertEqual(list(attachments), ["ATTACH1"])
        self.assertIsNone(attachments["ATTACH1"].indexed_chars)
        self.assertEqual(attachments["ATTACH1"].total_chars, 1)


class RecentItemsTests(DbTestCase):
    def setUp(self):
        super().setUp()
        with closing(sqlite3.connect(db._ZOTERO_DB)) as conn:
            conn.executescript(
                """
                CREATE TABLE items (
                    itemID INTEGER PRIMARY KEY,
                    key TEXT NOT NULL,
                    dateAdded TEXT NOT NULL
                );
                CREATE TABLE itemAttachments (
                    itemID INTEGER PRIMARY KEY,
                    parentItemID INT,
                    linkMode INT,
                    contentType TEXT,
                    path TEXT
                );
                CREATE TABLE fields (
                    fieldID INTEGER PRIMARY KEY,
                    fieldName TEXT NOT NULL
                );
                CREATE TABLE itemDataValues (
                    valueID INTEGER PRIMARY KEY,
                    value TEXT UNIQUE
                );
                CREATE TABLE itemData (
                    itemID INTEGER NOT NULL,
                    fieldID INTEGER NOT NULL,
                    valueID INTEGER NOT NULL
                );
                """
            )
            conn.executemany(
                "INSERT INTO items(itemID, key, dateAdded) VALUES (?, ?, ?)",
                [
                    (1, "ITEM1", "2026-03-10 10:00:00"),
                    (2, "ATTACH1", "2026-03-10 10:01:00"),
                ],
            )
            conn.execute("INSERT INTO fields(fieldID, fieldName) VALUES (?, ?)", (1, "title"))
            conn.execute("INSERT INTO itemDataValues(valueID, value) VALUES (?, ?)", (1, "Recent PDF"))
            conn.execute("INSERT INTO itemData(itemID, fieldID, valueID) VALUES (?, ?, ?)", (2, 1, 1))
            conn.execute(
                """INSERT INTO itemAttachments(itemID, parentItemID, linkMode, contentType, path)
                   VALUES (?, ?, ?, ?, ?)""",
                (2, 1, 0, "application/pdf", "storage:recent.pdf"),
            )
            conn.commit()

    def test_get_recent_items_includes_attachment_count_and_date_added(self):
        zot = Mock()
        zot.top.return_value = [
            {"data": {"itemType": "preprint"}},
            {"data": {"itemType": "attachment"}},
        ]
        zot.everything.return_value = [
            {"data": {"itemType": "preprint"}},
            {"data": {"itemType": "attachment"}},
        ]
        zot.items.return_value = [
            {
                "data": {
                    "key": "ITEM1",
                    "itemType": "preprint",
                    "title": "Recent Paper",
                    "creators": [{"firstName": "Jane", "lastName": "Example"}],
                    "date": "2026-03-10",
                    "dateAdded": "2026-03-10 10:00:00",
                    "DOI": "10.1000/recent",
                    "url": "https://example.org/recent",
                    "tags": [{"tag": "ml"}],
                    "collections": ["COLL123"],
                    "abstractNote": "Recent abstract.",
                }
            },
            {
                "data": {
                    "key": "ATTACH1",
                    "itemType": "attachment",
                    "title": "Attachment",
                    "creators": [],
                    "date": "",
                    "DOI": "",
                    "url": "",
                    "tags": [],
                    "collections": [],
                    "abstractNote": "",
                }
            },
        ]

        with patch("zoty.db._get_zot", return_value=zot):
            result = json.loads(db.get_recent_items(limit=1))

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["returned_count"], 1)
        self.assertEqual(result["requested_limit"], 1)
        self.assertEqual(result["applied_limit"], 1)
        self.assertEqual(result["limit_cap"], db._LIST_RESULT_LIMIT_CAP)
        self.assertFalse(result["limit_capped"])
        self.assertEqual(result["items"][0]["key"], "ITEM1")
        self.assertEqual(result["items"][0]["attachment_count"], 1)
        self.assertEqual(result["items"][0]["date_added"], "2026-03-10T10:00:00")
        self.assertEqual(result["items"][0]["collections"], [{"key": "COLL123", "name": "Valid Collection"}])
        self.assertNotIn("attachments", result["items"][0])
        zot.items.assert_called_once_with(limit=25, start=0, sort="dateAdded", direction="desc")


class SearchBehaviorTests(DbTestCase):
    def setUp(self):
        super().setUp()
        with closing(sqlite3.connect(db._ZOTERO_DB)) as conn:
            conn.executescript(
                """
                CREATE TABLE items (
                    itemID INTEGER PRIMARY KEY,
                    key TEXT NOT NULL,
                    dateAdded TEXT NOT NULL
                );
                CREATE TABLE itemAttachments (
                    itemID INTEGER PRIMARY KEY,
                    parentItemID INT,
                    linkMode INT,
                    contentType TEXT,
                    path TEXT
                );
                CREATE TABLE fields (
                    fieldID INTEGER PRIMARY KEY,
                    fieldName TEXT NOT NULL
                );
                CREATE TABLE itemDataValues (
                    valueID INTEGER PRIMARY KEY,
                    value TEXT UNIQUE
                );
                CREATE TABLE itemData (
                    itemID INTEGER NOT NULL,
                    fieldID INTEGER NOT NULL,
                    valueID INTEGER NOT NULL
                );
                """
            )
            conn.executemany(
                "INSERT INTO items(itemID, key, dateAdded) VALUES (?, ?, ?)",
                [
                    (1, "PARENT1", "2026-03-10 10:00:00"),
                    (2, "ATTACH1", "2026-03-10 10:01:00"),
                    (3, "ATTACH2", "2026-03-10 10:02:00"),
                ],
            )
            conn.execute("INSERT INTO fields(fieldID, fieldName) VALUES (?, ?)", (1, "title"))
            conn.executemany(
                "INSERT INTO itemDataValues(valueID, value) VALUES (?, ?)",
                [
                    (1, "Attached PDF"),
                    (2, "Second PDF"),
                ],
            )
            conn.executemany(
                "INSERT INTO itemData(itemID, fieldID, valueID) VALUES (?, ?, ?)",
                [
                    (2, 1, 1),
                    (3, 1, 2),
                ],
            )
            conn.executemany(
                """INSERT INTO itemAttachments(itemID, parentItemID, linkMode, contentType, path)
                   VALUES (?, ?, ?, ?, ?)""",
                [
                    (2, 1, 0, "application/pdf", "storage:paper.pdf"),
                    (3, 1, 0, "application/pdf", "storage:paper-2.pdf"),
                ],
            )
            conn.commit()

    def test_search_returns_error_when_index_not_loaded(self):
        db._search_state = None

        result = json.loads(db.search("body only"))

        self.assertEqual(result["error"], "Index is still building, please retry in a moment")
        self.assertEqual(result["items"], [])

    def test_search_returns_actionable_error_when_fts_query_fails(self):
        doc = {
            "doc_id": "meta:PARENT1",
            "parent_key": "PARENT1",
            "attachment_key": "",
            "doc_kind": "metadata",
            "chunk_index": 0,
            "char_start": 0,
            "char_end": 20,
            "token_count": 3,
            "text": "query match first",
            "text_hash": "hash-1",
        }
        self._install_search_state([(doc, 1.0)])

        with (
            patch(
                "zoty.db._ranked_fts_rows",
                side_effect=sqlite3.DatabaseError("damaged index"),
            ),
            patch("sys.stderr", new_callable=io.StringIO),
        ):
            result = json.loads(db.search("query"))

        self.assertEqual(result["items"], [])
        self.assertEqual(
            result["error"],
            "Search index is unavailable, please retry in a moment",
        )

    def test_body_only_query_returns_parent_with_attachment_snippet(self):
        attachment_doc = {
            "doc_id": "chunk:ATTACH1:0",
            "parent_key": "PARENT1",
            "attachment_key": "ATTACH1",
            "doc_kind": "attachment_chunk",
            "chunk_index": 0,
            "char_start": 0,
            "char_end": 80,
            "token_count": 12,
            "text": "This body text contains meatpotatoes evidence deep in the paper body.",
            "text_hash": "hash-body",
        }
        self._install_search_state([(attachment_doc, 7.25)])

        result = json.loads(db.search("meatpotatoes"))

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["key"], "PARENT1")
        self.assertIn("meatpotatoes", result["items"][0]["snippet"].lower())
        self.assertEqual(result["items"][0]["snippet_attachment_key"], "ATTACH1")

    def test_search_truncates_long_creator_lists(self):
        attachment_doc = {
            "doc_id": "chunk:ATTACH1:0",
            "parent_key": "PARENT1",
            "attachment_key": "ATTACH1",
            "doc_kind": "attachment_chunk",
            "chunk_index": 0,
            "char_start": 0,
            "char_end": 80,
            "token_count": 12,
            "text": "This body text contains meatpotatoes evidence deep in the paper body.",
            "text_hash": "hash-body",
        }
        parents = {
            "PARENT1": {
                "key": "PARENT1",
                "dateModified": "2026-03-10 10:00:00",
                "itemType": "preprint",
                "title": "Example Paper",
                "abstract": "Example abstract.",
                "creators": [f"Author {index + 1}" for index in range(7)],
                "collections": ["COLL123"],
                "tags": ["chemistry"],
                "date": "2026-03-10",
                "DOI": "10.1000/example",
                "url": "https://example.org/paper",
            }
        }
        self._install_search_state([(attachment_doc, 7.25)], parents=parents)

        result = json.loads(db.search("meatpotatoes"))

        self.assertEqual(
            result["items"][0]["creators"],
            [
                "Author 1",
                "Author 2",
                "Author 3",
                "Author 4",
                "Author 5",
                "... and 2 more",
            ],
        )

    def test_search_normalizes_duplicated_iso_dates_in_results(self):
        metadata_doc = {
            "doc_id": "meta:PARENT1",
            "parent_key": "PARENT1",
            "attachment_key": "",
            "doc_kind": "metadata",
            "chunk_index": 0,
            "char_start": 0,
            "char_end": 60,
            "token_count": 8,
            "text": "Example Paper Example Paper abstract novelty signal",
            "text_hash": "hash-meta",
        }
        parents = {
            "PARENT1": {
                "key": "PARENT1",
                "dateModified": "2026-03-10 10:00:00",
                "itemType": "preprint",
                "title": "Example Paper",
                "abstract": "Example abstract.",
                "creators": ["Jane Example"],
                "collections": ["COLL123"],
                "tags": ["chemistry"],
                "date": "2025-09-17 2025-09-17",
                "DOI": "10.1000/example",
                "url": "https://example.org/paper",
            }
        }
        self._install_search_state([(metadata_doc, 6.5)], parents=parents)

        result = json.loads(db.search("novelty"))

        self.assertEqual(result["items"][0]["date"], "2025-09-17")

    def test_metadata_query_uses_abstract_snippet_without_attachment_key(self):
        metadata_doc = {
            "doc_id": "meta:PARENT1",
            "parent_key": "PARENT1",
            "attachment_key": "",
            "doc_kind": "metadata",
            "chunk_index": 0,
            "char_start": 0,
            "char_end": 60,
            "token_count": 8,
            "text": "Example Paper Example Paper abstract novelty signal",
            "text_hash": "hash-meta",
        }
        self._install_search_state([(metadata_doc, 6.5)])

        result = json.loads(db.search("novelty"))

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["key"], "PARENT1")
        self.assertIn("example abstract", result["items"][0]["snippet"].lower())
        self.assertNotIn("snippet_attachment_key", result["items"][0])

    def test_multiple_matching_chunks_collapse_to_one_parent(self):
        parents = {
            "PARENT1": {
                "key": "PARENT1",
                "dateModified": "2026-03-10 10:00:00",
                "itemType": "preprint",
                "title": "First Paper",
                "abstract": "First abstract.",
                "creators": ["Jane Example"],
                "collections": ["COLL123"],
                "tags": ["chemistry"],
                "date": "2026-03-10",
                "DOI": "10.1000/one",
                "url": "https://example.org/one",
            },
            "PARENT2": {
                "key": "PARENT2",
                "dateModified": "2026-03-09 09:00:00",
                "itemType": "preprint",
                "title": "Second Paper",
                "abstract": "Second abstract.",
                "creators": ["John Example"],
                "collections": ["COLL123"],
                "tags": ["physics"],
                "date": "2026-03-09",
                "DOI": "10.1000/two",
                "url": "https://example.org/two",
            },
        }
        docs = [
            ({
                "doc_id": "chunk:ATTACH1:0",
                "parent_key": "PARENT1",
                "attachment_key": "ATTACH1",
                "doc_kind": "attachment_chunk",
                "chunk_index": 0,
                "char_start": 0,
                "char_end": 30,
                "token_count": 4,
                "text": "shared body text first hit",
                "text_hash": "hash-1",
            }, 9.0),
            ({
                "doc_id": "chunk:ATTACH2:0",
                "parent_key": "PARENT1",
                "attachment_key": "ATTACH2",
                "doc_kind": "attachment_chunk",
                "chunk_index": 0,
                "char_start": 0,
                "char_end": 30,
                "token_count": 4,
                "text": "shared body text second hit",
                "text_hash": "hash-2",
            }, 8.5),
            ({
                "doc_id": "meta:PARENT2",
                "parent_key": "PARENT2",
                "attachment_key": "",
                "doc_kind": "metadata",
                "chunk_index": 0,
                "char_start": 0,
                "char_end": 30,
                "token_count": 4,
                "text": "shared body text second paper",
                "text_hash": "hash-3",
            }, 7.0),
        ]
        self._install_search_state(docs, parents=parents)

        result = json.loads(db.search("shared"))

        self.assertEqual(result["total"], 2)
        self.assertEqual(result["returned_count"], 2)
        self.assertEqual([row["key"] for row in result["items"]], ["PARENT1", "PARENT2"])
        self.assertIsInstance(result["items"][0]["score"], float)

    def test_search_deduplicates_duplicate_papers_and_prefers_richer_item(self):
        parents = {
            "PARENT1": {
                "key": "PARENT1",
                "dateModified": "2026-03-10 10:00:00",
                "itemType": "preprint",
                "title": "Duplicate Paper",
                "abstract": "Duplicate abstract.",
                "creators": ["Jane Example"],
                "collections": [],
                "tags": [],
                "date": "2026-03-10",
                "DOI": "10.1000/duplicate",
                "url": "https://example.org/duplicate",
            },
            "PARENT2": {
                "key": "PARENT2",
                "dateModified": "2026-03-11 10:00:00",
                "itemType": "preprint",
                "title": "Duplicate Paper",
                "abstract": "Duplicate abstract.",
                "creators": ["Jane Example"],
                "collections": ["COLL123"],
                "tags": ["chemistry"],
                "date": "2026-03-10",
                "DOI": "10.1000/duplicate",
                "url": "https://example.org/duplicate",
            },
        }
        docs = [
            ({
                "doc_id": "meta:PARENT1",
                "parent_key": "PARENT1",
                "attachment_key": "",
                "doc_kind": "metadata",
                "chunk_index": 0,
                "char_start": 0,
                "char_end": 30,
                "token_count": 4,
                "text": "query match duplicate paper",
                "text_hash": "hash-1",
            }, 9.0),
            ({
                "doc_id": "meta:PARENT2",
                "parent_key": "PARENT2",
                "attachment_key": "",
                "doc_kind": "metadata",
                "chunk_index": 0,
                "char_start": 0,
                "char_end": 30,
                "token_count": 4,
                "text": "query match duplicate paper",
                "text_hash": "hash-2",
            }, 8.0),
        ]
        self._install_search_state(docs, parents=parents)

        result = json.loads(db.search("query"))

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["returned_count"], 1)
        self.assertEqual([row["key"] for row in result["items"]], ["PARENT2"])
        self.assertEqual(
            result["items"][0]["collections"],
            [{"key": "COLL123", "name": "Valid Collection"}],
        )

    def test_search_keeps_same_title_items_distinct_without_doi_or_url(self):
        parents = {
            "PARENT1": {
                "key": "PARENT1",
                "dateModified": "2026-03-10 10:00:00",
                "itemType": "preprint",
                "title": "Shared Title",
                "abstract": "First abstract.",
                "creators": ["Jane Example"],
                "collections": [],
                "tags": [],
                "date": "2026-03-10",
                "DOI": "",
                "url": "",
            },
            "PARENT2": {
                "key": "PARENT2",
                "dateModified": "2026-03-11 10:00:00",
                "itemType": "preprint",
                "title": "Shared Title",
                "abstract": "Second abstract.",
                "creators": ["John Example"],
                "collections": ["COLL123"],
                "tags": [],
                "date": "2026-03-11",
                "DOI": "",
                "url": "",
            },
        }
        docs = [
            ({
                "doc_id": "meta:PARENT1",
                "parent_key": "PARENT1",
                "attachment_key": "",
                "doc_kind": "metadata",
                "chunk_index": 0,
                "char_start": 0,
                "char_end": 30,
                "token_count": 4,
                "text": "query match first",
                "text_hash": "hash-1",
            }, 9.0),
            ({
                "doc_id": "meta:PARENT2",
                "parent_key": "PARENT2",
                "attachment_key": "",
                "doc_kind": "metadata",
                "chunk_index": 0,
                "char_start": 0,
                "char_end": 30,
                "token_count": 4,
                "text": "query match second",
                "text_hash": "hash-2",
            }, 8.0),
        ]
        self._install_search_state(docs, parents=parents)

        result = json.loads(db.search("query"))

        self.assertEqual(result["total"], 2)
        self.assertEqual(result["returned_count"], 2)
        self.assertCountEqual(
            [row["key"] for row in result["items"]],
            ["PARENT1", "PARENT2"],
        )

    def test_collection_and_item_type_filters_apply_at_parent_level(self):
        parents = {
            "PARENT1": {
                "key": "PARENT1",
                "dateModified": "2026-03-10 10:00:00",
                "itemType": "preprint",
                "title": "First Paper",
                "abstract": "First abstract.",
                "creators": ["Jane Example"],
                "collections": ["KEEP"],
                "tags": [],
                "date": "2026-03-10",
                "DOI": "",
                "url": "",
            },
            "PARENT2": {
                "key": "PARENT2",
                "dateModified": "2026-03-11 10:00:00",
                "itemType": "journalArticle",
                "title": "Second Paper",
                "abstract": "Second abstract.",
                "creators": ["John Example"],
                "collections": ["DROP"],
                "tags": [],
                "date": "2026-03-11",
                "DOI": "",
                "url": "",
            },
        }
        docs = [
            ({
                "doc_id": "meta:PARENT2",
                "parent_key": "PARENT2",
                "attachment_key": "",
                "doc_kind": "metadata",
                "chunk_index": 0,
                "char_start": 0,
                "char_end": 20,
                "token_count": 3,
                "text": "query match second",
                "text_hash": "hash-2",
            }, 8.0),
            ({
                "doc_id": "meta:PARENT1",
                "parent_key": "PARENT1",
                "attachment_key": "",
                "doc_kind": "metadata",
                "chunk_index": 0,
                "char_start": 0,
                "char_end": 20,
                "token_count": 3,
                "text": "query match first",
                "text_hash": "hash-1",
            }, 7.0),
        ]
        self._install_search_state(docs, parents=parents)

        result = json.loads(db.search("query", collection_key="KEEP", item_type="preprint"))

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["key"], "PARENT1")

    def test_search_warns_for_unknown_collection_key_filter(self):
        self._install_search_state([
            ({
                "doc_id": "meta:PARENT1",
                "parent_key": "PARENT1",
                "attachment_key": "",
                "doc_kind": "metadata",
                "chunk_index": 0,
                "char_start": 0,
                "char_end": 30,
                "token_count": 4,
                "text": "query match metadata",
                "text_hash": "hash-1",
            }, 7.0),
        ])

        result = json.loads(db.search("query", collection_key="missing"))

        self.assertEqual(result["items"], [])
        self.assertEqual(result["total"], 0)
        self.assertEqual(
            result["warning"],
            "Collection MISSING was not found in the search index",
        )

    def test_search_warns_for_unknown_item_type_filter(self):
        self._install_search_state([
            ({
                "doc_id": "meta:PARENT1",
                "parent_key": "PARENT1",
                "attachment_key": "",
                "doc_kind": "metadata",
                "chunk_index": 0,
                "char_start": 0,
                "char_end": 30,
                "token_count": 4,
                "text": "query match metadata",
                "text_hash": "hash-1",
            }, 7.0),
        ])

        result = json.loads(db.search("query", item_type="invalidType"))

        self.assertEqual(result["items"], [])
        self.assertEqual(result["total"], 0)
        self.assertEqual(
            result["warning"],
            "Item type 'invalidType' was not found in the search index",
        )

    def test_search_caps_large_requested_limits_and_reports_metadata(self):
        parents = {}
        docs = []
        for index in range(600):
            parent_key = f"PARENT{index + 1}"
            parents[parent_key] = {
                "key": parent_key,
                "dateModified": f"2026-03-{(index % 28) + 1:02d} 10:00:00",
                "itemType": "preprint",
                "title": f"Paper {index + 1}",
                "abstract": f"Abstract {index + 1}.",
                "creators": [f"Author {index + 1}"],
                "collections": ["COLL123"],
                "tags": [],
                "date": f"2026-03-{(index % 28) + 1:02d}",
                "DOI": "",
                "url": "",
            }
            docs.append(
                (
                    {
                        "doc_id": f"meta:{parent_key}",
                        "parent_key": parent_key,
                        "attachment_key": "",
                        "doc_kind": "metadata",
                        "chunk_index": 0,
                        "char_start": 0,
                        "char_end": 40,
                        "token_count": 4,
                        "text": f"query match {index + 1}",
                        "text_hash": f"hash-{index + 1}",
                    },
                    float(1000 - index),
                )
            )
        self._install_search_state(docs, parents=parents)

        with patch(
            "zoty.db._load_docs_by_rowid",
            wraps=db._load_docs_by_rowid,
        ) as load_docs_mock:
            result = json.loads(db.search("query", limit=1000))

        self.assertEqual(result["requested_limit"], 1000)
        self.assertEqual(result["applied_limit"], db._SEARCH_RESULT_LIMIT_CAP)
        self.assertEqual(result["limit_cap"], db._SEARCH_RESULT_LIMIT_CAP)
        self.assertTrue(result["limit_capped"])
        self.assertEqual(result["total"], 600)
        self.assertEqual(result["returned_count"], db._SEARCH_RESULT_LIMIT_CAP)
        self.assertEqual(len(result["items"]), db._SEARCH_RESULT_LIMIT_CAP)
        self.assertEqual(
            len(load_docs_mock.call_args.args[1]),
            db._SEARCH_RESULT_LIMIT_CAP,
        )

    def test_search_preserves_zero_requested_limit_while_reporting_total_matches(self):
        self._install_search_state([
            ({
                "doc_id": "meta:PARENT1",
                "parent_key": "PARENT1",
                "attachment_key": "",
                "doc_kind": "metadata",
                "chunk_index": 0,
                "char_start": 0,
                "char_end": 20,
                "token_count": 3,
                "text": "query match first",
                "text_hash": "hash-1",
            }, 7.0),
        ])

        result = json.loads(db.search("query", limit=0))

        self.assertEqual(result["items"], [])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["returned_count"], 0)
        self.assertEqual(result["requested_limit"], 0)
        self.assertEqual(result["applied_limit"], 0)
        self.assertEqual(result["limit_cap"], db._SEARCH_RESULT_LIMIT_CAP)
        self.assertFalse(result["limit_capped"])

    def test_search_preserves_zero_requested_limit_while_returning_empty_query_warning(self):
        self._install_search_state([
            ({
                "doc_id": "meta:PARENT1",
                "parent_key": "PARENT1",
                "attachment_key": "",
                "doc_kind": "metadata",
                "chunk_index": 0,
                "char_start": 0,
                "char_end": 20,
                "token_count": 3,
                "text": "query match first",
                "text_hash": "hash-1",
            }, 7.0),
        ])

        result = json.loads(db.search("the and or", limit=0))

        self.assertEqual(result["items"], [])
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["returned_count"], 0)
        self.assertEqual(result["requested_limit"], 0)
        self.assertEqual(result["applied_limit"], 0)
        self.assertEqual(result["warning"], db._EMPTY_QUERY_WARNING)

    def test_search_returns_warning_when_query_has_no_searchable_terms(self):
        self._install_search_state([
            ({
                "doc_id": "meta:PARENT1",
                "parent_key": "PARENT1",
                "attachment_key": "",
                "doc_kind": "metadata",
                "chunk_index": 0,
                "char_start": 0,
                "char_end": 20,
                "token_count": 3,
                "text": "query match first",
                "text_hash": "hash-1",
            }, 7.0),
        ])

        result = json.loads(db.search("the and or", limit=3))

        self.assertEqual(result["items"], [])
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["returned_count"], 0)
        self.assertEqual(result["warning"], db._EMPTY_QUERY_WARNING)

    def test_search_does_not_return_warning_for_valid_zero_match_query(self):
        self._install_search_state([
            ({
                "doc_id": "meta:PARENT1",
                "parent_key": "PARENT1",
                "attachment_key": "",
                "doc_kind": "metadata",
                "chunk_index": 0,
                "char_start": 0,
                "char_end": 20,
                "token_count": 3,
                "text": "query match first",
                "text_hash": "hash-1",
            }, 0.0),
        ])

        result = json.loads(db.search("quantum topology", limit=3))

        self.assertEqual(result["items"], [])
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["returned_count"], 0)
        self.assertNotIn("warning", result)

    def test_search_within_item_returns_multiple_ranked_matches_for_one_parent(self):
        parents = {
            "PARENT1": {
                "key": "PARENT1",
                "dateModified": "2026-03-10 10:00:00",
                "itemType": "preprint",
                "title": "First Paper",
                "abstract": "First abstract with metadata match.",
                "creators": ["Jane Example"],
                "collections": ["COLL123"],
                "tags": [],
                "date": "2026-03-10",
                "DOI": "",
                "url": "",
            },
            "PARENT2": {
                "key": "PARENT2",
                "dateModified": "2026-03-11 10:00:00",
                "itemType": "preprint",
                "title": "Second Paper",
                "abstract": "Second abstract.",
                "creators": ["John Example"],
                "collections": ["COLL123"],
                "tags": [],
                "date": "2026-03-11",
                "DOI": "",
                "url": "",
            },
        }
        docs = [
            ({
                "doc_id": "chunk:ATTACH1:0",
                "parent_key": "PARENT1",
                "attachment_key": "ATTACH1",
                "doc_kind": "attachment_chunk",
                "chunk_index": 0,
                "char_start": 0,
                "char_end": 25,
                "token_count": 4,
                "text": "query query query match strongest chunk",
                "text_hash": "hash-1",
            }, 9.0),
            ({
                "doc_id": "chunk:ATTACH2:0",
                "parent_key": "PARENT1",
                "attachment_key": "ATTACH2",
                "doc_kind": "attachment_chunk",
                "chunk_index": 1,
                "char_start": 30,
                "char_end": 60,
                "token_count": 4,
                "text": "query query match second chunk",
                "text_hash": "hash-2",
            }, 8.5),
            ({
                "doc_id": "meta:PARENT1",
                "parent_key": "PARENT1",
                "attachment_key": "",
                "doc_kind": "metadata",
                "chunk_index": 0,
                "char_start": 0,
                "char_end": 30,
                "token_count": 4,
                "text": "query match metadata",
                "text_hash": "hash-3",
            }, 7.5),
            ({
                "doc_id": "meta:PARENT2",
                "parent_key": "PARENT2",
                "attachment_key": "",
                "doc_kind": "metadata",
                "chunk_index": 0,
                "char_start": 0,
                "char_end": 20,
                "token_count": 3,
                "text": "query match outside",
                "text_hash": "hash-4",
            }, 10.0),
        ]
        self._install_search_state(docs, parents=parents)

        result = json.loads(db.search_within_item("parent1", "query", limit=3))

        self.assertEqual(result["key"], "PARENT1")
        self.assertEqual(
            result["item"],
            {"key": "PARENT1", "title": "First Paper", "itemType": "preprint"},
        )
        self.assertNotIn("abstract", result["item"])
        self.assertNotIn("attachments", result["item"])
        self.assertEqual(result["requested_limit"], 3)
        self.assertEqual(result["applied_limit"], 3)
        self.assertEqual(result["limit_cap"], db._SEARCH_WITHIN_RESULT_LIMIT_CAP)
        self.assertFalse(result["limit_capped"])
        self.assertEqual(result["total"], 3)
        self.assertEqual(
            [row["match_type"] for row in result["matches"]],
            ["attachment_chunk", "attachment_chunk", "metadata"],
        )
        self.assertGreater(
            result["matches"][0]["score"],
            result["matches"][1]["score"],
        )
        self.assertGreater(
            result["matches"][1]["score"],
            result["matches"][2]["score"],
        )
        self.assertNotIn("key", result["matches"][0])
        self.assertNotIn("title", result["matches"][0])
        self.assertNotIn("itemType", result["matches"][0])
        self.assertEqual(result["matches"][0]["attachment_key"], "ATTACH1")
        self.assertEqual(result["matches"][1]["attachment_key"], "ATTACH2")
        self.assertNotIn("attachment_key", result["matches"][2])
        self.assertNotIn("attachment_filepath", result["matches"][0])

    def test_search_within_item_returns_no_matches_when_limit_is_zero(self):
        self._install_search_state([
            ({
                "doc_id": "meta:PARENT1",
                "parent_key": "PARENT1",
                "attachment_key": "",
                "doc_kind": "metadata",
                "chunk_index": 0,
                "char_start": 0,
                "char_end": 20,
                "token_count": 3,
                "text": "query match first",
                "text_hash": "hash-1",
            }, 7.0),
        ])

        result = json.loads(db.search_within_item("parent1", "query", limit=0))

        self.assertEqual(
            result["item"],
            {"key": "PARENT1", "title": "Example Paper", "itemType": "preprint"},
        )
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["total"], 0)

    def test_search_within_item_zero_limit_still_returns_empty_query_warning(self):
        self._install_search_state([
            ({
                "doc_id": "meta:PARENT1",
                "parent_key": "PARENT1",
                "attachment_key": "",
                "doc_kind": "metadata",
                "chunk_index": 0,
                "char_start": 0,
                "char_end": 20,
                "token_count": 3,
                "text": "query match first",
                "text_hash": "hash-1",
            }, 7.0),
        ])

        result = json.loads(db.search_within_item("parent1", "the and", limit=0))

        self.assertEqual(
            result["item"],
            {"key": "PARENT1", "title": "Example Paper", "itemType": "preprint"},
        )
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["warning"], db._EMPTY_QUERY_WARNING)

    def test_search_within_item_returns_lean_item_summary_when_query_has_no_terms(self):
        self._install_search_state([
            ({
                "doc_id": "meta:PARENT1",
                "parent_key": "PARENT1",
                "attachment_key": "",
                "doc_kind": "metadata",
                "chunk_index": 0,
                "char_start": 0,
                "char_end": 20,
                "token_count": 3,
                "text": "query match first",
                "text_hash": "hash-1",
            }, 7.0),
        ])

        result = json.loads(db.search_within_item("parent1", "the and", limit=3))

        self.assertEqual(
            result["item"],
            {"key": "PARENT1", "title": "Example Paper", "itemType": "preprint"},
        )
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["requested_limit"], 3)
        self.assertEqual(result["applied_limit"], 3)
        self.assertEqual(result["limit_cap"], db._SEARCH_WITHIN_RESULT_LIMIT_CAP)
        self.assertFalse(result["limit_capped"])
        self.assertEqual(result["warning"], db._EMPTY_QUERY_WARNING)

    def test_search_within_item_does_not_return_warning_for_valid_zero_match_query(self):
        self._install_search_state([
            ({
                "doc_id": "meta:PARENT1",
                "parent_key": "PARENT1",
                "attachment_key": "",
                "doc_kind": "metadata",
                "chunk_index": 0,
                "char_start": 0,
                "char_end": 20,
                "token_count": 3,
                "text": "query match first",
                "text_hash": "hash-1",
            }, 0.0),
        ])

        result = json.loads(db.search_within_item("parent1", "quantum topology", limit=3))

        self.assertEqual(
            result["item"],
            {"key": "PARENT1", "title": "Example Paper", "itemType": "preprint"},
        )
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["requested_limit"], 3)
        self.assertEqual(result["applied_limit"], 3)
        self.assertEqual(result["limit_cap"], db._SEARCH_WITHIN_RESULT_LIMIT_CAP)
        self.assertFalse(result["limit_capped"])
        self.assertNotIn("warning", result)

    def test_search_within_item_returns_error_for_unknown_item(self):
        self._install_search_state([
            ({
                "doc_id": "meta:PARENT1",
                "parent_key": "PARENT1",
                "attachment_key": "",
                "doc_kind": "metadata",
                "chunk_index": 0,
                "char_start": 0,
                "char_end": 20,
                "token_count": 3,
                "text": "query match first",
                "text_hash": "hash-1",
            }, 7.0),
        ])

        result = json.loads(db.search_within_item("missing", "query"))

        self.assertEqual(result["key"], "MISSING")
        self.assertEqual(result["item"], {"key": "MISSING", "title": "", "itemType": ""})
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["requested_limit"], 5)
        self.assertEqual(result["applied_limit"], 5)
        self.assertEqual(result["limit_cap"], db._SEARCH_WITHIN_RESULT_LIMIT_CAP)
        self.assertFalse(result["limit_capped"])
        self.assertIn("was not found", result["error"])

    def test_search_within_item_supports_multi_item_queries(self):
        parents = {
            "PARENT1": {
                "key": "PARENT1",
                "dateModified": "2026-03-10 10:00:00",
                "itemType": "preprint",
                "title": "First Paper",
                "abstract": "First abstract.",
                "creators": ["Jane Example"],
                "collections": ["COLL123"],
                "tags": [],
                "date": "2026-03-10",
                "DOI": "",
                "url": "",
            },
            "PARENT2": {
                "key": "PARENT2",
                "dateModified": "2026-03-11 10:00:00",
                "itemType": "preprint",
                "title": "Second Paper",
                "abstract": "Second abstract.",
                "creators": ["John Example"],
                "collections": ["COLL123"],
                "tags": [],
                "date": "2026-03-11",
                "DOI": "",
                "url": "",
            },
            "PARENT3": {
                "key": "PARENT3",
                "dateModified": "2026-03-12 10:00:00",
                "itemType": "preprint",
                "title": "Third Paper",
                "abstract": "Third abstract.",
                "creators": ["Alex Example"],
                "collections": ["COLL123"],
                "tags": [],
                "date": "2026-03-12",
                "DOI": "",
                "url": "",
            },
        }
        docs = [
            ({
                "doc_id": "meta:PARENT2",
                "parent_key": "PARENT2",
                "attachment_key": "",
                "doc_kind": "metadata",
                "chunk_index": 0,
                "char_start": 0,
                "char_end": 20,
                "token_count": 3,
                "text": "query query query match second",
                "text_hash": "hash-2",
            }, 8.0),
            ({
                "doc_id": "chunk:ATTACH1:0",
                "parent_key": "PARENT1",
                "attachment_key": "ATTACH1",
                "doc_kind": "attachment_chunk",
                "chunk_index": 0,
                "char_start": 0,
                "char_end": 20,
                "token_count": 3,
                "text": "query query match first attachment",
                "text_hash": "hash-3",
            }, 7.5),
            ({
                "doc_id": "meta:PARENT1",
                "parent_key": "PARENT1",
                "attachment_key": "",
                "doc_kind": "metadata",
                "chunk_index": 0,
                "char_start": 0,
                "char_end": 20,
                "token_count": 3,
                "text": "query match first",
                "text_hash": "hash-1",
            }, 7.0),
        ]
        self._install_search_state(docs, parents=parents)

        result = json.loads(
            db.search_within_item(
                item_key="",
                item_keys=["parent1", "parent2", "parent3"],
                query="query",
                limit=3,
            )
        )

        self.assertEqual(result["item_keys"], ["PARENT1", "PARENT2", "PARENT3"])
        self.assertEqual(
            [item["returned_match_count"] for item in result["items"]],
            [2, 1, 0],
        )
        self.assertEqual(
            [item["top_match_type"] for item in result["items"]],
            ["attachment_chunk", "metadata", None],
        )
        self.assertGreater(result["items"][1]["top_score"], result["items"][0]["top_score"])
        self.assertIsNone(result["items"][2]["top_score"])
        self.assertEqual(result["requested_limit"], 3)
        self.assertEqual(result["applied_limit"], 3)
        self.assertEqual(result["limit_cap"], db._SEARCH_WITHIN_RESULT_LIMIT_CAP)
        self.assertFalse(result["limit_capped"])
        self.assertEqual(result["total"], 3)
        self.assertEqual([row["key"] for row in result["matches"]], ["PARENT2", "PARENT1", "PARENT1"])
        self.assertNotIn("title", result["matches"][0])
        self.assertNotIn("title", result["matches"][1])
        self.assertNotIn("itemType", result["matches"][0])

    def test_search_within_item_caps_large_requested_limits_and_reports_metadata(self):
        docs = [
            ({
                "doc_id": f"meta:PARENT1:{index}",
                "parent_key": "PARENT1",
                "attachment_key": "",
                "doc_kind": "metadata",
                "chunk_index": index,
                "char_start": index * 10,
                "char_end": (index * 10) + 20,
                "token_count": 3,
                "text": f"query match {index}",
                "text_hash": f"hash-{index}",
            }, 1000.0 - index)
            for index in range(600)
        ]
        self._install_search_state(docs)

        with patch("zoty.db._get_item_attachments_by_parent", return_value={"PARENT1": []}):
            result = json.loads(db.search_within_item("parent1", "query", limit=999))

        self.assertEqual(result["requested_limit"], 999)
        self.assertEqual(result["applied_limit"], db._SEARCH_WITHIN_RESULT_LIMIT_CAP)
        self.assertEqual(result["limit_cap"], db._SEARCH_WITHIN_RESULT_LIMIT_CAP)
        self.assertTrue(result["limit_capped"])
        self.assertEqual(result["total"], db._SEARCH_WITHIN_RESULT_LIMIT_CAP)
        self.assertEqual(len(result["matches"]), db._SEARCH_WITHIN_RESULT_LIMIT_CAP)


class FtsLifecycleTests(DbTestCase):
    def _make_parent(self, parent_key="PARENT1"):
        return db._ParentRecord(
            parent_key=parent_key,
            parent_item_id=1,
            item_version=1,
            date_modified="2026-03-10 10:00:00",
            item_type="preprint",
            title="Snapshot Paper",
            abstract="Snapshot abstract mentions alpha beta.",
            creators=["Jane Example"],
            collections=["COLL123"],
            tags=["chemistry"],
            date="2026-03-10",
            doi="10.1000/snapshot",
            url="https://example.org/snapshot",
            metadata_hash="parent-hash",
        )

    def _make_attachment(self, *, signature="sig-1", attachment_key="ATTACH1"):
        return db._AttachmentRecord(
            attachment_key=attachment_key,
            attachment_item_id=2,
            parent_key="PARENT1",
            item_version=1,
            content_type="application/pdf",
            link_mode=0,
            source_path=str(Path(self.temp_dir.name) / "paper.pdf"),
            cache_path=str(Path(self.temp_dir.name) / ".zotero-ft-cache"),
            storage_mod_time=1,
            storage_hash="",
            last_processed_mod_time=1,
            fulltext_version=1,
            indexed_pages=10,
            total_pages=10,
            indexed_chars=None,
            total_chars=None,
            source_signature=signature,
        )

    def _make_doc(
        self,
        text: str,
        *,
        doc_id: str = "chunk:ATTACH1:0",
        attachment_key: str = "ATTACH1",
    ):
        return {
            "doc_id": doc_id,
            "parent_key": "PARENT1",
            "attachment_key": attachment_key,
            "doc_kind": "metadata" if not attachment_key else "attachment_chunk",
            "chunk_index": 0,
            "char_start": 0,
            "char_end": len(text),
            "token_count": len(text.split()),
            "text": text,
            "text_hash": f"hash-{text}",
        }

    def _create_minimal_source_library(self):
        with closing(sqlite3.connect(db._ZOTERO_DB)) as conn:
            conn.executescript(
                """
                CREATE TABLE itemTypesCombined (
                    itemTypeID INTEGER PRIMARY KEY,
                    typeName TEXT NOT NULL
                );
                CREATE TABLE items (
                    itemID INTEGER PRIMARY KEY,
                    key TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    dateModified TEXT NOT NULL,
                    dateAdded TEXT NOT NULL,
                    itemTypeID INTEGER NOT NULL,
                    libraryID INTEGER
                );
                CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY);
                CREATE TABLE itemAttachments (
                    itemID INTEGER PRIMARY KEY,
                    parentItemID INTEGER,
                    contentType TEXT,
                    linkMode INTEGER,
                    path TEXT,
                    storageModTime INTEGER,
                    storageHash TEXT,
                    lastProcessedModificationTime INTEGER
                );
                CREATE TABLE fulltextItems (
                    itemID INTEGER PRIMARY KEY,
                    version INTEGER,
                    indexedPages INTEGER,
                    totalPages INTEGER,
                    indexedChars INTEGER,
                    totalChars INTEGER
                );
                CREATE TABLE fields (
                    fieldID INTEGER PRIMARY KEY,
                    fieldName TEXT NOT NULL
                );
                CREATE TABLE itemDataValues (
                    valueID INTEGER PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE itemData (
                    itemID INTEGER NOT NULL,
                    fieldID INTEGER NOT NULL,
                    valueID INTEGER NOT NULL
                );
                CREATE TABLE creators (
                    creatorID INTEGER PRIMARY KEY,
                    firstName TEXT,
                    lastName TEXT,
                    fieldMode INTEGER
                );
                CREATE TABLE itemCreators (
                    itemID INTEGER NOT NULL,
                    creatorID INTEGER NOT NULL,
                    orderIndex INTEGER NOT NULL
                );
                CREATE TABLE collectionItems (
                    itemID INTEGER NOT NULL,
                    collectionID INTEGER NOT NULL,
                    orderIndex INTEGER NOT NULL
                );
                CREATE TABLE tags (
                    tagID INTEGER PRIMARY KEY,
                    name TEXT NOT NULL
                );
                CREATE TABLE itemTags (
                    itemID INTEGER NOT NULL,
                    tagID INTEGER NOT NULL
                );

                INSERT INTO itemTypesCombined(itemTypeID, typeName) VALUES (1, 'preprint');
                INSERT INTO items(
                    itemID, key, version, dateModified, dateAdded, itemTypeID, libraryID
                ) VALUES (
                    1, 'PARENT1', 1, '2026-03-10 10:00:00', '2026-03-10 09:00:00', 1, 1
                );
                INSERT INTO fields(fieldID, fieldName) VALUES
                    (1, 'title'),
                    (2, 'abstractNote'),
                    (3, 'date'),
                    (4, 'DOI'),
                    (5, 'url');
                INSERT INTO itemDataValues(valueID, value) VALUES
                    (1, 'Snapshot Paper'),
                    (2, 'Snapshot abstract mentions alpha beta.'),
                    (3, '2026-03-10'),
                    (4, '10.1000/snapshot'),
                    (5, 'https://example.org/snapshot');
                INSERT INTO itemData(itemID, fieldID, valueID) VALUES
                    (1, 1, 1),
                    (1, 2, 2),
                    (1, 3, 3),
                    (1, 4, 4),
                    (1, 5, 5);
                INSERT INTO creators(creatorID, firstName, lastName, fieldMode)
                    VALUES (1, 'Jane', 'Example', 0);
                INSERT INTO itemCreators(itemID, creatorID, orderIndex) VALUES (1, 1, 0);
                INSERT INTO collectionItems(itemID, collectionID, orderIndex) VALUES (1, 1, 0);
                INSERT INTO tags(tagID, name) VALUES (1, 'chemistry');
                INSERT INTO itemTags(itemID, tagID) VALUES (1, 1);
                """
            )
            conn.commit()

    def test_connect_manifest_uses_read_only_uri_for_read_connections(self):
        mock_conn = Mock()

        with patch("zoty.db.sqlite3.connect", return_value=mock_conn) as connect_mock:
            conn = db._connect_manifest()

        self.assertIs(conn, mock_conn)
        connect_mock.assert_called_once_with(
            f"file:{db._manifest_db_path()}?mode=ro",
            uri=True,
        )
        self.assertEqual(mock_conn.row_factory, sqlite3.Row)

    def test_fts_initialization_reports_missing_runtime_support(self):
        conn = Mock()
        conn.execute.side_effect = sqlite3.OperationalError("no such module: fts5")

        with self.assertRaisesRegex(RuntimeError, "FTS5 support is required"):
            db._create_fts_objects(conn)

    def test_cold_migration_populates_existing_docs_in_batches(self):
        parent = self._make_parent()
        with closing(db._connect_manifest(writable=True)) as conn:
            db._initialize_manifest(conn)
            db._upsert_parent(conn, parent)
            conn.executescript(
                """
                DROP TRIGGER docs_fts_after_insert;
                DROP TRIGGER docs_fts_after_delete;
                DROP TRIGGER docs_fts_after_update;
                DROP TABLE docs_fts;
                DELETE FROM meta
                WHERE key IN ('fts_schema_version', 'fts_index_status');
                """
            )
            for index in range(5):
                db._insert_doc(
                    conn,
                    self._make_doc(
                        f"migrationterm document {index}",
                        doc_id=f"meta:PARENT1:{index}",
                        attachment_key="",
                    ),
                )
            db._set_meta(conn, "last_source_fingerprint", "fingerprint-1")
            conn.commit()

            with patch.object(db, "_FTS_MIGRATION_BATCH_SIZE", 2):
                migrated_count = db._initialize_manifest(conn)

            self.assertEqual(migrated_count, 5)
            self.assertEqual(db._get_meta(conn, "fts_index_status"), "ready")
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM docs_fts WHERE docs_fts MATCH ?",
                    (db._fts_match_expression("migrationterm"),),
                ).fetchone()[0],
                5,
            )

    def test_load_search_state_rejects_unpublished_empty_index(self):
        with closing(db._connect_manifest(writable=True)) as conn:
            db._initialize_manifest(conn)
            conn.commit()

        self.assertIsNone(db._load_search_state())

    def test_prepare_search_index_loads_existing_fts_and_skips_refresh(self):
        doc = self._make_doc("alpha beta", doc_id="meta:PARENT1", attachment_key="")
        self._install_search_state([(doc, 1.0)])
        db._search_state = None

        with (
            patch("zoty.db._compute_source_fingerprint", return_value="fingerprint-1"),
            patch("zoty.db._start_refresh_thread") as refresh_mock,
        ):
            db.prepare_search_index()

        self.assertIsNotNone(db._search_state)
        self.assertEqual(db._search_state.document_count, 1)
        refresh_mock.assert_not_called()

    def test_prepare_search_index_requests_refresh_when_fingerprint_changes(self):
        doc = self._make_doc("alpha beta", doc_id="meta:PARENT1", attachment_key="")
        self._install_search_state([(doc, 1.0)])
        db._search_state = None

        with (
            patch("zoty.db._compute_source_fingerprint", return_value="fingerprint-2"),
            patch("zoty.db._start_refresh_thread") as refresh_mock,
        ):
            db.prepare_search_index()

        refresh_mock.assert_called_once_with(force=False)

    def test_refresh_worker_builds_fts_and_restart_preserves_results(self):
        self._create_minimal_source_library()

        return_code = db._run_refresh_worker_process()

        self.assertEqual(return_code, 0)
        state = db._load_search_state()
        self.assertIsNotNone(state)
        self.assertEqual(state.document_count, 1)
        db._install_state(state)
        initial_search = json.loads(db.search("alpha beta"))
        initial_within = json.loads(
            db.search_within_item("", "alpha beta", item_keys=["PARENT1"]),
        )
        concurrent_results: list[dict | None] = [None] * 8

        def run_search(index):
            concurrent_results[index] = json.loads(db.search("alpha beta"))

        threads = [
            threading.Thread(target=run_search, args=(index,))
            for index in range(len(concurrent_results))
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertTrue(all(result == initial_search for result in concurrent_results))

        db._search_state = None
        with (
            patch("zoty.db._compute_source_fingerprint", return_value=state.source_fingerprint),
            patch("zoty.db._start_refresh_thread") as refresh_mock,
        ):
            db.prepare_search_index()

        restarted_search = json.loads(db.search("alpha beta"))
        restarted_within = json.loads(
            db.search_within_item("", "alpha beta", item_keys=["PARENT1"]),
        )
        self.assertEqual(initial_search, restarted_search)
        self.assertEqual(initial_within, restarted_within)
        self.assertEqual(restarted_search["items"][0]["key"], "PARENT1")
        self.assertEqual(restarted_within["matches"][0]["match_type"], "metadata")
        refresh_mock.assert_not_called()

    def test_build_index_background_keeps_old_state_available_until_worker_finishes(self):
        doc = self._make_doc("alpha beta", doc_id="meta:PARENT1", attachment_key="")
        self._install_search_state([(doc, 5.0)])
        old_state = db._search_state
        new_state = db._SearchState(
            source_fingerprint="fingerprint-2",
            document_count=1,
        )
        worker_started = threading.Event()
        release_worker = threading.Event()

        def run_worker():
            worker_started.set()
            release_worker.wait(timeout=2)
            return 0

        db._refresh_in_progress = True
        with (
            patch("zoty.db._run_refresh_worker_process", side_effect=run_worker),
            patch("zoty.db._load_search_state", return_value=new_state),
            patch("zoty.db._get_item_attachment_counts", return_value={"PARENT1": 0}),
        ):
            refresh_thread = threading.Thread(target=db.build_index_background)
            refresh_thread.start()
            self.assertTrue(worker_started.wait(timeout=1))

            result = json.loads(db.search("alpha"))
            self.assertIs(db._search_state, old_state)
            self.assertEqual(result["items"][0]["key"], "PARENT1")

            release_worker.set()
            refresh_thread.join(timeout=2)

        self.assertFalse(refresh_thread.is_alive())
        self.assertIs(db._search_state, new_state)
        self.assertFalse(db._refresh_in_progress)

    def test_build_index_background_keeps_old_state_when_worker_fails(self):
        self._install_search_state([])
        old_state = db._search_state
        db._refresh_in_progress = True

        with (
            patch("zoty.db._run_refresh_worker_process", return_value=-9),
            patch("zoty.db._load_search_state", return_value=old_state),
            patch("zoty.db._record_refresh_failure") as failure_mock,
            patch("sys.stderr", new_callable=io.StringIO),
        ):
            db.build_index_background()

        self.assertIs(db._search_state, old_state)
        self.assertFalse(db._refresh_in_progress)
        failure_mock.assert_called_once_with("index refresh worker exited with status -9")

    def test_build_index_background_preserves_worker_failure_details(self):
        self._install_search_state([])
        old_state = db._search_state
        db._refresh_in_progress = True

        with closing(db._connect_manifest(writable=True)) as conn:
            db._initialize_manifest(conn)
            db._set_meta(conn, "last_refresh_status", "failed: worker detail")
            conn.commit()

        with (
            patch("zoty.db._run_refresh_worker_process", return_value=1),
            patch("zoty.db._load_search_state", return_value=old_state),
            patch("zoty.db._record_refresh_failure") as failure_mock,
            patch("sys.stderr", new_callable=io.StringIO),
        ):
            db.build_index_background()

        self.assertIs(db._search_state, old_state)
        self.assertFalse(db._refresh_in_progress)
        failure_mock.assert_not_called()

    def test_incremental_refresh_handles_noop_edit_attachment_delete_and_parent_delete(self):
        parent = self._make_parent()
        attachment = self._make_attachment(signature="sig-1")
        ingested = db._AttachmentIngestResult(
            extraction_state="indexed",
            error_text="",
            content_hash="content-hash",
            content_chars=100,
            token_count=20,
            chunk_count=1,
            docs=[
                {
                    "doc_id": "chunk:ATTACH1:0",
                    "parent_key": "PARENT1",
                    "attachment_key": "ATTACH1",
                    "doc_kind": "attachment_chunk",
                    "chunk_index": 0,
                    "char_start": 0,
                    "char_end": 50,
                    "token_count": 20,
                    "text": "legacytoken attachment text",
                    "text_hash": "hash-doc",
                }
            ],
        )
        changed_ingest = db._AttachmentIngestResult(
            extraction_state="indexed",
            error_text="",
            content_hash="changed-content-hash",
            content_chars=120,
            token_count=3,
            chunk_count=1,
            docs=[self._make_doc("freshterm attachment text")],
        )

        with closing(db._connect_manifest(writable=True)) as conn:
            db._initialize_manifest(conn)
            with patch("zoty.db._ingest_attachment", return_value=ingested) as ingest_mock:
                changed_count = db._refresh_docs_manifest(
                    conn,
                    {"PARENT1": parent},
                    {"ATTACH1": attachment},
                )
                conn.commit()
                self.assertEqual(ingest_mock.call_count, 1)
                self.assertEqual(changed_count, 2)

            with patch("zoty.db._ingest_attachment", return_value=ingested) as ingest_mock:
                changed_count = db._refresh_docs_manifest(
                    conn,
                    {"PARENT1": parent},
                    {"ATTACH1": attachment},
                )
                conn.commit()
                self.assertEqual(ingest_mock.call_count, 0)
                self.assertEqual(changed_count, 0)

            changed_attachment = self._make_attachment(signature="sig-2")
            with patch("zoty.db._ingest_attachment", return_value=changed_ingest) as ingest_mock:
                changed_count = db._refresh_docs_manifest(
                    conn,
                    {"PARENT1": parent},
                    {"ATTACH1": changed_attachment},
                )
                conn.commit()
                self.assertEqual(ingest_mock.call_count, 1)
                self.assertEqual(changed_count, 1)
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM docs_fts WHERE docs_fts MATCH 'legacytoken'"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM docs_fts WHERE docs_fts MATCH 'freshterm'"
                    ).fetchone()[0],
                    1,
                )

            changed_count = db._refresh_docs_manifest(
                conn,
                {"PARENT1": parent},
                {},
            )
            conn.commit()
            self.assertEqual(changed_count, 1)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM docs_fts WHERE docs_fts MATCH 'freshterm'"
                ).fetchone()[0],
                0,
            )

            changed_count = db._refresh_docs_manifest(conn, {}, {})
            conn.commit()
            self.assertEqual(changed_count, 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0], 0)

    def test_search_keeps_a_read_snapshot_during_incremental_commit(self):
        doc = self._make_doc("alpha original", doc_id="meta:PARENT1", attachment_key="")
        self._install_search_state([(doc, 1.0)])
        read_started = threading.Event()
        release_read = threading.Event()
        result_holder: list[dict | None] = [None]
        real_load_parent_state = db._load_parent_state

        def pause_after_parent_read(conn):
            parents = real_load_parent_state(conn)
            read_started.set()
            release_read.wait(timeout=2)
            return parents

        def run_search():
            result_holder[0] = json.loads(db.search("alpha"))

        with (
            patch("zoty.db._load_parent_state", side_effect=pause_after_parent_read),
            patch("zoty.db._get_item_attachment_counts", return_value={"PARENT1": 0}),
        ):
            search_thread = threading.Thread(target=run_search)
            search_thread.start()
            self.assertTrue(read_started.wait(timeout=1))

            with closing(db._connect_manifest(writable=True)) as conn:
                db._insert_doc(
                    conn,
                    self._make_doc("beta replacement", doc_id="meta:PARENT1", attachment_key=""),
                )
                conn.commit()

            release_read.set()
            search_thread.join(timeout=2)

        self.assertFalse(search_thread.is_alive())
        self.assertEqual(result_holder[0]["total"], 1)
        self.assertEqual(json.loads(db.search("alpha"))["total"], 0)
        self.assertEqual(json.loads(db.search("beta"))["total"], 1)

    def test_query_quoting_handles_punctuation_underscores_and_unicode(self):
        doc = self._make_doc(
            "C++ foo-bar naïve café 中文测试 alpha_beta email@example.com",
            doc_id="meta:PARENT1",
            attachment_key="",
        )
        self._install_search_state([(doc, 1.0)])

        for query in (
            "C++ foo-bar",
            "naïve café",
            "中文测试",
            "alpha_beta",
            'foo:"bar"*) OR NOT',
        ):
            with self.subTest(query=query):
                result = json.loads(db.search(query))
                self.assertNotIn("error", result)
                self.assertEqual(result["total"], 1)

        self.assertEqual(json.loads(db.search("naive cafe"))["total"], 0)


class CitationEntryTests(DbTestCase):
    def test_normalize_item_keys_accepts_single_and_list_inputs(self):
        result = db._normalize_item_keys(
            item_key=" item123 ",
            item_keys=[" item456 ", "", "Item789"],
        )

        self.assertEqual(result, ["ITEM123", "ITEM456", "ITEM789"])

    def test_get_bibtex_and_citation_for_items_returns_single_item_exports(self):
        zot = Mock()

        def item_side_effect(item_key, **kwargs):
            self.assertEqual(kwargs["format"], "json")
            self.assertEqual(kwargs["include"], "bib,citation,bibtex")
            self.assertEqual(kwargs["style"], "apa")
            self.assertEqual(kwargs["locale"], "fr-FR")

            return {
                "citation": [f"<span>{item_key} &amp; cite</span>"],
                "bib": [f"<div>{item_key} <i>reference</i></div>"],
                "bibtex": [
                    (
                        f"@article{{{item_key},\n"
                        "  title={Example},\n"
                        "  author={Author 1 and Author 2 and Author 3 and Author 4 and Author 5 and "
                        "Author 6 and Author 7 and Author 8 and Author 9 and Author 10 and Author 11},\n"
                        f"  file={{PDF:{db._ZOTERO_STORAGE / item_key / 'paper.pdf'}:application/pdf}},\n"
                        "  abstract={Detailed summary with {nested} braces},\n"
                        "  year={2026}\n"
                        "}"
                    )
                ],
            }

        zot.item.side_effect = item_side_effect

        with patch("zoty.db._get_zot", return_value=zot):
            result = json.loads(
                db.get_bibtex_and_citation_for_items(
                    item_key="item123",
                    style="apa",
                    locale="fr-FR",
                )
            )

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["requested"], 1)
        self.assertEqual(result["style"], "apa")
        self.assertEqual(result["locale"], "fr-FR")
        self.assertEqual(
            result["items"],
            [
                {
                    "key": "ITEM123",
                    "citation": "ITEM123 & cite",
                    "bibliography": "ITEM123 reference",
                    "bibtex": (
                        "@article{ITEM123,\n"
                        "  title={Example},\n"
                        "  author={Author 1 and Author 2 and Author 3 and Author 4 and Author 5 and "
                        "Author 6 and Author 7 and Author 8 and Author 9 and Author 10 and others},\n"
                        "  year={2026}\n"
                        "}"
                    ),
                }
            ],
        )

    def test_get_bibtex_and_citation_for_items_returns_batch_shape_for_single_key(self):
        self.assertIn("batch shape under `items`", db.get_bibtex_and_citation_for_items.__doc__)

    def test_get_bibtex_and_citation_for_items_returns_multiple_items_and_partial_errors(self):
        zot = Mock()

        def item_side_effect(item_key, **kwargs):
            if item_key == "BADKEY":
                raise RuntimeError("missing item")

            self.assertEqual(kwargs["format"], "json")
            self.assertEqual(kwargs["include"], "bib,citation,bibtex")
            return {
                "citation": [f"<span>{item_key} cite</span>"],
                "bib": [f"<div>{item_key} ref</div>"],
                "bibtex": [f"@article{{{item_key}}}"],
            }

        zot.item.side_effect = item_side_effect

        with patch("zoty.db._get_zot", return_value=zot):
            result = json.loads(
                db.get_bibtex_and_citation_for_items(
                    item_keys=["good1", "badkey", "good2"],
                )
            )

        self.assertEqual(
            result["items"],
            [
                {
                    "key": "GOOD1",
                    "citation": "GOOD1 cite",
                    "bibliography": "GOOD1 ref",
                    "bibtex": "@article{GOOD1}",
                },
                {
                    "key": "GOOD2",
                    "citation": "GOOD2 cite",
                    "bibliography": "GOOD2 ref",
                    "bibtex": "@article{GOOD2}",
                },
            ],
        )
        self.assertEqual(
            result["errors"],
            [
                {
                    "key": "BADKEY",
                    "error": "Failed to fetch citation entry: missing item",
                }
            ],
        )
        self.assertEqual(result["requested"], 3)
        self.assertEqual(result["total"], 2)

    def test_get_bibtex_and_citation_for_items_deduplicates_combined_item_key_inputs(self):
        zot = Mock()

        def item_side_effect(item_key, **kwargs):
            self.assertEqual(kwargs["format"], "json")
            self.assertEqual(kwargs["include"], "bib,citation,bibtex")
            return {
                "citation": [f"<span>{item_key} cite</span>"],
                "bib": [f"<div>{item_key} ref</div>"],
                "bibtex": [f"@article{{{item_key}}}"],
            }

        zot.item.side_effect = item_side_effect

        with patch("zoty.db._get_zot", return_value=zot):
            result = json.loads(
                db.get_bibtex_and_citation_for_items(
                    item_key="good1",
                    item_keys=[" good1 ", "good2", "GOOD2", "good1"],
                )
            )

        self.assertEqual(result["items"], [
            {
                "key": "GOOD1",
                "citation": "GOOD1 cite",
                "bibliography": "GOOD1 ref",
                "bibtex": "@article{GOOD1}",
            },
            {
                "key": "GOOD2",
                "citation": "GOOD2 cite",
                "bibliography": "GOOD2 ref",
                "bibtex": "@article{GOOD2}",
            },
        ])
        self.assertEqual(result["requested"], 2)
        self.assertEqual(result["total"], 2)
        self.assertEqual(zot.item.call_count, 2)

    def test_get_bibtex_and_citation_for_items_sanitizes_invalid_style_errors(self):
        zot = Mock()
        zot.item.side_effect = FakeHttpError(
            "GET https://www.zotero.org/styles/not-a-style 404 Client Error",
            status_code=404,
            url="https://www.zotero.org/styles/not-a-style",
        )

        with patch("zoty.db._get_zot", return_value=zot):
            result = json.loads(
                db.get_bibtex_and_citation_for_items(
                    item_key="item123",
                    style="not-a-style",
                )
            )

        self.assertEqual(result["error"], "Citation style not-a-style was not found")
        self.assertEqual(
            result["errors"],
            [
                {
                    "key": "ITEM123",
                    "error": "Citation style not-a-style was not found",
                }
            ],
        )
        self.assertNotIn("zotero.org/styles", json.dumps(result))

    def test_get_bibtex_and_citation_for_items_sanitizes_invalid_csl_style_errors(self):
        with patch(
            "zoty.db._fetch_item_exports",
            side_effect=FakeHttpError(
                (
                    "Code: 404 URL: "
                    "http://localhost:23119/api/users/0/items/ITEM123"
                    "?format=json&include=bib,citation,bibtex&style=bad-style&locale=en-US "
                    "Method: GET Response: Invalid CSL style: bad-style"
                ),
                status_code=404,
                url=(
                    "http://localhost:23119/api/users/0/items/ITEM123"
                    "?format=json&include=bib,citation,bibtex&style=bad-style&locale=en-US"
                ),
            ),
        ):
            result = json.loads(
                db.get_bibtex_and_citation_for_items(
                    item_key="item123",
                    style="bad-style",
                    locale="en-US",
                )
            )

        self.assertEqual(result["error"], "Citation style bad-style was not found")
        self.assertEqual(
            result["errors"],
            [
                {
                    "key": "ITEM123",
                    "error": "Citation style bad-style was not found",
                }
            ],
        )
        self.assertNotIn("Item ITEM123 was not found", json.dumps(result))

    def test_get_bibtex_and_citation_for_items_makes_one_export_call_per_item(self):
        zot = Mock()
        zot.item.return_value = {
            "citation": ["<span>cite</span>"],
            "bib": ["<div>ref</div>"],
            "bibtex": ["@article{X}"],
        }

        with patch("zoty.db._get_zot", return_value=zot):
            result = json.loads(
                db.get_bibtex_and_citation_for_items(
                    item_keys=["good1", "good2"],
                    style="apa",
                    locale="en-GB",
                )
            )

        self.assertEqual(result["total"], 2)
        self.assertEqual(zot.item.call_count, 2)
        self.assertCountEqual([args[0] for args, _kwargs in zot.item.call_args_list], ["GOOD1", "GOOD2"])

        for _args, kwargs in zot.item.call_args_list:
            self.assertEqual(kwargs["format"], "json")
            self.assertEqual(kwargs["include"], "bib,citation,bibtex")
            self.assertEqual(kwargs["style"], "apa")
            self.assertEqual(kwargs["locale"], "en-GB")
            self.assertNotIn("content", kwargs)

    def test_get_bibtex_and_citation_for_items_fetches_multiple_exports_concurrently(self):
        barrier = threading.Barrier(2)

        def fetch_side_effect(item_key, *, style, locale):
            self.assertEqual(style, "apa")
            self.assertEqual(locale, "en-GB")
            barrier.wait(timeout=1)
            return {
                "citation": f"<span>{item_key} cite</span>",
                "bibliography": f"<div>{item_key} ref</div>",
                "bibtex": f"@article{{{item_key},\n  abstract={{A long abstract}}\n}}",
            }

        with patch("zoty.db._fetch_item_exports", side_effect=fetch_side_effect):
            result = json.loads(
                db.get_bibtex_and_citation_for_items(
                    item_keys=["good1", "good2"],
                    style="apa",
                    locale="en-GB",
                )
            )

        self.assertEqual(result["total"], 2)
        self.assertNotIn("errors", result)
        for item in result["items"]:
            self.assertNotIn("abstract", item["bibtex"].lower())

    def test_get_bibtex_and_citation_for_items_strips_file_field_and_truncates_long_author_lists(self):
        authors = " and ".join(
            f"Author{index} Example"
            for index in range(1, db._BIBTEX_MAX_AUTHORS + 4)
        )

        with patch(
            "zoty.db._fetch_item_exports",
            return_value={
                "citation": "<span>cite</span>",
                "bibliography": "<div>ref</div>",
                "bibtex": (
                    "@article{ITEM123,\n"
                    f"  author={{{authors}}},\n"
                    "  title={Example},\n"
                    "  abstract={Detailed summary},\n"
                    "  file={PDF:/Users/eric/Zotero/storage/6QDRGPA5/paper.pdf:application/pdf},\n"
                    "  year={2026}\n"
                    "}"
                ),
            },
        ):
            result = json.loads(db.get_bibtex_and_citation_for_items(item_key="item123"))

        bibtex = result["items"][0]["bibtex"]
        self.assertNotIn("abstract", bibtex.lower())
        self.assertNotIn("file =", bibtex.lower())
        self.assertNotIn("/Users/eric/Zotero/storage", bibtex)
        self.assertIn("Author1 Example", bibtex)
        self.assertIn(f"Author{db._BIBTEX_MAX_AUTHORS} Example", bibtex)
        self.assertNotIn(f"Author{db._BIBTEX_MAX_AUTHORS + 1} Example", bibtex)
        self.assertIn("and others", bibtex)

    def test_get_item_deduplicates_combined_item_key_inputs(self):
        zot = Mock()

        def item_side_effect(item_key, **kwargs):
            item = self._paper_item()
            item["data"]["key"] = item_key
            item["data"]["title"] = f"{item_key} title"
            return item

        zot.item.side_effect = item_side_effect

        with (
            patch("zoty.db._get_zot", return_value=zot),
            patch(
                "zoty.db._get_item_attachments_by_parent",
                return_value={"PARENT1": [], "PARENT2": []},
            ),
        ):
            result = json.loads(
                db.get_item(
                    item_key="parent1",
                    item_keys=[" parent1 ", "parent2", "PARENT2", "parent1"],
                )
            )

        self.assertEqual(result["item_keys"], ["PARENT1", "PARENT2"])
        self.assertEqual(result["requested"], 2)
        self.assertEqual(result["total"], 2)
        self.assertEqual([item["key"] for item in result["items"]], ["PARENT1", "PARENT2"])
        self.assertEqual(zot.item.call_count, 2)

    def test_get_bibtex_and_citation_for_items_sanitizes_item_not_found_http_errors(self):
        with patch(
            "zoty.db._fetch_item_exports",
            side_effect=FakeHttpError(
                (
                    "Code: 404 URL: "
                    "http://localhost:23119/api/users/0/items/MISSING"
                    "?format=json&include=bib,citation,bibtex&style=apa&locale=en-US "
                    "Method: GET Response: not found"
                ),
                status_code=404,
                url=(
                    "http://localhost:23119/api/users/0/items/MISSING"
                    "?format=json&include=bib,citation,bibtex&style=apa&locale=en-US"
                ),
            ),
        ):
            result = json.loads(
                db.get_bibtex_and_citation_for_items(
                    item_key="missing",
                    style="apa",
                    locale="en-US",
                )
            )

        self.assertEqual(result["error"], "Item MISSING was not found")
        self.assertEqual(
            result["errors"],
            [
                {
                    "key": "MISSING",
                    "error": "Item MISSING was not found",
                }
            ],
        )
        self.assertNotIn("localhost", json.dumps(result))

    def test_get_bibtex_and_citation_for_items_sanitizes_invalid_style_http_errors(self):
        with patch(
            "zoty.db._fetch_item_exports",
            side_effect=FakeHttpError(
                (
                    "Code: 404 URL: "
                    "http://localhost:23119/api/users/0/items/ITEM123"
                    "?format=json&include=bib,citation,bibtex&style=bad-style&locale=en-US "
                    "Method: GET Response: Citation style not found: "
                    "https://www.zotero.org/styles/bad-style"
                ),
                status_code=404,
                url=(
                    "http://localhost:23119/api/users/0/items/ITEM123"
                    "?format=json&include=bib,citation,bibtex&style=bad-style&locale=en-US"
                ),
            ),
        ):
            result = json.loads(
                db.get_bibtex_and_citation_for_items(
                    item_key="item123",
                    style="bad-style",
                    locale="en-US",
                )
            )

        self.assertEqual(result["error"], "Citation style bad-style was not found")
        self.assertEqual(
            result["errors"],
            [
                {
                    "key": "ITEM123",
                    "error": "Citation style bad-style was not found",
                }
            ],
        )
        self.assertNotIn("localhost", json.dumps(result))
        self.assertNotIn("zotero.org/styles", json.dumps(result))

    def test_get_bibtex_and_citation_for_items_requires_at_least_one_key(self):
        result = json.loads(db.get_bibtex_and_citation_for_items())

        self.assertEqual(
            result,
            {
                "error": "Provide item_key or item_keys",
                "items": [],
                "total": 0,
            },
        )


if __name__ == "__main__":
    unittest.main()
