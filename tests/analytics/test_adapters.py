import hashlib
import json
import socket
import tempfile
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from analytics.adapters import ChatSourceAdapter, JsonlExportAdapter, SqliteArchiveAdapter
from analytics.models import MediaQuality, MediaRole, MediaStatus, TimeRange, TypeCoverage
from analytics.privacy import Pseudonymizer, resolve_safe_path
from fixtures import ALICE, BOB, DIRECT, GROUP, STAMP, export_fixture, sqlite_fixture, write_export


def instant(offset=0):
    return datetime.fromtimestamp(STAMP, timezone.utc) + timedelta(seconds=offset)


def source_files(root):
    return {
        path.relative_to(root): (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in root.rglob("*") if path.is_file()
    }


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for module, name in (
            (urllib.request, "urlopen"), (urllib.request, "build_opener"),
            (socket, "create_connection"), (socket, "getaddrinfo"),
            (socket.socket, "connect"), (socket.socket, "connect_ex"), (socket.socket, "sendto"),
        ):
            blocked = self.enterContext(patch.object(module, name, side_effect=AssertionError("network disabled")))
            self.addCleanup(blocked.assert_not_called)

    def assert_no_sender_ids(self, snapshot):
        for output in (repr(snapshot), json.dumps(snapshot.to_dict())):
            for sender in (ALICE, BOB):
                self.assertNotIn(sender, output)


class JsonlAdapterTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.path, self.metadata, self.records = export_fixture(self.root / "export")
        self.adapter = JsonlExportAdapter(self.path)

    def test_repeat_and_reordered_import_keep_ids_coverage_and_evidence(self):
        first = self.adapter.build_snapshot(GROUP)
        second = JsonlExportAdapter(self.path).build_snapshot(GROUP)
        write_export(self.path, self.metadata, list(reversed(self.records)))
        reordered = self.adapter.build_snapshot(GROUP)
        self.assertEqual(first, second)
        self.assertEqual(reordered, first)
        self.assertRegex(first.snapshot_id, r"^[0-9a-f]{64}$")
        keys = [(message.sent_at, message.message_id) for message in first.messages]
        self.assertEqual(keys, sorted(set(keys)))
        self.assertEqual(first.coverage.total_top_level, 16)
        self.assertEqual(first.coverage.forward_child_count, 6)
        self.assertEqual(first.coverage.by_kind["chatrecord"], 2)
        self.assertEqual(first.coverage.earliest_at, instant())
        self.assertEqual(first.coverage.latest_at, instant(13))
        self.assertEqual(first.coverage.timezone, "Asia/Shanghai")
        parents = [msg for msg in first.messages if msg.kind == "chatrecord"]
        self.assertEqual({msg.text for msg in parents}, {"Thread", "Thread variant"})
        self.assertEqual(sum("message_id_collision:" in diag for diag in first.diagnostics), 1)
        for parent in parents:
            note, nested = parent.forward_children
            image, = nested.forward_children
            self.assertEqual(note.sent_at, instant(-30))
            self.assertEqual(note.text, "Earlier note")
            self.assertTrue(image.child_id.startswith(parent.message_id + "/"))
            webp, = image.media_refs
            self.assertTrue(webp.media_id.startswith(image.child_id + ":"))
            self.assertEqual((webp.mime, webp.frame_count, webp.dimensions), ("image/webp", 1, (16, 12)))
            self.assertFalse(parent.forward_truncated)
        fallback = next(msg for msg in first.messages if msg.text == "No server id")
        self.assertTrue(fallback.message_id.startswith("local:"))

    def test_media_status_and_original_coverage(self):
        snapshot = self.adapter.build_snapshot(GROUP)
        media_by_server = {msg.local_evidence.server_id: msg.media_refs for msg in snapshot.messages}
        png, = media_by_server["102"]
        self.assertEqual((png.status, png.mime), (MediaStatus.readable, "image/png"))
        self.assertEqual((png.role, png.quality), (MediaRole.main, MediaQuality.original))
        self.assertEqual((png.dimensions, png.frame_count, png.is_animated), ((16, 12), 1, False))
        self.assertEqual(png.content_hash, hashlib.sha256((self.path.parent / png.relative_path).read_bytes()).hexdigest())
        gif, = media_by_server["103"]
        self.assertEqual(gif.mime, "image/gif")
        self.assertEqual(gif.status, MediaStatus.readable)
        self.assertTrue(gif.is_animated)
        self.assertGreaterEqual(gif.frame_count, 2)
        thumbnail, main = media_by_server["106"]
        self.assertEqual((thumbnail.role, thumbnail.quality), (MediaRole.thumbnail, MediaQuality.thumbnail))
        self.assertEqual((thumbnail.status, main.status), (MediaStatus.readable, MediaStatus.missing))
        for server, status, reason in (
            ("107", MediaStatus.missing, "file_not_found"),
            ("108", MediaStatus.failed, "corrupt_image"),
            ("109", MediaStatus.failed, "path_outside_root"),
            ("110", MediaStatus.failed, "path_outside_root"),
            ("111", MediaStatus.unsupported, "aes_key_unavailable"),
            ("112", MediaStatus.unsupported, "invalid_silk_packets"),
            ("113", MediaStatus.not_requested, "skipped"),
        ):
            with self.subTest(server=server):
                ref, = media_by_server[server]
                self.assertEqual((ref.status, ref.reason), (status, reason))
                if reason == "path_outside_root":
                    self.assertIsNone(ref.relative_path)
                    self.assertIsNone(ref.content_hash)
        self.assertEqual(snapshot.coverage.media_by_kind["image"], TypeCoverage(
            eligible=8, readable=1, missing=2, unsupported=1, failed=3, not_requested=1,
        ))
        self.assertIn("resource_report_status:encrypted=1", snapshot.diagnostics)
        self.assertIn("resource_report_malformed:line=4", snapshot.diagnostics)
        self.assert_no_sender_ids(snapshot)

    def test_range_filters_before_media_reads_and_keeps_forward_context(self):
        window = TimeRange(instant(), instant(2))
        with patch.object(Image, "open", wraps=Image.open) as opening:
            snapshot = self.adapter.build_snapshot(GROUP, window)
        self.assertEqual({Path(call.args[0]).name for call in opening.call_args_list}, {"static.png", "animated.gif"})
        self.assertEqual({msg.local_evidence.server_id for msg in snapshot.messages}, {"101", "102", "103"})
        self.assertEqual({msg.local_evidence.server_id for msg in self.adapter.iter_messages(GROUP, window)},
                         {"101", "102", "103"})
        nested = self.adapter.build_snapshot(GROUP, TimeRange(instant(3), instant(4)))
        self.assertEqual((nested.coverage.total_top_level, nested.coverage.forward_child_count), (2, 6))

    def test_json_envelope_quote_link_and_immutable_maps(self):
        path = self.path.with_suffix(".json")
        write_export(path, self.metadata, self.records)
        adapter = JsonlExportAdapter(path, approved_root=self.root)
        self.assertIsInstance(adapter, ChatSourceAdapter)
        group, = adapter.list_groups()
        self.assertEqual((group.group_id, group.display_name), (GROUP, "Synthetic group"))
        snapshot = adapter.build_snapshot(GROUP)
        messages = {msg.kind: msg for msg in snapshot.messages if msg.kind in ("quote", "link")}
        original = next(msg for msg in snapshot.messages if msg.text == "Hello")
        quote = messages["quote"].reply_to
        self.assertEqual((quote.target_message_id, quote.sender_pseudonym, quote.text_excerpt),
                         (original.message_id, original.sender_pseudonym, "Hello"))
        self.assertEqual(messages["link"].link_meta.url, "https://example.invalid/article")
        self.assertEqual(messages["link"].link_meta.source_app, "Fixture app")
        with self.assertRaises(TypeError):
            snapshot.coverage.by_kind["quote"] = 3
        with self.assertRaises(TypeError):
            snapshot.coverage.media_by_kind["image"] = TypeCoverage()
        self.assertIsInstance(snapshot.messages, tuple)
        self.assert_no_sender_ids(snapshot)
        nested_png = next(msg for msg in snapshot.messages if msg.local_evidence.server_id == "102").media_refs[0]
        self.assertEqual(nested_png.relative_path, "export/synthetic_assets/resources/static.png")

    def test_depth_and_child_budget_mark_partial_trees(self):
        for bounds in ({"max_forward_depth": 1}, {"max_forward_children": 2}):
            with self.subTest(bounds=bounds):
                snapshot = JsonlExportAdapter(self.path, **bounds).build_snapshot(GROUP)
                self.assertEqual(snapshot.coverage.forward_child_count, 4)
                for parent in (msg for msg in snapshot.messages if msg.kind == "chatrecord"):
                    self.assertTrue(parent.forward_truncated)
                    self.assertTrue(parent.forward_children[1].truncated)

    def test_scope_isolates_identical_export_filenames(self):
        path, _, _ = export_fixture(self.root / "other")
        left = self.adapter.build_snapshot(GROUP).messages[0]
        right = JsonlExportAdapter(path).build_snapshot(GROUP).messages[0]
        self.assertNotEqual(left.source_id, right.source_id)
        self.assertNotEqual(left.sender_pseudonym, right.sender_pseudonym)
        self.assertNotEqual(left.message_id, right.message_id)


class SqliteAdapterTests(OfflineTestCase):
    def test_shards_quotes_nested_forward_and_read_only(self):
        root = self.root / "db"
        library = sqlite_fixture(root)
        before = source_files(root)
        with SqliteArchiveAdapter(library) as adapter:
            group, = adapter.list_groups()
            self.assertEqual((group.group_id, group.message_count_hint), (GROUP, 6))
            with self.assertRaises(KeyError):
                list(adapter.iter_messages(DIRECT))
            first = adapter.build_snapshot(GROUP)
            self.assertEqual(first, adapter.build_snapshot(GROUP))
            self.assertEqual((first.coverage.total_top_level, first.coverage.forward_child_count), (6, 3))
            self.assertEqual(first.coverage.by_kind, {"text": 3, "quote": 1, "chatrecord": 1, "image": 1})
            self.assertEqual(first.coverage.media_by_kind["image"], TypeCoverage(eligible=1, not_requested=1))
            self.assertEqual(first.messages[-1].text, "At the end")
            self.assertNotEqual(first.messages[-1].message_id, first.messages[-2].message_id)
            ties = list(adapter.iter_messages(GROUP, TimeRange(instant(), instant(1))))
            self.assertEqual([msg.local_evidence.shard for msg in ties], [0, 1])
            self.assertEqual([msg.text for msg in ties], ["Compressed hello", "Note:\nKeep this prefix"])
            subsecond = adapter.build_snapshot(GROUP, TimeRange(instant(0.1), instant(1.1)))
            reply, = subsecond.messages
            self.assertEqual(reply.reply_to.target_message_id, ties[0].message_id)
            self.assertEqual(reply.reply_to.sender_pseudonym, ties[0].sender_pseudonym)
            parent = next(msg for msg in first.messages if msg.kind == "chatrecord")
            note, nested = parent.forward_children
            self.assertEqual(note.sent_at, instant(-60))
            self.assertEqual(note.sender_pseudonym, ties[0].sender_pseudonym)
            sticker, = nested.forward_children
            self.assertEqual(sticker.kind, "emoji")
            image, = first.messages[-2].media_refs
            for ref in (image, *sticker.media_refs):
                self.assertEqual(ref.status, MediaStatus.not_requested)
                self.assertEqual(ref.reason, "offline_sqlite_without_media_store")
                self.assertIsNone(ref.content_hash)
            self.assert_no_sender_ids(first)
        self.assertEqual(before, source_files(root))


class PrivacyAndTimeTests(OfflineTestCase):
    def test_resolves_paths_only_inside_approved_root(self):
        approved = self.root / "approved"
        approved.mkdir()
        (approved / "image..png").touch()
        (approved / "local.png").symlink_to("image..png")
        (approved / "outside.png").symlink_to(self.root / "outside.png")
        self.assertEqual(resolve_safe_path(approved, "local.png"), approved / "image..png")
        for invalid in ("../outside.png", "outside.png", r"C:\sample.png", r"C:sample.png", "/sample.png"):
            with self.subTest(invalid=invalid):
                self.assertIsNone(resolve_safe_path(approved, invalid))

    def test_pseudonyms_are_stable_within_scopes(self):
        a = Pseudonymizer("synthetic-scope-a")
        b = Pseudonymizer("synthetic-scope-b")
        self.assertEqual(a(ALICE, display_name="Alice"), a(ALICE, display_name="Renamed"))
        self.assertEqual(a(display_name="Alice"), Pseudonymizer("synthetic-scope-a")(display_name="Alice"))
        self.assertNotEqual(a(ALICE), b(ALICE))
        self.assertNotIn(ALICE, a(ALICE))

    def test_time_range_rejects_naive_equal_and_inverted_bounds(self):
        for start, end in ((datetime(2026, 1, 1), None), (instant(), instant()), (instant(1), instant())):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                TimeRange(start, end)
        window = TimeRange(instant(), instant(1))
        self.assertTrue(window.contains(instant().astimezone(timezone(timedelta(hours=8)))))
        self.assertFalse(window.contains(instant(1)))
