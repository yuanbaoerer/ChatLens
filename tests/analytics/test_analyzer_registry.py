import unittest

from analytics.analyzer import AnalyzerRegistry, AnalyzerRegistryError, default_registry
from tests.analytics.analysis_fakes import SyntheticAnalyzer


class AnalyzerRegistryTests(unittest.TestCase):
    def test_valid_dependencies_run_in_deterministic_topological_order(self):
        registry = AnalyzerRegistry([
            SyntheticAnalyzer("gamma", prerequisites=("beta",)),
            SyntheticAnalyzer("alpha"),
            SyntheticAnalyzer("delta"),
            SyntheticAnalyzer("beta", prerequisites=("alpha",)),
        ])
        self.assertEqual(
            [analyzer.analyzer_id for analyzer in registry.plan()],
            ["alpha", "beta", "delta", "gamma"],
        )
        self.assertEqual(registry.plan(), registry.plan())
        self.assertIs(registry.get("beta"), registry.plan()[1])
        self.assertEqual(len(registry), 4)

    def test_duplicate_ids_are_rejected_before_execution(self):
        with self.assertRaises(AnalyzerRegistryError) as caught:
            AnalyzerRegistry([
                SyntheticAnalyzer("duplicate"),
                SyntheticAnalyzer("duplicate"),
            ])
        self.assertEqual(caught.exception.code, "duplicate_analyzer_id")
        self.assertEqual(caught.exception.analyzer_ids, ("duplicate",))

    def test_unknown_dependency_is_rejected_before_execution(self):
        with self.assertRaises(AnalyzerRegistryError) as caught:
            AnalyzerRegistry([SyntheticAnalyzer("stage", prerequisites=("missing",))])
        self.assertEqual(caught.exception.code, "unknown_dependency")
        self.assertIn("missing", str(caught.exception))
        self.assertEqual(caught.exception.analyzer_ids, ("stage",))

    def test_dependency_cycle_is_rejected_before_execution(self):
        with self.assertRaises(AnalyzerRegistryError) as caught:
            AnalyzerRegistry([
                SyntheticAnalyzer("alpha", prerequisites=("beta",)),
                SyntheticAnalyzer("beta", prerequisites=("alpha",)),
                SyntheticAnalyzer("gamma"),
            ])
        self.assertEqual(caught.exception.code, "dependency_cycle")
        self.assertEqual(caught.exception.analyzer_ids, ("alpha", "beta"))

    def test_invalid_metadata_is_rejected(self):
        with self.assertRaises(AnalyzerRegistryError) as caught:
            AnalyzerRegistry([SyntheticAnalyzer("stage", estimated_workload=-1)])
        self.assertEqual(caught.exception.code, "invalid_estimated_workload")

        with self.assertRaises(AnalyzerRegistryError) as caught:
            AnalyzerRegistry([SyntheticAnalyzer("stage", version="")])
        self.assertEqual(caught.exception.code, "invalid_analyzer_version")

        with self.assertRaises(AnalyzerRegistryError) as caught:
            AnalyzerRegistry([SyntheticAnalyzer("stage", prerequisites=("same", "same"))])
        self.assertEqual(caught.exception.code, "duplicate_prerequisite")

    def test_production_registry_is_empty_until_analyzers_are_implemented(self):
        registry = default_registry()
        self.assertEqual(registry.plan(), ())
        self.assertEqual(tuple(registry), ())


if __name__ == "__main__":
    unittest.main()
