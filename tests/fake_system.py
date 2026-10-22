#!/usr/bin/env python3
"""Isolated systemd/logind/Nix boundary for CLI lifecycle tests."""

import json
import os
from pathlib import Path
import sys

state_path = Path(os.environ["HOODDY_TEST_STATE"])
state = json.loads(state_path.read_text())
name = Path(sys.argv[0]).name
args = sys.argv[1:]
state["calls"].append([name, *args])


def finish(code=0, output=""):
    state_path.write_text(json.dumps(state))
    if output:
        print(output)
    sys.exit(code)


if name == "systemctl":
    assert args.pop(0) == "--user", "system services must never be used"
    operation = args[0]
    if operation in state.get("fail", []):
        finish(7, "simulated systemd failure")
    if operation == "show-environment":
        finish()
    if operation == "show":
        assert args[1:] == ["hooddy.service", "--property=FragmentPath", "--value"]
        finish(output=state.get("fragment", ""))
    if operation == "daemon-reload":
        finish()
    assert args[-1] == "hooddy.service"
    if operation == "enable":
        assert args == ["enable", "--now", "hooddy.service"]
        state["enabled"] = True
        if not state["active"]:
            state["starts"] += 1
        state["active"] = True
    elif operation == "disable":
        assert args in [["disable", "--now", "hooddy.service"], ["disable", "hooddy.service"]]
        state["enabled"] = False
        if "--now" in args:
            state["active"] = False
    elif operation == "reload-or-restart":
        if state["active"]:
            state["reloads"] += 1
        else:
            state["starts"] += 1
            state["active"] = True
    else:
        raise AssertionError(args)
elif name == "loginctl":
    operation = args[0]
    if operation in state.get("fail", []):
        finish(7, "simulated logind failure")
    if operation == "show-user":
        finish(output=state["linger"])
    if operation == "enable-linger":
        state["linger"] = "yes"
    elif operation == "disable-linger":
        state["linger"] = "no"
    else:
        raise AssertionError(args)
elif name == "nix-store":
    assert args[0] == "--add-root" and args[2:4] == ["--indirect", "-r"]
    Path(args[1]).symlink_to(args[4])
    if "nix-store" in state.get("fail", []):
        finish(7, "simulated Nix failure after creating the root")
else:
    raise AssertionError(name)
finish()
