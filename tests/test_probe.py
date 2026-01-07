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
    assert caplog.records[0].message == \
        "TCP probe ok, ip_address: 1.2.3.4, port: 12345"


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
            "TCP probing failed, ip_address: 1.2.3.4, port: 12345"
        assert ctx.probe_fail_count == 1

    elif fail_count == 1:
        assert probe_ok
        assert ctx.probe_fail_count == 2

    elif fail_count == 5:
        assert not probe_ok
        # the fail count is reset after reporting a probe error
        assert ctx.probe_fail_count == 0
