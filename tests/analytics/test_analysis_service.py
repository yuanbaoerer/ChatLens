import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from analytics.analysis_service import (
    AnalysisQueueFull,
    AnalysisService,
)
from analytics.analysis_store import (
    RunManifest,
    RunStatus,
    RunStore,
    StageSpec,
    StageStatus,
)
from analytics.analyzer import (
    AnalysisContext,
    AnalysisResult,
    AnalyzerRegistry,
    ArtifactRef,
)
from tests.analytics.analysis_fakes import GROUP, SyntheticAnalyzer, make_message, make_snapshot


class AnalysisServiceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.snapshot = make_snapshot()

    def service(self, analyzers, *, capacity: int = 8) -> AnalysisService:
        service = AnalysisService(
            AnalyzerRegistry(analyzers),
            self.root / "workspace",
            queue_capacity=capacity,
        )
        self.addCleanup(service.close)
        return service

    def manifest(self, run_id: str, *, analyzer_id: str = "manual") -> RunManifest:
        return RunManifest(
            run_id=run_id,
            snapshot_id=self.snapshot.snapshot_id,
            source_id=self.snapshot.source_id,
            account_scope=self.snapshot.account_scope,
            group_id=self.snapshot.group_id,
            time_range=self.snapshot.time_range,
            timezone=self.snapshot.timezone,
            coverage=self.snapshot.coverage.to_dict(),
            config_version="test-config",
            model_id=None,
            prompt_version=None,
            created_at=datetime.now(timezone.utc),
            stages=(
                StageSpec(
                    index=0,
                    analyzer_id=analyzer_id,
                    version="1",
                    prerequisites=(),
                    supported_message_types=frozenset({"text"}),
                    estimated_workload=1,
                ),
            ),
        )

    def test_submit_persists_manifest_and_runs_in_dependency_order(self):
        calls: list[str] = []
        service = self.service([
            SyntheticAnalyzer("gamma", prerequisites=("beta",), calls=calls),
            SyntheticAnalyzer("alpha", calls=calls),
            SyntheticAnalyzer("delta", calls=calls),
            SyntheticAnalyzer("beta", prerequisites=("alpha",), calls=calls),
        ])
        run_id = service.submit(
            self.snapshot,
            AnalysisContext(
                config_version="config-v1",
                model_id="synthetic-model",
                prompt_version="prompt-v1",
            ),
        )
        record = service.wait(run_id, timeout=5)

        self.assertEqual(record.status, RunStatus.succeeded)
        self.assertEqual(calls, ["alpha", "beta", "delta", "gamma"])
        self.assertEqual([stage.status for stage in record.stages], [StageStatus.succeeded] * 4)
        self.assertEqual(record.snapshot_id, self.snapshot.snapshot_id)
        self.assertEqual(record.group_id, GROUP)
        self.assertEqual(record.time_range, self.snapshot.time_range)
        self.assertEqual(record.coverage["total_top_level"], self.snapshot.coverage.total_top_level)
        self.assertEqual(record.config_version, "config-v1")
        self.assertEqual(record.model_id, "synthetic-model")
        self.assertEqual(record.prompt_version, "prompt-v1")
        self.assertIsNotNone(record.started_at)
        self.assertIsNotNone(record.finished_at)
        alpha = record.stages[0]
        self.assertEqual(alpha.version, "1")
        self.assertEqual(alpha.supported_message_types, frozenset({"text"}))
        self.assertEqual(alpha.estimated_workload, 1)
        self.assertEqual(alpha.evidence_ids, ("message-1", "message-1/f0"))
        self.assertEqual(alpha.output["analyzer"], "alpha")
        self.assertEqual(alpha.progress, 1.0)

    def test_queue_full_rejects_submission_without_creating_run(self):
        started = threading.Event()
        release = threading.Event()
        service = self.service(
            [SyntheticAnalyzer("blocking", started=started, release=release)],
            capacity=1,
        )
        first = service.submit(self.snapshot)
        self.assertTrue(started.wait(5))
        second = service.submit(self.snapshot)
        queued = service.get(second)
        self.assertEqual(queued.status, RunStatus.queued)
        self.assertEqual(queued.stages[0].status, StageStatus.pending)
        with self.assertRaises(AnalysisQueueFull):
            service.submit(self.snapshot)
        self.assertEqual(len(service.list_runs()), 2)

        release.set()
        self.assertEqual(service.wait(first, timeout=5).status, RunStatus.succeeded)
        self.assertEqual(service.wait(second, timeout=5).status, RunStatus.succeeded)
        self.assertEqual(len(service.list_runs()), 2)

    def test_cancel_queued_run_stays_cancelled(self):
        started = threading.Event()
        release = threading.Event()
        calls: list[str] = []
        service = self.service(
            [SyntheticAnalyzer("blocking", started=started, release=release, calls=calls)],
            capacity=1,
        )
        first = service.submit(self.snapshot)
        self.assertTrue(started.wait(5))
        second = service.submit(self.snapshot)

        self.assertTrue(service.cancel(second))
        cancelled = service.wait(second, timeout=5)
        self.assertEqual(cancelled.status, RunStatus.cancelled)
        self.assertEqual(cancelled.stages[0].status, StageStatus.cancelled)
        self.assertEqual(cancelled.stages[0].diagnostics, ("run_cancelled",))
        self.assertFalse(service.cancel(second))

        third = service.submit(self.snapshot)
        release.set()
        self.assertEqual(service.wait(first, timeout=5).status, RunStatus.succeeded)
        self.assertEqual(service.wait(third, timeout=5).status, RunStatus.succeeded)
        self.assertEqual(service.get(second).status, RunStatus.cancelled)
        self.assertEqual(calls, ["blocking", "blocking"])

    def test_running_cancel_is_not_overwritten_by_late_success(self):
        started = threading.Event()
        release = threading.Event()
        returned = threading.Event()
        service = self.service([
            SyntheticAnalyzer(
                "late",
                started=started,
                release=release,
                returned=returned,
                ignore_cancel=True,
            )
        ])
        run_id = service.submit(self.snapshot)
        self.assertTrue(started.wait(5))
        self.assertTrue(service.cancel(run_id))
        release.set()
        self.assertTrue(returned.wait(5))
        service.close()

        record = service.get(run_id)
        self.assertEqual(record.status, RunStatus.cancelled)
        stage = record.stages[0]
        self.assertEqual(stage.status, StageStatus.cancelled)
        self.assertEqual(dict(stage.output), {})
        self.assertEqual(stage.evidence_ids, ())

    def test_partial_failure_skips_dependents_and_keeps_independent_results(self):
        calls: list[str] = []
        secret = "TOPSECRET_CHAT_TEXT"
        service = self.service([
            SyntheticAnalyzer("alpha", calls=calls),
            SyntheticAnalyzer("beta", failure=RuntimeError(secret), calls=calls),
            SyntheticAnalyzer("gamma", prerequisites=("beta",), calls=calls),
            SyntheticAnalyzer("delta", calls=calls),
        ])
        run_id = service.submit(self.snapshot)
        record = service.wait(run_id, timeout=5)

        self.assertEqual(record.status, RunStatus.partial)
        statuses = {stage.analyzer_id: stage.status for stage in record.stages}
        self.assertEqual(statuses, {
            "alpha": StageStatus.succeeded,
            "beta": StageStatus.failed,
            "delta": StageStatus.succeeded,
            "gamma": StageStatus.skipped,
        })
        self.assertEqual(calls, ["alpha", "beta", "delta"])
        self.assertEqual(record.stages[1].error_type, "RuntimeError")
        gamma = next(stage for stage in record.stages if stage.analyzer_id == "gamma")
        self.assertEqual(gamma.diagnostics, ("dependency_failed:beta",))
        self.assertIn("missing_stages:beta,gamma", record.diagnostics)
        self.assertEqual(record.stages[0].output["analyzer"], "alpha")
        delta = next(stage for stage in record.stages if stage.analyzer_id == "delta")
        self.assertEqual(delta.output["analyzer"], "delta")
        self.assertNotIn(secret, repr(record.to_dict()))

    def test_unknown_evidence_id_fails_the_stage(self):
        service = self.service([
            SyntheticAnalyzer(
                "bad-evidence",
                result=AnalysisResult(evidence_ids=("not-in-snapshot",)),
            )
        ])
        run_id = service.submit(self.snapshot)
        record = service.wait(run_id, timeout=5)
        self.assertEqual(record.status, RunStatus.failed)
        self.assertEqual(record.stages[0].status, StageStatus.failed)
        self.assertEqual(record.stages[0].error_type, "AnalysisResultError")
        self.assertEqual(record.stages[0].evidence_ids, ())
        self.assertEqual(record.stages[0].output, {})

    def test_non_json_output_fails_the_stage(self):
        service = self.service([
            SyntheticAnalyzer(
                "bad-output",
                result=AnalysisResult(output={"value": object()}),
            )
        ])
        run_id = service.submit(self.snapshot)
        record = service.wait(run_id, timeout=5)
        self.assertEqual(record.status, RunStatus.failed)
        self.assertEqual(record.stages[0].status, StageStatus.failed)
        self.assertEqual(record.stages[0].error_type, "AnalysisResultError")
        self.assertEqual(record.stages[0].output, {})

    def test_artifacts_are_recorded_and_missing_artifacts_fail(self):
        def write_artifact(context, _snapshot):
            (context.workspace / "artifact.txt").write_text("synthetic artifact", encoding="utf-8")
            return AnalysisResult(
                output={"artifact": "artifact.txt"},
                evidence_ids=("message-1",),
                artifacts=(ArtifactRef("artifact.txt"),),
            )

        service = self.service([
            SyntheticAnalyzer("artifact", runner=write_artifact),
            SyntheticAnalyzer(
                "missing-artifact",
                result=AnalysisResult(
                    evidence_ids=("message-1",),
                    artifacts=(ArtifactRef("missing.txt"),),
                ),
            ),
        ])
        run_id = service.submit(self.snapshot)
        record = service.wait(run_id, timeout=5)
        statuses = {stage.analyzer_id: stage.status for stage in record.stages}
        self.assertEqual(statuses["artifact"], StageStatus.succeeded)
        self.assertEqual(statuses["missing-artifact"], StageStatus.failed)
        artifact_stage = next(stage for stage in record.stages if stage.analyzer_id == "artifact")
        self.assertEqual(artifact_stage.artifacts, (ArtifactRef("artifact.txt"),))
        self.assertEqual(record.artifacts, (ArtifactRef("artifact.txt"),))
        failed_stage = next(stage for stage in record.stages if stage.analyzer_id == "missing-artifact")
        self.assertEqual(failed_stage.error_type, "AnalysisResultError")

    def test_artifacts_are_scoped_to_each_run(self):
        seen: list[tuple[str | None, Path]] = []

        def runner(context, _snapshot):
            seen.append((context.run_id, context.workspace))
            (context.workspace / "same.txt").write_text(context.run_id, encoding="utf-8")
            return AnalysisResult(
                evidence_ids=("message-1",),
                artifacts=(ArtifactRef("same.txt"),),
            )

        service = self.service([SyntheticAnalyzer("scoped", runner=runner)])
        first = service.submit(self.snapshot)
        first_record = service.wait(first, timeout=5)
        second = service.submit(self.snapshot)
        second_record = service.wait(second, timeout=5)

        self.assertEqual(first_record.status, RunStatus.succeeded)
        self.assertEqual(second_record.status, RunStatus.succeeded)
        self.assertEqual([item[0] for item in seen], [first, second])
        self.assertNotEqual(seen[0][1], seen[1][1])
        self.assertEqual(service.run_workspace(first).parent, service.workspace / "runs")
        self.assertEqual((service.run_workspace(first) / "same.txt").read_text(encoding="utf-8"), first)
        self.assertEqual((service.run_workspace(second) / "same.txt").read_text(encoding="utf-8"), second)

    def test_progress_is_queryable_and_finishes_at_one(self):
        progress_done = threading.Event()
        release = threading.Event()
        service = self.service([
            SyntheticAnalyzer(
                "progress",
                progress_values=(0.25, 0.5),
                progress_done=progress_done,
                release=release,
            )
        ])
        run_id = service.submit(self.snapshot)
        self.assertTrue(progress_done.wait(5))
        running = service.get(run_id)
        self.assertEqual(running.status, RunStatus.running)
        self.assertEqual(running.stages[0].progress, 0.5)
        release.set()
        finished = service.wait(run_id, timeout=5)
        self.assertEqual(finished.stages[0].progress, 1.0)
        self.assertEqual(finished.status, RunStatus.succeeded)

    def test_empty_registry_finishes_without_stages(self):
        service = self.service([])
        run_id = service.submit(self.snapshot)
        record = service.wait(run_id, timeout=5)
        self.assertEqual(record.status, RunStatus.succeeded)
        self.assertEqual(record.stages, ())
        self.assertEqual(record.diagnostics, ("no_analyzers",))

    def test_close_terminalizes_running_and_queued_runs(self):
        started = threading.Event()
        release = threading.Event()
        service = self.service(
            [SyntheticAnalyzer(
                "blocking",
                started=started,
                release=release,
                ignore_cancel=True,
            )],
            capacity=1,
        )
        first = service.submit(self.snapshot)
        self.assertTrue(started.wait(5))
        second = service.submit(self.snapshot)

        service.close(wait=False)
        self.assertEqual(service.get(first).status, RunStatus.interrupted)
        self.assertEqual(service.get(second).status, RunStatus.interrupted)
        self.assertEqual(service.wait(first, timeout=5).status, RunStatus.interrupted)
        self.assertEqual(service.wait(second, timeout=5).status, RunStatus.interrupted)

        release.set()
        service.close()
        self.assertEqual(service.get(first).status, RunStatus.interrupted)
        self.assertEqual(service.get(second).status, RunStatus.interrupted)

    def test_wait_times_out_while_run_is_running(self):
        started = threading.Event()
        release = threading.Event()
        service = self.service([
            SyntheticAnalyzer("blocking", started=started, release=release)
        ])
        run_id = service.submit(self.snapshot)
        self.assertTrue(started.wait(5))
        with self.assertRaises(TimeoutError):
            service.wait(run_id, timeout=0.01)
        release.set()
        self.assertEqual(service.wait(run_id, timeout=5).status, RunStatus.succeeded)

    def test_success_logs_do_not_echo_analyzer_output(self):
        secret = "TOPSECRET_RESULT_TEXT"
        service = self.service([
            SyntheticAnalyzer(
                "success",
                result=AnalysisResult(
                    output={"secret": secret},
                    evidence_ids=("message-1",),
                ),
            )
        ])
        with self.assertLogs("analytics.analysis_service", level="INFO") as captured:
            run_id = service.submit(self.snapshot)
            record = service.wait(run_id, timeout=5)
        self.assertEqual(record.status, RunStatus.succeeded)
        self.assertNotIn(secret, "\n".join(captured.output))

    def test_restart_marks_only_unfinished_runs_interrupted(self):
        workspace = self.root / "recovery"
        store = RunStore(workspace)
        now = datetime.now(timezone.utc)

        for run_id in ("queued", "running", "done"):
            store.create_run(self.manifest(run_id))
        self.assertTrue(store.mark_running("running", now))
        self.assertTrue(store.mark_running("done", now))
        self.assertTrue(store.start_stage("done", "manual", now))
        self.assertTrue(store.complete_stage_success(
            "done",
            "manual",
            AnalysisResult(output={"ok": True}, evidence_ids=("message-1",)),
            now,
        ))
        self.assertTrue(store.finish_run(
            "done",
            RunStatus.succeeded,
            diagnostics=(),
            artifacts=(),
            finished_at=now,
        ))
        done_before = store.get_run("done")

        service = AnalysisService(AnalyzerRegistry(()), workspace, store=store)
        self.addCleanup(service.close)
        self.assertEqual(service.recovered_runs, 2)

        self.assertEqual(store.get_run("queued").status, RunStatus.interrupted)
        self.assertEqual(store.get_run("running").status, RunStatus.interrupted)
        self.assertIn("recovered_interrupted", store.get_run("running").diagnostics)
        self.assertEqual(store.get_run("queued").stages[0].status, StageStatus.interrupted)
        self.assertEqual(store.get_run("running").stages[0].status, StageStatus.interrupted)
        done_after = store.get_run("done")
        self.assertEqual(done_after.status, RunStatus.succeeded)
        self.assertEqual(done_after.finished_at, done_before.finished_at)
        self.assertEqual(done_after.stages[0].status, StageStatus.succeeded)
        self.assertEqual(done_after.stages[0].output, done_before.stages[0].output)

    def test_persisted_data_and_logs_do_not_contain_chat_text(self):
        secret = "TOPSECRET_CHAT_TEXT"
        snapshot = make_snapshot(messages=[make_message(text=secret)])
        service = self.service([
            SyntheticAnalyzer("leak", failure=RuntimeError(secret)),
        ])
        with self.assertLogs("analytics.analysis_service", level="INFO") as captured:
            run_id = service.submit(snapshot)
            record = service.wait(run_id, timeout=5)
        self.assertEqual(record.status, RunStatus.failed)
        self.assertEqual(record.stages[0].error_type, "RuntimeError")
        self.assertNotIn(secret, "\n".join(captured.output))
        self.assertNotIn(secret, repr(record.to_dict()))
        for path in (self.root / "workspace").rglob("*"):
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes(), msg=str(path))


if __name__ == "__main__":
    unittest.main()
