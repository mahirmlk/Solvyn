# Sandbox Runtime — Phase 3 first slice

Solvyn has a managed task workspace, permission-checked teammate file and fixed-profile verification tools, and a one-shot Docker execution primitive exposed through tenant-scoped task verification endpoints. Workspace records, content-free tool invocation metadata, and bounded verification results are persisted. In production Compose, the API delegates to a private sandbox supervisor on an internal network; the API container does not receive Docker Engine access.

## Implemented

- Creates UUID-named workspaces beneath one configured root.
- Associates one workspace with a tenant-owned workflow task and accepts either bounded UTF-8 file contents or explicitly selected repository files at creation.
- During a running delegated task handoff, allows model-requested file listing/reading with the exact `READ_ONLY` grant and file writing with the exact `MODIFY_FILES` grant. Each dispatch rechecks the active task, workspace, handoff lease, and permission.
- Persists tool name, path, status, byte counts, safe error code, and timestamps without storing file contents. Task-scoped history is exposed by the verification API's `GET .../tool-invocations` endpoint.
- Allows a teammate with the exact `EXECUTE_COMMANDS` grant to run only the fixed `test`, `build`, `lint`, `typecheck`, and `security` profiles through the configured sandbox runner. Each run is persisted as a normal verification record, and only bounded output is returned to the model.
- For repository seeding, requires a delegated child task, an active repository in the handoff's assigned project, and a signed-in organization member. Private repositories additionally require the active organization GitHub integration linked to that repository. Resolves the default branch to one commit SHA, checks the Git tree for regular files, fetches only selected paths at that SHA, and records repository, commit, and path provenance. The credential stays in the API process and is not copied into workspace or sandbox state.
- Accepts verification kinds for test, build, lint, type checking, and security, with default Python argv profiles when a command override is omitted: pytest, compileall, Ruff, mypy, and Ruff's `S` security rules.
- Persists command, the pre-run workspace SHA-256 digest, status, exit code, bounded output, timeout/truncation flags, duration, and runner version against the task and execution.
- Compares completed results for the same task, workspace, kind, and command, and reports baseline, regression, recovery, unchanged, or inconclusive status with whether the workspace digest changed. Overlapping runs and checks after interrupted runs are inconclusive.
- Rejects absolute paths, traversal, symlinks, oversized files, and oversized workspaces in filesystem operations.
- Runs an argv command in a fresh container from a locally built, fixed image. The caller cannot select an image or pass environment variables.
- Uses no network, a read-only container root, a non-root UID, dropped Linux capabilities, `no-new-privileges`, CPU, memory, PID, tmpfs, command-time, output, and workspace-size limits.
- Stages only the selected workspace into the container. The sandbox cannot see the host Docker socket, application credentials, or other host paths.
- Normalizes staged workspace permissions in a trusted setup step, then runs the requested command as the unprivileged container UID.
- Exports workspace changes as a bounded tar stream and accepts only regular files and directories with safe relative paths; symlinks and special files are rejected.
- Removes the container in a `finally` path and reports cleanup failure instead of silently treating it as success. Returns the command, exit code, bounded output, timeout status, duration, and run ID to its caller.

Container isolation depends on a correctly configured Docker Engine and host kernel. The trusted sandbox supervisor is the only Solvyn service with Docker Engine access. The job container, API, worker, and frontend do not receive the engine socket. Docker socket access gives the supervisor control over its Docker host. The current Compose integration is co-located and uses a local Docker socket plus a shared workspace volume; a separate physical runtime host is not supported yet. Compose places the supervisor on an internal-only network and shares only the managed workspace volume with the API. Do not expose the supervisor port publicly. A remote runtime needs a secure workspace transfer protocol and authenticated transport before it can be supported.

The runtime does not clone a full repository or create a Git worktree; it seeds only caller-selected files, with a 50-path limit, 1 MB per file limit, and 16 MiB aggregate limit. Teammate verification can choose only one fixed profile per call; it cannot supply arbitrary argv, install dependencies, or open a shell. The file and verification tools do not request a separate human approval; access is controlled by the teammate's exact permission grant and the active task handoff lease. The runtime does not install project dependencies. The default profiles target Python workspaces; callers can provide another argv command for other toolchains through the trusted user endpoint if they are present in the image or workspace. These profiles are basic checks, not acceptance criteria. Regression results compare outcomes by task, workspace, kind, and command; they do not imply semantic equivalence across different source digests. Commands are passed as argument vectors directly to the isolated container process. Network access is disabled, so dependencies must already be present in the sandbox image or workspace. Production Compose builds the fixed execution image and starts the supervisor, requiring `SANDBOX_RUNTIME_TOKEN` in `.env.production`. In standalone API development, leave `SANDBOX_RUNTIME_URL` unset to use the local Docker runner; configure `SANDBOX_IMAGE` and `SANDBOX_WORKSPACE_ROOT` for that process.

## Build and verify locally

Build the trusted base image explicitly:

```sh
docker build --file sandbox.Dockerfile --tag solvyn-sandbox:local .
```

For the production Compose stack, copy `.env.production.example` to `.env.production`, set a unique high-entropy `SANDBOX_RUNTIME_TOKEN`, and start the complete stack with:

```sh
docker compose --env-file .env.production --file docker-compose.prod.yml up --build -d
```

Compose builds the fixed job image, initializes the shared workspace volume for the API UID, and waits for the sandbox supervisor's Docker Engine and image health check before starting the API. The supervisor requires a Docker socket at `/var/run/docker.sock` and therefore has authority over the Docker host; this Compose setup is intended for a trusted single-host deployment.

The default pytest suite tests path confinement, output and resource policy construction, timeout handling, cleanup, and hostile archive rejection with a fake Docker CLI. With a running Linux Docker Engine and the image above, run the opt-in end-to-end smoke test:

```sh
SOLVYN_DOCKER_INTEGRATION=1 python -m pytest tests/test_sandbox.py -k docker_integration
```

The integration test writes and reads an actual file inside the isolated container. The image is never pulled implicitly at execution time.
