"""Synthetic tests for deterministic text, link and forward duplicate detection."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from analytics.analysis_service import AnalysisService
from analytics.analysis_store import RunStatus
from analytics.analyzer import AnalysisContext, AnalyzerRegistry, Cancellation, ProgressReporter
from analytics.analyzers.text_links import TextLinkDuplicateAnalyzer
from analytics.models import ForwardChild, LinkMeta, Message
from analytics.snapshot import SnapshotBuilder

STAMP = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


class TextLinkDuplicateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.builder = SnapshotBuilder(
            source_id="synthetic-source",
            account_scope="synthetic-account",
            group_id="synthetic@chatroom",
            timezone_name="Asia/Shanghai",
        )
        self.counter = 0

    def add_text(
        self,
        text: str,
        *,
        kind: str = "text",
        sender: str = "u_a",
        message_id: str | None = None,
    ) -> str:
        self.counter += 1
        message_id = message_id or f"msg-{self.counter}"
        self.builder.add_message(Message(
            source_id="synthetic-source",
            account_scope="synthetic-account",
            group_id="synthetic@chatroom",
            message_id=message_id,
            sender_pseudonym=sender,
            sent_at=STAMP + timedelta(seconds=self.counter),
            kind=kind,
            text=text,
        ))
        return message_id

    def add_link(
        self,
        url: str,
        *,
        title: str = "",
        kind: str = "link",
        text: str | None = None,
        sender: str = "u_a",
        message_id: str | None = None,
    ) -> str:
        self.counter += 1
        message_id = message_id or f"msg-{self.counter}"
        self.builder.add_message(Message(
            source_id="synthetic-source",
            account_scope="synthetic-account",
            group_id="synthetic@chatroom",
            message_id=message_id,
            sender_pseudonym=sender,
            sent_at=STAMP + timedelta(seconds=self.counter),
            kind=kind,
            text=title if text is None else text,
            link_meta=LinkMeta(url=url, title=title),
        ))
        return message_id

    def add_forward(
        self,
        children: list[dict],
        *,
        parent_id: str | None = None,
        sender: str = "u_parent",
    ) -> str:
        self.counter += 1
        parent_id = parent_id or f"parent-{self.counter}"
        built = []
        for index, child in enumerate(children):
            child_id = f"{parent_id}/f{index}"
            if "url" in child:
                built.append(ForwardChild(
                    child_id=child_id,
                    sender_pseudonym=child.get("sender", "u_child"),
                    sent_at=STAMP + timedelta(seconds=self.counter + index),
                    kind=child.get("kind", "link"),
                    text=child.get("text", ""),
                    link_meta=LinkMeta(
                        url=child["url"],
                        title=child.get("title", ""),
                    ),
                ))
            else:
                built.append(ForwardChild(
                    child_id=child_id,
                    sender_pseudonym=child.get("sender", "u_child"),
                    sent_at=STAMP + timedelta(seconds=self.counter + index),
                    kind="text",
                    text=child["text"],
                ))
        self.builder.add_message(Message(
            source_id="synthetic-source",
            account_scope="synthetic-account",
            group_id="synthetic@chatroom",
            message_id=parent_id,
            sender_pseudonym=sender,
            sent_at=STAMP,
            kind="chatrecord",
            text="Thread",
            forward_children=tuple(built),
        ))
        return parent_id

    def analyze(self, *, config: dict | None = None):
        analyzer = TextLinkDuplicateAnalyzer()
        return analyzer.run(
            self.builder.build(),
            AnalysisContext(config=config or {}),
            Cancellation(),
            ProgressReporter(lambda _value, _message: None),
        )

    def test_whitespace_and_nfkc_are_normalized(self):
        self.add_text("This   is a long duplicate sentence.")
        self.add_text("This is a long duplicate sentence.")
        self.add_text("价格是１００元，不是１０１元。")
        self.add_text("价格是100元，不是101元。")

        result = self.analyze()

        self.assertEqual(len(result.output["clusters"]), 2)
        self.assertEqual(result.output["short_phrase_clusters"], [])
        self.assertEqual(result.output["coverage"]["text_analyzed"], 4)
        self.assertEqual(result.output["coverage"]["text_clusters"], 2)
        self.assertEqual(result.output["coverage"]["confirmed_duplicate_occurrences"], 2)

    def test_numbers_negation_case_and_punctuation_are_preserved(self):
        self.add_text("This long sentence reports 100 units.")
        self.add_text("This long sentence reports 101 units.")
        self.add_text("This long sentence is not true.")
        self.add_text("This long sentence is true.")
        self.add_text("This Long Sentence Preserves Case.")
        self.add_text("This long Sentence Preserves Case.")
        self.add_text("这是一个很长的句子，表示同意！")
        self.add_text("这是一个很长的句子，表示同意。")

        result = self.analyze()

        self.assertEqual(result.output["clusters"], [])
        self.assertEqual(result.output["short_phrase_clusters"], [])
        self.assertEqual(result.output["coverage"]["text_analyzed"], 8)

    def test_short_phrases_are_counted_separately(self):
        self.add_text("哈哈")
        self.add_text("哈哈")
        self.add_text("This is a longer informational sentence that repeats.")
        self.add_text("This is a longer informational sentence that repeats.")

        result = self.analyze()

        self.assertEqual(len(result.output["clusters"]), 1)
        self.assertEqual(len(result.output["short_phrase_clusters"]), 1)
        self.assertEqual(result.output["coverage"]["short_phrase_clusters"], 1)
        self.assertEqual(result.output["coverage"]["short_phrase_duplicate_occurrences"], 1)
        self.assertEqual(result.output["coverage"]["confirmed_duplicate_occurrences"], 1)

    def test_blank_and_placeholder_text_is_excluded(self):
        self.add_text("")
        self.add_text("   ")
        self.add_text("[图片]")
        self.add_text("[链接]")

        result = self.analyze()

        self.assertEqual(result.output["clusters"], [])
        self.assertEqual(result.output["short_phrase_clusters"], [])
        self.assertEqual(result.output["coverage"]["text_blank"], 2)
        self.assertEqual(result.output["coverage"]["text_placeholder"], 2)
        self.assertEqual(result.output["coverage"]["text_analyzed"], 0)

    def test_link_tracking_params_are_removed_and_title_is_not_key(self):
        first = self.add_link(
            "https://Example.com/article?utm_source=x&id=42#part",
            title="First title",
        )
        second = self.add_link(
            "https://example.com/article?id=42#part",
            title="Second title",
        )

        result = self.analyze()

        cluster, = result.output["clusters"]
        self.assertEqual(cluster["content_kind"], "link")
        self.assertEqual(cluster["canonical_url"], "https://example.com/article?id=42#part")
        self.assertEqual(cluster["host"], "example.com")
        self.assertEqual(cluster["title_variant_count"], 2)
        self.assertEqual(cluster["member_evidence_ids"], sorted([first, second]))
        self.assertEqual(result.output["coverage"]["link_clusters"], 1)
        self.assertEqual(result.output["coverage"]["confirmed_duplicate_occurrences"], 1)

    def test_link_meaningful_query_and_fragment_are_preserved(self):
        for left, right in (
            ("https://example.com/a?id=1", "https://example.com/a?id=2"),
            ("https://example.com/a#one", "https://example.com/a#two"),
            ("https://example.com/a", "http://example.com/a"),
            ("https://example.com/a", "https://example.com/a/"),
        ):
            with self.subTest(left=left, right=right):
                self.builder = SnapshotBuilder(
                    source_id="synthetic-source",
                    account_scope="synthetic-account",
                    group_id="synthetic@chatroom",
                    timezone_name="Asia/Shanghai",
                )
                self.counter = 0
                self.add_link(left)
                self.add_link(right)
                result = self.analyze()
                self.assertEqual(result.output["clusters"], [])

    def test_default_port_and_host_case_are_canonicalized(self):
        self.add_link("HTTPS://EXAMPLE.COM:443/a")
        self.add_link("https://example.com/a")

        result = self.analyze()

        cluster, = result.output["clusters"]
        self.assertEqual(cluster["canonical_url"], "https://example.com/a")

    def test_link_title_alone_is_not_a_key(self):
        self.add_link("https://example.com/one", title="Same title")
        self.add_link("https://example.com/two", title="Same title")
        self.add_link("", title="Only a title")

        result = self.analyze()

        self.assertEqual(result.output["clusters"], [])
        self.assertEqual(result.output["coverage"]["by_kind"]["link"]["missing_url"], 1)

    def test_unsupported_link_schemes_and_userinfo_are_excluded(self):
        self.add_link("javascript:alert(1)")
        self.add_link("file:///tmp/example")
        self.add_link("https://user:pass@example.com/article")
        self.add_link("https://")

        result = self.analyze()

        self.assertEqual(result.output["clusters"], [])
        coverage = result.output["coverage"]["by_kind"]["link"]
        self.assertEqual(coverage["unsupported_scheme"], 2)
        self.assertEqual(coverage["userinfo"], 1)
        self.assertEqual(coverage["host_missing"], 1)

    def test_miniprogram_and_text_link_metadata_are_links(self):
        self.add_link("https://example.com/mini", kind="miniprogram", title="Mini")
        self.add_link("https://example.com/mini", kind="miniprogram", title="Mini")
        self.add_link(
            "https://example.com/from-text",
            kind="text",
            title="Link metadata on a text message",
            text="This text must not be counted as text.",
        )
        self.add_link(
            "https://example.com/from-text",
            kind="text",
            title="Link metadata on a text message",
            text="This text must not be counted as text.",
        )

        result = self.analyze()

        self.assertEqual(len(result.output["clusters"]), 2)
        self.assertTrue(all(
            cluster["content_kind"] == "link"
            for cluster in result.output["clusters"]
        ))
        self.assertEqual(result.output["coverage"]["text_analyzed"], 0)
        self.assertEqual(result.output["coverage"]["link_analyzed"], 4)

    def test_oversized_links_are_excluded(self):
        self.add_link("https://example.com/" + "a" * 40)
        self.add_link("https://example.com/" + "a" * 40)

        result = self.analyze(config={"max_url_length": 20})

        self.assertEqual(result.output["clusters"], [])
        self.assertEqual(result.output["coverage"]["link_url_too_long"], 2)

    def test_forward_children_use_child_evidence_ids(self):
        self.add_text("Forwarded duplicate text that is long enough.")
        self.add_link("https://example.com/forwarded")
        parent = self.add_forward([
            {"text": "Forwarded duplicate text that is long enough."},
            {"url": "https://example.com/forwarded"},
        ])

        result = self.analyze()

        by_kind = {cluster["content_kind"]: cluster for cluster in result.output["clusters"]}
        self.assertIn("text", by_kind)
        self.assertIn("link", by_kind)
        self.assertIn(f"{parent}/f0", by_kind["text"]["member_evidence_ids"])
        self.assertIn(f"{parent}/f1", by_kind["link"]["member_evidence_ids"])
        self.assertIn(f"{parent}/f0", result.evidence_ids)

    def test_link_text_is_not_double_counted_as_text(self):
        self.add_link(
            "https://example.com/link",
            title="This long sentence is link text.",
            text="This long sentence is link text.",
        )
        self.add_text("This long sentence is link text.")

        result = self.analyze()

        self.assertEqual(result.output["clusters"], [])
        self.assertEqual(result.output["coverage"]["text_analyzed"], 1)
        self.assertEqual(result.output["coverage"]["link_analyzed"], 1)

    def test_raw_text_and_titles_are_not_emitted(self):
        secret = "TOPSECRET_LONG_DUPLICATE_TEXT"
        title_secret = "TOPSECRET_LINK_TITLE"
        self.add_text(secret)
        self.add_text(secret)
        self.add_link("https://example.com/secret", title=title_secret)
        self.add_link("https://example.com/secret", title=title_secret)

        result = self.analyze()

        rendered = repr(result.output)
        self.assertNotIn(secret, rendered)
        self.assertNotIn(title_secret, rendered)
        self.assertEqual(result.output["coverage"]["text_clusters"], 1)
        self.assertEqual(result.output["coverage"]["link_clusters"], 1)

    def test_outputs_are_bounded(self):
        for index in range(3):
            text = f"Long duplicate informational text for group {index}."
            self.add_text(text)
            self.add_text(text)
        self.add_link("https://example.com/cluster")

        result = self.analyze(config={
            "max_clusters": 1,
            "max_member_ids": 1,
            "max_evidence_ids": 1,
        })

        coverage = result.output["coverage"]
        self.assertEqual(len(result.output["clusters"]), 1)
        self.assertEqual(len(result.output["clusters"][0]["member_evidence_ids"]), 1)
        self.assertTrue(result.output["clusters"][0]["member_ids_truncated"])
        self.assertEqual(len(result.evidence_ids), 1)
        self.assertTrue(coverage["evidence_ids_truncated"])
        self.assertGreater(coverage["clusters_truncated"], 0)
        self.assertIn("clusters_truncated:", "\n".join(result.diagnostics))
        self.assertIn("evidence_ids_truncated:", "\n".join(result.diagnostics))

    def test_config_rejects_out_of_range_values(self):
        for config in (
            {"short_text_max_chars": 0},
            {"max_url_length": 0},
            {"max_clusters": 0},
            {"max_member_ids": 0},
            {"max_evidence_ids": 0},
        ):
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    self.analyze(config=config)

    def test_analyzer_runs_through_service(self):
        self.add_text("This is a service duplicate sentence.")
        self.add_text("This is a service duplicate sentence.")
        service = AnalysisService(
            AnalyzerRegistry([TextLinkDuplicateAnalyzer()]),
            self.root / "workspace",
        )
        self.addCleanup(service.close)

        run_id = service.submit(self.builder.build())
        record = service.wait(run_id, timeout=5)

        self.assertEqual(record.status, RunStatus.succeeded)
        stage, = record.stages
        self.assertEqual(stage.analyzer_id, "text.links.duplicates")
        self.assertEqual(stage.output["coverage"]["text_clusters"], 1)
        self.assertEqual(len(stage.evidence_ids), 2)


if __name__ == "__main__":
    unittest.main()
