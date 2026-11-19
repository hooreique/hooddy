"""Protocol v1 activation. Called by hooddy.sh while its flock is held.

All service-manager access stays at the subprocess boundary so activation and
crash recovery can be tested without touching a user's actual services.
"""

import base64
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit


class ActivationError(Exception):
    pass


def run(*args, check=True):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode:
        raise ActivationError(f"{' '.join(map(str, args))}: {result.stderr.strip() or result.stdout.strip() or 'failed'}")
    return result


def exists(path):
    return path.exists() or path.is_symlink()


def plain_parents(path):
    for parent in [path, *path.parents]:
        if parent.is_symlink():
            raise ActivationError(f"refusing symlink destination: {parent}")


def read_regular(path):
    plain_parents(path)
    if not path.is_file():
        raise ActivationError(f"not a regular file: {path}")
    return path.read_bytes()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sync_dir(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path, data, mode=0o600):
    plain_parents(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".hooddy-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def write_json(path, value):
    atomic_write(path, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode())


def remove(path):
    if exists(path):
        path.unlink()
        sync_dir(path.parent)


def read_json(path):
    try:
        return json.loads(read_regular(path))
    except (ValueError, UnicodeError) as error:
        raise ActivationError(f"invalid JSON in {path}: {error}") from error


def service_name(name):
    return f"hooddy-{name}.service"


def has_generated_import(text):
    depth = 0
    for line in text.splitlines():
        if depth == 0 and re.fullmatch(r'\s*import\s+(?:generated/sites\.caddy|"generated/sites\.caddy")\s*(?:#.*)?', line):
            return True
        # Ignore braces in strings, environment expansions and comments.
        structural = re.sub(r'"(?:\\.|[^"\\])*"|\{\$[^}]*\}|#.*', "", line)
        depth += structural.count("{") - structural.count("}")
    return False


def valid_health(health):
    if health is None:
        return
    if not isinstance(health, dict) or set(health) != {"url", "timeoutSeconds"}:
        raise ActivationError("healthCheck must contain url and timeoutSeconds")
    timeout = health["timeoutSeconds"]
    if type(timeout) is not int or timeout <= 0:
        raise ActivationError("healthCheck timeoutSeconds must be a positive integer")
    url = health["url"]
    if not isinstance(url, str) or any(ord(c) < 33 for c in url):
        raise ActivationError("healthCheck URL must be loopback HTTP")
    try:
        parsed = urlsplit(url)
        address = ipaddress.ip_address("127.0.0.1" if parsed.hostname == "localhost" else parsed.hostname or "")
        port = parsed.port
        valid = (parsed.scheme == "http" and address.is_loopback and not parsed.username
                 and not parsed.password and not parsed.fragment and port != 0)
    except ValueError:
        valid = False
    if not valid:
        raise ActivationError("healthCheck URL must use a loopback HTTP address")


def validate_unit(text, name):
    section = ""
    wanted = False
    start = False
    # Continuations are ordinary systemd syntax; validate the resulting values.
    text = re.sub(r"\\\n\s*", " ", text)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            section = line
        elif "=" in line:
            key, value = (part.strip() for part in line.split("=", 1))
            if section == "[Install]":
                if key == "WantedBy" and value == "default.target":
                    wanted = True
                elif key in {"WantedBy", "RequiredBy", "UpheldBy", "Alias", "Also", "DefaultInstance"}:
                    raise ActivationError(f"{name}: only WantedBy=default.target may install links")
            if section == "[Service]" and key.startswith("Exec") and value:
                executable = value.lstrip("-@:+!").lstrip('"').split()[0].rstrip('"')
                if not re.fullmatch(r"/nix/store/[a-z0-9]{32}-[^/\s]+/[^\s]+", executable):
                    raise ActivationError(f"{name}: executables must use absolute store paths")
                if key == "ExecStart":
                    start = True
    if not wanted or not start:
        raise ActivationError(f"{name}: requires ExecStart and [Install] WantedBy=default.target")


def generation(path):
    path = path.resolve(strict=True)
    manifest = read_json(path / "manifest.json")
    if not isinstance(manifest, dict) or type(manifest.get("protocolVersion")) is not int or manifest["protocolVersion"] != 1:
        raise ActivationError("incompatible generation protocol version (CLI supports 1)")
    system = {"x86_64": "x86_64-linux", "aarch64": "aarch64-linux"}.get(platform.machine())
    if manifest.get("system") != system or system is None:
        raise ActivationError(f"generation system {manifest.get('system')} does not match host {system}")
    if manifest.get("sites") != "sites.caddy" or not isinstance(manifest.get("services"), dict):
        raise ActivationError("invalid generation manifest paths or services")
    files = {"generated/sites.caddy": read_regular(path / "sites.caddy")}
    for name, definition in manifest["services"].items():
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", name):
            raise ActivationError(f"invalid managed name: {name}")
        unit = service_name(name)
        if not isinstance(definition, dict) or definition.get("unit") != f"units/{unit}":
            raise ActivationError(f"invalid unit path for {name}")
        if "healthCheck" not in definition:
            raise ActivationError(f"missing healthCheck for {name}")
        valid_health(definition["healthCheck"])
        data = read_regular(path / "units" / unit)
        validate_unit(data.decode(), unit)
        files[unit] = data
    return {"generation": str(path), "manifest": manifest,
            "files": {key: digest(data) for key, data in files.items()}}, files


class Switch:
    def __init__(self):
        self.config = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "hooddy"
        self.units = self.config.parent / "systemd/user"
        self.state = self.config / ".switch"
        self.roots = self.state / "roots"
        self.current = self.state / "current.json"
        self.journal = self.state / "journal.json"
        plain_parents(self.state)

    def destination(self, key):
        if key == "generated/sites.caddy":
            return self.config / key
        if re.fullmatch(r"hooddy-[a-zA-Z0-9][a-zA-Z0-9_-]*\.service", key):
            return self.units / key
        raise ActivationError(f"invalid managed destination: {key}")

    def root(self, name, target):
        self.roots.mkdir(parents=True, exist_ok=True)
        plain_parents(self.roots)
        destination = self.roots / name
        if exists(destination) and not destination.is_symlink():
            raise ActivationError(f"unexpected GC root: {destination}")
        if target is None:
            remove(destination)
            return
        temporary = self.roots / f".{name}-new"
        if exists(temporary):
            if not temporary.is_symlink():
                raise ActivationError(f"unexpected GC root: {temporary}")
            remove(temporary)
        run("nix-store", "--add-root", str(temporary), "--indirect", "-r", target)
        # Register the final path as well: indirect roots track the link's name.
        if exists(destination):
            remove(destination)
        run("nix-store", "--add-root", str(destination), "--indirect", "-r", target)
        sync_dir(self.roots)
        remove(temporary)

    def load_current(self):
        if not exists(self.current):
            return None
        recorded = read_json(self.current)
        if not isinstance(recorded, dict) or not isinstance(recorded.get("generation"), str):
            raise ActivationError("invalid current generation record")
        actual, _ = generation(Path(recorded["generation"]))
        if actual != recorded:
            raise ActivationError("current generation record disagrees with immutable manifest")
        root = self.roots / "current"
        if not root.is_symlink() or root.resolve() != Path(recorded["generation"]):
            raise ActivationError("current generation GC root is missing or inconsistent")
        return recorded

    def systemctl(self, *args, check=True):
        return run("systemctl", "--user", *args, check=check)

    def status(self, unit):
        active = self.systemctl("is-active", unit, check=False)
        enabled = self.systemctl("is-enabled", unit, check=False)
        active_text = active.stdout.strip()
        enabled_text = enabled.stdout.strip()
        if active_text not in {"active", "inactive", "failed", "unknown"}:
            raise ActivationError(f"cannot record stable active state for {unit}: {active_text}")
        if enabled_text not in {"enabled", "enabled-runtime", "disabled", "not-found", ""}:
            raise ActivationError(f"conflicting enabled state for {unit}: {enabled_text}")
        return {"active": active_text == "active", "enabled": enabled_text}

    def unit_conflicts(self, unit, owned):
        destination = self.units / unit
        fragment = self.systemctl("show", unit, "--property=FragmentPath", "--value").stdout.strip()
        dropins = self.systemctl("show", unit, "--property=DropInPaths", "--value").stdout.strip()
        if fragment and (unit not in owned or Path(fragment) != destination):
            raise ActivationError(f"unit conflict for {unit}: {fragment}")
        if dropins or exists(self.units / f"{unit}.d"):
            raise ActivationError(f"unit conflict: unmanaged drop-ins for {unit}")
        wants = self.units / "default.target.wants" / unit
        plain_parents(wants.parent)
        if exists(wants) and (unit not in owned or not wants.is_symlink() or wants.resolve() != destination.resolve()):
            raise ActivationError(f"unit enablement conflict: {wants}")

    def ownership(self, old, candidate):
        previous = old["files"] if old else {}
        snapshots = {}
        for key in sorted(previous.keys() | candidate["files"].keys()):
            destination = self.destination(key)
            plain_parents(destination)
            data = read_regular(destination) if exists(destination) else None
            if data is not None and (key not in previous or digest(data) != previous[key]):
                raise ActivationError(f"refusing unmanaged or unexpectedly changed file: {destination}")
            snapshots[key] = base64.b64encode(data).decode() if data is not None else None
        names = set(old["manifest"]["services"] if old else {}) | set(candidate["manifest"]["services"])
        states = {}
        for name in sorted(names):
            unit = service_name(name)
            self.unit_conflicts(unit, previous)
            states[unit] = self.status(unit)
            if unit not in previous and (states[unit]["active"] or states[unit]["enabled"] in {"enabled", "enabled-runtime"}):
                raise ActivationError(f"unmanaged service state conflicts with {unit}")
        states["hooddy.service"] = self.status("hooddy.service")
        return snapshots, states

    def copy_config(self, destination):
        # Avoid copying roots back into the flake and recursively retaining old
        # generations. All application source files, including untracked ones,
        # remain in the snapshot.
        def ignore(directory, names):
            return {".switch", ".lock", "caddy"} & set(names) if Path(directory) == self.config else set()
        shutil.copytree(self.config, destination, symlinks=True, ignore=ignore)

    def validate(self, snapshot, candidate, files):
        caddyfile = snapshot / "Caddyfile"
        text = read_regular(caddyfile).decode()
        if not has_generated_import(text):
            raise ActivationError(f"add an explicit 'import generated/sites.caddy' line to {self.config / 'Caddyfile'}")
        generated = snapshot / "generated/sites.caddy"
        atomic_write(generated, files["generated/sites.caddy"], 0o644)
        executable = self.config / "caddy/bin/caddy"
        if not executable.is_file():
            raise ActivationError("installed Caddy executable is missing; reinstall hooddy")
        run(str(executable), "validate", "--config", str(caddyfile), "--adapter", "caddyfile")
        units = [str(Path(candidate["generation"]) / "units" / service_name(name))
                 for name in sorted(candidate["manifest"]["services"])]
        if units:
            run("systemd-analyze", "--user", "verify", *units)

    def healthy(self, name, health):
        unit = service_name(name)
        deadline = time.monotonic() + (health["timeoutSeconds"] if health else 5)
        error = "service is not active"
        while True:
            if self.systemctl("is-active", unit, check=False).returncode == 0:
                if health is None:
                    return
                parsed = urlsplit(health["url"])
                host = "127.0.0.1" if parsed.hostname == "localhost" else parsed.hostname
                connection = http.client.HTTPConnection(host, parsed.port or 80,
                                                       timeout=max(0.01, min(1, deadline - time.monotonic())))
                try:
                    connection.request("GET", (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else ""))
                    response = connection.getresponse()
                    if (200 <= response.status < 300
                            and self.systemctl("is-active", unit, check=False).returncode == 0
                            and time.monotonic() <= deadline):
                        return
                    error = f"HTTP {response.status}"
                except (OSError, http.client.HTTPException) as exception:
                    error = str(exception)
                finally:
                    connection.close()
            if time.monotonic() >= deadline:
                raise ActivationError(f"health check failed for {unit}: {error}")
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))

    def phase(self, journal, phase):
        updated = journal | {"phase": phase}
        write_json(self.journal, updated)
        journal.update(updated)

    def check_transaction_files(self, journal):
        for key, original in journal["snapshots"].items():
            destination = self.destination(key)
            plain_parents(destination)
            if exists(destination):
                allowed = {journal["candidate"]["files"].get(key)}
                if original is not None:
                    allowed.add(digest(base64.b64decode(original)))
                if key == "generated/sites.caddy" and journal.get("recoveryEmptySite") and original is None:
                    allowed.add(digest(b""))
                if digest(read_regular(destination)) not in allowed:
                    raise ActivationError(f"refusing unexpectedly changed file during activation/recovery: {destination}")

    def stop_if_running(self, unit):
        result = self.systemctl("is-active", unit, check=False)
        if result.stdout.strip() not in {"inactive", "failed", "unknown", ""}:
            self.systemctl("stop", unit)

    def disable_if_enabled(self, unit):
        if self.systemctl("is-enabled", unit, check=False).stdout.strip() in {"enabled", "enabled-runtime"}:
            self.systemctl("disable", unit)

    def restore(self, journal):
        old = journal["old"]
        failures = []

        def attempt(label, operation):
            try:
                operation()
            except (OSError, ActivationError, ValueError) as error:
                failures.append(f"{label}: {error}")

        # Check every file before any recovery mutation. A human edit must not
        # be overwritten just because a previous activation was interrupted.
        self.check_transaction_files(journal)
        for unit in journal["states"]:
            if unit != "hooddy.service":
                self.unit_conflicts(unit, journal["snapshots"])
        self.phase(journal, "rolling-back")
        for unit in journal["changed"]:
            attempt(f"stop {unit}", lambda unit=unit: self.stop_if_running(unit))
            if unit not in (old["files"] if old else {}):
                attempt(f"disable {unit}", lambda unit=unit: self.disable_if_enabled(unit))
        for key, original in journal["snapshots"].items():
            def restore_file(key=key, original=original):
                if original is None:
                    remove(self.destination(key))
                else:
                    atomic_write(self.destination(key), base64.b64decode(original), 0o644)
            attempt(f"restore {key}", restore_file)
        attempt("daemon-reload", lambda: self.systemctl("daemon-reload"))
        for unit, state in journal["states"].items():
            if unit == "hooddy.service":
                continue
            if state["enabled"] in {"enabled", "enabled-runtime"}:
                if state["enabled"] == "enabled-runtime":
                    attempt(f"clear persistent enablement of {unit}", lambda unit=unit: self.disable_if_enabled(unit))
                args = ["enable"] + (["--runtime"] if state["enabled"] == "enabled-runtime" else []) + [unit]
                attempt(f"enable {unit}", lambda args=args: self.systemctl(*args))
            elif unit in (old["files"] if old else {}):
                attempt(f"disable {unit}", lambda unit=unit: self.disable_if_enabled(unit))
            if state["active"]:
                attempt(f"start {unit}", lambda unit=unit: self.systemctl("start", unit))
            else:
                attempt(f"stop {unit}", lambda unit=unit: self.stop_if_running(unit))
        caddy = journal["states"]["hooddy.service"]
        if caddy["active"]:
            # First activation may have introduced the imported file. Preserve
            # pre-switch absence on disk after restoring the running config.
            site = self.config / "generated/sites.caddy"
            absent = not exists(site)
            try:
                if absent:
                    journal["recoveryEmptySite"] = True
                    write_json(self.journal, journal)
                    atomic_write(site, b"", 0o644)
                attempt("restore Caddy configuration", lambda: self.systemctl("reload-or-restart", "hooddy.service"))
            finally:
                if absent:
                    remove(site)
        else:
            attempt("stop Caddy", lambda: self.systemctl("stop", "hooddy.service"))
        if failures:
            journal["recoveryErrors"] = failures
            write_json(self.journal, journal)
            raise ActivationError("rollback failed; roots and recovery journal retained at "
                                  f"{self.journal}: " + "; ".join(failures))
        if old:
            self.root("current", old["generation"])
            write_json(self.current, old)
        else:
            self.root("current", None)
            remove(self.current)
        self.root("previous", journal.get("previous"))
        remove(self.journal)
        self.root("candidate", None)
        self.root("transaction-old", None)
        self.root("transaction-previous", None)

    def recover(self):
        if not exists(self.journal):
            return
        journal = read_json(self.journal)
        if journal.get("version") != 1:
            raise ActivationError("unsupported recovery journal; keep generation roots for manual recovery")
        if journal.get("phase") == "committed":
            # Commit is durable. A crash during GC-root cleanup must not undo it.
            self.load_current()
            remove(self.journal)
            self.root("candidate", None)
            self.root("transaction-old", None)
            self.root("transaction-previous", None)
            return
        print("hooddy: recovering interrupted switch", file=sys.stderr)
        self.restore(journal)

    def switch(self):
        linger = run("loginctl", "show-user", run("id", "-un").stdout.strip(), "--property=Linger", "--value").stdout.strip()
        if linger != "yes":
            raise ActivationError("switch requires enabled linger; run loginctl enable-linger $(id -un)")
        old = self.load_current()
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not exists(self.config / "flake.lock"):
            raise ActivationError(f"missing flake.lock; run nix flake lock in {self.config} before switching")
        lock = read_regular(self.config / "flake.lock")
        read_regular(self.config / "flake.nix")
        journal = None
        with tempfile.TemporaryDirectory(prefix="hooddy-switch-") as temporary:
            snapshot = Path(temporary) / "config"
            self.copy_config(snapshot)
            if read_regular(snapshot / "flake.lock") != lock:
                raise ActivationError("flake.lock changed while snapshotting configuration")
            try:
                self.root("candidate", None)
                self.roots.mkdir(parents=True, exist_ok=True)
                run("nix", "build", "--no-update-lock-file", "--no-write-lock-file", "--out-link",
                    str(self.roots / "candidate"), f"path:{snapshot}#hooddyConfigurations.default.activationPackage")
                candidate, files = generation(self.roots / "candidate")
                if read_regular(snapshot / "flake.lock") != lock or read_regular(self.config / "flake.lock") != lock:
                    raise ActivationError("flake.lock changed during evaluation")
                snapshots, states = self.ownership(old, candidate)
                self.validate(snapshot, candidate, files)
                # Recheck after slow validation, before recording the transaction.
                if self.ownership(old, candidate) != (snapshots, states):
                    raise ActivationError("managed files or service states changed during validation")
                old_files = old["files"] if old else {}
                changed = sorted(key for key in old_files.keys() | files.keys()
                                 if key != "generated/sites.caddy" and
                                 (old_files.get(key) != candidate["files"].get(key) or snapshots.get(key) is None))
                previous = self.roots / "previous"
                if exists(previous) and not previous.is_symlink():
                    raise ActivationError(f"unexpected GC root: {previous}")
                journal = {"version": 1, "old": old, "candidate": candidate, "snapshots": snapshots,
                           "states": states, "changed": changed,
                           "previous": str(previous.resolve()) if previous.is_symlink() else None}
                if old:
                    self.root("transaction-old", old["generation"])
                if journal["previous"]:
                    self.root("transaction-previous", journal["previous"])
                self.phase(journal, "stopping-services")
                for unit in changed:
                    if unit in old_files:
                        self.stop_if_running(unit)
                    if unit not in files:
                        self.disable_if_enabled(unit)
                self.phase(journal, "writing-units")
                self.check_transaction_files(journal)
                for unit in changed:
                    if unit in files:
                        atomic_write(self.destination(unit), files[unit], 0o644)
                self.systemctl("daemon-reload")
                self.phase(journal, "starting-services")
                for name, definition in sorted(candidate["manifest"]["services"].items()):
                    unit = service_name(name)
                    self.systemctl("enable", unit)
                    self.systemctl("start", unit)
                    self.healthy(name, definition["healthCheck"])
                self.phase(journal, "reloading-caddy")
                self.check_transaction_files(journal)
                atomic_write(self.config / "generated/sites.caddy", files["generated/sites.caddy"], 0o644)
                self.systemctl("reload-or-restart", "hooddy.service")
                if self.systemctl("is-active", "hooddy.service", check=False).returncode:
                    raise ActivationError("Caddy is not active after switch")
                self.phase(journal, "committing")
                self.check_transaction_files(journal)
                for unit in changed:
                    if unit not in files:
                        remove(self.destination(unit))
                self.systemctl("daemon-reload")
                if old and old["generation"] != candidate["generation"]:
                    self.root("previous", old["generation"])
                self.root("current", candidate["generation"])
                write_json(self.current, candidate)
                self.phase(journal, "committed")
                remove(self.journal)
                self.root("candidate", None)
                self.root("transaction-old", None)
                self.root("transaction-previous", None)
            except (ActivationError, OSError, ValueError) as error:
                if journal is not None and exists(self.journal) and journal.get("phase") != "committed":
                    try:
                        self.restore(journal)
                    except (ActivationError, OSError, ValueError) as rollback_error:
                        raise ActivationError(f"{error}\n{rollback_error}") from error
                elif journal is None:
                    self.root("candidate", None)
                raise
        print("hooddy: switched to " + candidate["generation"])


def main():
    def interrupted(signum, frame):
        raise ActivationError(f"interrupted by signal {signum}")
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        manager = Switch()
        manager.recover()
        command = sys.argv[1]
        if command == "switch":
            manager.switch()
        elif command == "check-uninstall":
            current = manager.load_current()
            if current and current["manifest"]["services"]:
                raise ActivationError("switch-managed services remain; switch to a configuration with no app services before uninstall")
        elif command != "recover":
            raise ActivationError(f"unknown activation operation: {command}")
    except (ActivationError, OSError, ValueError) as error:
        print(f"hooddy: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
