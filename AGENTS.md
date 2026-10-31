# BBAG project instructions

## Current authorization

- The user accepted S0 and explicitly authorized its merge and S1 on 2026-10-31. Reviewed S0 SHA: f46fad2ff4b3ec9659001659fa44201e10951fd5; merge-result SHA: b2555efe7201c473bdb66c7dd1c14552ad130ac5. The independent review source was verified as a separate Codex audit chat; its report disclaims authenticated GPT Work identity, and the project owner accepted it.
- Implement S1 only: deterministic factor operators, restricted expression audit, separate forward labels, synthetic typed examples, verification and a commit-bound review package. Do not implement alpha evaluation, QuantConnect integrations, GPT calls, backtests, optimization or deployment, or begin S2.
- Independent S1 review of f5c07d2b878c3b7e9f122bcf064ef45589d9aaa7 returned CHANGES_REQUIRED / engineering FAIL with blocking S1-001 (label quote field versus open execution event). Preserve that result; repair on this development branch and submit a new SHA for independent re-review. S0 acceptance is unchanged.
- Work on a development branch. Do not push implementation directly to main, merge S1 or enable auto-merge until independent review of its exact SHA passes and the user explicitly authorizes that action.
- Developer self-tests and collaborator checks are not the independent GPT Work acceptance decision. Keep external review PENDING until genuine evidence is supplied.

## Engineering requirements

- Read docs/ROADMAP.md, docs/ACCEPTANCE.md and docs/FLOW_ACCEPTANCE.md. Preserve S6.4 revalidation, final-test isolation and fixed versus rolling-model rules.
- Use Python 3.11 or later, src/bbag, explicit typed contracts and machine-readable JSON examples. Reject extra keys, invalid types, invalid ranges and inconsistent time boundaries.
- Examples are ENGINEERING_TEST and DRAFT. Explicitly identify unresolved research parameters; configuration validation is not economic approval or runtime access control.
- Retain meaningful S0 regressions and add S1 hand-calculated, causal, prefix, batch/incremental and label-maturity checks. Store raw test evidence with the reviewed commit. Never fabricate platform results, review approval, task IDs or economic conclusions.
- Generated artifacts, virtual environments, credentials and unredacted final-test data do not belong in Git. Keep legacy handoff snapshots clearly identified as historical planning material.

## Per-development handoff

- Commit the source, tests, schemas and maintained documentation on the development branch. Generate the delivery report and audit request after committing, so both bind the exact SHA without a self-referential commit.
- Each audit package must contain the exact committed source, dependency lock, examples, raw verification output, reproduction commands, implementation SHA, file hashes and an unfilled independent review template.
- Store generated per-SHA packages outside tracked source. Code changes after review invalidate the affected acceptance items and require a new commit and review package.
- Any local review-receipt checker is an advisory consistency check; it does not authenticate reviewers or grant merge permission. No automatic merge or stage advancement is allowed.
