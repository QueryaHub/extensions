#!/usr/bin/env python3
"""The extension registry's checks and index builder (standard library only).

    registry.py check RECORD.json --out ENTRY.json [--archive FILE] [--extract DIR]
    registry.py build-index ENTRIES_DIR --out SITE_DIR [--compat compat.json]

`check` reads a record (extensions/<id>.json), downloads the release asset it
names, and verifies everything the Marketplace will rely on: the SHA-256, that
the archive is safe to unpack, that manifest.json inside it agrees with the
record. It writes the index entry. `build-index` joins the entries into
index.json, the file Querya Desktop reads.
"""
import argparse
import hashlib
import json
import posixpath
import re
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

INDEX_SCHEMA = 1
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_UNPACKED_BYTES = 500 * 1024 * 1024
MAX_ENTRIES = 5000

ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*(\.[a-z0-9]+(-[a-z0-9]+)*)+$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
ASSET_RE = re.compile(r"^https://github\.com/[^/]+/[^/]+/releases/download/[^/]+/[^/]+$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_RE = re.compile(r"^ghcr\.io/queryahub/querya-testdb-[a-z0-9-]+:[A-Za-z0-9._-]+$")

# Build targets of per-platform archives; the names are the app's (and the tester's).
PLATFORMS = ("linux-x64", "macos-arm64", "macos-x64", "windows-x64")
RECORD_KEYS = {"id", "version", "downloadUrl", "sha256", "platforms", "yanked", "testEnv"}
TESTENV_KEYS = {"image", "port", "connect", "query", "table", "database"}
# The newest manifest.json format Querya Desktop defines (`manifestVersion`, 1 when
# absent). Raise it in the change that adds a format to Querya Desktop.
MAX_MANIFEST_VERSION = 1

# Fields copied from manifest.json into the index. The record never supplies them.
MANIFEST_KEYS = [
    "id", "name", "version", "publisher", "type", "description", "icon", "engines",
    "main", "author", "homepage", "license", "preview", "tags", "sandbox",
    "capabilities", "contributions",
]


class CheckError(Exception):
    pass


def validate_source(url, sha, where):
    if not isinstance(url, str) or not ASSET_RE.match(url):
        raise CheckError(f"{where}downloadUrl must be https://github.com/<owner>/<repo>/releases/download/<tag>/<asset>")
    if not isinstance(sha, str) or not SHA_RE.match(sha):
        raise CheckError(f"{where}sha256 must be 64 lowercase hex characters")


def validate_record(record, filename):
    """The rules of schema/record.schema.json, without a dependency."""
    if not isinstance(record, dict):
        raise CheckError("the record must be a JSON object")
    extra = set(record) - RECORD_KEYS
    if extra:
        raise CheckError(f"unknown field(s): {', '.join(sorted(extra))}")
    for key in ("id", "version"):
        if not isinstance(record.get(key), str):
            raise CheckError(f"{key} is required and must be a string")
    if not ID_RE.match(record["id"]):
        raise CheckError(f"id {record['id']!r} must look like publisher.name (lowercase, digits, dashes)")
    if filename and Path(filename).name != f"{record['id']}.json":
        raise CheckError(f"the file must be named {record['id']}.json")
    if not VERSION_RE.match(record["version"]):
        raise CheckError("version must be x.y.z")

    single = "downloadUrl" in record or "sha256" in record
    if single and "platforms" in record:
        raise CheckError("use either downloadUrl + sha256 (one archive for every platform) or platforms, not both")
    if not single and "platforms" not in record:
        raise CheckError("downloadUrl + sha256, or platforms, is required")
    if single:
        validate_source(record.get("downloadUrl"), record.get("sha256"), "")
    else:
        platforms = record["platforms"]
        if not isinstance(platforms, dict) or not platforms:
            raise CheckError("platforms must be a non-empty object")
        for key, source in platforms.items():
            if key not in PLATFORMS:
                raise CheckError(f"unknown platform {key!r}; use {', '.join(PLATFORMS)}")
            if not isinstance(source, dict) or set(source) - {"downloadUrl", "sha256"}:
                raise CheckError(f"platforms.{key} must have only downloadUrl and sha256")
            validate_source(source.get("downloadUrl"), source.get("sha256"), f"platforms.{key}.")

    if "yanked" in record and not isinstance(record["yanked"], bool):
        raise CheckError("yanked must be true or false")
    env = record.get("testEnv")
    if env is not None:
        if not isinstance(env, dict):
            raise CheckError("testEnv must be an object")
        extra = set(env) - TESTENV_KEYS
        if extra:
            raise CheckError(f"unknown testEnv field(s): {', '.join(sorted(extra))}")
        if not isinstance(env.get("image"), str) or not IMAGE_RE.match(env["image"]):
            raise CheckError("testEnv.image must be a ghcr.io/queryahub/querya-testdb-<db>:<tag> image")
        port = env.get("port")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise CheckError("testEnv.port must be a port number")
        if "connect" in env and not isinstance(env["connect"], dict):
            raise CheckError("testEnv.connect must be an object")
        for key in ("query", "table", "database"):
            if key in env and not isinstance(env[key], str):
                raise CheckError(f"testEnv.{key} must be a string")


def download(url, dest):
    request = urllib.request.Request(url, headers={"User-Agent": "querya-extensions-registry"})
    size = 0
    with urllib.request.urlopen(request, timeout=60) as response, open(dest, "wb") as out:
        final = response.geturl()
        if not final.startswith("https://"):
            raise CheckError(f"the download was redirected to a non-https URL: {final}")
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_ARCHIVE_BYTES:
                raise CheckError(f"the archive is larger than {MAX_ARCHIVE_BYTES // (1 << 20)} MB")
            out.write(chunk)
    return size


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def entry_name_problem(name):
    if not name or name.startswith("/") or "\\" in name or "\x00" in name:
        return "an absolute or malformed path"
    parts = posixpath.normpath(name).split("/")
    if ".." in parts or re.match(r"^[A-Za-z]:", name):
        return "a path that leaves the extension directory"
    return None


def inspect_archive(path):
    """Checks the zip is safe to unpack and returns manifest.json from its root."""
    if not zipfile.is_zipfile(path):
        raise CheckError("the asset is not a zip archive")
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise CheckError(f"the archive has more than {MAX_ENTRIES} entries")
        total = 0
        for info in infos:
            problem = entry_name_problem(info.filename)
            if problem:
                raise CheckError(f"entry {info.filename!r} is {problem}")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise CheckError(f"entry {info.filename!r} is a symbolic link")
            total += info.file_size
            if total > MAX_UNPACKED_BYTES:
                raise CheckError(f"the archive unpacks to more than {MAX_UNPACKED_BYTES // (1 << 20)} MB")
        names = {i.filename for i in infos}
        if "manifest.json" not in names:
            raise CheckError("manifest.json is not at the root of the archive")
        try:
            manifest = json.loads(zf.read("manifest.json"))
        except ValueError as e:
            raise CheckError(f"manifest.json is not valid JSON: {e}")
        if not isinstance(manifest, dict):
            raise CheckError("manifest.json must be a JSON object")
        main = manifest.get("main")
        if manifest.get("type") == "database_driver" and (not isinstance(main, str) or main not in names):
            raise CheckError(f"manifest.json main {main!r} is not a file in the archive")
        return manifest


# What every platform's manifest.json has to agree on (main may differ: bin/x vs bin/x.exe).
SAME_ACROSS_PLATFORMS = ("id", "version", "name", "type", "engines", "manifestVersion")


def base_entry(record, manifest):
    """The index entry's fields that come from manifest.json."""
    for key in ("id", "version"):
        if manifest.get(key) != record[key]:
            raise CheckError(f"manifest.json {key} is {manifest.get(key)!r}, the record says {record[key]!r}")
    for key in ("name", "type"):
        if not isinstance(manifest.get(key), str) or not manifest[key].strip():
            raise CheckError(f"manifest.json has no {key}")
    engines = manifest.get("engines")
    if not isinstance(engines, dict) or not isinstance(engines.get("querya_desktop"), str):
        raise CheckError('manifest.json needs engines.querya_desktop, for example ">=0.4.19"')
    entry = {k: manifest[k] for k in MANIFEST_KEYS if k in manifest}
    entry["manifestVersion"] = manifest_version(manifest)
    return entry


def manifest_version(manifest):
    """manifestVersion of manifest.json: a whole number >= 1, 1 when absent."""
    value = manifest.get("manifestVersion", 1)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise CheckError(f"manifest.json manifestVersion must be a whole number, 1 or more, got {value!r}")
    if value > MAX_MANIFEST_VERSION:
        raise CheckError(
            f"manifest.json manifestVersion {value} is not defined yet; Querya Desktop reads formats up to {MAX_MANIFEST_VERSION}")
    return value


def sources_of(record):
    """{platform or None: (url, sha256)}: None is one archive for every platform."""
    if "platforms" in record:
        return {k: (v["downloadUrl"], v["sha256"]) for k, v in record["platforms"].items()}
    return {None: (record["downloadUrl"], record["sha256"])}


def cmd_check(args):
    record_path = Path(args.record)
    try:
        record = json.loads(record_path.read_text())
    except ValueError as e:
        raise CheckError(f"{record_path.name} is not valid JSON: {e}")
    validate_record(record, record_path.name)

    local = {}  # tests and fixtures: --archive [PLATFORM=]FILE skips the download
    for item in args.archive or []:
        key, sep, path = item.partition("=")
        local[key if sep else None] = Path(path if sep else key)

    entry = None
    platforms = {}
    first_manifest = None
    testable = None  # the archive CI can run: linux-x64, or the one for every platform
    for key, (url, sha) in sources_of(record).items():
        label = key or "all"
        if key in local:
            archive, size = local[key], local[key].stat().st_size
        else:
            archive = Path(args.out).with_name(f"{Path(args.out).stem}-{label}.zip")
            size = download(url, archive)
        actual = sha256_of(archive)
        if actual != sha:
            raise CheckError(f"{label}: sha256 mismatch: the record says {sha}, the asset is {actual}")
        try:
            manifest = inspect_archive(archive)
            fields = base_entry(record, manifest)
        except CheckError as e:
            raise CheckError(f"{label}: {e}")
        if first_manifest is None:
            first_manifest, entry = manifest, fields
        else:
            for same in SAME_ACROSS_PLATFORMS:
                if manifest.get(same) != first_manifest.get(same):
                    raise CheckError(f"{label}: manifest.json {same} differs from the other platforms")
        platforms[key] = {"downloadUrl": url, "sha256": sha, "size": size, "main": manifest.get("main")}
        if key in (None, "linux-x64"):
            testable = archive
            if key == "linux-x64":  # prefer the manifest of the build that is tested
                first_manifest, entry = manifest, fields

    if None in platforms:
        only = platforms[None]
        entry.update(downloadUrl=only["downloadUrl"], sha256=only["sha256"], size=only["size"])
    else:
        entry["platforms"] = {
            k: {kk: vv for kk, vv in v.items() if vv is not None} for k, v in sorted(platforms.items())
        }
    if record.get("yanked"):
        entry["yanked"] = True
    Path(args.out).write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n")

    if args.extract and testable is not None:
        with zipfile.ZipFile(testable) as zf:
            zf.extractall(args.extract)
    if not args.keep_archive:
        for key in platforms:
            if key not in local:
                label = key or "all"
                Path(args.out).with_name(f"{Path(args.out).stem}-{label}.zip").unlink(missing_ok=True)
    total = sum(p["size"] for p in platforms.values())
    print(f"ok {record['id']} {record['version']} ({len(platforms)} archive(s), {total} bytes)")


def cmd_test_args(args):
    """Writes how to test a record as GitHub step outputs: testable, image, port,
    connect (JSON for db.connect), query, table."""
    record = json.loads(Path(args.record).read_text())
    entry = json.loads(Path(args.entry).read_text())
    env = record.get("testEnv")
    # CI runs Linux x86-64: the one-for-all archive, or the linux-x64 build.
    runnable = "platforms" not in entry or "linux-x64" in entry["platforms"]
    testable = entry.get("type") == "database_driver" and env is not None and runnable
    out = {"testable": "true" if testable else "false"}
    if testable:
        connect = {"host": "127.0.0.1", "port": env["port"], **env.get("connect", {})}
        if env.get("database"):
            connect["database"] = env["database"]
        out.update(
            image=env["image"],
            port=str(env["port"]),
            connect=json.dumps(connect, separators=(",", ":")),
            query=env.get("query", "SELECT 1"),
            table=env.get("table", ""),
        )
    for key, value in out.items():
        if "\n" in value:
            raise CheckError(f"testEnv.{key} must be a single line")
        print(f"{key}={value}")


def cmd_build_index(args):
    entries = []
    for path in sorted(Path(args.entries).glob("*.json")):
        entries.append(json.loads(path.read_text()))
    ids = [e["id"] for e in entries]
    if len(ids) != len(set(ids)):
        raise CheckError("two records have the same id")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    index = {
        "schema": INDEX_SCHEMA,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "extensions": entries,
    }
    (out / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    if args.compat and Path(args.compat).exists():
        (out / "compat.json").write_text(Path(args.compat).read_text())
    schema_dir = out / "schema"
    schema_dir.mkdir(exist_ok=True)
    src = Path(__file__).resolve().parent.parent / "schema" / "record.schema.json"
    if src.exists():
        (schema_dir / src.name).write_text(src.read_text())
    print(f"index.json: {len(entries)} extension(s)")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("record")
    check.add_argument("--out", required=True)
    check.add_argument("--archive", action="append",
                       help="use this local zip instead of downloading (tests, fixtures); "
                            "PLATFORM=FILE for a per-platform record, repeatable")
    check.add_argument("--extract", help="also unpack the archive here")
    check.add_argument("--keep-archive", action="store_true")
    check.set_defaults(func=cmd_check)
    test_args = sub.add_parser("test-args")
    test_args.add_argument("record")
    test_args.add_argument("entry")
    test_args.set_defaults(func=cmd_test_args)
    build = sub.add_parser("build-index")
    build.add_argument("entries")
    build.add_argument("--out", required=True)
    build.add_argument("--compat")
    build.set_defaults(func=cmd_build_index)
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except CheckError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
