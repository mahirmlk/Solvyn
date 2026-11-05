# Sandbox Runtime — Phase 3 first slice

Solvyn now has an internal, one-shot Docker execution primitive and a managed workspace API. It is not yet connected to teammate tools or exposed as a public endpoint. User and teammate permissions, approval requests, and durable run records must be wired in before agent access is enabled.

## Implemented

- Creates UUID-named workspaces beneath one configured root.
- Rejects absolute paths, traversal, symlinks, oversized files, and oversized workspaces in filesystem operations.
- Runs an argv command in a fresh container from a locally built, fixed image. The caller cannot select an image or pass environment variables.
- Uses no network, a read-only container root, a non-root UID, dropped Linux capabilities, `no-new-privileges`, CPU, memory, PID, tmpfs, command-time, output, and workspace-size limits.
- Stages only the selected workspace into the container. The sandbox cannot see the host Docker socket, application credentials, or other host paths.
- Normalizes staged workspace permissions in a trusted setup step, then runs the requested command as the unprivileged container UID.
- Exports workspace changes as a bounded tar stream and accepts only regular files and directories with safe relative paths; symlinks and special files are rejected.
- Removes the container in a `finally` path and reports cleanup failure instead of silently treating it as success. Returns the command, exit code, bounded output, timeout status, duration, and run ID to its caller.

Container isolation depends on a correctly configured Docker Engine and host kernel. The control-plane process still needs Docker Engine access to create containers; do not grant that access to an untrusted teammate or mount the engine socket into the sandbox container. Use a dedicated, trusted runtime host for deployments where the API host should not hold daemon access.

The first slice does not clone repositories, persist workspace/run metadata, enforce project or teammate permissions, request approvals, install dependencies, or add test/build profiles. It does not provide a shell API. Callers pass argument vectors directly to the isolated container process. Network access is disabled, so dependencies must already be present in the sandbox image or workspace.

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
