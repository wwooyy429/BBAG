# BBAG project instructions

## Current authorization

- Historical owner-authorized merges are preserved: S0 f46fad2ff4b3ec9659001659fa44201e10951fd5 merged as b2555efe7201c473bdb66c7dd1c14552ad130ac5; S1 c56db883cd7841c94517ee9817d7ad551e811dc2 merged as 41789706f710e131d4a654e349798ddb15ea70f5. The owner accepted the independent Codex review source, whose report disclaims authenticated GPT Work identity.
- The joint re-review reaudit_joint_0889d277bfdd_20261031_1522 approved S0 at exact SHA 0889d277bfdda4894d7e359157d092a4f07639f5 and closed S0-004 and S0-N01. S1 remains CHANGES_REQUIRED / engineering FAIL due to new S1-002 (P2): the S1 packager did not compare the disk dependency lock with its exact Git commit. S1-001 remains closed; S0-N02 remains a non-blocking P3 suggestion. Preserve all prior reports and merge facts.
- Current scope is S1-002 on codex/s1-committed-lock-remediation, based on reviewed 0889d277, with CLI regressions, accurate current reports, and fresh SHA-bound S0/S1 verification and review packages. Preserve S0's accepted validation logic. Do not change formulas, labels, reference answers, dependencies or research parameters to fit tests. Preserve the separate S2 development branch and evidence without advancing S2 or issuing platform tasks during this repair.
- Work on a development branch. Do not push implementation directly to main, merge this repair, roll back main or enable auto-merge. The new exact implementation SHA requires independent re-review and owner authorization before merge or stage advancement.
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
