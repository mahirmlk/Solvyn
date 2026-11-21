# GitHub App setup

Solvyn uses a GitHub App for private repository access. An organization owner or admin installs the App, then binds the installation to the matching Solvyn organization from **Repositories → GitHub access**. Solvyn verifies the installation and account with GitHub before saving their numeric IDs.

## App configuration

Set these protected environment variables on the API service:

| Variable | Value |
| --- | --- |
| `GITHUB_APP_ID` | Numeric App ID from the GitHub App settings |
| `GITHUB_APP_SLUG` | App slug used to open the install/configure page in the UI |
| `GITHUB_APP_PRIVATE_KEY` | GitHub App RSA private key PEM; escaped `\n` line breaks are accepted |
| `GITHUB_WEBHOOK_SECRET` | Webhook secret configured on the App |

Never commit the private key or webhook secret. App ID and slug are identifiers, but keep environment configuration in the deployment's protected settings. The private key must be an RSA PEM key supported by `cryptography`.

## Repository permissions

Grant only these App permissions:

- **Contents:** read and write. Reads support repository inspection and workspaces; writes support Solvyn branches and commits.
- **Pull requests:** read and write. Reads support PR inspection; writes support draft PR creation.
- **Metadata:** read (GitHub requires this for repository identity and installation access).
- **Dependabot alerts:** read, if the scheduled Dependabot reconciliation workflow is enabled.
- **Code scanning alerts** and **Secret scanning alerts:** read only if Solvyn will later add API polling for those signals. Current alert ingestion uses signed webhooks and does not read their API endpoints.

The API asks GitHub for an installation token scoped to the connected repository's numeric ID and only the permission profile needed for the operation. Those tokens stay in process memory, are cached by exact installation/repository/permission scope until shortly before expiry, and are never persisted. Repository discovery uses a temporary installation-wide token solely for the installation repository-list endpoint; that token is not used for repository content or writes.

Install the App only on the repositories Solvyn should access. The installation's selected repositories are the outer access limit; Solvyn additionally scopes every content token to one connected repository.

## Webhooks

Set the App webhook URL to `https://<api-host>/api/v1/webhooks/github`, enable SSL verification, and configure the same random secret as `GITHUB_WEBHOOK_SECRET`. Subscribe to `workflow_run`, `issues`, `pull_request`, `dependabot_alert`, `code_scanning_alert`, and `secret_scanning_alert` for the signals Solvyn handles. GitHub signs deliveries with SHA-256; Solvyn checks the signature and then requires both the installation ID and numeric repository ID to match a connected repository before recording events.

## Connect the installation

1. Install or configure the App for the GitHub organization/account and choose its allowed repositories.
2. In Solvyn, select the matching organization, open **Repositories**, and use the GitHub App installation link.
3. Copy the installation ID from the GitHub installation settings URL and enter it in Solvyn. The API verifies the ID and account using the App private key; only owner/admin members can bind it.
4. Discover and connect repositories. Each connection stores the numeric GitHub repository ID so renamed repositories and webhook deliveries remain bound to the same repository identity.

The binding API is `PUT /api/v1/organizations/{organization_id}/integrations/github/installation` with `{ "installation_id": 123 }`. The old personal-token endpoint returns `410 Gone`; private repository reads and writes require an App installation. Existing encrypted PAT credentials are deleted from Solvyn when an owner/admin binds an App installation, and their integration is disabled. Solvyn cannot revoke a personal token in GitHub; the response and UI remind the owner to revoke it there if it is no longer needed. Reconnect private repositories through the App.

## Access policy

Current product authorization is organization-wide: any active organization member or viewer can use connected repositories for reads, while owner/admin checks protect repository connections, installation binding, and write operations. `ProjectMembership` rows do not currently restrict project access. Repositories are still bound to one Solvyn project and tenant, and GitHub tokens are scoped to a repository; project membership enforcement needs a separate product-wide authorization rollout and migration.

This work adds database fields for installation/account and repository IDs. Apply the Alembic migration before deploying the code. Migration revision follows `0063` after the dependency verification change lands.
