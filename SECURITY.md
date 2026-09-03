# Security

## Reporting a vulnerability

Please report security issues **privately**, not as a public issue:

- GitHub → **Security** → *Report a vulnerability* (private advisory), or
- email **jens@alker.org**

Useful in a report: what the issue is, how to reproduce it, and what an attacker gains. A
proof-of-concept is welcome but not required.

## What this project can and cannot promise

Chronos-Stream is maintained by **one person, unpaid, alongside other work**. This section states
plainly what that means, because a promise this project cannot keep would be worse for you than
no promise at all:

- **No response time is guaranteed.** Reports are read and handled as capacity allows. If a
  report is urgent for you, say so — it will not create an obligation, but it will help with
  prioritisation.
- **No commitment to fix.** A confirmed issue may be fixed, documented as a known limitation, or
  left open. Which of the three happens depends on severity and on available time.
- **Only the current release is considered.** There is no backporting to earlier versions and no
  table of supported versions, because maintaining one would be a commitment this project cannot
  honour.
- **No bug bounty**, and no compensation of any kind.

None of this is a reason to withhold a report. It is a reason not to plan around a response.

## Disclosure

Please give a reasonable window before publishing — 90 days is the usual convention and is
appreciated here. That is a request, not a condition: this project claims no right to delay your
disclosure, and reporting an issue creates no obligation on your side.

If a fix is released, the advisory will credit the reporter unless anonymity is requested.

## Scope

In scope: the library and its command-line tools — the contract validator, the store, the
pipeline runner, the recording layer, the connector transformations, and anything that parses
input from outside the process.

Out of scope: the third-party services the connectors talk to (report those to their operators),
and the security of any deployment that embeds this library — its configuration, credentials and
operating environment are the deployer's responsibility.

Input from untrusted sources is an explicit concern of this project: the parsers and the
validator are meant to be safe to point at a file of unknown origin. Findings in that area are
particularly welcome.
