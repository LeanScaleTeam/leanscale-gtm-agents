# Deploying the catalog site

`site/dist/` is **gitignored**. Netlify publishes `site/` with no build command, so the
zips only ever reach the live site from a workstation. That is how the site came to serve
three-week-old v1.0.0 archives while the repo held eleven current plugins: nothing in CI
could see the artifacts, because they are not in the repo.

**Never deploy without rebuilding first.** The two commands are:

```bash
python3 tools/vendor.py && python3 tools/package.py   # rebuild every zip from source
python3 tools/check-catalog.py                        # catalog, README, INSTALL, SPEC agree
```

Then deploy `site/` however you normally do (Netlify CLI from this directory).

## Why the zips are not committed

They are build artifacts derived entirely from tracked source, and committing ~3 MB of
binaries per release makes every diff unreadable. The trade is that freshness cannot be
enforced by CI — so it is enforced by habit and by `check-catalog.py`, which fails the
build whenever a shipped plugin is missing from a customer-facing surface even though it
cannot inspect the archives themselves.

## The better fix, when there is time

Give Netlify a build command so the archives are generated at deploy time and staleness
becomes structurally impossible:

```toml
[build]
  command = "python3 ../tools/vendor.py && python3 ../tools/package.py"
  publish = "."
```

This depends on the site's configured base directory (it must be `site/`, so that `../`
reaches the repo root) and on the build image having Python 3 — both true of Netlify's
default image, but verify on a draft deploy with `--alias` before pointing production at
it.
