# GitHub PR release rules

Read this before invoking `core.release.ReleaseManager`.

## Preconditions

- Load the App ID, repository root, Git remote, GitHub owner/repository, base branch
  and reviewers from the trusted registry.
- Load the report only from the registry's evidence root by `run_id + cycle`.
  Verify its coordinator HMAC and bind it to App ID, GitHub repository, frozen
  policy digest and exact Skill digest map; an arbitrary report path is invalid.
- Recompute the workspace digest and require equality with the verified candidate.
- Require local `HEAD` to equal the current remote base before creating a branch.
- Require the complete dirty-file set to equal the caller's explicit
  `changed_files` allowlist. Reject every changed file matching the signed policy's
  `workspace_ignore`, because its bytes were excluded from the verified digest.
  Never stage `.` or use a wildcard.
- Reject repository-local hooks, filters, URL rewrites, credential helpers,
  fsmonitor, SSH command overrides, push URLs and related executable Git config.
  Git subprocesses use a minimal environment that excludes verification/GitHub
  secrets and force hooks/fsmonitor/signing off.
- Require `GITHUB_TOKEN` from the environment. Never place it in a repository file,
  prompt, log, report or command argument.

## Mutation order

1. Check `git diff --check`.
2. Confirm the target base branch exists remotely.
3. Confirm the planned fix branch is new, or that an interrupted prior attempt has
   the exact local/remote commit and authorized diff needed for idempotent resume.
4. Create `fix/<problem-slug>_<YYYYMMDD>_<sequence>`.
5. Stage the exact bytes of only the allowlisted paths through Git plumbing,
   bypassing repository clean filters, and create one configured commit.
6. Recheck that the worktree is clean and the Git commit-tree digest—not merely
   the filtered working tree—equals the verified candidate.
7. Push only the fix branch. For HTTPS, pass the token through a temporary
   askpass helper, never through argv or the candidate environment.
8. Create or locate the matching open GitHub PR.
9. Request every configured reviewer.
10. Persist the PR receipt bound to the verification run.

If a failure happens after a branch, commit, push or PR is created, report the exact
partial state and retry idempotently. Do not delete or force-push automatically.

Run only after the trusted Coordinator has persisted its attestation. The standalone
verification CLI deliberately refuses to sign a `VERIFIED` result:

```bash
export LOOP_ENGINEER_VERIFICATION_SIGNING_KEY='<same coordinator-only key>'
export GITHUB_TOKEN='<fine-grained token>'
uv run python -m core.release --registry applications.json --request release.json
```

## Hard prohibitions

- Never push directly to `main`, `master`, `develop`, or the configured base.
- Never create a PR from a non-VERIFIED report or a changed candidate.
- Never silently include unrelated dirty files.
- Never auto-merge the PR or publish production. Human approval remains the final
  production boundary from the article.

The article uses `develop`. The current CCB remote exposes `main` and no `develop`, so
the local registry example targets `main`. This is an explicit environment-driven
difference, not a reinterpretation of the article.
