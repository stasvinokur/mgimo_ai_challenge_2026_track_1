"""Проверка недоверенного текста до модели: маскирование персональных данных и эвристики инъекций.

Это первая линия, а не доказуемая защита: вторая — spotlighting в адаптере LLM и поле
instructions_detected у читателя; третья — фильтр инструментов и хуки IDE.
"""

from __future__ import annotations

import re

from strategist.application.ports import ScreenResult

PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    (
        "phone",
        re.compile(r"(?<![\d\w])(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\d)"),
    ),
    ("card", re.compile(r"(?<!\d)(?:\d{4}[\s\-]){3}\d{4}(?!\d)")),
    ("snils", re.compile(r"(?<!\d)\d{3}-\d{3}-\d{3}[\s\-]\d{2}(?!\d)")),
    ("passport", re.compile(r"(?i)паспорт[^\n\d]{0,20}\d{2}\s?\d{2}\s?\d{6}")),
    (
        "full_name",
        re.compile(
            r"\b[А-ЯЁ][а-яё]+(?:ов|ев|ин|ын|ский|цкий|ова|ева|ина|ына|ская|цкая)\s+[А-ЯЁ][а-яё]+\s+"
            r"[А-ЯЁ][а-яё]+(?:ович|евич|ич|овна|евна|ична|инична)\b"
        ),
    ),
]

INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"игнорир\w*\s+(?:все\s+)?(?:предыдущ|прошл|ранее|системн)\w*\s+(?:инструкц|указан|правил)\w*",
        r"забудь\w*\s+(?:все\s+)?(?:инструкц|правил|указан)\w*",
        r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions|rules)",
        r"disregard\s+(?:all\s+)?(?:previous|prior)\s+instructions",
        r"(?:выведи|покажи|раскрой)\s+(?:свой\s+)?(?:системн\w*\s+)?(?:промпт|инструкц\w*)",
        r"(?:reveal|print)\s+(?:your\s+)?system\s+prompt",
        r"(?:ты|вы)\s+(?:теперь|должн\w+)\s+(?:рекомендова|порекомендова|ответ\w*|сказа)\w*",
        r"(?:новые|обновлённые)\s+инструкции\s+для\s+(?:ии|ai|модел|агент)\w*",
        r"(?:you\s+are\s+now|new\s+instructions\s*:)",
        r"(?:<\|im_start\|>|<\|system\|>|\[INST\]|###\s*system)",
        r"(?:ии|ai)[-\s]?(?:агент|ассистент|модель)\w*\s*,?\s+(?:обязательно|немедленно)\s+",
    )
]


class RegexContentGuard:
    def __init__(self, mask_pii: bool = True) -> None:
        self.mask_pii = mask_pii

    def screen(self, text: str) -> ScreenResult:
        found = 0
        kinds: list[str] = []
        masked = text
        for kind, pattern in PII_PATTERNS:
            masked, count = pattern.subn(f"[{kind.upper()} скрыт]" if self.mask_pii else r"\g<0>", masked)
            if count:
                found += count
                kinds.append(kind)
        injections = []
        for pattern in INJECTION_PATTERNS:
            for match in pattern.finditer(text):
                start = max(0, match.start() - 40)
                snippet = text[start : match.end() + 80].strip().replace("\n", " ")
                if snippet not in injections:
                    injections.append(snippet[:240])
        return ScreenResult(text=masked, pii_found=found, pii_kinds=kinds, injections=injections)
