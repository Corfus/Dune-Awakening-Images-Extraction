#!/usr/bin/env python3
"""Pinned Steam archive validation and daemonless registry mirroring (Python 3.10+)."""
import argparse
import contextlib
import datetime
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

PREFIX = "registry.funcom.com/funcom/self-hosting/"
# Archive filenames differ from their embedded image names.
IMAGES = {
    **{f"images/operators/{n}-operator.tar": (f"igw-k8s-{n}-operator", "operator_version")
       for n in ("battlegroup", "database", "server", "utilities")},
    **{f"images/battlegroup/{n}.tar": (f"seabass-{n}", "game_version") for n in (
        "server", "server-bg-director", "server-gateway", "server-text-router",
        "server-rabbitmq", "server-db-utils")},
    "images/prerequisites/igw-postgres.tar": ("igw-postgres", "postgres_version"),
}
METADATA = {"images/operators/version.txt": "operator_version",
            "images/battlegroup/version.txt": "game_version"}


class MirrorError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise MirrorError(message)


def sha(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def stream_sha(stream):
    digest = hashlib.sha256()
    while chunk := stream.read(4 * 1024 * 1024):
        digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".report-")
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(value, out, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def registry(required=True):
    """Destination comes exclusively from GitLab's project registry variable."""
    target = os.getenv("CI_REGISTRY_IMAGE")
    if not target:
        require(not required, "CI_REGISTRY_IMAGE is unavailable; enable GitLab Container Registry "
                "or export the project registry prefix when running locally")
        return None
    require(isinstance(target, str) and re.fullmatch(
        r"[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]+)?/[a-z0-9]+(?:[._-]+[a-z0-9]+)*(?:/[a-z0-9]+(?:[._-]+[a-z0-9]+)*)*",
        target), "Invalid registry image prefix; expected host[:port]/project/path without a tag")
    return target


def lockfile(path):
    raw = Path(path).read_bytes()
    lock = json.loads(raw)
    require(lock["steam_app"] == 4754530 and lock["steam_depot"] == 4754532,
            "Only the official Linux app 4754530 / depot 4754532 is supported")
    require(isinstance(lock["steam_manifest"], str) and
            re.fullmatch(r"[1-9][0-9]+", lock["steam_manifest"]), "Invalid manifest pin")
    for key in ("operator_version", "game_version", "postgres_version"):
        require(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", lock[key]) and
                lock[key].lower() != "latest", f"Invalid version pin: {key}")
    return lock, sha(raw)


def member_bytes(tar, name, limit=16 * 1024 * 1024):
    member = tar.getmember(name)
    require(member.isfile() and member.size <= limit, f"Invalid metadata member: {name}")
    return tar.extractfile(member).read()


def inspect_archive(path, source):
    """Check config hash, layer diffIDs and content-addressed blob hashes.

    Supports classic docker save and modern OCI-blob Docker archives. Never extracts paths.
    """
    with tarfile.open(path, "r:*") as tar:
        members = tar.getmembers()
        names = [m.name for m in members]
        require(len(names) == len(set(names)), f"Duplicate tar entries: {path}")
        for m in members:
            require(not PurePosixPath(m.name).is_absolute() and
                    ".." not in PurePosixPath(m.name).parts and
                    (m.isfile() or m.isdir() or m.issym() or m.islnk()), f"Unsafe tar entry: {m.name}")
            if m.issym() or m.islnk():
                target = posixpath.normpath(posixpath.join(posixpath.dirname(m.name), m.linkname)
                                           if m.issym() else m.linkname)
                require(not PurePosixPath(m.linkname).is_absolute() and
                        ".." not in PurePosixPath(target).parts and tar.getmember(target).isfile(),
                        f"Unsafe tar link: {m.name}")
        manifest = json.loads(member_bytes(tar, "manifest.json"))
        require(isinstance(manifest, list) and len(manifest) == 1,
                f"Expected exactly one image in {path}")
        image = manifest[0]
        require(image.get("RepoTags") == [source],
                f"{path}: expected exactly {source}, found {image.get('RepoTags')}")
        raw = member_bytes(tar, image["Config"])
        config = json.loads(raw)
        config_digest = sha(raw)
        require(Path(image["Config"]).stem == config_digest.split(":")[1],
                f"Config digest mismatch: {path}")
        require(config.get("os") == "linux" and config.get("architecture") == "amd64",
                f"Expected linux/amd64: {path}")
        diffids = config["rootfs"]["diff_ids"]
        require(config["rootfs"]["type"] == "layers" and
                len(image["Layers"]) == len(diffids) and len(diffids) > 0,
                f"Layer count mismatch: {path}")
        for name, expected in zip(image["Layers"], diffids):
            member = tar.getmember(name)
            require(member.isfile(), f"Missing layer: {name}")
            with tar.extractfile(member) as layer:
                header = layer.read(2)
                layer.seek(0)
                if header == b"\x1f\x8b":
                    with gzip.GzipFile(fileobj=layer) as plain:
                        actual = stream_sha(plain)
                else:
                    actual = stream_sha(layer)
            require(actual == expected, f"Layer diffID mismatch: {path}:{name}")
            if name.startswith("blobs/sha256/"):
                with tar.extractfile(member) as layer:
                    require(stream_sha(layer) == "sha256:" + Path(name).name,
                            f"Blob digest mismatch: {path}:{name}")
    with open(path, "rb") as archive:
        archive_digest = stream_sha(archive)
    return {"config_digest": config_digest, "diff_ids": diffids,
            "archive_sha256": archive_digest, "archive_bytes": path.stat().st_size}


def validate(package, lock):
    target = registry(required=False)
    images = []
    for relative, (name, version) in IMAGES.items():
        archive = package / relative
        require(archive.is_file(), f"Missing archive: {archive}; run download again")
        source = PREFIX + name + ":" + lock[version]
        print(f"Validating {relative}", file=sys.stderr, flush=True)
        images.append({"archive": str(archive.resolve()), "source": source,
                       "destination": target + "/" + name + ":" + lock[version] if target else None,
                       **inspect_archive(archive, source)})
    for relative, version in METADATA.items():
        metadata = package / relative
        if metadata.exists():
            require(metadata.read_text().strip() == lock[version],
                    f"Version metadata differs from lock: {metadata}")
    return images


def tool(name):
    require(shutil.which(name), f"Missing dependency: {name}; see README.md")
    return name


def execute(argv, **kwargs):
    # Authentication error output from third-party tools can contain secrets.
    return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)


def skopeo(args, authfile=None, missing_ok=False):
    command = [tool("skopeo"), *args]
    if authfile:
        command += ["--authfile", str(authfile)]
    result = execute(command)
    if result.returncode:
        error = result.stderr.decode(errors="replace").lower()
        if missing_ok and any(code in error for code in
                              ("manifest unknown", "manifest_unknown", "name unknown", "name_unknown")):
            return None
        category = next((label for marker, label in (
            ("denied", "registry access denied"), ("unauthorized", "authentication rejected"),
            ("certificate", "TLS certificate failure"), ("timeout", "network timeout"),
            ("no such host", "DNS resolution failed"), ("no space left", "disk full"),
            ("policy.json", "Skopeo trust policy missing")) if marker in error), "registry operation failed")
        raise MirrorError(f"Skopeo: {category} (raw error suppressed to protect credentials). "
                          "Check registry access, credentials, TLS, and disk space.")
    return result.stdout


@contextlib.contextmanager
def credentials():
    username, password = os.getenv("CI_REGISTRY_USER"), os.getenv("CI_REGISTRY_PASSWORD")
    if os.getenv("CI") == "true":
        require(all(os.getenv(key) for key in (
            "CI_REGISTRY", "CI_REGISTRY_IMAGE", "CI_REGISTRY_USER", "CI_REGISTRY_PASSWORD")),
            "GitLab registry variables are unavailable; enable Container Registry for this project")
    require(bool(username) == bool(password), "CI_REGISTRY_USER and CI_REGISTRY_PASSWORD must be supplied together")
    host = registry().split("/")[0]
    if username:
        require(os.getenv("CI_REGISTRY") == host,
                "CI_REGISTRY must match the host in CI_REGISTRY_IMAGE")
    if username:
        with tempfile.TemporaryDirectory(prefix="dune-auth-") as tmp:
            path = Path(tmp) / "auth.json"
            result = execute([tool("skopeo"), "login", "--tls-verify=true", "--authfile", str(path),
                              "--username", username, "--password-stdin", host],
                             input=(password + "\n").encode())
            require(result.returncode == 0, "GitLab registry login failed; check that the job token has registry access")
            path.chmod(0o600)
            yield path
    else:
        # Default Skopeo auth lookup includes existing Docker credentials.
        yield os.getenv("REGISTRY_AUTH_FILE")


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def remote(image, authfile, missing_ok=False, deep=False):
    reference = "docker://" + image["destination"]
    raw = skopeo(["inspect", "--tls-verify=true", "--raw", reference], authfile, missing_ok)
    if raw is None:
        return None
    manifest = json.loads(raw)
    require(manifest.get("schemaVersion") == 2 and "config" in manifest and "layers" in manifest,
            f"Expected a single-image manifest: {image['destination']}")
    require(manifest["config"]["digest"] == image["config_digest"],
            f"Destination tag conflict (will not overwrite): {image['destination']}")
    digest = sha(raw)
    immutable = "docker://" + image["destination"].rsplit(":", 1)[0] + "@" + digest
    config_raw = skopeo(["inspect", "--tls-verify=true", "--config", "--raw", immutable], authfile)
    require(sha(config_raw) == image["config_digest"], "Remote config content digest differs")
    config = json.loads(config_raw)
    require(config["rootfs"]["diff_ids"] == image["diff_ids"] and
            len(manifest["layers"]) == len(image["diff_ids"]), "Remote layer identity differs")
    if deep:
        with tempfile.TemporaryDirectory(prefix="dune-verify-") as tmp:
            archive = Path(tmp) / "image.tar"
            skopeo(["copy", "--src-tls-verify=true", immutable, "docker-archive:" + str(archive) +
                    ":" + image["source"]], authfile)
            verified = inspect_archive(archive, image["source"])
            require(verified["config_digest"] == image["config_digest"], "Downloaded image differs")
    return {"destination_digest": digest, "verified_at": now(),
            "verification": "full-pull" if deep else "manifest-and-config"}


def depot(package, lock, executable, filelist, pinned=True):
    package.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt") as listing:
        listing.write("\n".join(filelist) + "\n")
        listing.flush()
        command = [tool(executable), "-app", str(lock["steam_app"]), "-depot", str(lock["steam_depot"]),
                   "-os", "linux", "-osarch", "64", "-dir", str(package.resolve()),
                   "-filelist", listing.name, "-validate"]
        if pinned:
            command += ["-manifest", lock["steam_manifest"]]
        # Anonymous Steam login only; no Steam secrets accepted or cached.
        result = subprocess.run(command)
        require(result.returncode == 0, "DepotDownloader failed; rerun download to resume")
    cache = package / ".DepotDownloader"
    if pinned:
        require((cache / f"{lock['steam_depot']}_{lock['steam_manifest']}.manifest").is_file(),
                "Pinned Steam manifest cache missing; download did not complete correctly")
    return list(cache.glob(f"{lock['steam_depot']}_*.manifest"))


def run(args):
    lock, lock_digest = lockfile(args.lock)
    target = registry(required=args.command in ("plan", "publish", "verify"))
    package = (args.package or Path(".work/packages") / lock["steam_manifest"]).resolve()
    report_path = args.report or Path("reports") / (args.command + ".json")
    report = {"schema_version": 1, "command": args.command, "started_at": now(),
              "lock": lock, "lock_sha256": lock_digest, "package": str(package),
              "destination_registry": target,
              "status": "in-progress", "images": []}
    try:
        if args.command == "download":
            depot(package, lock, args.depotdownloader, [*IMAGES, *METADATA])
            for metadata in METADATA:
                require((package / metadata).is_file(), f"Missing downloaded metadata: {metadata}")
            atomic_json(package / "source.json", {"steam_app": lock["steam_app"],
                        "steam_depot": lock["steam_depot"], "steam_manifest": lock["steam_manifest"],
                        "downloaded_at": now(), "method": "DepotDownloader -validate"})
        provenance = package / "source.json"
        report["source_provenance"] = "local-input-unattested"
        if provenance.is_file():
            source = json.loads(provenance.read_text())
            require(all(source[k] == lock[k] for k in ("steam_app", "steam_depot", "steam_manifest")),
                    "Package source provenance conflicts with lock")
            report["source_provenance"] = source
        images = validate(package, lock)  # Entire batch before any registry access.
        report["images"] = images
        for image in images:
            image["status"] = "validated"
            if args.command == "plan":
                image["action"] = "publish-if-absent; fully-verify-before-skip; refuse-conflicts"
        if args.command in ("publish", "verify"):
            with credentials() as authfile:
                for image in images:  # Preflight all tags before any push.
                    observed = remote(image, authfile, missing_ok=args.command == "publish",
                                      deep=args.deep or args.command == "publish")
                    image["status"] = "missing" if observed is None else "verified"
                    if observed:
                        image.update(observed)
                atomic_json(report_path, report)
                for image in images:
                    if image["status"] == "missing":
                        print(f"Publishing {image['destination']}", file=sys.stderr, flush=True)
                        skopeo(["copy", "--dest-tls-verify=true", "--format", "v2s2",
                                "docker-archive:" + image["archive"],
                                "docker://" + image["destination"]], authfile)
                        image.update(remote(image, authfile, deep=args.deep))
                        image["status"] = "published"
                        atomic_json(report_path, report)
        report["status"] = "complete"
        report["finished_at"] = now()
        atomic_json(report_path, report)
        print(json.dumps(report, indent=2))
    except (Exception, KeyboardInterrupt) as error:
        report["status"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        report["finished_at"] = now()
        atomic_json(report_path, report)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["download", "validate", "plan", "publish", "verify"])
    parser.add_argument("--lock", type=Path, default=Path(__file__).with_name("images.lock"))
    parser.add_argument("--package", type=Path, help="Existing package; default .work/packages/<manifest>")
    parser.add_argument("--report", type=Path, help="JSON path; default reports/<command>.json")
    parser.add_argument("--depotdownloader", default=os.getenv("DEPOT_DOWNLOADER", "DepotDownloader"))
    parser.add_argument("--deep", action="store_true", help="Pull remote images and verify every layer")
    args = parser.parse_args()
    Path(".work").mkdir(exist_ok=True)
    with open(".work/mirror.lck", "w") as mutex:
        try:
            try:
                fcntl.flock(mutex, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise MirrorError("Another mirror command is running in this checkout") from None
            run(args)
        except (MirrorError, OSError, ValueError, KeyError, tarfile.TarError, EOFError) as error:
            print(f"Error: {error}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("Interrupted. Rerun the same command to resume.", file=sys.stderr)
            return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
