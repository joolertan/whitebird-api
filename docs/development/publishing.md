# Publishing

Releases are automated: merging a release pull request publishes the package to its registry.
This page lists the one-time setup a maintainer has to do.

## How a release happens

1. Commits follow [Conventional Commits](https://www.conventionalcommits.org/) with the package
   directory as scope (`feat(python): ...`, `fix(go): ...`).
2. On every push to `main`, [release-please](https://github.com/googleapis/release-please) opens
   or updates one release pull request per package with the version bump and changelog.
3. Merging a release pull request creates a tag and a GitHub release:

    | Package | Tag | Registry |
    |---|---|---|
    | Python | `python-v1.2.3` | PyPI |
    | Node.js | `node-v1.2.3` | npm |
    | Go | `go/v1.2.3` | Go module proxy |
    | Rust | `rust-v1.2.3` | crates.io |

4. The `release` workflow publishes the tagged package. PyPI, npm and crates.io use
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

Go has no registry to sign up for. A module version is a git tag: when the `go/v1.2.3` tag is
pushed, the release workflow asks `proxy.golang.org` for it, and
[pkg.go.dev](https://pkg.go.dev/github.com/joolertan/whitebird-api/go) shows the documentation a
few minutes later. If it does not, open that page and press **Request**.

## Read the Docs

1. Sign up on [readthedocs.org](https://readthedocs.org) with your GitHub account.
2. Choose **Add project**, pick `joolertan/whitebird-api` and keep the slug `whitebird-api`, so
   the site lives at `https://whitebird-api.readthedocs.io`. The build is configured by
   `.readthedocs.yaml` in the repository.
3. In **Settings → Pull request builds**, enable previews for pull requests.

Read the Docs adds the GitHub webhook itself; every push to `main` rebuilds the site.

## Badges

The README badges read from PyPI, npm, crates.io, pkg.go.dev, Read the Docs and GitHub Actions.
They show "not found" until the first release of each package and the first docs build.
