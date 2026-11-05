# Repository Intelligence — current scope

Repository intelligence is an early workstream. It adds a provider boundary and a read-only GitHub implementation that can connect a public repository to an authorized project, inspect its default-branch tree and common manifests, summarize direct dependencies and likely frameworks, list branches, search file paths, and inspect pull requests.

## API

See the [Repository Intelligence section in API.md](../API.md#repository-intelligence) for routes and response limits.

## Boundaries

- Callers must have a user JWT and organization membership in the organization that owns the project. Connecting a repository also requires owner or admin membership. This API follows the current foundation API's organization-wide project access model; project-specific membership enforcement is not implemented yet.
- Only public GitHub repositories are supported. No GitHub credentials are stored or accepted by these routes.
- The GitHub client uses a fixed `api.github.com` origin, validates `owner/repository` names, uses bounded timeouts, rejects truncated or oversized trees, caps manifest reads, and returns provider errors without reflecting upstream response bodies.
- Repository contents and metadata are marked untrusted. They are not passed to an agent or interpreted as instructions by this feature.
- File search matches path names only. This slice does not search file contents, clone repositories, execute code, create branches, write files, or create pull requests.
- Branch listing returns at most 100 results. `possibly_truncated` signals that more results may exist.
- Pull request listing is a paginated live snapshot from GitHub, not a persisted local synchronization. Detail inspection returns the latest review decision per reviewer (scanning up to 1,100 reviews and probing for more), plus a unified diff capped at 1 MB. Pull request data and diffs remain untrusted input.

Repository discovery beyond a caller-supplied GitHub name, content search, Git writes, and branch/worktree management remain future work. Creating branches or pull requests requires encrypted credential storage, repository permission mapping, and isolated workspaces. Private-repository access remains unsupported until those boundaries are designed.
