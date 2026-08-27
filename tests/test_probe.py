import logging
import socket
from unittest.mock import patch

import pytest

from ha_script.config import HAScriptConfig
from ha_script.context import HAScriptContext
from ha_script.tcp_probing import tcp_probe


@pytest.fixture()
def test_conf():
    conf = HAScriptConfig(
        route_table_id="rtb-0869eb690cef8c3a6",
        primary_instance_id="i-1234",
        secondary_instance_id="i-2345",
        probe_enabled=True, probe_max_fail=5, probe_port=12345)
    with patch("socket.socket") as soc:
        yield (conf, soc)


def test_probe_success(test_conf, caplog):
    """socket connect successful. counter is reset"""
    caplog.set_level(logging.DEBUG)
    conf, soc = test_conf
    ctx = HAScriptContext(probe_fail_count=2)

    assert tcp_probe(conf, ["1.2.3.4"], 12345, ctx)
    assert ctx.probe_fail_count == 0
    soc.return_value.bind.assert_not_called()
    assert caplog.records[0].message == \
        "TCP probe ok, ip_address: 1.2.3.4, port: 12345, source_ip: "


@pytest.mark.parametrize("fail_count", [0, 1, 5])
def test_probe_success_then_fail(fail_count, test_conf, caplog):
    """socket fails. if fail_count is 1, the probe result is True
    socket fails. if fail_count is 5, the probe result is False
    """
    caplog.set_level(logging.DEBUG)
    conf, soc = test_conf
    ctx = HAScriptContext(probe_fail_count=fail_count)

    soc.return_value.connect.side_effect = socket.error(111,
                                                        "Connection refused")

    probe_ok = tcp_probe(conf, ["1.2.3.4"], 12345, ctx)

    if fail_count == 0:
        assert probe_ok
        assert caplog.records[0].message == \
            "TCP probing failed, ip_address: 1.2.3.4, port: 12345, " \
            "source_ip: "
        assert ctx.probe_fail_count == 1

    elif fail_count == 1:
        assert probe_ok
        assert ctx.probe_fail_count == 2

    elif fail_count == 5:
        assert not probe_ok
        # the fail count is reset after reporting a probe error
        assert ctx.probe_fail_count == 0


def test_probe_success_with_source_ip(test_conf, caplog):
    """socket is bound to the given source address before connecting"""
    caplog.set_level(logging.DEBUG)
    conf, soc = test_conf
    ctx = HAScriptContext()

    assert tcp_probe(conf, ["1.2.3.4"], 12345, ctx, source_ip="10.0.1.5")
    soc.return_value.bind.assert_called_once_with(("10.0.1.5", 0))
    assert caplog.records[0].message == \
        "TCP probe ok, ip_address: 1.2.3.4, port: 12345, " \
        "source_ip: 10.0.1.5"


def test_probe_bind_failure_counts_as_probe_failure(test_conf, caplog):
    """the engine cannot send the probe at all, so it counts as a failure"""
    caplog.set_level(logging.DEBUG)
    conf, soc = test_conf
    ctx = HAScriptContext()

    soc.return_value.bind.side_effect = OSError(99, "Cannot assign")

    assert tcp_probe(conf, ["1.2.3.4", "5.6.7.8"], 12345, ctx,
                     source_ip="10.0.1.5")
    soc.return_value.connect.assert_not_called()
    # bind fails the same way for every host, the rest are not tried
    soc.return_value.bind.assert_called_once_with(("10.0.1.5", 0))
    soc.return_value.close.assert_called_once_with()
    assert ctx.probe_fail_count == 1
    # Reported apart from a connection failure, with the local cause.
    assert caplog.records[0].levelno == logging.ERROR
    assert caplog.records[0].exc_info is not None
    assert caplog.records[0].message == \
        "TCP probe cannot bind source address, source_ip: 10.0.1.5"


def test_probe_bind_failure_switches_over(test_conf, caplog):
    """bind keeps failing: probe_max_fail is reached and the probe fails"""
    caplog.set_level(logging.DEBUG)
    conf, soc = test_conf
    ctx = HAScriptContext()

    soc.return_value.bind.side_effect = OSError(99, "Cannot assign")

    for _ in range(conf.probe_max_fail):
        assert tcp_probe(conf, ["1.2.3.4"], 12345, ctx, source_ip="10.0.1.5")

    assert not tcp_probe(conf, ["1.2.3.4"], 12345, ctx, source_ip="10.0.1.5")
    # Logged on every attempt, not only on the first one.
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == conf.probe_max_fail + 1
