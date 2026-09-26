"""`scripts/kernel_config_check.py`: a synthetic `.config` text asserted
against a fixed symbol list (0014 rev 5, design §2.8 and §5). No kernel
build, no root."""


from scripts.kernel_config_check import STAGE1_BUILTINS, main, require_builtin

CONFIG_ALL_BUILTIN = """\
CONFIG_WATCHDOG_CORE=y
CONFIG_BCM2835_WDT=y
CONFIG_MAGIC_SYSRQ=y
CONFIG_DETECT_HUNG_TASK=y
"""


def test_require_builtin_all_yes_is_empty(tmp_path):
    config = tmp_path / "config"
    config.write_text(CONFIG_ALL_BUILTIN)
    assert require_builtin(config, ["WATCHDOG_CORE", "BCM2835_WDT",
                                    "MAGIC_SYSRQ", "DETECT_HUNG_TASK"]) == []


def test_require_builtin_names_a_module_row(tmp_path):
    config = tmp_path / "config"
    config.write_text(CONFIG_ALL_BUILTIN.replace("CONFIG_BCM2835_WDT=y", "CONFIG_BCM2835_WDT=m"))
    assert require_builtin(config, ["WATCHDOG_CORE", "BCM2835_WDT"]) == ["BCM2835_WDT"]


def test_require_builtin_names_an_absent_row(tmp_path):
    config = tmp_path / "config"
    config.write_text("CONFIG_WATCHDOG_CORE=y\n# CONFIG_BCM2835_WDT is not set\n")
    assert require_builtin(config, ["WATCHDOG_CORE", "BCM2835_WDT"]) == ["BCM2835_WDT"]


def test_require_builtin_accepts_a_bare_symbol_without_the_config_prefix(tmp_path):
    config = tmp_path / "config"
    config.write_text("CONFIG_MAGIC_SYSRQ=y\n")
    assert require_builtin(config, ["MAGIC_SYSRQ"]) == []
    assert require_builtin(config, ["CONFIG_MAGIC_SYSRQ"]) == []


def test_main_exits_1_and_names_every_missing_symbol(tmp_path, capsys):
    config = tmp_path / "config"
    config.write_text("CONFIG_WATCHDOG_CORE=y\n")
    rc = main(["--config", str(config), "--symbol", "WATCHDOG_CORE", "--symbol", "BCM2835_WDT"])
    assert rc == 1
    assert "BCM2835_WDT" in capsys.readouterr().err


def test_main_exits_0_when_every_symbol_is_builtin(tmp_path):
    config = tmp_path / "config"
    config.write_text(CONFIG_ALL_BUILTIN)
    assert main(["--config", str(config), "--symbol", "WATCHDOG_CORE"]) == 0


def test_main_checks_every_stage1_builtin_by_default(tmp_path, capsys):
    config = tmp_path / "config"
    config.write_text(CONFIG_ALL_BUILTIN)
    assert main(["--config", str(config)]) == 1
    assert "IP_PNP, IP_PNP_DHCP, PCIE_BRCMSTB, MFD_RP1, MACB" in capsys.readouterr().err
    config.write_text("".join(f"CONFIG_{symbol}=y\n" for symbol in STAGE1_BUILTINS))
    assert main(["--config", str(config)]) == 0


def test_stage1_builtins_cover_liveness_and_the_kernel_dhcp():
    assert set(STAGE1_BUILTINS) == {"WATCHDOG_CORE", "BCM2835_WDT", "MAGIC_SYSRQ",
                                    "DETECT_HUNG_TASK", "IP_PNP", "IP_PNP_DHCP", "PCIE_BRCMSTB",
                                    "MFD_RP1", "MACB"}
