from pathlib import Path


def test_dependency_proxy_allows_only_public_python_and_npm_registries() -> None:
    config = Path("ops/dependency-proxy.conf").read_text(encoding="utf-8")
    assert "acl allowed_registry dstdomain pypi.org files.pythonhosted.org registry.npmjs.org" in config
    assert "http_access deny !allowed_registry" in config
    assert "http_access deny all" in config
    assert "example.com" not in config
