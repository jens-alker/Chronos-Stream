"""
runner.py — der Runner des Test-/Integrations-Harness (Feinkonzept_SchichtS §3.0).

Führt Schritte in Dependency-Ordnung aus (Toposort über Ein-/Ausgangstabellen),
reicht die Tabellen durch, validiert JEDEN Ausgang gegen seinen Schicht-D-Kontrakt
und ist FAIL-CLOSED: verletzt ein Modul den Kontrakt, bricht die Pipeline ab, statt
still Müll weiterzureichen. Der Speicher ist append-only (nur `append`, kein Update).
Nur Standardbibliothek.
"""
from dataclasses import dataclass
from typing import List, Callable
from contracts import validate, TableContract
from store import MemStore, SqliteStore   # noqa: F401  (SqliteStore re-exportiert)

# Rückwärtskompatibler Alias: Default-Store ist In-Memory.
Store = MemStore


class ContractError(Exception):
    def __init__(self, step_name, violations):
        self.step_name = step_name
        self.violations = violations
        super().__init__(f"[{step_name}] Kontrakt verletzt:\n" +
                         "\n".join("  " + str(v) for v in violations))


@dataclass
class Step:
    name: str
    inputs: List[str]
    output: TableContract
    fn: Callable          # fn(inputs: dict[str, list[dict]], ctx) -> list[dict]


def _toposort(steps):
    produced = {s.output.name: s for s in steps}
    order, visited, temp = [], set(), set()

    def visit(s):
        if s.name in visited:
            return
        if s.name in temp:
            raise ValueError(f"Zyklus in der Pipeline bei '{s.name}'")
        temp.add(s.name)
        for inp in s.inputs:
            if inp in produced:
                visit(produced[inp])
        temp.discard(s.name)
        visited.add(s.name)
        order.append(s)

    for s in steps:
        visit(s)
    return order


class Pipeline:
    def __init__(self, steps):
        self.steps = steps

    def run(self, store=None, ctx=None):
        store = store or Store()
        for step in _toposort(self.steps):
            inputs = {t: store.get(t) for t in step.inputs}
            rows = step.fn(inputs, ctx)
            violations = validate(rows, step.output)
            if violations:
                raise ContractError(step.name, violations)   # fail-closed
            store.append(step.output.name, rows)
        return store
