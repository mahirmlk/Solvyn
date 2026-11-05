# Repository Intelligence — current scope

Repository intelligence is an early workstream. It adds a provider boundary and GitHub integration that can connect a public repository to an authorized project, inspect its default-branch tree and common manifests, summarize direct dependencies and likely frameworks, list branches, search file paths, inspect pull requests, and perform bounded branch and commit operations.

## API

See the [Repository Intelligence section in API.md](../API.md#repository-intelligence) for routes and response limits.

## Boundaries

- Callers must have a user JWT and organization membership in the organization that owns the project. Connecting a repository also requires owner or admin membership. This API follows the current foundation API's organization-wide project access model; project-specific membership enforcement is not implemented yet.
- Repository inspection and pull-request reads currently support public GitHub repositories. An organization owner or admin can connect a GitHub token; the token is validated before storage, encrypted with `INTEGRATION_ENCRYPTION_KEYS`, and never returned by the API. With no encryption keys configured, credential connection is disabled.
- The GitHub client uses a fixed `api.github.com` origin, validates `owner/repository` names, uses bounded timeouts, rejects truncated or oversized trees, caps manifest reads, and returns provider errors without reflecting upstream response bodies.
- Repository contents and metadata are marked untrusted. They are not passed to an agent or interpreted as instructions by this feature.
- File search matches path names only. Solvyn can create `solvyn/...` branches, commit bounded UTF-8 text changes with optimistic branch-head checks, and open draft PRs using the organization token. Generated changes remain subject to human review in GitHub; Solvyn does not mark PRs ready or merge them.
- Branch listing returns at most 100 results. `possibly_truncated` signals that more results may exist.
- Pull request listing is a paginated live snapshot from GitHub, not a persisted local synchronization. Detail inspection returns the latest review decision per reviewer (scanning up to 1,100 reviews and probing for more), plus a unified diff capped at 1 MB. Pull request data and diffs remain untrusted input.

Repository discovery beyond a caller-supplied GitHub name, content search, and branch/worktree management remain future work. Private-repository access remains unsupported until repository permission mapping and isolated workspaces are designed.
