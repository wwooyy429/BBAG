# BBAG project instructions

## Current authorization

- Implement stage S0 only. Do not implement factor operators, alpha evaluation, QuantConnect integrations, GPT calls, backtests, optimization or deployment in this stage.
- Work on a development branch. Do not push implementation directly to main, merge a pull request, enable auto-merge or begin S1 until an independent GPT Work review of the exact implementation commit has passed and the user authorizes the next scope.
- Developer self-tests and collaborator checks are not the independent GPT Work acceptance decision. Keep external review PENDING until genuine evidence is supplied.

## Engineering requirements

- Read docs/ROADMAP.md, docs/ACCEPTANCE.md and docs/FLOW_ACCEPTANCE.md. Preserve S6.4 revalidation, final-test isolation and fixed versus rolling-model rules.
- Use Python 3.11 or later, src/bbag, explicit typed contracts and machine-readable JSON examples. Reject extra keys, invalid types, invalid ranges and inconsistent time boundaries.
- Examples are ENGINEERING_TEST and DRAFT. Explicitly identify unresolved research parameters; configuration validation is not economic approval or runtime access control.
- Run meaningful S0 contract, CLI and provenance tests. Store raw test evidence with the reviewed commit. Never fabricate platform results, review approval, task IDs or economic conclusions.
- Generated artifacts, virtual environments, credentials and unredacted final-test data do not belong in Git. Keep legacy handoff snapshots clearly identified as historical planning material.

## Per-development handoff

- Commit the source, tests, schemas and maintained documentation on the development branch. Generate the delivery report and audit request after committing, so both bind the exact SHA without a self-referential commit.
- Each audit package must contain the exact committed source, dependency lock, examples, raw verification output, reproduction commands, implementation SHA, file hashes and an unfilled independent review template.
- Store generated per-SHA packages outside tracked source. Code changes after review invalidate the affected acceptance items and require a new commit and review package.
- Any local review-receipt checker is an advisory consistency check; it does not authenticate reviewers or grant merge permission. No automatic merge or stage advancement is allowed.
