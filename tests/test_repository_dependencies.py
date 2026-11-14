from __future__ import annotations

import json

from solvyn.repositories.dependencies import (
    MAX_DEPENDENCIES_PER_MANIFEST,
    MAX_MANIFESTS,
    parse_dependencies,
)


def test_parse_supported_manifest_formats() -> None:
    manifests = {
        "nested/package.json": json.dumps(
            {
                "dependencies": {"react": "^19", "@scope/ui": "1"},
                "devDependencies": {"vitest": "3"},
                "optionalDependencies": {"optional-pkg": "1"},
                "peerDependencies": {"peer-pkg": "1"},
            }
        ),
        "pyproject.toml": """
[project]
dependencies = ["requests>=2", "httpx[http2]>=0.27"]

[project.optional-dependencies]
test = ["pytest>=8"]

[dependency-groups]
lint = ["ruff>=0.8"]
""",
        "requirements.txt": "requests>=2\nflask[async]>=3; python_version >= '3.10'\n",
        "services/api/deps.requirements.txt": "uvicorn[standard]>=0.32\n",
        "go.mod": """
module example.com/project

require example.com/one v1.2.3
require (
    example.com/two v2.0.0
    example.com/three v3.0.0 // indirect
)
""",
        "Cargo.toml": """
[dependencies]
serde = { version = "1", features = ["derive"] }

[dev-dependencies]
pretty_assertions = "1"

[target.'cfg(unix)'.dependencies]
libc = "0.2"
""",
        "pom.xml": """
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <dependencies>
    <dependency><groupId>org.example</groupId><artifactId>core</artifactId><version>1</version></dependency>
  </dependencies>
  <dependencyManagement><dependencies>
    <dependency><groupId>org.example</groupId><artifactId>managed-only</artifactId></dependency>
  </dependencies></dependencyManagement>
</project>
""",
        "build.gradle": """
dependencies {
  implementation 'org.example:core:1.2'
  testImplementation("org.example:test-kit:2.0")
  // implementation 'org.example:commented:1'
  /*
   * api 'org.example:also-commented:1'
   */
}
""",
        "build.gradle.kts": "dependencies {\n  implementation(" + '"org.example:kotlin-lib:1.0"' + ")\n}",
        "Gemfile": "gem 'rails', '~> 8'\ngem \"sidekiq\", \"~> 7\"\n",
        "composer.json": json.dumps(
            {"require": {"php": ">=8.2", "vendor/package": "^1"}, "require-dev": {"phpunit/phpunit": "^11"}}
        ),
    }

    result = parse_dependencies(manifests)

    assert result["nested/package.json"] == ["@scope/ui", "optional-pkg", "peer-pkg", "react", "vitest"]
    assert result["pyproject.toml"] == ["httpx", "pytest", "requests", "ruff"]
    assert result["requirements.txt"] == ["flask", "requests"]
    assert result["services/api/deps.requirements.txt"] == ["uvicorn"]
    assert result["go.mod"] == ["example.com/one", "example.com/three", "example.com/two"]
    assert result["Cargo.toml"] == ["libc", "pretty_assertions", "serde"]
    assert result["pom.xml"] == ["org.example:core"]
    assert result["build.gradle"] == ["org.example:core", "org.example:test-kit"]
    assert result["build.gradle.kts"] == ["org.example:kotlin-lib"]
    assert result["Gemfile"] == ["rails", "sidekiq"]
    assert result["composer.json"] == ["phpunit/phpunit", "vendor/package"]


def test_parse_poetry_static_dependencies_and_skip_python_runtime_marker() -> None:
    result = parse_dependencies(
        {
            "pyproject.toml": """
[tool.poetry.dependencies]
python = "^3.12"
requests = { version = "^2", extras = ["security"] }

[tool.poetry.dev-dependencies]
pytest = "^8"

[tool.poetry.group.docs.dependencies]
sphinx = "^8"
"""
        }
    )

    assert result["pyproject.toml"] == ["pytest", "requests", "sphinx"]


def test_requirements_skips_options_and_raw_urls_without_inventing_names() -> None:
    result = parse_dependencies(
        {
            "requirements.txt": """
--index-url https://packages.example/simple
-e git+https://example/repo.git#egg=editable-package
https://example/archive.whl
git+https://example/repo.git
named-package @ https://example/named.whl
normal-package==1.2
"""
        }
    )

    assert result["requirements.txt"] == ["named-package", "normal-package"]


def test_parser_is_order_independent_and_keys_include_unrecognized_manifests() -> None:
    left = {"z/README.md": "docs", "a/package.json": '{"dependencies":{"z":"1","a":"1"}}'}
    right = dict(reversed(list(left.items())))

    assert parse_dependencies(left) == parse_dependencies(right)
    assert parse_dependencies(left)["z/README.md"] == []


def test_parser_bounds_manifest_count_bytes_and_dependency_results() -> None:
    too_many = {
        f"{index:02d}/package.json": '{"dependencies":{"pkg":"1"}}' for index in range(MAX_MANIFESTS + 1)
    }
    capped = parse_dependencies(too_many)
    assert len(capped) == MAX_MANIFESTS
    assert f"{MAX_MANIFESTS:02d}/package.json" not in capped

    large = parse_dependencies({"requirements.txt": "#" + ("x" * 64_000)})
    assert large == {"requirements.txt": []}

    many_dependencies = json.dumps(
        {"dependencies": {f"package-{index:03d}": "1" for index in range(MAX_DEPENDENCIES_PER_MANIFEST + 10)}}
    )
    capped_names = parse_dependencies({"package.json": many_dependencies})["package.json"]
    assert len(capped_names) == MAX_DEPENDENCIES_PER_MANIFEST
    assert capped_names == sorted(capped_names)


def test_malformed_and_xml_entity_manifests_fail_closed() -> None:
    result = parse_dependencies(
        {
            "bad/package.json": "{not json",
            "bad/pyproject.toml": "[project\ninvalid",
            "bad/pom.xml": "<!DOCTYPE project [<!ENTITY x 'expanded'>]><project />",
            "good/package.json": '{"dependencies":{"safe":"1"}}',
        }
    )

    assert result == {
        "bad/package.json": [],
        "bad/pyproject.toml": [],
        "bad/pom.xml": [],
        "good/package.json": ["safe"],
    }
