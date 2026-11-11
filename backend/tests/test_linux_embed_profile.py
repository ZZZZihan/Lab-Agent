"""Tests for Spec T3.8 containerized Linux Ollama embedder profile (PR-30)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import yaml
from app.config import repo_root


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def test_docker_compose_linux_embed_profile_structure(tmp_path: Path) -> None:
    root = repo_root()
    compose_path = root / "docker-compose.yml"
    data = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

    assert "ollama_models" in data["volumes"]

    services = data["services"]
    assert "embedder" in services
    assert "embedder-init" in services

    embedder = services["embedder"]
    assert str(embedder["image"]).startswith("ollama/ollama:")
    assert embedder["profiles"] == ["linux-embed"]
    assert "ollama_models:/root/.ollama" in embedder["volumes"]
    assert "127.0.0.1:${OLLAMA_CONTAINER_PORT:-11435}:11434" in embedder["ports"]
    hc = embedder["healthcheck"]
    assert hc["test"] == ["CMD", "ollama", "list"]
    assert hc["interval"] == "5s"
    assert hc["timeout"] == "5s"
    assert hc["retries"] == 20
    assert hc["start_period"] == "10s"

    embedder_init = services["embedder-init"]
    assert embedder_init["image"] == embedder["image"]
    assert embedder_init["profiles"] == ["linux-embed"]
    assert embedder_init["restart"] == "no"
    env = embedder_init["environment"]
    if isinstance(env, dict):
        assert env.get("OLLAMA_HOST") == "http://embedder:11434"
    else:
        assert "OLLAMA_HOST=http://embedder:11434" in env
    assert embedder_init["depends_on"] == {
        "embedder": {"condition": "service_healthy"}
    }
    assert (
        "./docker/ollama/Modelfile.qwen3-embed:/Modelfile.qwen3-embed:ro"
        in embedder_init["volumes"]
    )
    assert embedder_init.get("entrypoint") == ["/bin/sh", "-c"]
    cmd_str = (
        " ".join(embedder_init["command"])
        if isinstance(embedder_init["command"], list)
        else str(embedder_init["command"])
    )
    assert 'ollama pull "${OLLAMA_BASE_MODEL:-qwen3-embedding:8b}"' in cmd_str
    assert (
        "ollama create lab-agent-qwen3-embed -f /Modelfile.qwen3-embed" in cmd_str
    )

    api_deps = services["api"]["depends_on"]
    assert api_deps["embedder-init"] == {
        "condition": "service_completed_successfully",
        "required": False,
    }

    modelfile = root / "docker" / "ollama" / "Modelfile.qwen3-embed"
    assert modelfile.is_file()
    assert not (root / "docker" / "ollama" / "Modelfile.qwen3-embedding").exists()
    modelfile_text = modelfile.read_text(encoding="utf-8")
    assert "FROM qwen3-embedding:8b" in modelfile_text
    assert "PARAMETER num_ctx 8192" in modelfile_text

    demo_sh = (root / "scripts" / "demo.sh").read_text(encoding="utf-8")
    assert 'docker compose "${COMPOSE_FILES[@]}" up -d --wait --wait-timeout 600 embedder' in demo_sh
    assert 'docker compose "${COMPOSE_FILES[@]}" run --rm embedder-init' in demo_sh
    assert 'ollama_port="${OLLAMA_CONTAINER_PORT:-$(env_val OLLAMA_CONTAINER_PORT)}"' in demo_sh
    assert "docker/ollama/Modelfile.qwen3-embed" in demo_sh
    assert "Modelfile.qwen3-embedding" not in demo_sh

    if shutil.which("docker") is not None:
        key_file = tmp_path / "magpie.key"
        key_file.write_text("dummy-test-key\n", encoding="utf-8")
        key_file.chmod(0o600)
        env_file = tmp_path / ".env"
        env_file.write_text(
            "POSTGRES_PASSWORD=testpgpass\n"
            "REDIS_PASSWORD=testredispass\n"
            "INTERNAL_PROXY_SECRET=testproxysecret\n"
            f"LAB_AGENT_MAGPIE_KEY_FILE={key_file}\n",
            encoding="utf-8",
        )
        proc_default = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(compose_path),
                "--env-file",
                str(env_file),
                "config",
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc_default.returncode == 0:
            rendered_default = json.loads(proc_default.stdout)
            assert "api" in rendered_default["services"]
            assert "embedder" not in rendered_default["services"]

        proc = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(compose_path),
                "--env-file",
                str(env_file),
                "--profile",
                "linux-embed",
                "config",
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            rendered = json.loads(proc.stdout)
            assert "embedder" in rendered["services"]
            assert "embedder-init" in rendered["services"]


def test_init_env_linux_embed_sets_container_embedding_base_url(
    tmp_path: Path,
) -> None:
    root = repo_root()
    init_env = root / "scripts" / "init-env.sh"

    # 1. Calling `init-env.sh --linux-embed` in a temporary directory sets
    # EMBEDDING_BASE_URL=http://embedder:11434/v1
    linux_dir = tmp_path / "linux_embed"
    linux_dir.mkdir()
    key_file = tmp_path / "magpie.key"
    key_file.write_text("sk-test-magpie-key-value\n", encoding="utf-8")
    key_file.chmod(0o600)

    env = {
        **os.environ,
        "LLM_MODEL": "claude-sonnet-5-5",
        "LAB_AGENT_MAGPIE_KEY_FILE": str(key_file),
    }
    env.pop("EMBEDDING_BASE_URL", None)

    res_linux = subprocess.run(
        [str(init_env), "--linux-embed"],
        cwd=linux_dir,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert res_linux.returncode == 0
    parsed_linux = _parse_env_file(linux_dir / ".env")
    assert parsed_linux["EMBEDDER"] == "openai_compatible"
    assert parsed_linux["EMBEDDING_BASE_URL"] == "http://embedder:11434/v1"
    assert parsed_linux["EMBEDDING_MODEL"] == "lab-agent-qwen3-embed"
    assert (
        parsed_linux["PUBLIC_ORIGIN"]
        == "http://127.0.0.1:43123,http://localhost:43123"
    )

    # 2. Default real-mode `init-env.sh` (without --linux-embed) sets host.docker.internal
    host_dir = tmp_path / "host_embed"
    host_dir.mkdir()
    res_host = subprocess.run(
        [str(init_env)],
        cwd=host_dir,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert res_host.returncode == 0
    parsed_host = _parse_env_file(host_dir / ".env")
    assert parsed_host["EMBEDDER"] == "openai_compatible"
    assert (
        parsed_host["EMBEDDING_BASE_URL"]
        == "http://host.docker.internal:11434/v1"
    )
    assert (
        parsed_host["PUBLIC_ORIGIN"]
        == "http://127.0.0.1:43123,http://localhost:43123"
    )

    # 3. Offline `init-env.sh --offline` also sets both loopback origins in PUBLIC_ORIGIN
    offline_dir = tmp_path / "offline_embed"
    offline_dir.mkdir()
    res_offline = subprocess.run(
        [str(init_env), "--offline"],
        cwd=offline_dir,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert res_offline.returncode == 0
    parsed_offline = _parse_env_file(offline_dir / ".env")
    assert (
        parsed_offline["PUBLIC_ORIGIN"]
        == "http://127.0.0.1:43123,http://localhost:43123"
    )


def test_pytest_isolates_upload_dir_and_bootstrap_token_file() -> None:
    from app.config import get_settings

    settings = get_settings()
    root = repo_root().resolve()
    upload_path = Path(settings.upload_dir).resolve()
    token_path = Path(settings.bootstrap_token_file).resolve()
    assert not upload_path.is_relative_to(root)
    assert not token_path.is_relative_to(root)

