import pytest

from sdwan.cli import build_parser, main


def test_parser_degrade_options():
    ns = build_parser().parse_args(
        ["degrade", "wan_a", "--loss", "30", "--delay", "150", "--jitter", "40", "--rate", "1mbit"]
    )
    assert ns.command == "degrade"
    assert ns.path == "wan_a"
    assert (ns.loss, ns.delay, ns.jitter, ns.rate) == (30.0, 150.0, 40.0, "1mbit")


def test_parser_traffic_client_defaults():
    ns = build_parser().parse_args(["traffic", "client", "--target", "10.2.0.10"])
    assert (ns.command, ns.role) == ("traffic", "client")
    assert ns.target == "10.2.0.10"
    assert ns.port == 7000
    assert ns.interval_ms == 100


def test_parser_status_json_flag():
    ns = build_parser().parse_args(["status", "--json"])
    assert ns.command == "status"
    assert ns.json is True


def test_main_help_lists_subcommands(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name in ("agent", "traffic", "monitor", "status", "degrade", "restore"):
        assert name in out
