"""Сканер секретов: ищет ключи и токены в файлах репозитория.

Запуск: `uv run python scripts/check_secrets.py [файлы...]`.
Без аргументов проверяет все файлы, которые git отслеживает или видит как новые (с учётом .gitignore).
Код выхода 1 — найден возможный секрет.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("openai/anthropic style key", re.compile(r"\bsk-(?:ant-|or-|proj-)?[A-Za-z0-9_\-]{24,}\b")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    (
        "assigned secret env var",
        re.compile(
            r"^\s*(?:export\s+)?(?:OLLAMA_API_KEY|TYPESAFE_API_KEY|OPENROUTER_API_KEY|AI_GATEWAY_API_KEY)"
            r"[ \t]*=[ \t]*['\"]?[A-Za-z0-9_\-\.]{12,}",
            re.MULTILINE,
        ),
    ),
    (
        "generic api key literal",
        re.compile(r"(?i)\b(?:api[_-]?key|secret|token)\b[ \t]*[:=][ \t]*['\"][A-Za-z0-9_\-]{24,}['\"]"),
    ),
]

SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".xlsx", ".docx", ".sqlite", ".db", ".lock", ".ico"}
SKIP_PARTS = {".git", ".venv", "node_modules", "__pycache__"}
# Файлы-примеры, где переменные перечислены без значений, и сам сканер с шаблонами.
ALLOW_FILES = {"scripts/check_secrets.py", "tests/architecture/test_secrets_scanner.py"}


def repo_files() -> list[Path]:
    """Файлы репозитория. -z — без экранирования: иначе git заключает имена с кириллицей в кавычки
    с восьмеричными кодами, такие пути не открываются, и файлы молча не проверялись."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    return [ROOT / line for line in out if line.strip()]


def scan_text(text: str) -> list[str]:
    return [name for name, pattern in PATTERNS if pattern.search(text)]


def scan_file(path: Path) -> list[str]:
    rel = path.relative_to(ROOT).as_posix() if path.is_absolute() and ROOT in path.parents else str(path)
    if rel in ALLOW_FILES or path.suffix.lower() in SKIP_SUFFIXES or SKIP_PARTS & set(path.parts):
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
        return []
    return scan_text(text)


def main(argv: list[str]) -> int:
    files = [Path(a).resolve() for a in argv] if argv else repo_files()
    findings = []
    for path in files:
        for name in scan_file(path):
            findings.append(f"{path.relative_to(ROOT) if ROOT in path.parents else path}: {name}")
    if findings:
        print("Найдены возможные секреты — уберите их и используйте .env:")
        for item in findings:
            print(f"  - {item}")
        return 1
    print(f"Секретов не найдено ({len(files)} файлов).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
