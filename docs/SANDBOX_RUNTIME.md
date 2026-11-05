# Sandbox Runtime — Phase 3 first slice

Solvyn has a one-shot Docker execution primitive exposed through tenant-scoped task verification endpoints. Workspace records and bounded verification results are persisted. Teammates do not yet invoke verification automatically; a user or trusted caller can create a task workspace, add files, and submit an argv command for execution.

## Implemented

- Creates UUID-named workspaces beneath one configured root.
- Associates one workspace with a tenant-owned workflow task and accepts bounded UTF-8 file contents at creation.
- Accepts verification kinds for test, build, lint, type checking, and security, with default Python argv profiles when a command override is omitted: pytest, compileall, Ruff, mypy, and Ruff's `S` security rules.
- Persists command, status, exit code, bounded output, timeout/truncation flags, duration, and runner version against the task and execution.
- Rejects absolute paths, traversal, symlinks, oversized files, and oversized workspaces in filesystem operations.
- Runs an argv command in a fresh container from a locally built, fixed image. The caller cannot select an image or pass environment variables.
- Uses no network, a read-only container root, a non-root UID, dropped Linux capabilities, `no-new-privileges`, CPU, memory, PID, tmpfs, command-time, output, and workspace-size limits.
- Stages only the selected workspace into the container. The sandbox cannot see the host Docker socket, application credentials, or other host paths.
- Normalizes staged workspace permissions in a trusted setup step, then runs the requested command as the unprivileged container UID.
- Exports workspace changes as a bounded tar stream and accepts only regular files and directories with safe relative paths; symlinks and special files are rejected.
- Removes the container in a `finally` path and reports cleanup failure instead of silently treating it as success. Returns the command, exit code, bounded output, timeout status, duration, and run ID to its caller.

Container isolation depends on a correctly configured Docker Engine and host kernel. The control-plane process still needs Docker Engine access to create containers; do not grant that access to an untrusted teammate or mount the engine socket into the sandbox container. Use a dedicated, trusted runtime host for deployments where the API host should not hold daemon access.

The runtime does not clone repositories, enforce teammate-level permissions, request approvals for verification, or install project dependencies. The default profiles target Python workspaces; callers can provide another argv command for other toolchains if they are present in the image or workspace. These profiles are basic checks, not acceptance criteria or baseline regression comparisons. The runtime does not provide a shell API. Commands are passed as argument vectors directly to the isolated container process. Network access is disabled, so dependencies must already be present in the sandbox image or workspace. Build the image with the command below and configure `SANDBOX_IMAGE` and `SANDBOX_WORKSPACE_ROOT` for the API process.

## Build and verify locally

Build the trusted base image explicitly:

```sh
docker build --file sandbox.Dockerfile --tag solvyn-sandbox:local .
```

The default pytest suite tests path confinement, output and resource policy construction, timeout handling, cleanup, and hostile archive rejection with a fake Docker CLI. With a running Linux Docker Engine and the image above, run the opt-in end-to-end smoke test:

```sh
SOLVYN_DOCKER_INTEGRATION=1 python -m pytest tests/test_sandbox.py -k docker_integration
```

The integration test writes and reads an actual file inside the isolated container. The image is never pulled implicitly at execution time.
