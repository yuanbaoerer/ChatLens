"""Synthetic tests for deterministic image/GIF/sticker duplicate detection."""
from __future__ import annotations

import hashlib
import mimetypes
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw

from analytics.analysis_service import AnalysisService
from analytics.analysis_store import RunStatus
from analytics.analyzer import (
    AnalysisCancelled,
    AnalysisContext,
    AnalyzerRegistry,
    Cancellation,
    ProgressReporter,
)
from analytics.analyzers.media_duplicates import MediaDuplicateAnalyzer
from analytics.models import (
    ForwardChild,
    MediaQuality,
    MediaRef,
    MediaRole,
    MediaStatus,
    Message,
)
from analytics.snapshot import SnapshotBuilder

STAMP = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def image(
    *,
    size: tuple[int, int] = (64, 48),
    color: str = "white",
    rect: tuple[str, tuple[int, int, int, int]] | None = ("navy", (1, 1, 10, 10)),
) -> Image.Image:
    result = Image.new("RGB", size, color)
    if rect is not None:
        ImageDraw.Draw(result).rectangle(rect[1], fill=rect[0])
    return result


class MediaDuplicateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.media_root = self.root / "media"
        self.media_root.mkdir()
        self.builder = SnapshotBuilder(
            source_id="synthetic-source",
            account_scope="synthetic-account",
            group_id="synthetic@chatroom",
            timezone_name="Asia/Shanghai",
        )
        self.counter = 0

    def save_image(self, filename: str, picture: Image.Image, *, raw: bytes | None = None) -> Path:
        path = self.media_root / filename
        if raw is not None:
            path.write_bytes(raw)
        else:
            picture.save(path)
        return path

    def add_media(
        self,
        filename: str | None,
        *,
        picture: Image.Image | None = None,
        raw: bytes | None = None,
        caption: str = "",
        kind: str = "image",
        role: MediaRole = MediaRole.main,
        quality: MediaQuality = MediaQuality.original,
        status: MediaStatus = MediaStatus.readable,
        relative_path: str | None | object = ...,
        content_hash: str | None | object = ...,
        dimensions: tuple[int, int] | None | object = ...,
        frame_count: int | None | object = ...,
        is_animated: bool | None | object = ...,
        sender: str = "u_a",
        message_id: str | None = None,
    ) -> tuple[str, MediaRef]:
        self.counter += 1
        message_id = message_id or f"msg-{self.counter}"
        path = self.media_root / filename if filename else None
        if path is not None and raw is not None:
            path.write_bytes(raw)
        elif path is not None and picture is not None:
            picture.save(path)

        if path is not None and path.is_file():
            try:
                with Image.open(path) as opened:
                    actual_dimensions = opened.size
                    actual_frames = int(getattr(opened, "n_frames", 1))
            except Exception:
                actual_dimensions = None
                actual_frames = None
        else:
            actual_dimensions = None
            actual_frames = None

        if relative_path is ...:
            relative_path = filename
        if content_hash is ...:
            content_hash = (
                hashlib.sha256(path.read_bytes()).hexdigest()
                if path is not None and path.is_file()
                else None
            )
        if dimensions is ...:
            dimensions = actual_dimensions
        if frame_count is ...:
            frame_count = actual_frames
        if is_animated is ...:
            is_animated = None if actual_frames is None else actual_frames > 1

        ref = MediaRef(
            media_id=f"{message_id}:m0",
            kind=kind,
            relative_path=relative_path,
            mime=mimetypes.guess_type(filename or "")[0],
            content_hash=content_hash,
            dimensions=dimensions,
            frame_count=frame_count,
            is_animated=is_animated,
            role=role,
            quality=quality,
            status=status,
            reason=None if status is MediaStatus.readable else status.value,
        )
        message = Message(
            source_id="synthetic-source",
            account_scope="synthetic-account",
            group_id="synthetic@chatroom",
            message_id=message_id,
            sender_pseudonym=sender,
            sent_at=STAMP + timedelta(seconds=self.counter),
            kind=kind,
            text=caption,
            media_refs=(ref,),
        )
        self.builder.add_message(message)
        return message_id, ref

    def add_gif(
        self,
        filename: str,
        frames: list[Image.Image],
        *,
        raw: bytes | None = None,
        duration: int = 100,
        **kwargs,
    ) -> tuple[str, MediaRef]:
        path = self.media_root / filename
        if raw is not None:
            path.write_bytes(raw)
        else:
            frames[0].save(
                path,
                save_all=True,
                append_images=frames[1:],
                duration=duration,
                loop=0,
            )
        return self.add_media(filename, **kwargs)

    def add_webp(
        self,
        filename: str,
        frames: list[Image.Image],
        *,
        duration: int | list[int] = 100,
        raw: bytes | None = None,
        **kwargs,
    ) -> tuple[str, MediaRef]:
        path = self.media_root / filename
        if raw is not None:
            path.write_bytes(raw)
        else:
            frames[0].save(
                path,
                save_all=True,
                append_images=frames[1:],
                duration=duration,
                loop=0,
            )
        return self.add_media(filename, **kwargs)

    def analyze(
        self,
        *,
        media_root: Path | str | None = None,
        config: dict | None = None,
    ):
        settings = {} if media_root is None else {"media_root": str(media_root)}
        if config:
            settings.update(config)
        analyzer = MediaDuplicateAnalyzer()
        return analyzer.run(
            self.builder.build(),
            AnalysisContext(config=settings),
            Cancellation(),
            ProgressReporter(lambda _value, _message: None),
        )

    def test_exact_content_hash_forms_one_cluster(self):
        picture = image()
        first_path = self.save_image("a.png", picture)
        self.add_media("a.png", picture=picture, caption="same caption", sender="u_a")
        self.add_media(
            "b.png",
            picture=picture,
            raw=first_path.read_bytes(),
            caption="same caption",
            sender="u_b",
        )

        result = self.analyze(media_root=self.media_root)

        cluster, = result.output["clusters"]
        self.assertEqual(cluster["relation"], "exact_duplicate")
        self.assertEqual(cluster["member_evidence_ids"], ["msg-1", "msg-2"])
        self.assertEqual(cluster["occurrence_count"], 2)
        self.assertEqual(cluster["unique_sender_count"], 2)
        self.assertEqual(cluster["media_kinds"], ["image"])
        self.assertEqual(result.output["coverage"]["analyzed"], 2)
        self.assertEqual(result.output["coverage"]["unique_media_groups"], 1)
        self.assertEqual(result.evidence_ids, ("msg-1", "msg-2"))

    def test_missing_thumbnail_and_unreadable_media_are_excluded(self):
        picture = image()
        self.add_media(
            "missing.png",
            status=MediaStatus.missing,
            relative_path=None,
            content_hash=None,
            dimensions=None,
            frame_count=None,
            is_animated=None,
        )
        self.add_media(
            "thumb.png",
            picture=picture,
            role=MediaRole.thumbnail,
            quality=MediaQuality.thumbnail,
        )
        self.add_media(
            "failed.png",
            picture=picture,
            status=MediaStatus.failed,
            relative_path=None,
            content_hash=None,
            dimensions=None,
            frame_count=None,
            is_animated=None,
        )

        result = self.analyze(media_root=self.media_root)

        self.assertEqual(result.output["clusters"], [])
        self.assertEqual(result.output["candidates"], [])
        self.assertEqual(result.evidence_ids, ())
        coverage = result.output["coverage"]
        self.assertEqual(coverage["media_refs_seen"], 3)
        self.assertEqual(coverage["analyzed"], 0)
        self.assertEqual(coverage["excluded"]["missing"], 1)
        self.assertEqual(coverage["excluded"]["thumbnail"], 1)
        self.assertEqual(coverage["excluded"]["failed"], 1)

    def test_exact_hash_still_works_without_media_root(self):
        picture = image()
        first_path = self.save_image("a.png", picture)
        self.add_media("a.png", picture=picture, caption="caption")
        self.add_media("b.png", picture=picture, raw=first_path.read_bytes(), caption="caption")

        result = self.analyze()

        self.assertEqual(len(result.output["clusters"]), 1)
        self.assertEqual(result.output["coverage"]["analyzed"], 2)
        self.assertIn("near_detection_skipped:media_root_missing", result.diagnostics)

    def test_near_static_image_is_candidate_but_not_exact_cluster(self):
        base = image()
        variant = image(rect=("navy", (2, 2, 11, 11)))
        self.add_media("base.png", picture=base, caption="same caption")
        self.add_media("variant.png", picture=variant, caption="same caption")

        result = self.analyze(media_root=self.media_root)

        self.assertEqual(result.output["clusters"], [])
        candidate, = result.output["candidates"]
        self.assertEqual(candidate["relation"], "near_duplicate")
        self.assertLessEqual(candidate["hamming_distance"], 10)
        self.assertFalse(candidate["caption_changed"])
        self.assertEqual(result.output["coverage"]["near_candidates"], 1)

    def test_near_image_with_changed_caption_is_template_candidate(self):
        base = image()
        variant = image(rect=("navy", (2, 2, 11, 11)))
        self.add_media("base.png", picture=base, caption="caption one")
        self.add_media("variant.png", picture=variant, caption="caption two")

        result = self.analyze(media_root=self.media_root)

        self.assertEqual(result.output["clusters"], [])
        candidate, = result.output["candidates"]
        self.assertEqual(candidate["relation"], "same_meme_template")
        self.assertEqual(candidate["reason"], "caption_changed")
        self.assertTrue(candidate["caption_changed"])

    def test_placeholder_caption_is_treated_as_no_caption(self):
        base = image()
        variant = image(rect=("navy", (2, 2, 11, 11)))
        self.add_media("base.png", picture=base, caption="[图片]")
        self.add_media("variant.png", picture=variant, caption="")

        result = self.analyze(media_root=self.media_root)

        candidate, = result.output["candidates"]
        self.assertEqual(candidate["relation"], "near_duplicate")
        self.assertFalse(candidate["caption_changed"])

    def test_gif_exact_duplicate_and_first_frame_are_not_collapsed(self):
        frames = [image(), image(color="gold")]
        first_path = self.media_root / "first.gif"
        frames[0].save(first_path, save_all=True, append_images=frames[1:], duration=100, loop=0)
        self.add_gif("first.gif", frames, kind="emoji")
        self.add_gif("copy.gif", frames, raw=first_path.read_bytes(), kind="emoji")
        self.save_image("first-frame.png", frames[0])
        self.add_media("first-frame.png", caption="")

        result = self.analyze(media_root=self.media_root)

        cluster, = result.output["clusters"]
        self.assertEqual(cluster["occurrence_count"], 2)
        self.assertEqual(cluster["media_kinds"], ["emoji"])
        self.assertEqual(result.output["candidates"], [])
        self.assertGreater(result.output["coverage"]["near_excluded"]["animation"], 0)

    def test_gif_second_frame_difference_prevents_near_duplicate(self):
        first = [image(), image(color="gold")]
        different = [image(), image(color="black")]
        self.add_gif("first.gif", first, kind="emoji")
        self.add_gif("different.gif", different, kind="emoji")

        result = self.analyze(media_root=self.media_root)

        self.assertFalse(any(
            candidate["relation"] == "near_duplicate"
            for candidate in result.output["candidates"]
        ))
        self.assertTrue(any(
            rejected["reason"] == "visual_distance"
            for rejected in result.output["rejected"]
        ))
        self.assertGreater(result.output["coverage"]["near_excluded"]["visual_distance"], 0)

    def test_gif_near_sequence_is_candidate(self):
        first = [image(), image(color="gold")]
        near = [
            image(color="#f8f8f8"),
            image(color="gold", rect=("navy", (1, 1, 10, 10))),
        ]
        self.add_gif("first.gif", first, kind="emoji")
        self.add_gif("near.gif", near, kind="emoji")

        result = self.analyze(media_root=self.media_root)

        candidate, = result.output["candidates"]
        self.assertEqual(candidate["relation"], "near_duplicate")
        self.assertEqual(candidate["frame_count"], [2, 2])

    def test_gif_frame_count_change_is_not_a_near_duplicate(self):
        first = [image(), image(color="gold")]
        longer = [image(), image(color="gold"), image(color="red")]
        self.add_gif("first.gif", first, kind="emoji")
        self.add_gif("longer.gif", longer, kind="emoji")

        result = self.analyze(media_root=self.media_root)

        self.assertFalse(any(
            candidate["relation"] == "near_duplicate"
            for candidate in result.output["candidates"]
        ))
        self.assertGreater(result.output["coverage"]["near_excluded"]["frame_count"], 0)

    def test_gif_duration_change_is_not_a_near_duplicate(self):
        frames = [image(), image(color="gold")]
        self.add_gif("fast.gif", frames, kind="emoji", duration=100)
        self.add_gif("slow.gif", frames, kind="emoji", duration=500)

        result = self.analyze(media_root=self.media_root)

        self.assertFalse(any(
            candidate["relation"] == "near_duplicate"
            for candidate in result.output["candidates"]
        ))
        self.assertGreater(result.output["coverage"]["near_excluded"]["duration"], 0)

    def test_forward_child_media_uses_child_evidence_id(self):
        picture = image()
        first_path = self.save_image("child.png", picture)
        child_ref = MediaRef(
            media_id="parent/f0:m0",
            kind="image",
            relative_path="child.png",
            mime="image/png",
            content_hash=hashlib.sha256(first_path.read_bytes()).hexdigest(),
            dimensions=(64, 48),
            frame_count=1,
            is_animated=False,
            role=MediaRole.main,
            quality=MediaQuality.original,
            status=MediaStatus.readable,
        )
        child = ForwardChild(
            child_id="parent/f0",
            sender_pseudonym="u_child",
            sent_at=STAMP + timedelta(seconds=2),
            kind="image",
            text="",
            media_refs=(child_ref,),
        )
        self.builder.add_message(Message(
            source_id="synthetic-source",
            account_scope="synthetic-account",
            group_id="synthetic@chatroom",
            message_id="parent",
            sender_pseudonym="u_parent",
            sent_at=STAMP,
            kind="chatrecord",
            text="thread",
            forward_children=(child,),
        ))
        self.add_media("top.png", picture=picture, raw=first_path.read_bytes(), caption="")

        result = self.analyze(media_root=self.media_root)

        cluster, = result.output["clusters"]
        self.assertIn("parent/f0", cluster["member_evidence_ids"])
        self.assertIn("parent/f0", result.evidence_ids)

    def test_animated_webp_duration_is_read_after_loading_frames(self):
        frames = [image(), image(color="gold")]
        self.add_webp("fast.webp", frames, duration=[100, 100], kind="emoji")
        self.add_webp("slow.webp", frames, duration=[100, 500], kind="emoji")

        result = self.analyze(media_root=self.media_root)

        self.assertFalse(any(
            candidate["relation"] == "near_duplicate"
            for candidate in result.output["candidates"]
        ))
        self.assertGreater(result.output["coverage"]["near_excluded"]["duration"], 0)

    def test_invalid_hash_is_recomputed_when_media_root_exists(self):
        base = image()
        variant = image(color="gold")
        self.add_media("base.png", picture=base, content_hash="")
        self.add_media("variant.png", picture=variant, content_hash="")

        result = self.analyze(media_root=self.media_root)

        self.assertEqual(result.output["clusters"], [])
        self.assertEqual(result.output["coverage"]["analyzed"], 2)
        self.assertEqual(result.output["coverage"]["excluded"]["invalid_hash"], 0)

    def test_invalid_hash_without_media_root_is_excluded(self):
        base = image()
        self.add_media("base.png", picture=base, content_hash="")
        self.add_media("variant.png", picture=image(color="gold"), content_hash="")

        result = self.analyze()

        self.assertEqual(result.output["clusters"], [])
        self.assertEqual(result.evidence_ids, ())
        self.assertEqual(result.output["coverage"]["excluded"]["invalid_hash"], 2)

    def test_missing_hash_is_recomputed_from_media_root(self):
        picture = image()
        first_path = self.save_image("a.png", picture)
        self.add_media("a.png", picture=picture, content_hash=None)
        self.add_media("b.png", picture=picture, raw=first_path.read_bytes(), content_hash=None)

        result = self.analyze(media_root=self.media_root)

        self.assertEqual(len(result.output["clusters"]), 1)
        self.assertEqual(result.output["coverage"]["analyzed"], 2)
        self.assertEqual(result.output["coverage"]["excluded"]["invalid_hash"], 0)

    def test_exact_hash_uses_current_file_when_media_root_exists(self):
        base = image()
        variant = image(color="gold")
        base_hash = hashlib.sha256(base.tobytes()).hexdigest()
        self.add_media("base.png", picture=base, content_hash=base_hash)
        self.add_media("variant.png", picture=variant, content_hash=base_hash)

        result = self.analyze(media_root=self.media_root)

        self.assertEqual(result.output["clusters"], [])
        self.assertEqual(result.output["coverage"]["analyzed"], 2)
        self.assertIn("content_hash_mismatch:2", result.diagnostics)

    def test_exact_image_with_changed_caption_is_template_cluster(self):
        picture = image()
        first_path = self.save_image("a.png", picture)
        self.add_media("a.png", picture=picture, caption="caption one")
        self.add_media("b.png", picture=picture, raw=first_path.read_bytes(), caption="caption two")

        result = self.analyze(media_root=self.media_root)

        cluster, = result.output["clusters"]
        self.assertEqual(cluster["relation"], "same_meme_template")
        self.assertTrue(cluster["cluster_id"].startswith("template:"))
        self.assertTrue(cluster["caption_changed"])
        self.assertEqual(cluster["caption_variant_count"], 2)
        self.assertNotIn("caption_variants", cluster)
        self.assertEqual(result.output["coverage"]["exact_clusters"], 0)
        self.assertEqual(result.output["coverage"]["template_clusters"], 1)
        self.assertEqual(result.output["coverage"]["confirmed_duplicate_occurrences"], 0)

    def test_media_root_must_be_absolute_and_non_empty(self):
        self.add_media("a.png", picture=image())
        for invalid in ("", " ", "relative/media"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.analyze(media_root=invalid)

    def test_config_rejects_out_of_range_values(self):
        snapshot = self.builder.build()
        analyzer = MediaDuplicateAnalyzer()
        for config in (
            {"phash_threshold": 64},
            {"max_frames": 1},
            {"max_candidate_pairs": 0},
            {"max_rejected_pairs": 0},
            {"max_near_groups": 0},
            {"max_clusters": 0},
            {"max_member_ids": 0},
            {"max_evidence_ids": 0},
        ):
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    analyzer.run(
                        snapshot,
                        AnalysisContext(config=config),
                        Cancellation(),
                        ProgressReporter(lambda _value, _message: None),
                    )

    def test_coverage_reconciles_kinds_and_confirmed_duplicates(self):
        picture = image()
        first_path = self.save_image("a.png", picture)
        self.add_media("a.png", picture=picture)
        self.add_media("b.png", picture=picture, raw=first_path.read_bytes())
        self.add_media("voice.mp3", raw=b"synthetic voice", kind="voice", status=MediaStatus.readable)

        result = self.analyze(media_root=self.media_root)

        coverage = result.output["coverage"]
        self.assertEqual(coverage["media_refs_seen"], 3)
        self.assertEqual(coverage["excluded"]["unsupported_kind"], 1)
        self.assertEqual(coverage["by_kind"]["image"]["eligible"], 2)
        self.assertEqual(coverage["by_kind"]["image"]["analyzed"], 2)
        self.assertEqual(coverage["by_kind"]["image"]["exact_occurrences"], 2)
        self.assertEqual(coverage["by_kind"]["voice"]["unsupported_kind"], 1)
        self.assertEqual(coverage["confirmed_duplicate_occurrences"], 1)

    def test_corrupt_readable_media_is_reported_without_crashing(self):
        path = self.media_root / "broken.png"
        path.write_bytes(b"synthetic broken image")
        self.add_media(
            "broken.png",
            status=MediaStatus.readable,
            content_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
            dimensions=None,
            frame_count=None,
            is_animated=None,
        )
        self.add_media("valid.png", picture=image())

        result = self.analyze(media_root=self.media_root)

        self.assertEqual(result.output["clusters"], [])
        self.assertEqual(result.output["coverage"]["near_excluded"]["no_signature"], 1)
        self.assertIn("near_signature_unavailable:1", result.diagnostics)

    def test_cluster_member_and_evidence_outputs_are_bounded(self):
        first = image()
        second = image(color="gold")
        first_path = self.save_image("a.png", first)
        second_path = self.save_image("b.png", second)
        self.add_media("a.png", picture=first)
        self.add_media("a-copy.png", picture=first, raw=first_path.read_bytes())
        self.add_media("b.png", picture=second)
        self.add_media("b-copy.png", picture=second, raw=second_path.read_bytes())

        result = self.analyze(
            media_root=self.media_root,
            config={
                "max_clusters": 1,
                "max_member_ids": 1,
                "max_evidence_ids": 1,
            },
        )

        coverage = result.output["coverage"]
        self.assertEqual(len(result.output["clusters"]), 1)
        self.assertEqual(coverage["clusters_returned"], 1)
        self.assertEqual(coverage["clusters_truncated"], 1)
        self.assertEqual(coverage["exact_clusters"], 2)
        self.assertEqual(len(result.output["clusters"][0]["member_evidence_ids"]), 1)
        self.assertTrue(result.output["clusters"][0]["member_ids_truncated"])
        self.assertEqual(len(result.evidence_ids), 1)
        self.assertTrue(coverage["evidence_ids_truncated"])
        self.assertIn("clusters_truncated:1", result.diagnostics)
        self.assertIn("evidence_ids_truncated:3", result.diagnostics)

    def test_rejected_edges_are_bounded(self):
        self.add_media("a.png", picture=image(size=(64, 48)))
        self.add_media("b.png", picture=image(size=(48, 64)))
        self.add_media("c.png", picture=image(size=(80, 20)))

        result = self.analyze(
            media_root=self.media_root,
            config={"max_rejected_pairs": 1},
        )

        coverage = result.output["coverage"]
        self.assertEqual(len(result.output["rejected"]), 1)
        self.assertTrue(coverage["near_rejected_truncated"])
        self.assertIn("near_rejected_truncated:1", result.diagnostics)

    def test_cancellation_stops_before_media_scan(self):
        self.add_media("a.png", picture=image())
        cancellation = Cancellation()
        cancellation.cancel()
        analyzer = MediaDuplicateAnalyzer()

        with self.assertRaises(AnalysisCancelled):
            analyzer.run(
                self.builder.build(),
                AnalysisContext(config={"media_root": str(self.media_root)}),
                cancellation,
                ProgressReporter(lambda _value, _message: None),
            )

    def test_resized_image_is_near_candidate(self):
        picture = image()
        self.add_media("a.png", picture=picture, caption="same")
        self.add_media("b.png", picture=picture.resize((48, 36)), caption="same")

        result = self.analyze(media_root=self.media_root)

        candidate, = result.output["candidates"]
        self.assertEqual(candidate["relation"], "near_duplicate")

    def test_cropped_image_is_near_candidate(self):
        picture = image()
        self.add_media("a.png", picture=picture, caption="same")
        self.add_media("b.png", picture=picture.crop((0, 0, 60, 45)), caption="same")

        result = self.analyze(media_root=self.media_root)

        candidate, = result.output["candidates"]
        self.assertEqual(candidate["relation"], "near_duplicate")

    def test_media_path_outside_root_is_excluded(self):
        outside = self.root / "outside.png"
        image().save(outside)
        outside_hash = hashlib.sha256(outside.read_bytes()).hexdigest()
        self.add_media(
            None,
            relative_path="../outside.png",
            content_hash=outside_hash,
            dimensions=(64, 48),
            frame_count=1,
            is_animated=False,
        )

        result = self.analyze(media_root=self.media_root)

        self.assertEqual(result.output["clusters"], [])
        self.assertEqual(result.evidence_ids, ())
        self.assertEqual(result.output["coverage"]["excluded"]["outside_root"], 1)

    def test_media_duplicate_analyzer_runs_through_service(self):
        picture = image()
        first_path = self.save_image("a.png", picture)
        self.add_media("a.png", picture=picture, caption="caption")
        self.add_media("b.png", picture=picture, raw=first_path.read_bytes(), caption="caption")

        service = AnalysisService(
            AnalyzerRegistry([MediaDuplicateAnalyzer()]),
            self.root / "workspace",
        )
        self.addCleanup(service.close)
        run_id = service.submit(
            self.builder.build(),
            AnalysisContext(config={"media_root": str(self.media_root)}),
        )
        record = service.wait(run_id, timeout=5)

        self.assertEqual(record.status, RunStatus.succeeded)
        stage, = record.stages
        self.assertEqual(stage.analyzer_id, "media.duplicates")
        self.assertEqual(stage.output["coverage"]["exact_clusters"], 1)
        self.assertEqual(stage.evidence_ids, ("msg-1", "msg-2"))
        self.assertEqual(stage.progress_message, "finalizing media duplicates")


if __name__ == "__main__":
    unittest.main()
