# Locked dependency installation for verification

Verification jobs can install the Python dependencies declared by a repository only when the root contains a supported pyproject.toml and uv.lock. The lock must use public PyPI registry artifacts. Git, URL, local, editable, workspace, custom-index and source-only dependency forms fail closed.

## Boundary

The trusted sandbox supervisor reads only the root lock and project manifest, then sends their bounded bytes to a separate fetch service. The fetcher has no workspace mount, Docker socket, application secret, repository token, or provider credential. It exports the lock with a pinned uv binary and downloads only wheel artifacts with SHA-256 hashes.

The fetcher joins only an internal Docker network. A separate Squid proxy is its only route to the public network and allows HTTPS to pypi.org and files.pythonhosted.org; it denies localhost, private and link-local destinations. The API and worker do not join the fetch network. A run container has --network none.

The supervisor checks each wheel against the exported requirement hashes, validates archive paths and limits, and verifies the fetcher and sandbox Python ABI/platform fingerprints. It installs with pip 26.3 into a distinct /deps tmpfs using --no-index, --require-hashes, --only-binary=:all: and --no-deps. The mounted source workspace remains separate and its fingerprint is checked after dependency setup.

## Limits and evidence

Inputs, requirement exports, package count, wheel size, total wheelhouse size, extracted file count, unpacked bytes, subprocess output, fetch duration, and concurrent fetches have hard limits. Wheelhouses are temporary and per run. Every job container is removed after completion or failure.

Verification history records the lock SHA-256, wheelhouse SHA-256, package count, wheelhouse bytes, Python runtime fingerprint, and pinned uv/pip versions. An unsupported or oversized project fails verification before the test command starts.

## Threat model

Repository-controlled manifests, lock files, wheels and verification commands are untrusted. The fetch path can read only the two bounded manifest files and can reach only the allowlisted package hosts through the proxy. Source distributions and arbitrary dependency source URLs are never built or installed. The downloaded wheel hash must appear on the corresponding exported pinned requirement and is rechecked before transfer.

Dependency installation and tests run in a one-shot container without network, Docker access, host credentials, or writable host paths. A malicious wheel can still execute code when imported by the test command; that code is confined to the same restricted verification container. A malicious repository owner can intentionally lock a malicious wheel hash, so this feature provides isolation and reproducibility rather than approval of a package's behavior.
