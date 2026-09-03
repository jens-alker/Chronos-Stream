"""
test_runner.py — the runner is where "fail-closed" is either true or decorative.

A pipeline that keeps going after a producer breaks its contract does not lose one table; it
hands the bad rows to every consumer downstream, which then look healthy. These tests pin the
abort, and — more importantly — pin that nothing of the offending step reaches the store.

Run offline, no network, standard library only:
  python3 System/harness/tests/test_runner.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from contracts import Field, TableContract                                   # noqa: E402
from runner import ContractError, Pipeline, Step, Store                      # noqa: E402


def _contract(name, felder=()):
    """An operational contract: t_ingest plus whatever the test needs."""
    return TableContract(
        name=name, art="operativ",
        fields=[Field("t_ingest", "time"), *felder], key=["t_ingest"])


def _step(name, inputs, output, rows):
    return Step(name=name, inputs=list(inputs), output=output,
                fn=lambda eingaben, ctx, _r=rows: list(_r))


class TestExecutionOrder(unittest.TestCase):

    def test_a_step_runs_after_the_step_that_feeds_it(self):
        gesehen = []

        def erst(eingaben, ctx):
            gesehen.append("erst")
            return [{"t_ingest": "2026-01-01"}]

        def dann(eingaben, ctx):
            gesehen.append("dann")
            # The dependency is only real if the input has actually arrived.
            self.assertEqual(len(eingaben["quelle"]), 1)
            return [{"t_ingest": "2026-01-02"}]

        pipeline = Pipeline([
            Step("dann", ["quelle"], _contract("ziel"), dann),      # declared first on purpose
            Step("erst", [], _contract("quelle"), erst),
        ])
        pipeline.run()
        self.assertEqual(gesehen, ["erst", "dann"])

    def test_a_cycle_is_reported_instead_of_looping(self):
        pipeline = Pipeline([
            _step("a", ["b"], _contract("a"), [{"t_ingest": "2026-01-01"}]),
            _step("b", ["a"], _contract("b"), [{"t_ingest": "2026-01-01"}]),
        ])
        with self.assertRaises(ValueError) as ctx:
            pipeline.run()
        self.assertIn("Zyklus", str(ctx.exception))

    def test_an_input_nobody_produces_is_passed_through_as_empty(self):
        # External inputs are normal — the runner must not invent a producer for them.
        def schritt(eingaben, ctx):
            self.assertEqual(eingaben["von_aussen"], [])
            return [{"t_ingest": "2026-01-01"}]

        Pipeline([Step("s", ["von_aussen"], _contract("z"), schritt)]).run()


class TestFailClosed(unittest.TestCase):

    def test_a_contract_violation_aborts_the_run(self):
        schlecht = _step("schlecht", [], _contract("obs"),
                         [{"t_ingest": None}])                 # missing the one required role
        with self.assertRaises(ContractError):
            Pipeline([schlecht]).run()

    def test_the_error_names_the_step_and_carries_the_violations(self):
        # An abort that does not say which producer broke what is a stack trace, not a diagnosis.
        schlecht = _step("der_schuldige", [], _contract("obs"), [{"t_ingest": None}])
        with self.assertRaises(ContractError) as ctx:
            Pipeline([schlecht]).run()
        self.assertEqual(ctx.exception.step_name, "der_schuldige")
        self.assertTrue(ctx.exception.violations)
        self.assertIn("der_schuldige", str(ctx.exception))

    def test_rows_of_the_failing_step_are_not_stored(self):
        # The whole point: bad rows must not be visible to anyone afterwards. Validation runs
        # BEFORE the append, so a caught error leaves no trace to clean up.
        store = Store()
        schlecht = _step("schlecht", [], _contract("obs"), [{"t_ingest": None}])
        with self.assertRaises(ContractError):
            Pipeline([schlecht]).run(store=store)
        self.assertEqual(store.get("obs"), [])

    def test_downstream_steps_do_not_run_after_an_abort(self):
        nachher = []
        schlecht = _step("schlecht", [], _contract("obs"), [{"t_ingest": None}])
        folge = Step("folge", ["obs"], _contract("weiter"),
                     lambda eingaben, ctx: nachher.append(1) or [{"t_ingest": "2026-01-01"}])
        with self.assertRaises(ContractError):
            Pipeline([schlecht, folge]).run()
        self.assertEqual(nachher, [], "a consumer ran on rows that never passed their contract")


class TestSuccessfulRun(unittest.TestCase):

    def test_valid_rows_are_stored_and_handed_on(self):
        # The counter-probe: without it, every fail-closed test above could be green because
        # the pipeline never runs anything at all.
        store = Pipeline([
            _step("erzeuger", [], _contract("quelle", [Field("wert", "gemessen")]),
                  [{"t_ingest": "2026-01-01", "wert": 7}]),
            Step("verbraucher", ["quelle"], _contract("ziel", [Field("summe", "gemessen")]),
                 lambda eingaben, ctx: [{"t_ingest": "2026-01-02",
                                         "summe": sum(r["wert"] for r in eingaben["quelle"])}]),
        ]).run()
        self.assertEqual(store.get("quelle"), [{"t_ingest": "2026-01-01", "wert": 7}])
        self.assertEqual(store.get("ziel")[0]["summe"], 7)

    def test_the_context_object_reaches_every_step(self):
        gesehen = []
        Pipeline([_step("s", [], _contract("z"), [{"t_ingest": "2026-01-01"}])]).run(ctx=None)
        Pipeline([Step("s", [], _contract("z"),
                       lambda eingaben, ctx: gesehen.append(ctx) or [{"t_ingest": "2026-01-01"}])
                  ]).run(ctx={"as_of": "2026-01-01"})
        self.assertEqual(gesehen, [{"as_of": "2026-01-01"}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
