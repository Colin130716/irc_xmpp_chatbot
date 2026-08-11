from pathlib import Path

from ircxmppbot.client import ShellClient


def _write_cfg(tmp_path: Path) -> Path:
    cfg = {
        "client": {
            "name": "shell_node_1",
            "server": {
                "host": "127.0.0.1",
                "port": 8443,
                "token": "test-token",
                "tls": {"verify": False},
            },
        },
        "root_session_ttl": 300,
    }
    p = tmp_path / "client.yaml"
    import yaml
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return p


def test_client_creates_link(tmp_path):
    c = ShellClient(_write_cfg(tmp_path))
    assert c.link is not None
    assert c.link.client_type == "shell"
    assert c.cfg["client"]["name"] == "shell_node_1"
