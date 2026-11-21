from __future__ import annotations

from pathlib import Path

import yaml
from yaml.constructor import ConstructorError


ROOT = Path(__file__).resolve().parents[2]


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict[object, object]:
    loader.flatten_mapping(node)
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ConstructorError("while constructing a mapping", node.start_mark, f"duplicate key {key!r}", key_node.start_mark)
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def test_production_compose_has_unique_and_isolated_dependency_services() -> None:
    config = yaml.load((ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    services = config["services"]
    assert {"api", "worker", "sandbox", "dependency-fetcher", "dependency-proxy"} <= set(services)
    assert services["api"]["build"]["context"] == "."
    assert services["api"]["environment"]["SANDBOX_RUNTIME_URL"] == "http://sandbox:8001"

    fetch_networks = services["dependency-fetcher"]["networks"]
    assert fetch_networks == ["dependency-fetch"]
    assert set(services["dependency-proxy"]["networks"]) == {"dependency-fetch", "dependency-egress"}
    assert "dependency-fetch" in services["sandbox"]["networks"]
    assert "dependency-fetch" not in services["api"]["networks"]
    assert "dependency-fetch" not in services["worker"]["networks"]
    assert config["networks"]["dependency-fetch"]["internal"] is True
    assert "internal" not in (config["networks"]["dependency-egress"] or {})

    proxy = (ROOT / "ops" / "dependency-proxy.conf").read_text(encoding="utf-8")
    assert "dstdomain pypi.org files.pythonhosted.org" in proxy
    assert "http_access deny to_private" in proxy
    assert "http_access deny to_linklocal" in proxy
    assert "http_access deny to_localhost" in proxy
