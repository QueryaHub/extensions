# Querya extension registry

The list of extensions that Querya Desktop's Marketplace offers. It is a
Git repository and a static file: no server, no database. Add a record in a pull
request, CI checks the extension, and after the merge it appears in

```
https://queryahub.github.io/extensions/index.json
```

## Publish an extension

1. Release your extension on GitHub: a **zip** release asset with `manifest.json`
   at its root (start from [extension-template](https://github.com/QueryaHub/extension-template)).
2. Add `extensions/<id>.json`, where `<id>` is the `id` in your `manifest.json`:

   ```json
   {
     "id": "acme.redis-driver",
     "version": "1.0.0",
     "downloadUrl": "https://github.com/acme/redis-driver/releases/download/v1.0.0/redis-driver.zip",
     "sha256": "<sha256 of that zip, lowercase hex>",
     "testEnv": {
       "image": "ghcr.io/queryahub/querya-testdb-redis:0.4.19",
       "port": 6379,
       "query": "GET querya:app:name",
       "table": "querya:app:name"
     }
   }
   ```

3. Open a pull request. CI does the rest.

Everything else the Marketplace shows (name, description, type, `engines`,
sandbox permissions, drivers) is read from the `manifest.json` **inside the
archive**, so the listing cannot disagree with what is installed.

To release a new version, change `version`, `downloadUrl` and `sha256` in the
same file. To hide an extension without deleting it, add `"yanked": true`.

## What CI checks

For every record in a pull request:

| Check | Why |
|-------|-----|
| the record matches [`schema/record.schema.json`](schema/record.schema.json); the file is named `<id>.json` | one record per extension |
| `downloadUrl` is `https://github.com/<owner>/<repo>/releases/download/<tag>/<asset>` | the app only downloads GitHub release assets |
| the asset downloads, is a zip of at most 100 MB, and its SHA-256 is the record's | what is listed is what is installed |
| no entry leaves the extension folder (`..`, absolute, drive letters, symlinks), at most 500 MB unpacked | unpacking is safe |
| `manifest.json` is at the root, its `id` and `version` equal the record's, it declares `engines.querya_desktop`, and a driver's `main` is in the archive | the manifest is the source of truth |
| a database driver with a `testEnv` passes [`querya-ext-tester`](https://github.com/QueryaHub/Querya-Desktop/blob/main/docs/extension-testing.md) **in the OS sandbox** against that database, for each Querya version in [`hosts.json`](hosts.json) | it works, and inside its declared permissions |

`testEnv.image` must be a Querya test database
(`ghcr.io/queryahub/querya-testdb-{postgres,mysql,mongo,clickhouse,redis}`),
with the seeds already inside. Only a Linux x86-64 build is exercised.

Your extension's binary **runs in CI**, in the OS sandbox, in a job with no
secrets and a read-only token (`pull_request`, never `pull_request_target`).

## What is published

After every merge, and every night, the index is rebuilt from all records:

- `index.json` — `{ "schema": 1, "generatedAt": ..., "extensions": [...] }`, one entry per extension: the manifest fields plus `downloadUrl`, `sha256`, `size` (and `yanked`). This is what the app reads.
- `compat.json` — for each extension and each Querya version in `hosts.json`: `pass`, `fail` or `untested` (themes, or a driver without `testEnv`).

The nightly run re-checks every asset. One that disappeared, or whose hash
changed, fails its check and is **left out of the index**; the run then fails so
the maintainers notice. An extension whose tests fail on every Querya version is
left out as well.

## The scripts

```bash
python3 -m unittest discover -s tests                       # their tests
python3 scripts/registry.py check extensions/<id>.json --out entry.json
python3 scripts/registry.py build-index entries --out site
```

Standard library only. `check` also takes `--archive FILE` to use a local zip
(tests and fixtures).

The [Pipeline test](.github/workflows/pipeline.yml) workflow runs the whole chain
on [extension-template](https://github.com/QueryaHub/extension-template) without
publishing it.
