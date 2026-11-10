# Publishing

The project is released as a whole: one version for all four packages, one tag, one GitHub release
and one [changelog](https://github.com/joolertan/whitebird-api/blob/main/CHANGELOG.md). Merging the
release pull request publishes every package to its registry. This page describes the flow and the
one-time setup a maintainer has to do.

## How a release happens

1. Commits follow [Conventional Commits](https://www.conventionalcommits.org/) with the package
   directory as scope (`feat(python): ...`, `fix(go): ...`).
2. On every push to `main`, [release-please](https://github.com/googleapis/release-please) opens
   or updates the release pull request, *chore(main): release 1.2.3*. It sets the new version in
   every package and adds the changes to `CHANGELOG.md`.
3. Optionally run the checks on it before merging: each CI workflow (Python, Node.js, Go, Rust,
   Codegen, Docs) has **Run workflow** in the Actions tab; pick the branch
   `release-please--branches--main`.
4. Merging the pull request creates the tag `v1.2.3` and its GitHub release. Read the Docs builds a
   documentation version for the tag.
5. The `release` workflow publishes the packages:

    | Package | Registry |
    |---|---|
    | Python | PyPI |
    | Node.js | npm |
    | Go | Go module proxy, through the tag `go/v1.2.3` the workflow adds |
    | Rust | crates.io |

    If a publish job fails, fix the cause and use **Re-run failed jobs** on that run; the release
    itself is not repeated. PyPI, npm and crates.io use
    [trusted publishing](https://docs.pypi.org/trusted-publishers/): GitHub Actions exchanges a
    short-lived OIDC token for upload rights, so no registry tokens are stored in the repository.

## GitHub

1. Create the public repository `joolertan/whitebird-api` and push `main`.
2. In **Settings → Environments**, create `pypi`, `npm` and `crates-io`. Add yourself as a
   required reviewer if every publish should wait for a manual approval.
3. In **Settings → Actions → General**, allow GitHub Actions to create pull requests (needed by
   release-please).

!!! note
    If the repository lives under a different owner or name, replace `joolertan/whitebird-api`
    across the repository. The Go module path depends on it.

## PyPI

1. Create an account on [pypi.org](https://pypi.org/account/register/) and enable two-factor
   authentication.
2. Open **Your account → Publishing → Add a new pending publisher** and fill in:

    | Field | Value |
    |---|---|
    | PyPI project name | `whitebird-api` |
    | Owner | `joolertan` |
    | Repository name | `whitebird-api` |
    | Workflow name | `release.yml` |
    | Environment name | `pypi` |

The pending publisher turns into a regular one with the first release; no manual upload is needed.

## npm

npm only allows trusted publishing for packages that already exist, so the first version is
published by hand.

1. Create an account on [npmjs.com](https://www.npmjs.com/signup) and enable two-factor
   authentication.
2. Publish the first version from a clean checkout:

    ```bash
    cd node
    npm ci
    npm run build
    npm login
    npm publish --access public
    ```

3. On the package page, open **Settings → Trusted publishing**, choose GitHub Actions and enter
   owner `joolertan`, repository `whitebird-api`, workflow `release.yml`, environment `npm`.
   Under **Allowed actions**, allow it to publish directly: by default a trusted publisher may
   only stage a version, and the workflow's `npm publish` fails with
   `403 OIDC permission denied for this action`.
4. In the same settings, select **Require two-factor authentication and disallow tokens**.

## crates.io

crates.io also needs the first version to be published with a token.

1. Sign in to [crates.io](https://crates.io) with GitHub and verify your email address in
   **Account Settings**.
2. Create an API token with the `publish-new` scope, then publish:

    ```bash
    cd rust
    cargo login
    cargo publish
    ```

3. On the crate page, open **Settings → Trusted Publishing → Add** and enter owner `joolertan`,
   repository `whitebird-api`, workflow `release.yml`, environment `crates-io`.
4. Revoke the API token.

## Go

Go has no registry to sign up for. A module version is a git tag, and because the module lives in
the `go/` directory, its tags are `go/v1.2.3`. The release workflow creates that tag next to
`v1.2.3` and asks `proxy.golang.org` for the version, and
[pkg.go.dev](https://pkg.go.dev/github.com/joolertan/whitebird-api/go) shows the documentation a
few minutes later. If it does not, open that page and press **Request**.

## Read the Docs

1. Sign up on [readthedocs.org](https://readthedocs.org) with your GitHub account.
2. Choose **Add project**, pick `joolertan/whitebird-api` and keep the slug `whitebird-api`, so
   the site lives at `https://whitebird-api.readthedocs.io`. The build is configured by
   `.readthedocs.yaml` in the repository.
3. In **Settings → Pull request builds**, enable previews for pull requests.
4. In **Settings → Automation rules**, add a rule: match **SemVer versions**, version type
   **Tag**, action **Activate version**. Each release tag `v1.2.3` then gets its own documentation
   version; the Go tags `go/v1.2.3` do not match.
5. In **Settings → General**, set **Default version** to `stable`. Read the Docs points `stable` at
   the highest `v1.2.3` tag, and `latest` follows `main`.

Read the Docs adds the GitHub webhook itself; every push to `main` rebuilds `latest`, and every
release adds a version to the version menu.

## Badges

The README badges read from PyPI, npm, crates.io, pkg.go.dev, Read the Docs and GitHub Actions.
They show "not found" until the first release of each package and the first docs build.
