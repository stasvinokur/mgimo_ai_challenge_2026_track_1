"""Ошибки application-слоя."""

from __future__ import annotations


class StrategistError(Exception):
    """Базовая ошибка агента."""


class SchemaError(StrategistError):
    """Модель не вернула валидный структурированный ответ даже после повтора и эскалации."""


class NotAllowedError(StrategistError):
    """Источник вне allowlist или инструмент запрещён на этом шаге."""


class IntegrityError(StrategistError):
    """Нарушена целостность материалов (хеш не совпал) или данных кейса."""


class NeedsConfirmation(StrategistError):
    """Шаг с внешним побочным эффектом прерван — повтор только после подтверждения человека."""

    def __init__(self, step: str, message: str = "") -> None:
        super().__init__(message or f"Шаг {step} требует подтверждения человека перед повтором")
        self.step = step


class ModelServiceError(StrategistError):
    """Модели недоступны после повторов и запасных. Завершённые шаги сохранены — работу можно продолжить."""


class UsageLimitReached(ModelServiceError):
    """Исчерпаны кредиты или лимит облачного тарифа: повтор через секунды не поможет."""


class SyntheticQuarantineViolation(StrategistError):
    """Модельные респонденты попали в факты или в числа о спросе и долях — работа останавливается."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__(
            "Синтетика вышла из карантина (модельные респонденты не доказывают спрос): "
            + "; ".join(problems[:5])
        )
        self.problems = problems


class CaseNotFound(StrategistError):
    pass
