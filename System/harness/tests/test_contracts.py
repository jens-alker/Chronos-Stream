"""
test_contracts.py — the contract validator is the point where a temporal guarantee stops being
an intention and becomes a check. These tests pin that behaviour.

Each test names the invariant it defends, because a validator whose own tests are decorative is
worse than none: it makes a claim nobody re-examines.

Run offline, no network, standard library only:
  python3 System/harness/tests/test_contracts.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from contracts import (                                                      # noqa: E402
    Field, TableContract, validate, TIME_REQUIREMENTS, STATUS, ORDINAL_STAERKE,
)


def _rules(violations):
    """The rule names that fired — assertions read better against these than against messages."""
    return sorted(v.rule for v in violations)


class TestTemporalRolesAreMandatory(unittest.TestCase):
    """The core promise: a row without its time roles is not storable, whatever else is right."""

    def test_every_declared_table_type_has_a_time_requirement(self):
        # The map IS the specification of which roles each table type owes. If a type were added
        # without one, rows of that type would silently need no time at all.
        self.assertEqual(
            set(TIME_REQUIREMENTS),
            {"beobachtung", "projektion", "referenz", "walkforward", "operativ"})
        for art, required in TIME_REQUIREMENTS.items():
            self.assertTrue(required, f"table type {art!r} requires no time column at all")
            self.assertIn("t_ingest", required,
                          f"{art!r} does not record when the system learned the fact")

    def test_observation_requires_event_disclosure_and_ingestion(self):
        # The three roles that make look-ahead detectable: when it happened, when it became
        # public, when we took it in. Dropping any one of them makes a cutoff uncheckable.
        self.assertEqual(TIME_REQUIREMENTS["beobachtung"],
                         {"t_event", "t_disclosed", "t_ingest"})

    def test_row_missing_a_time_value_is_rejected(self):
        contract = TableContract(
            name="observation", art="beobachtung",
            fields=[Field("t_event", "time"), Field("t_disclosed", "time"),
                    Field("t_ingest", "time"), Field("wert", "gemessen")],
            key=["t_ingest"])
        rows = [{"t_event": "2026-01-02", "t_disclosed": None,
                 "t_ingest": "2026-01-05", "wert": 1.0}]
        self.assertIn("zeit-spalte-fehlt", _rules(validate(rows, contract)))

    def test_empty_string_counts_as_missing_time(self):
        # "" is not a timestamp. Treating it as present is how a blank date slips through and
        # becomes an unbounded as-of window.
        contract = TableContract(
            name="observation", art="beobachtung",
            fields=[Field("t_event", "time"), Field("t_disclosed", "time"),
                    Field("t_ingest", "time")],
            key=["t_ingest"])
        rows = [{"t_event": "", "t_disclosed": "2026-01-03", "t_ingest": "2026-01-05"}]
        self.assertIn("zeit-spalte-fehlt", _rules(validate(rows, contract)))

    def test_contract_that_omits_a_required_time_column_is_itself_rejected(self):
        # The validator checks the contract before the rows: a contract that never declares
        # t_disclosed would let every row pass while measuring nothing.
        contract = TableContract(
            name="observation", art="beobachtung",
            fields=[Field("t_event", "time"), Field("t_ingest", "time")],
            key=["t_ingest"])
        viol = validate([], contract)
        self.assertIn("zeit-spalte-fehlt-im-kontrakt", _rules(viol))
        self.assertEqual([v.field for v in viol], ["t_disclosed"])

    def test_unknown_table_type_stops_validation_instead_of_passing(self):
        # Fail-closed: an unrecognised type must not mean "no requirements apply".
        contract = TableContract(name="x", art="erfunden",
                                 fields=[Field("t_ingest", "time")], key=[])
        viol = validate([{"anything": 1}], contract)
        self.assertEqual(_rules(viol), ["unbekannter-tabellen-typ"])


class TestForbiddenAndPairedFields(unittest.TestCase):

    def test_forbidden_field_is_caught_in_contract_and_in_rows(self):
        contract = TableContract(
            name="edge", art="operativ",
            fields=[Field("t_ingest", "time"), Field("knoten_id", "id")], key=["t_ingest"])
        viol = validate([{"t_ingest": "2026-01-01", "knoten_id": "k1"}], contract)
        # Once for the declaration, once for the row — the declaration alone would let a
        # producer that never fills the field look clean.
        self.assertEqual(_rules(viol).count("verbotenes-feld"), 2)

    def test_category_id_without_version_is_rejected(self):
        # A node is (kat_id, version). An id without its version silently pins to "latest",
        # which is a look-ahead in disguise.
        contract = TableContract(name="obs", art="operativ",
                                 fields=[Field("t_ingest", "time")], key=["t_ingest"])
        viol = validate([{"t_ingest": "2026-01-01", "kat_id": "energy"}], contract)
        self.assertIn("id-version-paarung", _rules(viol))

    def test_category_id_with_version_passes(self):
        contract = TableContract(name="obs", art="operativ",
                                 fields=[Field("t_ingest", "time")], key=["t_ingest"])
        self.assertEqual(
            validate([{"t_ingest": "2026-01-01", "kat_id": "energy", "version": 3}], contract), [])


class TestJudgedFieldsStayOrdinal(unittest.TestCase):
    """Convention 3.12: judged things are ordinal, measured things are numeric — never mixed."""

    def test_ordinal_field_carrying_a_number_is_rejected(self):
        contract = TableContract(
            name="obs", art="operativ",
            fields=[Field("t_ingest", "time"), Field("staerke", "ordinal", enum=ORDINAL_STAERKE)],
            key=["t_ingest"])
        viol = validate([{"t_ingest": "2026-01-01", "staerke": 0.73}], contract)
        self.assertIn("ordinal-als-zahl", _rules(viol))

    def test_boolean_is_not_treated_as_a_number(self):
        # bool is a subclass of int in Python; a naive check would call True a decimal
        # confidence and fire the wrong rule.
        contract = TableContract(
            name="obs", art="operativ",
            fields=[Field("t_ingest", "time"), Field("flag", "bool")], key=["t_ingest"])
        self.assertEqual(validate([{"t_ingest": "2026-01-01", "flag": True}], contract), [])

    def test_value_outside_the_vocabulary_is_rejected(self):
        contract = TableContract(
            name="obs", art="operativ",
            fields=[Field("t_ingest", "time"), Field("status", "status")], key=["t_ingest"])
        viol = validate([{"t_ingest": "2026-01-01", "status": "ziemlich sicher"}], contract)
        self.assertIn("wert-nicht-im-vokabular", _rules(viol))

    def test_each_status_of_the_vocabulary_is_accepted(self):
        # Guards against a vocabulary that drifts out from under its own validator.
        contract = TableContract(
            name="obs", art="operativ",
            fields=[Field("t_ingest", "time"), Field("status", "status")], key=["t_ingest"])
        for wert in sorted(STATUS):
            with self.subTest(status=wert):
                self.assertEqual(
                    validate([{"t_ingest": "2026-01-01", "status": wert}], contract), [])

    def test_measured_field_must_be_numeric(self):
        contract = TableContract(
            name="obs", art="operativ",
            fields=[Field("t_ingest", "time"), Field("wert", "gemessen")], key=["t_ingest"])
        viol = validate([{"t_ingest": "2026-01-01", "wert": "hoch"}], contract)
        self.assertIn("gemessen-nicht-numerisch", _rules(viol))


class TestMandatoryFields(unittest.TestCase):
    """"Contract-valid" has to mean complete, or it is not a guarantee about the row."""

    def test_absent_non_nullable_field_is_rejected(self):
        contract = TableContract(
            name="obs", art="operativ",
            fields=[Field("t_ingest", "time"), Field("quelle", "text")], key=["t_ingest"])
        viol = validate([{"t_ingest": "2026-01-01"}], contract)
        self.assertIn("pflichtfeld-fehlt-oder-null", _rules(viol))

    def test_empty_string_is_not_a_value(self):
        contract = TableContract(
            name="obs", art="operativ",
            fields=[Field("t_ingest", "time"), Field("quelle", "text")], key=["t_ingest"])
        viol = validate([{"t_ingest": "2026-01-01", "quelle": ""}], contract)
        self.assertIn("pflichtfeld-fehlt-oder-null", _rules(viol))

    def test_nullable_field_may_be_absent(self):
        contract = TableContract(
            name="obs", art="operativ",
            fields=[Field("t_ingest", "time"), Field("notiz", "text", nullable=True)],
            key=["t_ingest"])
        self.assertEqual(validate([{"t_ingest": "2026-01-01"}], contract), [])

    def test_empty_list_and_false_are_values(self):
        # [] and False are legitimate contents; rejecting them would force producers to
        # invent placeholder data, which is the opposite of what the contract is for.
        contract = TableContract(
            name="obs", art="operativ",
            fields=[Field("t_ingest", "time"), Field("spuren", "list"), Field("aktiv", "bool")],
            key=["t_ingest"])
        rows = [{"t_ingest": "2026-01-01", "spuren": [], "aktiv": False}]
        self.assertEqual(validate(rows, contract), [])

    def test_a_fully_valid_row_produces_no_violations(self):
        # The counter-probe to every test above: if this failed, the others could be green
        # for the wrong reason.
        contract = TableContract(
            name="observation", art="beobachtung",
            fields=[Field("t_event", "time"), Field("t_disclosed", "time"),
                    Field("t_ingest", "time"), Field("status", "status"),
                    Field("wert", "gemessen"), Field("notiz", "text", nullable=True)],
            key=["t_ingest"])
        rows = [{"t_event": "2026-01-02", "t_disclosed": "2026-01-03",
                 "t_ingest": "2026-01-05", "status": "beobachtet", "wert": 42}]
        self.assertEqual(validate(rows, contract), [])


class TestViolationReporting(unittest.TestCase):

    def test_violation_names_table_row_and_field(self):
        # A verdict nobody can locate is not actionable; the row index is the whole point.
        contract = TableContract(
            name="observation", art="operativ",
            fields=[Field("t_ingest", "time"), Field("wert", "gemessen")], key=["t_ingest"])
        rows = [{"t_ingest": "2026-01-01", "wert": 1},
                {"t_ingest": "2026-01-01", "wert": "nope"}]
        viol = validate(rows, contract)
        self.assertEqual(len(viol), 1)
        self.assertEqual(viol[0].row_index, 1)
        self.assertIn("observation[1].wert", str(viol[0]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
