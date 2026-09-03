# Contributing

Contributions are welcome. Please read the next section first — it is short, and it is the part
most contribution guides leave out.

## What to expect

Chronos-Stream is maintained by **one person, unpaid, alongside other work**. So:

- **A pull request may go unreviewed, or be reviewed slowly.** No turnaround is promised. If a
  change matters to you and nothing happens, a friendly ping on the pull request is welcome.
- **A pull request may be declined**, including one that works. The most common reason will be
  scope: this project is deliberately narrow, and a correct change that widens it is still a
  change that has to be maintained afterwards.
- **A fork is a legitimate outcome.** The licence is MIT precisely so that you are never stuck
  waiting. If your change is right for you and not for this project, take it and go.

None of this is discouragement. It is what a single-maintainer project can honestly say, stated
up front instead of discovered after two weeks of silence.

## Before you write code

**Open an issue first** for anything beyond a small fix. A short description of the problem you
hit is more useful than a finished patch for a problem this project does not have — and it costs
you nothing if the answer is "out of scope".

Especially worth asking first: anything that adds a runtime dependency. The trusted core uses
only the Python standard library, because a component whose purpose is trustworthiness pays for
every dependency in auditability. That is a design constraint, not an oversight.

## What makes a change easy to accept

- **A test that fails before your change and passes after it.** This matters more here than in
  most projects: the guarantees are the product, so a change without a test is a claim without
  evidence.
- **Check that your test actually bites.** Take your own change back out and confirm the test
  goes red. A test that passes either way is worse than no test, because it makes a claim nobody
  re-examines. Every test of the integrity core in this repository was checked this way.
- **Offline and keyless.** The suite runs without network access and without API keys, and it
  stays that way. Test a connector against a recorded payload, never against the live service.
- **One thing at a time.** A small pull request that does one thing will be looked at long before
  a large one that does five.

## Running the tests

```bash
for t in $(find System -name 'test_*.py' | sort); do
  (cd "$(dirname "$t")" && python3 "$(basename "$t")") || echo "FAILED: $t"
done
```

Each test file is standalone and can be run on its own. No test runner, no configuration, no
third-party packages.

## Style

Match the file you are editing. There is no linter configuration to satisfy and no formatter to
run — the existing code is the specification of the style.

Comments explain *why*, not *what*. A comment that restates the code adds a second thing to keep
in sync; one that records the reason for a non-obvious decision saves the next reader an
investigation.

## Reporting bugs

Include what you did, what you expected, and what happened. For a data-integrity issue, a minimal
dataset that reproduces it is worth more than any description — and if that dataset is small and
redistributable, it may end up in the conformance corpus.

Security issues go to [SECURITY.md](SECURITY.md), not to the issue tracker.

## Licence

Contributions are accepted under the [MIT licence](LICENSE), the same terms as the rest of the
project. There is no contributor licence agreement to sign.
