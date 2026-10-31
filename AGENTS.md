# BBAG project instructions

## Current authorization

- The user accepted S0 and explicitly authorized its merge and S1 on 2026-10-31. Reviewed S0 SHA: f46fad2ff4b3ec9659001659fa44201e10951fd5; merge-result SHA: b2555efe7201c473bdb66c7dd1c14552ad130ac5. The independent review source was verified as a separate Codex audit chat; its report disclaims authenticated GPT Work identity, and the project owner accepted it.
- The user explicitly accepted the independent Codex S1 re-audit of c56db883cd7841c94517ee9817d7ad551e811dc2 (APPROVED, engineering PASS, S1-001 closed), accepted its stated unauthenticated GPT Work identity boundary, and authorized S1 merge and S2. PR #2 was merged as 41789706f710e131d4a654e349798ddb15ea70f5, with the reviewed source tree unchanged. Preserve the original f5c07d2b878c3b7e9f122bcf064ef45589d9aaa7 CHANGES_REQUIRED / FAIL history and the unchanged S0 acceptance.
- Implement S2 only: platform metadata and task adapters, durable task/budget/recovery records, point-in-time data/identity/corporate-action contracts, historical universe and quality checks, guarded data access, and exact-version evidence. Do not begin S3 alpha evaluation, model calls, strategy backtests, optimization or deployment. Bounded platform engineering checks need an explicit data/date/asset/budget scope; S0/S1 demonstration dates and zero budgets do not authorize real queries.
- Work on a development branch. Do not push implementation directly to main, merge S2 or enable auto-merge until independent review of its exact SHA passes and the user explicitly authorizes that action.
- Developer self-tests and collaborator checks are not the independent GPT Work acceptance decision. Keep external review PENDING until genuine evidence is supplied.

## Engineering requirements

- Read docs/ROADMAP.md, docs/ACCEPTANCE.md and docs/FLOW_ACCEPTANCE.md. Preserve S6.4 revalidation, final-test isolation and fixed versus rolling-model rules.
- Use Python 3.11 or later, src/bbag, explicit typed contracts and machine-readable JSON examples. Reject extra keys, invalid types, invalid ranges and inconsistent time boundaries.
- Examples are ENGINEERING_TEST and DRAFT. Explicitly identify unresolved research parameters; configuration validation is not economic approval or runtime access control.
- Retain S0/S1 regressions and add S2 access-before-IO, historical identity/universe, raw/corporate-action semantics, quality, durable task recovery and accounting checks. Store raw test evidence with the reviewed commit. Never fabricate platform results, review approval, task IDs or economic conclusions.
- QuantConnect Cloud dataset values and reconstructable derivatives stay in Cloud. Upload readable source and perform assertions there; return only allowlisted non-reconstructable control/check metadata. Local fixtures and handoffs use synthetic values. No price logging, raw Cloud exports, screenshots/OCR, encoded extraction, credential logging or unlicensed dataset redistribution. Record this provider boundary and the remaining operator/server permission boundary honestly.
- Generated artifacts, virtual environments, credentials and unredacted final-test data do not belong in Git. Keep legacy handoff snapshots clearly identified as historical planning material.

## Per-development handoff

- Commit the source, tests, schemas and maintained documentation on the development branch. Generate the delivery report and audit request after committing, so both bind the exact SHA without a self-referential commit.
- Each audit package must contain the exact committed source, dependency lock, examples, raw verification output, reproduction commands, implementation SHA, file hashes and an unfilled independent review template.
- Store generated per-SHA packages outside tracked source. Code changes after review invalidate the affected acceptance items and require a new commit and review package.
- Any local review-receipt checker is an advisory consistency check; it does not authenticate reviewers or grant merge permission. No automatic merge or stage advancement is allowed.
