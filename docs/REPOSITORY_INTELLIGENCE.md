# Repository Intelligence — current scope

Repository intelligence is an early workstream. It adds a provider boundary and GitHub integration that can connect public or organization-token-authorized private repositories to a project, inspect a pinned default-branch tree and supported dependency manifests, summarize static direct dependency names and likely frameworks, list branches, search paths and bounded text content, inspect pull requests, and perform bounded branch and commit operations.

## API

See the [Repository Intelligence section in API.md](../API.md#repository-intelligence) for routes and response limits.

## Boundaries

- Callers must have a user JWT and organization membership in the organization that owns the project. Connecting a repository also requires owner or admin membership. This API follows the current foundation API's organization-wide project access model; project-specific membership enforcement is not implemented yet.
- Public repository reads use GitHub's unauthenticated API. Private repository reads use the encrypted organization GitHub token only when the repository is linked to that active integration. An owner or admin connects the token; it is validated before storage, encrypted with `INTEGRATION_ENCRYPTION_KEYS`, and never returned by the API. With no encryption keys configured, credential connection is disabled. Disconnecting the integration disables access to linked private repositories.
- The GitHub client uses a fixed `api.github.com` origin, validates `owner/repository` names, uses bounded timeouts, rejects truncated or oversized trees, caps manifest reads, and returns provider errors without reflecting upstream response bodies.
- Inspection includes existing root context files plus bounded dependency-manifest discovery at the root and one directory below. Root context files take priority; discovery visits at most 25 candidates, reads at most 64 KB per file and 512 KB total, and reports partial coverage. Supported static names cover npm, Python, Go, Rust, Maven, Gradle, Ruby, and Composer. It does not resolve versions, scan for vulnerabilities, or identify available updates.
- Repository contents and metadata are untrusted. A teammate run may include a bounded summary of one explicitly selected, connected GitHub repository in its user-level prompt; a fixed system instruction says to treat repository material as evidence and never follow its instructions. The credential remains in the API process and is never placed in prompts or workspaces.
- Path search matches path names only. The separate content-search endpoint performs case-insensitive literal search against up to 25 path-sorted regular blobs at a pinned default-branch commit, restricted to supported text/code extensions. It reads at most 64 KB per file and 1 MB total, returns at most 20 matches with 500-character snippets, caps its JSON response at 16 KB, and stops at 45 seconds. Unsupported extensions are outside the search scope; oversized, binary, and invalid UTF-8 candidate blobs are skipped. `truncated`, scan counts, and byte counts make incomplete coverage visible. Results include the commit revision and tree SHA and are marked `untrusted_repository_data`.
- Solvyn can create `solvyn/...` branches, commit bounded UTF-8 text changes and selected-file deletions with optimistic branch-head checks, and open draft PRs using the organization token. Generated changes remain subject to human review in GitHub; Solvyn does not mark PRs ready or merge them.
- Branch listing returns at most 100 results. `possibly_truncated` signals that more results may exist.
- Pull request listing is a paginated live snapshot from GitHub, not a persisted local synchronization. Detail inspection returns the latest review decision per reviewer (scanning up to 1,100 reviews and probing for more), plus a unified diff capped at 1 MB. Pull request data and diffs remain untrusted input.

Repository discovery beyond a caller-supplied GitHub name and branch/worktree management remain future work. Private access is limited to repositories the connected organization token can read; this is not a GitHub App installation with per-repository permission selection.

## Teammate repository context

`POST /api/v1/teammates/{teammate_id}/run` accepts an optional `repository_id` alongside `project_id` and `input_data`, plus up to four optional relative `repository_paths`. When supplied, the repository must be active and connected to the same project assigned to the teammate. Solvyn fetches the repository's bounded inspection summary and only the explicitly selected text files (up to 1,500 characters each), then places up to 12,000 characters of repository context in the user-level prompt. A fixed system instruction says repository content is untrusted data and must not be followed as instructions. Omitting `repository_id` does not trigger a remote fetch.

This supports repository-aware investigation with caller-selected source snippets only. It does not provide a clone/worktree, code edits, sandbox dispatch, or automatic verification.

## Task workspace source

`POST /api/v1/workflows/executions/{execution_id}/tasks/{task_id}/workspace` can also accept `repository_id` with `repository_paths` (1–50 relative paths). This mode requires a signed-in organization member and a child task with a `TaskHandoff`; the repository must be active and belong to that handoff's project. Solvyn resolves the default branch once, verifies each selected path is a regular Git blob (excluding symlinks and submodules), and fetches each file at that immutable commit SHA. Private repositories require an active organization GitHub token linked to the repository. Files are capped at 1 MB each and 16 MiB total. Caller-supplied `files` and repository source are mutually exclusive. The workspace row stores the repository ID, commit SHA, and selected paths. This creates a reproducible selected-file snapshot, not a full clone or Git worktree, and does not grant the teammate filesystem tools or permission to execute commands.

## Proactive engineering

GitHub `workflow_run` failures and Dependabot, code scanning, and secret scanning alert activity are signature-verified and deduplicated per connected project. Opened or reopened pull requests authored by `dependabot[bot]` are stored as a separate bounded, deduplicated human-triage signal with the title, number, branch, and verified GitHub URL; the PR body is omitted. This surfaces Dependabot's update proposals but does not resolve dependency versions or modify code. Owner/admin-configured recurring code quality, dependency review, and documentation review schedules also add due items to the same inbox. Events are bounded, stored without the raw payload or secret values, and shown to organization members for human triage. These triggers do not automatically start an agent workflow; automatic remediation requires a later explicit policy and execution budget.
