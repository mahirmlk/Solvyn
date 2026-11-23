# Locked dependency installation for verification

Verification jobs can stage dependencies for a supported root-level Python or npm project. Python requires `pyproject.toml` and `uv.lock`; npm requires `package.json` and `package-lock.json` version 2 or 3. The two ecosystems cannot be mixed in one workspace. Git, URL, local, editable, workspace, custom-index, source-only, and unsupported lock forms fail closed.

## Boundary

The trusted sandbox supervisor reads only the root lock and project manifest, then sends their bounded bytes to a separate fetch service. The fetcher has no workspace mount, Docker socket, application secret, repository token, or provider credential. For Python, it exports the lock with a pinned uv binary and downloads only wheel artifacts with SHA-256 hashes. For npm, it parses the lock without running npm, rejects duplicate JSON keys and unsupported dependency forms, and fetches only lock-enumerated tarballs with a single SHA-512 integrity value each. npm workspaces, shrinkwrap, yarn/pnpm locks, custom registries, redirects, and git/file dependencies are not supported.

The fetcher joins only an internal Docker network. A separate Squid proxy is its only route to the public network and allows HTTPS to `pypi.org`, `files.pythonhosted.org`, and `registry.npmjs.org`; it denies localhost, private and link-local destinations. npm tarball URLs must use the exact HTTPS npm registry origin and may not contain credentials, ports, query strings, fragments, or path escapes. The API and worker do not join the fetch network. A run container has `--network none`.

For Python, the supervisor checks each wheel against the exported requirement hashes, validates archive paths and limits, and verifies the fetcher and sandbox Python ABI/platform fingerprints. It installs with pip 26.3 into a distinct `/deps` tmpfs using `--no-index`, `--require-hashes`, `--only-binary=:all:` and `--no-deps`. Pip staging also stays inside `/deps/tmp`; the 1536 MiB dependency tmpfs budgets for the maximum compressed wheelhouse plus bounded unpacked staging and target copies.

For npm, the fetch service safely extracts verified package archives to a bounded dependency bundle and checks package identity against the lock. The runtime stages that bundle on a separate `node_modules` tmpfs, outside the source workspace fingerprint and export. It uses the image-pinned Node.js 22.23.3/npm 10.9.9 toolchain and runs a bounded `npm ls --all --json --offline --ignore-scripts --no-audit --no-fund` preflight with network disabled. This validates the staged dependency graph and declared semver ranges without invoking installation or lifecycle scripts. Only fixed npm `test`, `build`, `lint`, and `typecheck` profiles run; callers cannot substitute a custom command for an npm workspace. npm's offline and ignore-scripts settings are enforced in the sandbox environment as well.

Existing `node_modules` directories in the source tree are excluded from all sandbox staging and exports, including runs that use a shell wrapper. A Node/npm command can use dependencies only when a verified root package manifest and lockfile produce the separate dependency bundle; arbitrary workspace `node_modules` contents never become available to the sandbox.

Dependency-enabled jobs receive at least 2 GiB of container memory. The source workspace remains separate and its fingerprint is checked after dependency setup.

## Limits and evidence

Inputs, requirement exports, package count, individual artifact size, total bundle size, extracted file count, unpacked bytes, subprocess output, fetch duration, and concurrent fetches have hard limits. npm inputs are capped at 512 KiB for the manifest and 4 MiB for the lock, at 500 lock packages, 32 MiB per tarball and 256 MiB total, with a 180-second aggregate fetch deadline. Archive paths must be relative regular files or directories; links, devices, path traversal, and collisions are rejected. Bundles are temporary and per run. Every job container is removed after completion or failure.

Verification history records the lockfile SHA-256, artifact bundle SHA-256 and compressed bytes, package count, environment fingerprint, runtime and installer versions, platform, and architecture. Python runs additionally retain wheelhouse and Python ABI evidence. npm evidence identifies the Node.js/npm runtime and the npm lockfile; it does not populate Python wheelhouse fields. An unsupported or oversized project fails verification before the test command starts.

## Threat model

Repository-controlled manifests, lock files, wheels and verification commands are untrusted. The fetch path can read only the two bounded manifest files and can reach only the allowlisted package hosts through the proxy. Source distributions and arbitrary dependency source URLs are never built or installed. The downloaded wheel hash must appear on the corresponding exported pinned requirement and is rechecked before transfer.

Dependency installation and tests run in a one-shot container without network, Docker access, host credentials, or writable host paths. A malicious wheel can still execute code when imported by the test command; that code is confined to the same restricted verification container. A malicious repository owner can intentionally lock a malicious wheel hash, so this feature provides isolation and reproducibility rather than approval of a package's behavior.
