"""Сканер секретов ловит типовые ключи и пропускает пустые шаблоны."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("check_secrets", ROOT / "scripts" / "check_secrets.py")
assert _spec and _spec.loader
check_secrets = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_secrets)


def test_detects_assigned_api_key() -> None:
    assert check_secrets.scan_text("OLLAMA_API_KEY=abcd1234efgh5678ijkl")


def test_detects_private_key_header() -> None:
    assert check_secrets.scan_text("-----BEGIN OPENSSH PRIVATE KEY-----")


def test_empty_template_is_clean() -> None:
    assert check_secrets.scan_text("OLLAMA_API_KEY=\nTYPESAFE_API_KEY=\n") == []


def test_repository_has_no_secrets() -> None:
    assert check_secrets.main([]) == 0


def test_files_with_cyrillic_names_are_scanned(tmp_path, monkeypatch) -> None:
    """git по умолчанию экранирует не-ASCII имена; такие пути не открывались, и файлы с кириллицей в имени (задачи
    Backlog) молча не проверялись. Проверка — на временном репозитории: от состава этого репозитория она не зависит."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    secret = tmp_path / "заметки — ключи.md"
    secret.write_text("OLLAMA_API_KEY=abcd1234efgh5678ijkl\n", encoding="utf-8")
    subprocess.run(["git", "add", secret.name], cwd=tmp_path, check=True)
    (tmp_path / "черновик.md").write_text(
        "пусто\n", encoding="utf-8"
    )  # неотслеживаемый файл тоже проверяется
    monkeypatch.setattr(check_secrets, "ROOT", tmp_path)
    files = check_secrets.repo_files()
    assert {f.name for f in files} == {"заметки — ключи.md", "черновик.md"}
    assert all(f.exists() for f in files)
    assert check_secrets.main([]) == 1  # ключ в файле с кириллическим именем найден
