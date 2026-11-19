#!/usr/bin/env python3
"""Process boundaries for switch tests; never communicate with host services."""

import json
import os
from pathlib import Path
import signal
import sys

state_path = Path(os.environ["HOODDY_TEST_STATE"])
state = json.loads(state_path.read_text())
command = Path(sys.argv[0]).name
arguments = sys.argv[1:]
state["calls"].append([command, *arguments])


def save():
    temporary = state_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state))
    temporary.replace(state_path)


def finish(code=0, output=""):
    save()
    if output:
        print(output)
    raise SystemExit(code)


def failure(operation, unit=""):
    selectors = [command, f"{command}:{operation}", f"{command}:{operation}:{unit}"]
    for mutation in list(state.get("mutate_once", [])):
        if mutation["selector"] in selectors:
            Path(mutation["path"]).write_text(mutation["content"])
            state["mutate_once"].remove(mutation)
    for option in ["interrupt_once", "fail_once", "fail"]:
        for selector in selectors:
            if selector not in state.get(option, []):
                continue
            if option != "fail":
                state[option].remove(selector)
            if option == "interrupt_once":
                save()
                os.kill(os.getppid(), signal.SIGKILL)
                # The test can resume as soon as the parent dies. Do not save
                # stale state again and overwrite its next failure injection.
                os._exit(9)
            finish(7, f"simulated failure: {selector}")


def unit_state(unit):
    return state.setdefault("units", {}).setdefault(
        unit, {"active": False, "enabled": False, "starts": 0, "stops": 0, "reloads": 0})


def create_root(path, target):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        path.unlink()
    path.symlink_to(target)


if command == "systemctl":
    assert arguments.pop(0) == "--user", "system services must never be used"
    operation = arguments[0]
    unit = next((a for a in arguments[1:] if a.endswith(".service")), "")
    failure(operation, unit)
    if operation in ["show-environment", "daemon-reload", "reset-failed"]:
        finish()
    current = unit_state(unit)
    if operation == "show":
        prop = next((a.split("=", 1)[1] for a in arguments if a.startswith("--property=")), None)
        if prop is None and "--property" in arguments:
            prop = arguments[arguments.index("--property") + 1]
        if prop == "FragmentPath":
            path = Path(os.environ["XDG_CONFIG_HOME"]) / "systemd/user" / unit
            finish(output=state.get("fragments", {}).get(unit, str(path) if path.exists() else ""))
        if prop == "DropInPaths":
            finish(output=state.get("dropins", {}).get(unit, ""))
        if prop == "ActiveState":
            finish(output="active" if current["active"] else "inactive")
        if prop == "UnitFileState":
            finish(output=current.get("enabled_kind", "enabled") if current["enabled"] else "disabled")
        if prop == "LoadState":
            finish(output="loaded")
        raise AssertionError(arguments)
    if operation == "is-active":
        finish(0 if current["active"] else 3, "active" if current["active"] else "inactive")
    path = Path(os.environ["XDG_CONFIG_HOME"]) / "systemd/user" / unit
    if operation == "is-enabled":
        if not current["enabled"] and not path.exists():
            finish(4, "not-found")
        finish(0 if current["enabled"] else 1, current.get("enabled_kind", "enabled") if current["enabled"] else "disabled")
    if operation in ["enable", "start", "reload", "reload-or-restart", "restart"] and not path.exists():
        finish(5, f"Unit {unit} does not exist")
    if operation in ["stop", "disable"] and not path.exists() and not current["active"] and not current["enabled"]:
        finish(5, f"Unit {unit} does not exist")
    if operation == "enable":
        current["enabled"] = True
        current["enabled_kind"] = "enabled-runtime" if "--runtime" in arguments else "enabled"
        if "--now" in arguments:
            operation = "start"
    if operation == "disable":
        runtime = "--runtime" in arguments
        runtime_only = current.get("enabled_kind") == "enabled-runtime"
        if runtime == runtime_only:
            if current.pop("also_enabled_runtime", False) and not runtime:
                current["enabled_kind"] = "enabled-runtime"
            else:
                current["enabled"] = False
        if "--now" in arguments:
            operation = "stop"
    if operation == "start":
        if not current["active"]:
            current["starts"] += 1
        current["active"] = True
    elif operation == "stop":
        if current["active"]:
            current["stops"] += 1
        current["active"] = False
    elif operation in ["reload", "reload-or-restart", "restart"]:
        if current["active"] and operation != "restart":
            current["reloads"] += 1
        else:
            current["starts"] += 1
        current["active"] = True
    elif operation not in ["enable", "disable"]:
        raise AssertionError(arguments)
elif command == "loginctl":
    operation = arguments[0]
    failure(operation)
    if operation == "show-user":
        finish(output=state["linger"])
    if operation == "enable-linger":
        state["linger"] = "yes"
    elif operation == "disable-linger":
        state["linger"] = "no"
    else:
        raise AssertionError(arguments)
elif command == "nix":
    operation = arguments[0]
    failure(operation)
    if operation == "flake":
        assert arguments[1] == "lock", arguments
        output = Path(arguments[arguments.index("--output-lock-file") + 1])
        assert output.is_absolute(), arguments
        assert any(arg.startswith("path:") for arg in arguments), arguments
        output.write_text('{"nodes":{"root":{}},"root":"root","version":7}\n')
        finish()
    assert operation == "build", arguments
    assert "--no-update-lock-file" in arguments, arguments
    assert "--no-write-lock-file" in arguments, arguments
    reference = next(a for a in arguments if a.startswith("path:"))
    assert reference.endswith("#hooddyConfigurations.default.activationPackage"), reference
    source = Path(reference[5:].split("#", 1)[0])
    state["builds"].append({
        "source": str(source),
        "lock": (source / "flake.lock").read_text(),
        "local_file": (source / "untracked.nix").read_text(),
    })
    output_link = next((arguments[i + 1] for i, a in enumerate(arguments) if a in ["--out-link", "-o"]), None)
    if output_link:
        create_root(output_link, state["generation"])
    if "--json" in arguments:
        finish(output=json.dumps([{"outputs": {"out": state["generation"]}}]))
    if "--print-out-paths" in arguments:
        finish(output=state["generation"])
elif command == "nix-store":
    failure("root")
    assert arguments[0] == "--add-root" and arguments[2:4] == ["--indirect", "-r"], arguments
    create_root(arguments[1], arguments[4])
elif command == "systemd-analyze":
    failure("verify")
    assert "--user" in arguments and "verify" in arguments, arguments
    for argument in arguments:
        if argument.endswith(".service"):
            assert Path(argument).is_file(), argument
else:
    raise AssertionError(command)
finish()
