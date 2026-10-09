"""The agent's SSH access on a V2 Node (docs/runbook.md, "Reaching a Node over SSH"): the base
package declares Debian's server and stages Photo Wall's sshd configuration, the one authorized key
and the ssh.service drop-in. Static here; the node-pid1 `success` leg runs the real sshd from the
installed package (tests/test_node_pid1.py `verify_ssh`)."""
from __future__ import annotations

import stat

from support.repo import REPO
from test_netboot_liveness import _parse_unit

from scripts import build_node_base_deb as base
from scripts.debian_packages import packages

KEY_FILE = REPO / "appliance/ssh/photo_wall_agent.pub"


def _sshd(root):
    """keyword -> every value in the staged sshd configuration, in order."""
    text = (root / base.SSH_FILES["appliance/ssh/sshd.conf"]).read_text()
    rows = [line.split(None, 1) for line in text.splitlines() if line.strip() and not line.startswith("#")]
    found: dict[str, list[str]] = {}
    for keyword, value in rows:
        found.setdefault(keyword, []).append(value)
    return found


def test_the_base_depends_on_debians_ssh_server():
    assert "openssh-server" in packages("node-base")
    assert "openssh-server" not in packages("bootstrapper", "player")


def test_the_one_key_is_a_public_ed25519_key_and_no_private_key_ships():
    lines = KEY_FILE.read_text().splitlines()
    assert len(lines) == 1 and lines[0].split()[0] == "ssh-ed25519", lines
    assert lines[0].split()[2] == "photo-wall-agent"
    for path in (REPO / "appliance/ssh").rglob("*"):
        assert "PRIVATE KEY" not in path.read_text(), path


def test_the_base_stages_key_only_root_login_with_the_repo_key(tmp_path):
    root = tmp_path / "package"
    base.stage_tree(REPO, root)
    authorized = root / base.SSH_AUTHORIZED_KEYS
    assert authorized.read_bytes() == KEY_FILE.read_bytes()
    # StrictModes refuses a group- or world-writable key file or directory.
    assert stat.S_IMODE(authorized.stat().st_mode) == 0o644
    assert stat.S_IMODE(authorized.parent.stat().st_mode) == 0o755
    assert _sshd(root) == {
        "AllowUsers": ["root"],
        "PermitRootLogin": ["prohibit-password"],
        "AuthenticationMethods": ["publickey"],
        "PubkeyAuthentication": ["yes"],
        "AuthorizedKeysFile": ["/" + base.SSH_AUTHORIZED_KEYS],
        "PasswordAuthentication": ["no"],
        "KbdInteractiveAuthentication": ["no"],
        "HostKey": ["/run/photo-wall-ssh/ssh_host_ed25519_key"],
    }


def test_the_drop_in_makes_this_boots_host_key_before_the_config_check_and_always_restarts(tmp_path):
    root = tmp_path / "package"
    base.stage_tree(REPO, root)
    unit = _parse_unit((root / base.SSH_FILES["appliance/ssh/ssh.service.conf"]).read_text())
    host_key = _sshd(root)["HostKey"][0]
    service = unit["Service"]
    # Debian's own `sshd -t` is reset and re-added after the key is made: with no host key, the
    # check would fail and the server never start.
    first, keygen, check = service["ExecStartPre"]
    assert first == "" and check == "/usr/sbin/sshd -t"
    assert f"ssh-keygen -q -t ed25519 -N \"\" -C \"\" -f {host_key};" in keygen
    assert service["RuntimeDirectory"] == ["photo-wall-ssh"]
    assert host_key.startswith("/run/photo-wall-ssh/")
    assert service["RuntimeDirectoryPreserve"] == ["yes"]   # a restart keeps this boot's key
    assert service["Restart"] == ["always"] and unit["Unit"]["StartLimitIntervalSec"] == ["0"]
    assert unit["Unit"]["ConditionKernelCommandLine"] == ["photowall.node=v2"]
    # Nothing in the drop-in ties the server to the app, the display or Central.
    assert not {"Requires", "BindsTo", "Requisite", "PartOf", "After", "Wants"} & set(unit["Unit"])
