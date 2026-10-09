# Release an experimental alpha

The release workflow checks the distribution, runs the installed-wheel suite, and publishes only after those checks pass. Human-label and developer-adoption studies are deferred research, not prerequisites for this alpha. Keep their results unclaimed until measured.

## One-time setup

The GitHub repository is `nothans/ex-regex`. Create GitHub environments named `testpypi` and `pypi`; restrict them to version tags and configure an approval rule for production publication where the repository plan supports it.

Register a pending Trusted Publisher separately on [TestPyPI](https://test.pypi.org/manage/account/publishing/) and [PyPI](https://pypi.org/manage/account/publishing/):

| Field | Value |
|---|---|
| Project name | `ex-regex` |
| Owner | `nothans` |
| Repository | `ex-regex` |
| Workflow filename | `release.yml` |
| Environment | `testpypi` on TestPyPI; `pypi` on PyPI |

Both services need their own account setup. GitHub authentication alone does not register these publishers. No long-lived API token is needed. A pending publisher does not reserve the package name. See the official [Trusted Publishing setup](https://docs.pypi.org/trusted-publishers/adding-a-publisher/) and [first-publication guide](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).

## Prepare and check

Use a fresh output directory with exactly one current wheel and sdist. Update `src/exregex/_version.py` and `CHANGELOG.md` for each new version; package indexes do not allow replacing an uploaded distribution with different bytes.

For the first public upload, update the README's source-preview installation notice and verify that its GitHub documentation links are publicly accessible. Keep the checkout instructions available until the PyPI installation has been verified.

```sh
python -m pip install build 'twine>=7,<8' pytest ruff mypy
python -m ruff check src tests evals examples scripts
python -m mypy src examples/support --check-untyped-defs
python -m build --outdir release-dist
python scripts/release.py check release-dist --tag v0.1.0a1
python -m twine check release-dist/*
python scripts/release.py test release-dist
```

Run the last command in a disposable virtual environment: it replaces that environment's installed `ex-regex` wheel. It extracts tests, fixtures and examples from the sdist with `src/` absent, checks the import origin, runs the default suite, and executes the CLI and support demo. CI does this on Ubuntu Python 3.10–3.13, Windows 3.13 and macOS 3.13. The wheel is built from the sdist, and each matrix job tests the same artifact pair.

Twine 7 or newer is required for the metadata version emitted by current Hatchling. See [Twine's metadata 2.5 compatibility fix](https://twine.readthedocs.io/en/latest/changelog.html#twine-7-0-0-2026-07-27).

## Publish deliberately

1. Push the reviewed repository and let **Package checks** finish. The release workflow must exist on the default branch before manual dispatch is available.
2. Create and push `v0.1.0a1` at the reviewed commit. Keep version tags immutable. A tag push alone does not publish.
3. In Actions, run **Publish package**, selecting that tag and `testpypi`. Selecting a branch or a tag that differs from the package version fails before upload.
4. Verify the TestPyPI installation in a fresh environment:

   ```sh
   python -m pip install --index-url https://test.pypi.org/simple/ --no-deps ex-regex==0.1.0a1
   python -c "import exregex; print(exregex.__version__)"
   exregex --help
   ```

5. Run **Publish package** on the same immutable tag with `pypi`. This reruns the checks and builds a new tested pair from that tag; it does not promote the previously uploaded TestPyPI bytes. Confirm any configured environment approval.
6. Verify with `python -m pip install --pre ex-regex` in another clean environment. Record the workflow run, tag, version and published artifact hashes.

Only the publishing job can request an OIDC token. It downloads the same-run tested artifacts and does not build code. Index URLs are fixed in the workflow. Duplicate/partial uploads fail visibly; there is no `skip-existing`. This follows [PyPA's build/publish separation](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/).

Local `dist/` may contain historical builds. Never upload it with a wildcard; use a fresh validated directory or explicit filenames.
