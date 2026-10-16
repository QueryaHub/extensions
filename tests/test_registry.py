import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import registry  # noqa: E402

URL = "https://github.com/acme/ext/releases/download/v1.0.0/ext.zip"
MANIFEST = {
    "id": "acme.redis-driver",
    "name": "Redis",
    "version": "1.0.0",
    "publisher": "Acme",
    "type": "database_driver",
    "main": "bin/driver",
    "engines": {"querya_desktop": ">=0.4.19"},
    "contributions": {"drivers": [{"driverId": "redis", "displayName": "Redis"}]},
}


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def zip(self, files=None, manifest=MANIFEST, extra=None):
        path = self.dir / "ext.zip"
        with zipfile.ZipFile(path, "w") as zf:
            if manifest is not None:
                zf.writestr("manifest.json", json.dumps(manifest))
            for name, data in (files if files is not None else {"bin/driver": "x"}).items():
                zf.writestr(name, data)
            for info, data in extra or []:
                zf.writestr(info, data)
        return path

    def record(self, archive, **over):
        record = {
            "id": "acme.redis-driver",
            "version": "1.0.0",
            "downloadUrl": URL,
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        }
        record.update(over)
        path = self.dir / f"{record['id']}.json"
        path.write_text(json.dumps(record))
        return path

    def check(self, archive, record_path):
        out = self.dir / "entry.json"
        code = registry.main(["check", str(record_path), "--out", str(out), "--archive", str(archive)])
        return code, out


class Check(Case):
    def test_good_record_gives_an_entry_from_the_manifest(self):
        archive = self.zip()
        code, out = self.check(archive, self.record(archive))
        self.assertEqual(code, 0)
        entry = json.loads(out.read_text())
        self.assertEqual(entry["name"], "Redis")
        self.assertEqual(entry["downloadUrl"], URL)
        self.assertEqual(entry["sha256"], hashlib.sha256(archive.read_bytes()).hexdigest())
        self.assertEqual(entry["size"], archive.stat().st_size)
        self.assertEqual(entry["engines"], {"querya_desktop": ">=0.4.19"})
        self.assertNotIn("yanked", entry)

    def test_yanked_is_carried_to_the_entry(self):
        archive = self.zip()
        _, out = self.check(archive, self.record(archive, yanked=True))
        self.assertTrue(json.loads(out.read_text())["yanked"])

    def test_wrong_sha256(self):
        archive = self.zip()
        code, _ = self.check(archive, self.record(archive, sha256="0" * 64))
        self.assertEqual(code, 1)

    def test_manifest_disagrees_with_the_record(self):
        archive = self.zip(manifest=dict(MANIFEST, version="2.0.0"))
        self.assertEqual(self.check(archive, self.record(archive))[0], 1)
        archive = self.zip(manifest=dict(MANIFEST, id="acme.other"))
        self.assertEqual(self.check(archive, self.record(archive))[0], 1)

    def test_manifest_must_declare_engines(self):
        bad = {k: v for k, v in MANIFEST.items() if k != "engines"}
        archive = self.zip(manifest=bad)
        self.assertEqual(self.check(archive, self.record(archive))[0], 1)

    def test_no_manifest(self):
        archive = self.zip(manifest=None)
        self.assertEqual(self.check(archive, self.record(archive))[0], 1)

    def test_driver_main_must_be_in_the_archive(self):
        archive = self.zip(files={"other": "x"})
        self.assertEqual(self.check(archive, self.record(archive))[0], 1)

    def test_path_traversal_and_absolute_entries(self):
        for name in ("../evil", "/etc/evil", "a/../../evil", "C:/evil", "a\\b"):
            archive = self.zip(extra=[(name, "x")])
            self.assertEqual(self.check(archive, self.record(archive))[0], 1, name)

    def test_symlink_entry(self):
        info = zipfile.ZipInfo("link")
        info.external_attr = (0o120777 << 16)
        archive = self.zip(extra=[(info, "bin/driver")])
        self.assertEqual(self.check(archive, self.record(archive))[0], 1)

    def test_not_a_zip(self):
        path = self.dir / "ext.zip"
        path.write_text("nope")
        self.assertEqual(self.check(path, self.record(path))[0], 1)


class Record(Case):
    def bad(self, **over):
        archive = self.zip()
        return self.check(archive, self.record(archive, **over))[0]

    def test_download_url_must_be_a_github_release_asset(self):
        for url in (
            "https://example.com/ext.zip",
            "http://github.com/acme/ext/releases/download/v1/ext.zip",
            "https://github.com/acme/ext/archive/main.zip",
            "https://github.com.evil.com/acme/ext/releases/download/v1/ext.zip",
            "https://github.com/acme/ext/releases/latest",
        ):
            self.assertEqual(self.bad(downloadUrl=url), 1, url)

    def test_field_formats(self):
        self.assertEqual(self.bad(version="1.0"), 1)
        self.assertEqual(self.bad(id="Acme.Redis"), 1)
        self.assertEqual(self.bad(id="single"), 1)
        self.assertEqual(self.bad(sha256="ABC"), 1)
        self.assertEqual(self.bad(unknown="x"), 1)
        self.assertEqual(self.bad(yanked="yes"), 1)

    def test_file_name_must_be_the_id(self):
        archive = self.zip()
        path = self.record(archive)
        renamed = path.with_name("something-else.json")
        path.rename(renamed)
        self.assertEqual(self.check(archive, renamed)[0], 1)

    def test_test_env(self):
        good = {"image": "ghcr.io/queryahub/querya-testdb-redis:0.4.19", "port": 6379}
        self.assertEqual(self.bad(testEnv=good), 0)
        self.assertEqual(self.bad(testEnv=dict(good, image="docker.io/library/redis:7")), 1)
        self.assertEqual(self.bad(testEnv=dict(good, image="ghcr.io/evil/querya-testdb-x:1")), 1)
        self.assertEqual(self.bad(testEnv=dict(good, port=0)), 1)
        self.assertEqual(self.bad(testEnv=dict(good, port=True)), 1)
        self.assertEqual(self.bad(testEnv=dict(good, extra=1)), 1)
        self.assertEqual(self.bad(testEnv={"port": 1}), 1)


class BuildIndex(Case):
    def test_joins_entries_and_copies_compat(self):
        entries = self.dir / "entries"
        entries.mkdir()
        (entries / "b.json").write_text(json.dumps({"id": "b.two"}))
        (entries / "a.json").write_text(json.dumps({"id": "a.one"}))
        compat = self.dir / "compat.json"
        compat.write_text('{"hosts": ["0.5.0"]}')
        site = self.dir / "site"
        self.assertEqual(registry.main(["build-index", str(entries), "--out", str(site), "--compat", str(compat)]), 0)
        index = json.loads((site / "index.json").read_text())
        self.assertEqual(index["schema"], 1)
        self.assertEqual([e["id"] for e in index["extensions"]], ["a.one", "b.two"])
        self.assertRegex(index["generatedAt"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(json.loads((site / "compat.json").read_text()), {"hosts": ["0.5.0"]})

    def test_duplicate_ids_are_refused(self):
        entries = self.dir / "entries"
        entries.mkdir()
        (entries / "a.json").write_text(json.dumps({"id": "x.y"}))
        (entries / "b.json").write_text(json.dumps({"id": "x.y"}))
        self.assertEqual(registry.main(["build-index", str(entries), "--out", str(self.dir / "s")]), 1)

    def test_empty_registry_is_a_valid_index(self):
        entries = self.dir / "entries"
        entries.mkdir()
        site = self.dir / "site"
        self.assertEqual(registry.main(["build-index", str(entries), "--out", str(site)]), 0)
        self.assertEqual(json.loads((site / "index.json").read_text())["extensions"], [])


if __name__ == "__main__":
    unittest.main()
