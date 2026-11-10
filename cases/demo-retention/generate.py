"""Генератор синтетических материалов демо-кейса «Скворечник».

«Скворечник» — ВЫМЫШЛЕННЫЙ подписочный сервис семейной геолокации вымышленного мобильного оператора.
Все внутренние данные (когорты, воронка, обращения в поддержку, опрос ушедших, интервью, паспорт продукта,
A/B-тест онбординга) — синтетика. Выдержки из открытых источников (materials/web_*.md), brief.md и README.md
этим скриптом не создаются: они написаны вручную.

Запуск из корня репозитория:

    uv run python cases/demo-retention/generate.py

Повторный запуск с тем же SEED даёт побайтно одинаковые файлы (метки времени внутри xlsx/docx фиксированы).
"""

from __future__ import annotations

import hashlib
import io
import math
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from docx import Document
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font

SEED = 20261104
CASE_DIR = Path(__file__).resolve().parent
MATERIALS = CASE_DIR / "materials"
NEW_EVIDENCE = CASE_DIR / "new_evidence"
GENERATOR = "uv run python cases/demo-retention/generate.py"
RETRIEVED = "2026-11-04"
OOXML_TS = "2026-10-01T09:00:00Z"
ZIP_DATE = (2026, 10, 1, 9, 0, 0)

# Календарь мира кейса: t = 0 соответствует 2025-01, последний месяц — 2026-10.
MONTHS = [f"{y}-{m:02d}" for y in (2025, 2026) for m in range(1, 13)][:22]


def t_of(month: str) -> int:
    return MONTHS.index(month)


def month_num(t: int) -> int:
    return int(MONTHS[t][5:])


T_BUNDLE = t_of("2026-02")  # автоподключение в тарифе «Семейный»
T_CAMPAIGN = t_of("2026-03")  # кампания «Знайте, где ваш ребёнок. Всегда.»
T_AB = t_of("2026-09")  # когорта A/B-теста нового онбординга
T_CUT = t_of("2026-09")  # последний календарный месяц платежей, видимый в materials/
T_CUT_NEW = t_of("2026-10")  # последний календарный месяц платежей в new_evidence/
LAST_COHORT = t_of("2026-08")  # последняя когорта в materials/
MAX_K = 12

CHANNELS = ("app_store", "operator_app", "tariff_bundle")
CHANNEL_RU = {
    "app_store": "Магазины приложений",
    "operator_app": "Витрина в приложении оператора",
    "tariff_bundle": "Тариф «Семейный» (автоподключение)",
}
AGES = ("6-10", "11-15")
AGE_RU = {"6-10": "6–10 лет", "11-15": "11–15 лет", "unknown": "не указан"}
PLATFORMS = ("android", "ios")
ARMS_AB = ("control", "new_onboarding")  # у когорт до 2026-09 плечо — "base" (старый онбординг)

SEASON_ACQ = {
    1: 0.85,
    2: 0.9,
    3: 1.0,
    4: 1.0,
    5: 0.95,
    6: 0.75,
    7: 1.1,
    8: 1.6,
    9: 1.7,
    10: 1.1,
    11: 0.95,
    12: 0.9,
}
SEASON_SURV = {6: 0.97, 7: 0.96, 8: 0.97, 9: 1.02}
IOS_SHARE = {"6-10": 0.33, "11-15": 0.42}

# Экономика (синтетические константы финансовой службы).
NET_REVENUE_PER_PAYER = 148
VAR_COST_PER_PAYER = 29
CAC_APP_STORE_2025 = 330
CAC_APP_STORE_2026 = 410
CAC_OPERATOR_APP = 60
TEAM_COST_MONTH = 2_300_000
PRICE = 199

GROUPS = ("P", "UI", "UN")  # подключил ребёнка; установил приложение, но не подключил; не установил


# --------------------------------------------------------------------------------------------------
# Модель
# --------------------------------------------------------------------------------------------------


def mean_volume(channel: str, t: int) -> float:
    season = SEASON_ACQ[month_num(t)]
    if channel == "app_store":
        value = 3600 * 1.012**t * season
        if t >= T_CAMPAIGN:
            value *= 1.25
        return value
    if channel == "operator_app":
        return 3000 * 1.008**t * season
    if t < T_BUNDLE:
        return 0.0
    ramp = {T_BUNDLE: 0.6, T_BUNDLE + 1: 0.9}.get(t, 1.0)
    return 5200 * ramp * (0.5 + 0.5 * season)


def teen_share(channel: str, t: int) -> float:
    if channel == "app_store":
        return 0.56 if t >= T_CAMPAIGN else 0.34
    if channel == "operator_app":
        return 0.40
    return 0.46


@dataclass(frozen=True)
class Probs:
    install: float
    invite: float
    child: float
    perm: float
    alert: float
    p1_paired: float
    p1_unpaired: float
    s_paired: float
    s_unpaired: float


def probs(channel: str, age: str, platform: str, t: int, arm: str) -> Probs:
    teen = age == "11-15"
    ios = platform == "ios"
    bundle = channel == "tariff_bundle"
    install = {"app_store": 0.97, "operator_app": 0.88, "tariff_bundle": 0.46}[channel]
    invite = (0.84 if teen else 0.90) * (0.88 if bundle else 1.0)
    child = {(False, False): 0.90, (False, True): 0.86, (True, False): 0.78, (True, True): 0.72}[(teen, ios)]
    child *= 0.93 if bundle else 1.0
    perm = 0.70 if ios else 0.93
    alert = 0.74 if teen else 0.88
    if bundle:
        p1_paired, p1_unpaired = (0.58 if teen else 0.66), 0.16
        s_paired = 0.80 if teen else 0.86
    elif channel == "app_store":
        p1_paired, p1_unpaired = (0.70 if teen else 0.80), 0.20
        s_paired = 0.85 if teen else 0.92
        if teen and t >= T_CAMPAIGN:
            p1_paired, s_paired = 0.66, 0.81
    else:
        p1_paired, p1_unpaired = (0.70 if teen else 0.80), 0.22
        s_paired = 0.845 if teen else 0.91
    if arm == "new_onboarding":
        invite = min(0.97, invite + 0.04)
        child = min(0.97, child * (1.02 if teen else 1.07))
        perm = 0.86 if ios else 0.95
        if teen:
            # Дополнительно «уговорённые» подростки быстро отключают приложение.
            p1_paired -= 0.07
            alert -= 0.06
    return Probs(install, invite, child, perm, alert, p1_paired, p1_unpaired, s_paired, 0.60)


@dataclass
class Cell:
    cohort: int
    channel: str
    age: str  # латентный возраст ребёнка
    platform: str  # латентная платформа телефона ребёнка
    arm: str
    connected: int
    installed: int
    invite: int
    invite_ios: int
    child: int
    child_ios: int
    paired: int
    alert: int
    sizes: dict[str, int]
    paid: dict[str, list[int | None]]  # paid[group][k], k = 1..MAX_K (индекс 0 не используется)


def simulate(rng: np.random.Generator) -> list[Cell]:
    cells: list[Cell] = []
    for c in range(T_AB + 1):
        for channel in CHANNELS:
            mean = mean_volume(channel, c)
            if mean <= 0:
                continue
            total = int(rng.poisson(mean))
            teens = int(rng.binomial(total, teen_share(channel, c)))
            for age, n_age in (("6-10", total - teens), ("11-15", teens)):
                n_ios = int(rng.binomial(n_age, IOS_SHARE[age]))
                for platform, n_pl in (("android", n_age - n_ios), ("ios", n_ios)):
                    if c == T_AB:
                        n_test = int(rng.binomial(n_pl, 0.5))
                        arms = (("control", n_pl - n_test), ("new_onboarding", n_test))
                    else:
                        arms = (("base", n_pl),)
                    for arm, n in arms:
                        cells.append(simulate_cell(rng, c, channel, age, platform, arm, n))
    return cells


def simulate_cell(
    rng: np.random.Generator, c: int, channel: str, age: str, platform: str, arm: str, n: int
) -> Cell:
    p = probs(channel, age, platform, c, arm)
    installed = int(rng.binomial(n, p.install))
    invite = int(rng.binomial(installed, p.invite))
    child = int(rng.binomial(invite, p.child))
    paired = int(rng.binomial(child, p.perm))
    alert = int(rng.binomial(paired, p.alert))
    ios = platform == "ios"
    sizes = {"P": paired, "UI": installed - paired, "UN": n - installed}
    unpaired = sizes["UI"] + sizes["UN"]
    paid1_p = int(rng.binomial(paired, p.p1_paired))
    paid1_u = int(rng.binomial(unpaired, p.p1_unpaired))
    share_ui = sizes["UI"] / unpaired if unpaired else 0.0
    paid1_ui = int(rng.binomial(paid1_u, share_ui))
    first = {"P": paid1_p, "UI": paid1_ui, "UN": paid1_u - paid1_ui}
    paid: dict[str, list[int | None]] = {}
    for group in GROUPS:
        series: list[int | None] = [None] * (MAX_K + 1)
        series[1] = first[group]
        prev = first[group]
        surv = p.s_paired if group == "P" else p.s_unpaired
        for k in range(2, MAX_K + 1):
            cal = c + k
            if cal > T_CUT_NEW:
                break
            factor = SEASON_SURV.get(month_num(cal), 1.0)
            prev = int(rng.binomial(prev, min(0.99, surv * factor)))
            series[k] = prev
        paid[group] = series
    return Cell(
        cohort=c,
        channel=channel,
        age=age,
        platform=platform,
        arm=arm,
        connected=n,
        installed=installed,
        invite=invite,
        invite_ios=invite if ios else 0,
        child=child,
        child_ios=child if ios else 0,
        paired=paired,
        alert=alert,
        sizes=sizes,
        paid=paid,
    )


def observed_age(cell: Cell, group: str) -> str:
    return "unknown" if group == "UN" else cell.age


def payers_at(cells: list[Cell], cal: int, pred=None) -> int:
    """Число платящих в календарном месяце cal (M1 когорты c приходится на c + 1)."""
    total = 0
    for cell in cells:
        if pred is not None and not pred(cell):
            continue
        k = cal - cell.cohort
        if 1 <= k <= MAX_K:
            for group in GROUPS:
                value = cell.paid[group][k]
                if value is not None:
                    total += value
    return total


# --------------------------------------------------------------------------------------------------
# Утилиты записи
# --------------------------------------------------------------------------------------------------


def yq(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def front_matter(
    title: str,
    kind: str,
    publisher: str,
    published: str,
    note: str,
    *,
    synthetic: bool = True,
    source_url: str | None = None,
    generator: bool = True,
) -> str:
    lines = [
        f"title: {yq(title)}",
        f"kind: {kind}",
        f"synthetic: {'true' if synthetic else 'false'}",
        f"source_url: {source_url if source_url else 'null'}",
        f"publisher: {yq(publisher)}",
        f"published: {published}",
        f"retrieved: {RETRIEVED}",
        f"note: {yq(note)}",
    ]
    if generator:
        lines.append(f"generator: {yq(GENERATOR)}")
    return "\n".join(lines) + "\n"


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def write_meta(data_path: Path, title: str, kind: str, publisher: str, published: str, note: str) -> None:
    meta = front_matter(title, kind, publisher, published, note)
    write_text(data_path.with_name(data_path.name + ".meta.yaml"), meta)


def write_md(path: Path, title: str, kind: str, publisher: str, published: str, note: str, body: str) -> None:
    write_text(path, "---\n" + front_matter(title, kind, publisher, published, note) + "---\n\n" + body)


def normalize_ooxml(path: Path) -> None:
    """Фиксирует метки времени внутри zip-контейнера xlsx/docx, чтобы файл был побайтно воспроизводим."""
    with zipfile.ZipFile(path) as src:
        items = [(info.filename, src.read(info.filename)) for info in src.infolist()]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as dst:
        for name, data in items:
            if name == "docProps/core.xml":
                text = data.decode("utf-8")
                for tag in ("created", "modified"):
                    text = re.sub(
                        rf"(<dcterms:{tag}[^>]*>)[^<]*(</dcterms:{tag}>)", rf"\g<1>{OOXML_TS}\g<2>", text
                    )
                data = text.encode("utf-8")
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            dst.writestr(info, data)
    path.write_bytes(buf.getvalue())


def pct(num: float, den: float, digits: int = 1) -> str:
    if not den:
        return "—"
    return f"{100 * num / den:.{digits}f}".replace(".", ",")


def fmt_int(value: float) -> str:
    return f"{round(value):,}".replace(",", " ")


def md_table(header: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------------------------------
# materials/internal_cohorts_retention.csv
# --------------------------------------------------------------------------------------------------

GAP_COHORT, GAP_CHANNEL, GAP_K = t_of("2025-11"), "operator_app", 4  # сбой выгрузки биллинга за 2026-03


def cohort_rows(cells: list[Cell], cohorts: range, cut: int, arm_filter=None) -> list[dict]:
    agg: dict[tuple, dict] = {}
    for cell in cells:
        if cell.cohort not in cohorts or (arm_filter is not None and cell.arm not in arm_filter):
            continue
        for group in GROUPS:
            key = (cell.cohort, cell.channel, observed_age(cell, group))
            row = agg.setdefault(key, {"connected": 0, **{k: 0 for k in range(1, MAX_K + 1)}})
            row["connected"] += cell.sizes[group]
            for k in range(1, MAX_K + 1):
                value = cell.paid[group][k]
                row[k] = None if value is None or row[k] is None else row[k] + value
    age_order = {"6-10": 0, "11-15": 1, "unknown": 2}
    out = []
    for (c, channel, age), row in sorted(
        agg.items(), key=lambda kv: (kv[0][0], CHANNELS.index(kv[0][1]), age_order[kv[0][2]])
    ):
        record = {
            "cohort_month": MONTHS[c],
            "channel": channel,
            "child_age_group": age,
            "connected": row["connected"],
        }
        for k in range(1, MAX_K + 1):
            observed = c + k <= cut and row[k] is not None
            gap = c == GAP_COHORT and channel == GAP_CHANNEL and k == GAP_K
            record[f"paid_m{k}"] = row[k] if observed and not gap else None
        out.append(record)
    return out


def write_cohorts(cells: list[Cell]) -> pd.DataFrame:
    rows = cohort_rows(cells, range(0, LAST_COHORT + 1), T_CUT)
    df = pd.DataFrame(rows)
    for k in range(1, MAX_K + 1):
        df[f"paid_m{k}"] = df[f"paid_m{k}"].astype("Int64")
    path = MATERIALS / "internal_cohorts_retention.csv"
    df.to_csv(path, index=False, lineterminator="\n")
    write_meta(
        path,
        "Когорты удержания «Скворечника» по месяцам подключения, январь 2025 — август 2026",
        "table",
        "Аналитика продукта «Скворечник» (вымышленная команда)",
        "2026-10-01",
        "СИНТЕТИКА: продукт и данные вымышлены. Выгрузка биллинга на 30.09.2026. Одна строка — когорта "
        "(месяц подключения) x канал x возраст ребёнка. Столбцы: cohort_month — месяц подключения; channel — "
        "app_store (магазины приложений, 14 дней бесплатно), operator_app (витрина в приложении оператора, "
        "14 дней), tariff_bundle (автоподключение в тарифе «Семейный» на 30 дней, с 2026-02); child_age_group "
        "— 6-10, 11-15 или unknown (родитель не установил приложение и возраст не указал); connected — число "
        "подключений; paid_mK — сколько из них оплатили K-й платный период (M1 — первая оплата после "
        "бесплатного периода). Пусто — период ещё не наступил к дате выгрузки. Пропуск paid_m4 у "
        "operator_app когорты 2025-11 — сбой выгрузки биллинга за март 2026.",
    )
    return df


# --------------------------------------------------------------------------------------------------
# materials/internal_onboarding_funnel.xlsx
# --------------------------------------------------------------------------------------------------

FUNNEL_HEADERS = [
    "Месяц подключения",
    "Канал",
    "Возраст ребёнка",
    "1. Подключили услугу",
    "2. Установили приложение родителя",
    "3. Отправили приглашение ребёнку",
    "3a. …из них iPhone у ребёнка",
    "4. Ребёнок установил приложение",
    "4a. …из них iPhone",
    "5. Подключили ребёнка (разрешение на геолокацию за 3 дня)",
    "5a. …из них iPhone",
    "6. Получили первое уведомление о геозоне за 7 дней",
    "7. Первая оплата (M1)",
    "7a. …из них подключили ребёнка",
]

STOP_REASONS = [
    "Ребёнок не захотел ставить приложение",
    "Не получилось выдать разрешения на телефоне ребёнка",
    "Телефона ребёнка нет рядом, сделаю позже",
    "Не понял(а), что нужно сделать",
    "Передумал(а), не нужно",
    "Другое",
]
STOP_DIST = {
    ("6-10", "android"): [0.10, 0.14, 0.38, 0.16, 0.12, 0.10],
    ("6-10", "ios"): [0.08, 0.41, 0.24, 0.13, 0.08, 0.06],
    ("11-15", "android"): [0.39, 0.12, 0.20, 0.08, 0.13, 0.08],
    ("11-15", "ios"): [0.31, 0.33, 0.14, 0.07, 0.10, 0.05],
}


def funnel_rows(cells: list[Cell], cohorts: range, arm_filter=None) -> list[list]:
    agg: dict[tuple, list[int]] = {}
    for cell in cells:
        if cell.cohort not in cohorts or (arm_filter is not None and cell.arm not in arm_filter):
            continue
        known = (cell.cohort, cell.channel, cell.age)
        unknown = (cell.cohort, cell.channel, "unknown")
        row = agg.setdefault(known, [0] * 11)
        paired_ios = cell.paired if cell.platform == "ios" else 0
        values = [
            cell.installed,
            cell.installed,
            cell.invite,
            cell.invite_ios,
            cell.child,
            cell.child_ios,
            cell.paired,
            paired_ios,
            cell.alert,
            cell.paid["P"][1] + cell.paid["UI"][1],
            cell.paid["P"][1],
        ]
        for i, v in enumerate(values):
            row[i] += v
        urow = agg.setdefault(unknown, [0] * 11)
        urow[0] += cell.sizes["UN"]
        urow[9] += cell.paid["UN"][1]
    age_order = {"6-10": 0, "11-15": 1, "unknown": 2}
    out = []
    for (c, channel, age), v in sorted(
        agg.items(), key=lambda kv: (kv[0][0], CHANNELS.index(kv[0][1]), age_order[kv[0][2]])
    ):
        out.append([MONTHS[c], CHANNEL_RU[channel], AGE_RU[age], *v])
    return out


def write_funnel(cells: list[Cell], rng: np.random.Generator) -> list[list]:
    wb = Workbook()
    bold = Font(bold=True)

    ws = wb.active
    ws.title = "Воронка по месяцам"
    ws.append(FUNNEL_HEADERS)
    rows = funnel_rows(cells, range(0, LAST_COHORT + 1))
    for row in rows:
        ws.append(row)
    for cell in ws[1]:
        cell.font = bold
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "D2"
    ws.column_dimensions["A"].width = 12
    ws.column_dimensions["B"].width = 34
    ws.column_dimensions["C"].width = 14
    for col in "DEFGHIJKLMN":
        ws.column_dimensions[col].width = 16

    ws2 = wb.create_sheet("Сводка дашборда")
    ws2.append(
        [
            "Месяц подключения",
            "Подключили услугу",
            "Подключили ребёнка за 3 дня",
            "Доля подключивших ребёнка, %",
            "Первая оплата (M1)",
            "M1, %",
        ]
    )
    by_month: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for row in rows:
        by_month[row[0]][0] += row[3]
        by_month[row[0]][1] += row[9]
        by_month[row[0]][2] += row[12]
    for month, (conn, paired, paid1) in sorted(by_month.items()):
        ws2.append([month, conn, paired, round(100 * paired / conn, 1), paid1, round(100 * paid1 / conn, 1)])
    for cell in ws2[1]:
        cell.font = bold
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    for col in "ABCDEF":
        ws2.column_dimensions[col].width = 18

    ws3 = wb.create_sheet("Причины остановки")
    ws3.append(["Период", "Возраст ребёнка", "Телефон ребёнка", *STOP_REASONS, "Всего ответов"])
    for age in AGES:
        for platform in PLATFORMS:
            stopped = sum(
                c.invite - c.paired
                for c in cells
                if T_BUNDLE - 1 <= c.cohort <= LAST_COHORT and c.age == age and c.platform == platform
            )
            n = int(rng.binomial(stopped, 0.09))
            counts = rng.multinomial(n, STOP_DIST[(age, platform)])
            ws3.append(
                [
                    "2026-01 — 2026-08",
                    AGE_RU[age],
                    "iPhone" if platform == "ios" else "Android",
                    *[int(x) for x in counts],
                    n,
                ]
            )
    for cell in ws3[1]:
        cell.font = bold
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws3.column_dimensions["A"].width = 20
    for col in "BCDEFGHIJ":
        ws3.column_dimensions[col].width = 18

    ws4 = wb.create_sheet("Определения")
    definitions = [
        ("СИНТЕТИКА", "Продукт «Скворечник», оператор и все цифры вымышлены для демонстрационного кейса."),
        ("Дата выгрузки", "2026-10-01; когорты подключений 2025-01 — 2026-08; оплаты — по 2026-09."),
        (
            "Подключили услугу",
            "Начало пробного периода (14 дней) или автоподключение по тарифу «Семейный» "
            "(30 дней, с 2026-02). В дашборде считаются вместе.",
        ),
        (
            "Установили приложение родителя",
            "Родитель вошёл в приложение. Возраст ребёнка указывается на этом "
            "шаге, поэтому для не установивших возраст «не указан», и в их строках шаги 2–6 равны нулю.",
        ),
        ("Отправили приглашение", "Родитель отправил ребёнку код или ссылку для детского приложения."),
        ("Ребёнок установил приложение", "Детское приложение установлено и связано с родительским."),
        (
            "Подключили ребёнка",
            "Выдано фоновое разрешение на геолокацию («Всегда» на iPhone) в течение 3 дней "
            "после подключения услуги.",
        ),
        (
            "Первое уведомление",
            "Родитель получил уведомление о приходе или уходе из геозоны в первые 7 дней.",
        ),
        ("Первая оплата (M1)", "Успешное списание за первый платный период."),
        ("…из них подключили ребёнка", "Оплатившие M1, у которых на шаге 5 был подключён ребёнок."),
        (
            "Причины остановки",
            "Короткий опрос на экране подключения ребёнка, если родитель закрыл экран, "
            "не завершив подключение. Отвечают около 9% остановившихся; период 2026-01 — 2026-08.",
        ),
        (
            "Каналы",
            "Магазины приложений; витрина в приложении оператора; тариф «Семейный» (автоподключение).",
        ),
    ]
    ws4.append(["Термин", "Определение"])
    for item in definitions:
        ws4.append(list(item))
    for cell in ws4[1]:
        cell.font = bold
    ws4.column_dimensions["A"].width = 32
    ws4.column_dimensions["B"].width = 110
    for row in ws4.iter_rows(min_row=2):
        row[1].alignment = Alignment(wrap_text=True, vertical="top")

    wb.properties.creator = "Аналитика продукта «Скворечник» (синтетика)"
    wb.properties.title = "Воронка онбординга «Скворечника» (синтетика)"
    path = MATERIALS / "internal_onboarding_funnel.xlsx"
    wb.save(path)
    normalize_ooxml(path)
    write_meta(
        path,
        "Воронка онбординга «Скворечника» по месяцам подключения, январь 2025 — август 2026",
        "table",
        "Аналитика продукта «Скворечник» (вымышленная команда)",
        "2026-10-01",
        "СИНТЕТИКА: продукт и данные вымышлены. Листы: «Воронка по месяцам» (месяц x канал x возраст "
        "ребёнка, шаги 1–7; отдельно — сколько на шагах 3–5 детей с iPhone и сколько оплативших M1 "
        "подключили ребёнка), «Сводка дашборда» (все каналы "
        "вместе, как видит руководитель), «Причины остановки» (микроопрос на экране подключения ребёнка, "
        "2026-01 — 2026-08), «Определения». Подключения и первые оплаты совпадают с "
        "internal_cohorts_retention.csv.",
    )
    return rows


# --------------------------------------------------------------------------------------------------
# materials/internal_support_report_2026.docx
# --------------------------------------------------------------------------------------------------

TOPICS = [
    "Списание после бесплатного периода / «не подключал(а)»",
    "Дорого, просьба о скидке",
    "Возврат денег",
    "Не получается подключить телефон ребёнка",
    "Ребёнок отключил геолокацию / удалил приложение",
    "Неточная геолокация, задержка уведомлений",
    "Быстро садится батарея у ребёнка",
    "Прочее",
]
MONEY_TOPICS = (0, 1, 2)


def support_counts(cells: list[Cell], rng: np.random.Generator) -> dict[int, list[int]]:
    counts: dict[int, list[int]] = {}
    for cal in range(0, T_CUT + 1):
        first_nonbundle = first_bundle_u = first_bundle_p = 0
        conn_nonbundle = conn_bundle = 0
        for cell in cells:
            if cell.cohort == cal - 1:
                if cell.channel == "tariff_bundle":
                    first_bundle_u += cell.paid["UI"][1] + cell.paid["UN"][1]
                    first_bundle_p += cell.paid["P"][1]
                else:
                    first_nonbundle += sum(cell.paid[g][1] for g in GROUPS)
            if cell.cohort == cal:
                if cell.channel == "tariff_bundle":
                    conn_bundle += cell.installed
                else:
                    conn_nonbundle += cell.installed
        payers = payers_at(cells, cal)
        teen_payers = payers_at(cells, cal, lambda c: c.age == "11-15")
        charge = 0.012 * first_nonbundle + 0.24 * first_bundle_u + 0.05 * first_bundle_p
        rates = [
            charge,
            0.0055 * payers + 0.03 * first_bundle_u,
            0.30 * charge + 0.0015 * payers,
            0.045 * conn_nonbundle + 0.03 * conn_bundle,
            0.011 * teen_payers,
            0.004 * payers,
            0.002 * payers,
            0.003 * payers + 60,
        ]
        counts[cal] = [int(rng.poisson(r)) for r in rates]
    return counts


def write_support(cells: list[Cell], rng: np.random.Generator) -> dict[int, list[int]]:
    counts = support_counts(cells, rng)
    months_2025 = [t for t in counts if t < t_of("2026-01")]
    months_2026 = [t for t in counts if t >= t_of("2026-01")]
    avg_2025 = [sum(counts[t][i] for t in months_2025) / len(months_2025) for i in range(len(TOPICS))]

    def money_share(values: list[float]) -> float:
        return sum(values[i] for i in MONEY_TOPICS) / sum(values)

    share_2025 = money_share(avg_2025)
    share_last = money_share(counts[T_CUT])
    total_2025 = sum(avg_2025)
    total_last = sum(counts[T_CUT])

    doc = Document()
    doc.core_properties.author = "Служба поддержки цифровых сервисов (синтетика)"
    doc.core_properties.title = "Отчёт поддержки «Скворечника», январь–сентябрь 2026 (синтетика)"
    doc.core_properties.last_modified_by = "generate.py"
    doc.core_properties.revision = 1
    doc.core_properties.created = datetime(2026, 10, 8, 9, 0, 0)
    doc.core_properties.modified = datetime(2026, 10, 8, 9, 0, 0)

    doc.add_heading("Обращения в поддержку по «Скворечнику»: январь–сентябрь 2026", level=1)
    doc.add_paragraph(
        "СИНТЕТИКА. Документ создан для демонстрационного кейса: продукт «Скворечник», оператор, "
        "обращения и цитаты вымышлены. Подготовлено группой поддержки цифровых сервисов, 8 октября 2026 г."
    )

    doc.add_heading("1. Главное", level=2)
    for text in (
        f"Обращений стало больше: в 2025 году в среднем {fmt_int(total_2025)} в месяц, в сентябре 2026 — "
        f"{fmt_int(total_last)}.",
        f"Резко выросла доля обращений, связанных с деньгами (списания, «дорого», возвраты): "
        f"{pct(share_2025 * 100, 100, 0)}% в 2025 году против {pct(share_last * 100, 100, 0)}% в сентябре 2026.",
        "Клиенты прямо пишут, что 199 ₽ в месяц — дорого за «просто карту», и сравнивают нас с бесплатными "
        "Локатором и Family Link.",
        "Вывод группы поддержки: главная причина оттока — цена. Остальные темы стабильны или растут "
        "вместе с базой.",
    ):
        doc.add_paragraph(text, style="List Bullet")

    doc.add_heading("2. Обращения по темам, штук в месяц", level=2)
    header = ["Тема", "2025, в среднем"] + [MONTHS[t][5:] + ".26" for t in months_2026]
    table = doc.add_table(rows=1, cols=len(header))
    table.style = "Table Grid"
    for i, text in enumerate(header):
        table.rows[0].cells[i].text = text
    for i, topic in enumerate(TOPICS):
        cells_row = table.add_row().cells
        cells_row[0].text = topic
        cells_row[1].text = fmt_int(avg_2025[i])
        for j, t in enumerate(months_2026):
            cells_row[2 + j].text = fmt_int(counts[t][i])
    total_row = table.add_row().cells
    total_row[0].text = "Всего"
    total_row[1].text = fmt_int(total_2025)
    for j, t in enumerate(months_2026):
        total_row[2 + j].text = fmt_int(sum(counts[t]))

    doc.add_heading("3. Доля «денежных» обращений", level=2)
    doc.add_paragraph("«Денежные» — сумма первых трёх тем таблицы: списания, «дорого», возвраты.")
    table2 = doc.add_table(rows=1, cols=3)
    table2.style = "Table Grid"
    for i, text in enumerate(("Период", "«Денежные» обращения", "Доля от всех, %")):
        table2.rows[0].cells[i].text = text
    row = table2.add_row().cells
    row[0].text = "2025, в среднем за месяц"
    row[1].text = fmt_int(sum(avg_2025[i] for i in MONEY_TOPICS))
    row[2].text = pct(share_2025 * 100, 100, 0)
    for t in months_2026:
        row = table2.add_row().cells
        row[0].text = MONTHS[t]
        row[1].text = fmt_int(sum(counts[t][i] for i in MONEY_TOPICS))
        row[2].text = pct(money_share(counts[t]) * 100, 100, 0)

    doc.add_heading("4. Типичные формулировки (обезличены, вымышлены)", level=2)
    for quote in (
        "«Я ничего не подключал, откуда списание 199 рублей? Верните деньги».",
        "«Пришло списание за какой-то Скворечник, я даже приложение не ставила».",
        "«199 в месяц за то, что бесплатно есть в айфоне? Отключите».",
        "«Не могу подключить телефон сына: приложение просит разрешение „Всегда“, а его нет в настройках».",
        "«Дочь удалила приложение и сказала, что я за ней слежу. Можно вернуть деньги за месяц?»",
        "«Уведомление „в школе“ пришло через двадцать минут после звонка на урок».",
        "«Дорого. Сделайте хотя бы скидку на второго ребёнка».",
    ):
        doc.add_paragraph(quote, style="List Bullet")

    doc.add_heading("5. Предложения группы поддержки", level=2)
    for text in (
        "Снизить цену до 99 ₽ в месяц или ввести семейную скидку на второго ребёнка.",
        "Продлить бесплатный период до 30 дней во всех каналах.",
        "Дать операторам поддержки право возвращать деньги за первый месяц без согласования.",
    ):
        doc.add_paragraph(text, style="List Number")

    doc.add_heading("6. Как считали", level=2)
    doc.add_paragraph(
        "Тему присваивает оператор поддержки по первому сообщению клиента, одна тема на обращение. "
        "Обращения из чата приложения, с горячей линии оператора и из формы на сайте объединены. "
        "В системе обращений нет поля «канал подключения», поэтому обращения абонентов тарифа «Семейный» "
        "отдельно не выделяются. Данные за 2025 год — среднее за двенадцать месяцев."
    )

    path = MATERIALS / "internal_support_report_2026.docx"
    doc.save(path)
    normalize_ooxml(path)
    write_meta(
        path,
        "Отчёт службы поддержки: обращения по «Скворечнику», январь–сентябрь 2026",
        "document",
        "Служба поддержки цифровых сервисов (вымышленная)",
        "2026-10-08",
        "СИНТЕТИКА: продукт, обращения и цитаты вымышлены. Темы обращений по месяцам, доля «денежных» "
        "обращений, выводы и предложения группы поддержки. Интерпретация «главная причина оттока — цена» — "
        "мнение авторов отчёта, а не проверенный факт.",
    )
    return counts


# --------------------------------------------------------------------------------------------------
# materials/internal_exit_survey_2026.csv
# --------------------------------------------------------------------------------------------------

R_PRICE = "Дорого"
R_CHARGE = "Неожиданно списали деньги / не подключал(а) сам(а)"
R_SETUP = "Не получилось подключить телефон ребёнка"
R_CHILD = "Ребёнок против / отключил приложение"
R_FREE = "Перешли на бесплатное (Локатор, Family Link и др.)"
R_QUALITY = "Неточная геолокация / уведомления опаздывают"
R_NONEED = "Больше не нужно (каникулы, ребёнок вырос и т. п.)"
R_OTHER = "Другое"
REASONS = [R_PRICE, R_CHARGE, R_SETUP, R_CHILD, R_FREE, R_QUALITY, R_NONEED, R_OTHER]
REASON_DIST = {
    ("UN", "6-10"): [0.30, 0.34, 0.06, 0.03, 0.04, 0.01, 0.15, 0.07],
    ("UN", "11-15"): [0.30, 0.34, 0.06, 0.03, 0.04, 0.01, 0.15, 0.07],
    ("UI", "6-10"): [0.30, 0.06, 0.30, 0.07, 0.09, 0.03, 0.08, 0.07],
    ("UI", "11-15"): [0.27, 0.05, 0.24, 0.22, 0.08, 0.02, 0.06, 0.06],
    ("P", "6-10"): [0.16, 0.02, 0.02, 0.05, 0.17, 0.21, 0.29, 0.08],
    ("P", "11-15"): [0.13, 0.02, 0.02, 0.42, 0.10, 0.11, 0.14, 0.06],
}
RESPONSE_RATE = {"P": 0.035, "UI": 0.03, "UN": 0.01}
NPS_MEAN = {("P", "6-10"): 6.8, ("P", "11-15"): 5.2, ("UI", "6-10"): 3.9, ("UI", "11-15"): 3.6}
SURVEY_FROM = t_of("2026-04")


def reason_dist(group: str, age: str, channel: str) -> list[float]:
    dist = list(REASON_DIST[(group, age)])
    if channel == "tariff_bundle" and group == "UI":
        dist[1] += 0.12  # «не подключал сам» встречается и среди установивших приложение
        dist[6] = max(0.0, dist[6] - 0.06)
        dist[7] = max(0.0, dist[7] - 0.06)
    total = sum(dist)
    return [d / total for d in dist]


def write_survey(cells: list[Cell], rng: np.random.Generator) -> pd.DataFrame:
    records = []
    for cal in range(SURVEY_FROM, T_CUT + 1):
        for cell in cells:
            k_now = cal - cell.cohort
            if k_now < 1 or k_now > MAX_K:
                continue
            for group in GROUPS:
                if k_now == 1:
                    churned = cell.sizes[group] - cell.paid[group][1]
                    paid_before = 0
                else:
                    prev, now = cell.paid[group][k_now - 1], cell.paid[group][k_now]
                    if prev is None or now is None:
                        continue
                    churned, paid_before = prev - now, k_now - 1
                if churned <= 0:
                    continue
                n = int(rng.binomial(churned, RESPONSE_RATE[group]))
                dist = reason_dist(group, cell.age, cell.channel)
                for _ in range(n):
                    reason = REASONS[int(rng.choice(len(REASONS), p=dist))]
                    if group == "P":
                        trial_days = 30 if cell.channel == "tariff_bundle" else 14
                        window = trial_days if paid_before == 0 else 30
                        rate = 0.30 if (cell.age == "11-15" and reason == R_CHILD) else 0.62
                        days = int(rng.binomial(window, rate))
                        nps_mean = NPS_MEAN[("P", cell.age)]
                    else:
                        days = 0
                        nps_mean = NPS_MEAN.get((group, cell.age), 2.6)
                    if reason == R_CHARGE:
                        nps_mean -= 1.5
                    nps = int(np.clip(round(rng.normal(nps_mean, 2.4)), 0, 10))
                    records.append(
                        {
                            "response_month": MONTHS[cal],
                            "channel": cell.channel,
                            "child_age_group": observed_age(cell, group),
                            "child_platform": cell.platform if group == "P" else "unknown",
                            "paired_child_device": "yes" if group == "P" else "no",
                            "paid_months_before_cancel": paid_before,
                            "location_days_last_30": days,
                            "main_reason": reason,
                            "nps_0_10": nps,
                        }
                    )
    df = pd.DataFrame(records)
    order = rng.permutation(len(df))
    df = df.iloc[order].sort_values("response_month", kind="stable").reset_index(drop=True)
    df.insert(0, "response_id", [f"R{i + 1:05d}" for i in range(len(df))])
    path = MATERIALS / "internal_exit_survey_2026.csv"
    df.to_csv(path, index=False, lineterminator="\n")
    write_meta(
        path,
        "Опрос отключившихся от «Скворечника»: причины отказа и NPS, апрель–сентябрь 2026",
        "table",
        "Аналитика продукта «Скворечник» (вымышленная команда)",
        "2026-10-05",
        "СИНТЕТИКА: продукт и ответы вымышлены, персональных данных нет. Одна строка — один ответ. Опрос "
        "показывается при отключении в приложении; тем, кто приложение не устанавливал, приходит SMS со "
        "ссылкой (отвечают реже). Поля channel, child_age_group, child_platform, paired_child_device, "
        "paid_months_before_cancel и location_days_last_30 (дней с полученной геолокацией ребёнка за "
        "последние 30 дней перед отключением) подтянуты из продуктовой аналитики; main_reason — один "
        "закрытый вариант ответа; nps_0_10 — готовность рекомендовать. child_platform известна только для "
        "подключивших телефон ребёнка.",
    )
    return df


# --------------------------------------------------------------------------------------------------
# materials/internal_interviews_notes.md
# --------------------------------------------------------------------------------------------------

INTERVIEWS = [
    {
        "id": "И-1",
        "profile": "Родитель ребёнка 8 лет. У ребёнка Android-смартфон, у родителя тоже Android. "
        "Пришёл из магазина приложений осенью 2025 года, платит девять месяцев.",
        "notes": [
            "Подключение заняло «минут пять»: код ввели вместе с ребёнком вечером.",
            "Карту почти не открывает. Главное — уведомления «в школе» и «дома»: "
            "«Мне не нужно знать, где он каждую минуту. Мне нужно знать, что дошёл».",
            "Цена устраивает: «Как две чашки кофе в месяц, за спокойствие нормально».",
            "Раздражает задержка уведомления, когда ребёнок заходит в школу через задний двор.",
            "Хотел бы поддержку детских часов: «Летом он без телефона ходит на площадку».",
        ],
        "observer": "Ценность связана с уведомлениями о приходе, а не с картой. Онбординг без проблем.",
    },
    {
        "id": "И-2",
        "profile": "Родитель подростка 13 лет. У подростка iPhone, у родителя Android. Пришёл весной 2026 "
        "года по рекламе «Знайте, где ваш ребёнок. Всегда.», отключился на втором месяце.",
        "notes": [
            "Подключали три вечера: разрешение «Всегда» на iPhone «то появлялось, то пропадало». "
            "Писал в поддержку.",
            "Через три недели сын выключил геолокацию: «Сказал, что я за ним шпионю. "
            "Мы поругались, и я понял, что проиграл».",
            "В опросе при отключении выбрал «Дорого»: «А за что платить, если не работает?»",
            "На вопрос, что бы помогло: «Не знаю. Наверное, если бы он сам решал, когда показывать».",
        ],
        "observer": "Формально — сложный онбординг на iPhone, но отказ случился после подключения, "
        "из-за конфликта с подростком. Ответ «Дорого» в опросе не совпадает с рассказанной причиной.",
    },
    {
        "id": "И-3",
        "profile": "Родитель подростка 14 лет. У подростка Android. Подключился через витрину в приложении "
        "оператора, платит одиннадцать месяцев.",
        "notes": [
            "До установки договорились о правилах: «Смотрю карту, только если она не отвечает больше часа».",
            "Дочь сама включает «поделиться маршрутом», когда поздно возвращается с тренировки, — "
            "делает это через мессенджер, потому что в «Скворечнике» такой функции нет.",
            "«Было бы честно, если бы она видела, когда я открываю карту. Тогда ей не было бы обидно».",
            "Цена не обсуждалась как проблема.",
        ],
        "observer": "Подростковая семья удерживается благодаря договорённости, а не функциям. "
        "Запрос на прозрачность для ребёнка — функции нет в продукте.",
    },
    {
        "id": "И-4",
        "profile": "Абонент тарифа «Семейный», ребёнок 9 лет. Сервис подключился автоматически в марте "
        "2026 года вместе с тарифом.",
        "notes": [
            "Не знал, что сервис подключён: SMS при подключении «потерялось среди рекламы».",
            "Через месяц увидел списание 199 ₽: «Ощущение, что залезли в карман». Написал в поддержку, "
            "потребовал возврат, отключил.",
            "Приложение не устанавливал. Об идее сервиса: «Может, и полезно, но не так. Спросите сначала».",
            "В опросе выбрал «Неожиданно списали деньги».",
        ],
        "observer": "Отказ связан с автоподключением и списанием без предупреждения, а не с ценой "
        "или онбордингом: до онбординга дело не дошло.",
    },
    {
        "id": "И-5",
        "profile": "Родитель двоих детей, 7 и 11 лет. У старшего iPhone, у младшего Android, у родителя "
        "Android. Магазин приложений, платит за двоих (дважды по 199 ₽) семь месяцев.",
        "notes": [
            "Младшего подключил сразу, старшего — со второй попытки по инструкции из поддержки: "
            "«Если бы был QR-код, как в банке, было бы проще».",
            "Ценит одну карту для разных телефонов: «Локатор на Android не поставишь, а Family Link "
            "для айфона не работает».",
            "Цена «терпимая», но 398 ₽ за двоих заметны: «Семейная подписка была бы честнее».",
            "Старший пока не возражает, но «через год, наверное, начнётся».",
        ],
        "observer": "Ценность — кроссплатформенность для семьи с разными телефонами. Трение — "
        "разрешения на iPhone. Цена ощущается при нескольких детях.",
    },
    {
        "id": "И-6",
        "profile": "Родитель ребёнка 10 лет, вся семья на iPhone. Магазин приложений, отключился через "
        "четыре месяца.",
        "notes": [
            "Перешёл на встроенный Локатор: «То же самое, только бесплатно; уведомления о приходе тоже есть».",
            "Онбординг и качество претензий не вызывали.",
            "В опросе выбрал «Дорого». Вернулся бы, «если будет то, чего нет в Локаторе», — "
            "назвал историю маршрутов за месяц и SOS с автоматическим звонком.",
        ],
        "observer": "Отказ из-за бесплатной встроенной альтернативы внутри одной экосистемы. "
        "Ответ «Дорого» здесь означает «не стоит денег при бесплатном аналоге».",
    },
]


def write_interviews() -> None:
    parts = [
        "# Заметки интервью с родителями — пользователями «Скворечника»\n",
        "> СИНТЕТИКА. Интервью смоделированы для демонстрационного кейса: собеседников не существует, "
        "цитаты вымышлены, персональных данных нет. Шесть заметок — материал для гипотез; они не "
        "доказывают ни спрос, ни распространённость причин.\n",
        "**Как набирали (в мире кейса).** Сентябрь 2026, онлайн, 30–40 минут. Четверо из шести — "
        "из родителей, писавших в поддержку за последние два месяца; двое — из ушедших, оставивших "
        "контакт в опросе при отключении. Выборка смещена к тем, у кого были проблемы.\n",
    ]
    for item in INTERVIEWS:
        parts.append(f"## {item['id']}\n")
        parts.append(f"**Профиль.** {item['profile']}\n")
        parts.append("**Заметки.**\n")
        parts.append("\n".join(f"- {line}" for line in item["notes"]) + "\n")
        parts.append(f"**Комментарий интервьюера.** {item['observer']}\n")
    write_md(
        MATERIALS / "internal_interviews_notes.md",
        "Заметки шести интервью с родителями — пользователями «Скворечника» (синтетика)",
        "interview",
        "Команда продукта «Скворечник» (вымышленная)",
        "2026-09-30",
        "СИНТЕТИКА: интервью смоделированы, собеседников нет, цитаты вымышлены. Только для гипотез.",
        "\n".join(parts),
    )


# --------------------------------------------------------------------------------------------------
# materials/internal_product_passport.md
# --------------------------------------------------------------------------------------------------


def dashboard_metrics(cells: list[Cell]) -> list[dict]:
    rows = []
    for c in range(0, LAST_COHORT + 1):
        cohort = [cell for cell in cells if cell.cohort == c]
        conn = sum(cell.connected for cell in cohort)
        paired = sum(cell.paired for cell in cohort)
        m = {}
        for k in (1, 3):
            if c + k <= T_CUT:
                m[k] = sum(cell.paid[g][k] for cell in cohort for g in GROUPS)
        rows.append({"cohort": c, "connected": conn, "paired": paired, "m1": m.get(1), "m3": m.get(3)})
    return rows


def economics(cells: list[Cell]) -> dict[str, float]:
    window = range(SURVEY_FROM, T_CUT + 1)
    payers = np.mean([payers_at(cells, t) for t in window])
    app_conn = np.mean(
        [sum(c.connected for c in cells if c.cohort == t and c.channel == "app_store") for t in window]
    )
    op_conn = np.mean(
        [sum(c.connected for c in cells if c.cohort == t and c.channel == "operator_app") for t in window]
    )
    bundle_installed = np.mean(
        [sum(c.installed for c in cells if c.cohort == t and c.channel == "tariff_bundle") for t in window]
    )
    revenue = payers * NET_REVENUE_PER_PAYER
    var_cost = payers * VAR_COST_PER_PAYER + bundle_installed * VAR_COST_PER_PAYER
    marketing = app_conn * CAC_APP_STORE_2026 + op_conn * CAC_OPERATOR_APP
    result = revenue - var_cost - marketing - TEAM_COST_MONTH
    return {
        "payers": payers,
        "revenue": revenue,
        "var_cost": var_cost,
        "marketing": marketing,
        "team": TEAM_COST_MONTH,
        "result": result,
    }


def write_passport(cells: list[Cell]) -> dict:
    dash = dashboard_metrics(cells)
    eco = economics(cells)
    dash_rows = [
        [
            MONTHS[r["cohort"]],
            fmt_int(r["connected"]),
            pct(r["paired"], r["connected"]),
            pct(r["m1"], r["connected"]) if r["m1"] is not None else "—",
            pct(r["m3"], r["connected"]) if r["m3"] is not None else "—",
        ]
        for r in dash
    ]
    mln = 1_000_000

    def money(value: float) -> str:
        return f"{value / mln:.2f}".replace(".", ",").replace("-", "−")

    eco_rows = [
        ["Платящих в месяц (среднее)", fmt_int(eco["payers"])],
        ["Чистая выручка, млн ₽", money(eco["revenue"])],
        ["Переменные затраты (включая бесплатный месяц по тарифу «Семейный»), млн ₽", money(eco["var_cost"])],
        ["Маркетинг (магазины приложений и витрина), млн ₽", money(eco["marketing"])],
        ["Команда, млн ₽", money(eco["team"])],
        ["Итог за месяц, млн ₽", money(eco["result"])],
    ]
    features = [
        ["Карта с текущим местоположением ребёнка", "да", "обновление раз в 1–5 минут, при движении чаще"],
        ["Геозоны «дом», «школа», «секция» с уведомлениями о приходе и уходе", "да", "до 5 зон"],
        ["История перемещений", "да", "7 дней"],
        ["Кнопка SOS в детском приложении", "да", "отправляет координаты и звонит родителю"],
        ["Уровень заряда телефона ребёнка", "да", ""],
        ["Громкий сигнал на телефоне ребёнка", "да", "только Android"],
        ["Одна карта для Android и iPhone", "да", "родитель и ребёнок могут быть на разных системах"],
        [
            "Работа без мобильного интернета на телефоне ребёнка",
            "нет",
            "координаты передаются только через интернет; SMS-резерв в бэклоге, оценка — II квартал 2027",
        ],
        ["Поддержка детских часов", "нет", "в 2026 году не планируется"],
        ["Ребёнок видит, когда родитель открывал карту", "нет", "идея в бэклоге, не оценена"],
        ["Ребёнок сам временно делится маршрутом", "нет", "идея в бэклоге, не оценена"],
        ["Семейная подписка на нескольких детей", "нет", "каждый следующий ребёнок — ещё 199 ₽"],
    ]
    body = f"""# Паспорт продукта «Скворечник»

> СИНТЕТИКА. «Скворечник», оператор, тарифы, цифры и решения вымышлены для демонстрационного кейса.
> Совпадения с реальными продуктами и компаниями случайны.

Версия на 1 октября 2026 года. Готовит команда продукта для планирования на 2027 год.

## Что это

Подписочный сервис семейной геолокации мобильного оператора (в документах — «оператор»). Два приложения:
для родителя и для ребёнка. Запущен в сентябре 2024 года. Цена — {PRICE} ₽ в месяц, с запуска не менялась.
Сервис доступен абонентам любых операторов; оплата — картой в магазине приложений или со счёта телефона.

## Что умеет и чего не умеет

{md_table(["Функция", "Есть", "Комментарий"], features)}
## Каналы подключения

- **Магазины приложений** (`app_store` в выгрузках): 14 дней бесплатно, затем оплата картой или со счёта.
- **Витрина в приложении оператора** (`operator_app`): 14 дней бесплатно, затем оплата со счёта.
- **Тариф «Семейный»** (`tariff_bundle`): с 1 февраля 2026 года сервис автоматически подключается новым
  абонентам тарифа на 30 дней бесплатно, затем — {PRICE} ₽ в месяц со счёта, если абонент не отключил его.
  При подключении приходит SMS со ссылкой на приложение. Отдельного напоминания перед первым списанием нет.

## Как считаются показатели в дашборде

- **Подключение** — начало бесплатного периода или автоподключение по тарифу. С 1 февраля 2026 года оба
  случая считаются в одном показателе; фильтра по каналу в дашборде нет.
- **Подключили ребёнка** — телефон ребёнка привязан, и в течение 3 дней выдано фоновое разрешение на
  геолокацию.
- **Mk** — доля подключений когорты, оплативших k-й платный период; M1 — первая оплата после бесплатного
  периода. Целевой показатель команды — M3.
- **Возраст ребёнка** родитель указывает в приложении; если приложение не установлено — «не указан».
- Выгрузка биллинга за март 2026 года по витрине оператора частично потеряна (сбой).

## Дашборд: показатели по месяцам подключения (все каналы вместе)

{md_table(["Месяц подключения", "Подключений", "Подключили ребёнка, %", "M1, %", "M3, %"], dash_rows)}
«—» — период ещё не наступил к дате выгрузки (30.09.2026).

## Продвижение и коммуникация

- **Сентябрь 2024 — февраль 2026.** Кампания «Спокойно: все дома». Аудитория — родители младших
  школьников. Главное сообщение — уведомление «дошёл до школы» и «дома».
- **С марта 2026.** Кампания «Знайте, где ваш ребёнок. Всегда.» Аудитория расширена на родителей детей
  11–15 лет, бюджет в магазинах приложений увеличен примерно на четверть. Стоимость одного подключения
  выросла с {CAC_APP_STORE_2025} до {CAC_APP_STORE_2026} ₽.
- **Черновик зимней кампании** (на согласовании, не запущен): «Скворечник сообщит, что ребёнок дошёл до
  школы, — даже если у него закончился интернет». Канал — магазины приложений и витрина оператора.

## Экономика (оценка финансовой службы)

- Чистая выручка на платящего — около {NET_REVENUE_PER_PAYER} ₽ в месяц (после налогов и комиссий магазинов
  и платёжных систем).
- Переменные затраты — около {VAR_COST_PER_PAYER} ₽ в месяц на пользователя с подключённым сервисом
  (картографические сервисы, push и SMS, хранение истории, поддержка).
- Стоимость одного подключения: магазины приложений — {CAC_APP_STORE_2025} ₽ в 2025 году и
  {CAC_APP_STORE_2026} ₽ с марта 2026 года; витрина оператора — около {CAC_OPERATOR_APP} ₽ (внутренняя оценка
  трафика); тариф «Семейный» — без затрат на маркетинг.
- Команда — 7 человек, полная стоимость около {money(TEAM_COST_MONTH)} млн ₽ в месяц.

Средний месяц, апрель–сентябрь 2026 года:

{md_table(["Статья", "Значение"], eco_rows)}
- Выход на безубыточность был запланирован на IV квартал 2026 года; при текущей динамике план не
  выполняется.
- Аналитика оператора: у семей-абонентов с подключённым «Скворечником» отток с тарифа за 6 месяцев на
  1,4 п. п. ниже, чем у похожих семей без него. Сравнение без контроля отбора, причинность не проверялась.

## Что сейчас в работе

- С 1 сентября 2026 года идёт A/B-тест нового онбординга на 50% новых подключений во всех каналах:
  QR-код для привязки телефона ребёнка, пошаговая инструкция разрешений для iPhone, напоминание через
  24 часа, если ребёнок не подключён. Итоги по первой оплате ожидаются в начале ноября.
- NPS активных платящих (квартальный опрос): IV квартал 2025 — +41, II квартал 2026 — +33; у родителей
  детей 6–10 лет — +44, 11–15 лет — +12.

## Ограничения на 2027 год

- Бюджет продукта не растёт, численность команды не меняется.
- Любое изменение должно показать эффект в течение квартала.
- Цена и состав тарифа «Семейный» согласуются с коммерческим блоком оператора: решение не за командой
  продукта.
"""
    write_md(
        MATERIALS / "internal_product_passport.md",
        "Паспорт продукта «Скворечник»: функции, каналы, метрики, коммуникация, экономика (синтетика)",
        "document",
        "Команда продукта «Скворечник» (вымышленная)",
        "2026-10-01",
        "СИНТЕТИКА: продукт, оператор, тарифы, цифры и решения вымышлены. Дашборд в документе посчитан "
        "из тех же данных, что internal_cohorts_retention.csv.",
        body,
    )
    return {"dash": dash, "eco": eco}


# --------------------------------------------------------------------------------------------------
# new_evidence/: A/B-тест нового онбординга на когорте сентября 2026
# --------------------------------------------------------------------------------------------------

AB_COLUMNS = [
    "connected",
    "parent_app_installed",
    "invite_sent",
    "invite_sent_ios",
    "child_app_installed",
    "child_app_installed_ios",
    "paired_3d",
    "paired_3d_ios",
    "first_alert_7d",
    "paid_m1",
    "paid_m1_paired",
]


def ab_rows(cells: list[Cell]) -> pd.DataFrame:
    agg: dict[tuple, dict[str, int]] = {}
    for cell in cells:
        if cell.cohort != T_AB:
            continue
        ios = cell.platform == "ios"
        known = agg.setdefault((cell.arm, cell.channel, cell.age), dict.fromkeys(AB_COLUMNS, 0))
        known["connected"] += cell.installed
        known["parent_app_installed"] += cell.installed
        known["invite_sent"] += cell.invite
        known["invite_sent_ios"] += cell.invite_ios
        known["child_app_installed"] += cell.child
        known["child_app_installed_ios"] += cell.child_ios
        known["paired_3d"] += cell.paired
        known["paired_3d_ios"] += cell.paired if ios else 0
        known["first_alert_7d"] += cell.alert
        known["paid_m1"] += cell.paid["P"][1] + cell.paid["UI"][1]
        known["paid_m1_paired"] += cell.paid["P"][1]
        unknown = agg.setdefault((cell.arm, cell.channel, "unknown"), dict.fromkeys(AB_COLUMNS, 0))
        unknown["connected"] += cell.sizes["UN"]
        unknown["paid_m1"] += cell.paid["UN"][1]
    age_order = {"6-10": 0, "11-15": 1, "unknown": 2}
    rows = []
    for (arm, channel, age), values in sorted(
        agg.items(), key=lambda kv: (ARMS_AB.index(kv[0][0]), CHANNELS.index(kv[0][1]), age_order[kv[0][2]])
    ):
        rows.append(
            {"cohort_month": MONTHS[T_AB], "arm": arm, "channel": channel, "child_age_group": age, **values}
        )
    return pd.DataFrame(rows)


def diff_ci(x1: int, n1: int, x0: int, n0: int) -> tuple[float, float, float]:
    p1, p0 = x1 / n1, x0 / n0
    se = math.sqrt(p1 * (1 - p1) / n1 + p0 * (1 - p0) / n0)
    d = p1 - p0
    return 100 * d, 100 * (d - 1.96 * se), 100 * (d + 1.96 * se)


def fmt_pp(value: float) -> str:
    return f"{value:+.1f}".replace(".", ",").replace("-", "−")


def write_new_evidence(cells: list[Cell], rng: np.random.Generator) -> pd.DataFrame:
    df = ab_rows(cells)
    path = NEW_EVIDENCE / "onboarding_ab_2026-09_cohort.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, lineterminator="\n")
    write_meta(
        path,
        "Когорта сентября 2026: A/B-тест нового онбординга «Скворечника», первая оплата",
        "table",
        "Аналитика продукта «Скворечник» (вымышленная команда)",
        "2026-11-02",
        "СИНТЕТИКА: продукт и данные вымышлены. Свежая когорта подключений 2026-09, данные по 31.10.2026. "
        "arm — control (старый онбординг) или new_onboarding (QR-код, инструкция разрешений для iPhone, "
        "напоминание через 24 часа). Одна строка — плечо x канал x возраст ребёнка; столбцы — шаги воронки "
        "в тех же определениях, что internal_onboarding_funnel.xlsx (для child_age_group = unknown "
        "приложение родителя не установлено, connected — число таких подключений), paid_m1 — первая "
        "оплата, paid_m1_paired — из них подключившие ребёнка. Протокол — onboarding_ab_2026-09_protocol.md.",
    )

    def sums(mask) -> dict[str, int]:
        part = df[mask]
        return {col: int(part[col].sum()) for col in AB_COLUMNS}

    def row_for(label: str, mask_base) -> list[str]:
        c = sums(mask_base & (df["arm"] == "control"))
        t = sums(mask_base & (df["arm"] == "new_onboarding"))
        d, lo, hi = diff_ci(t["paid_m1"], t["connected"], c["paid_m1"], c["connected"])
        return [
            label,
            fmt_int(c["connected"]),
            fmt_int(t["connected"]),
            pct(c["paired_3d"], c["connected"]),
            pct(t["paired_3d"], t["connected"]),
            pct(c["paid_m1"], c["connected"]),
            pct(t["paid_m1"], t["connected"]),
            f"{fmt_pp(d)} [{fmt_pp(lo)}; {fmt_pp(hi)}]",
        ]

    every = df["arm"].notna()
    nonbundle = df["channel"] != "tariff_bundle"
    result_rows = [
        row_for("Все каналы", every),
        row_for("Магазины приложений и витрина, 6–10 лет", nonbundle & (df["child_age_group"] == "6-10")),
        row_for("Магазины приложений и витрина, 11–15 лет", nonbundle & (df["child_age_group"] == "11-15")),
        row_for("Тариф «Семейный», все", df["channel"] == "tariff_bundle"),
    ]
    ios_rows = []
    for arm in ARMS_AB:
        s = sums(df["arm"] == arm)
        android_inv = s["invite_sent"] - s["invite_sent_ios"]
        android_paired = s["paired_3d"] - s["paired_3d_ios"]
        ios_rows.append(
            [
                "контроль" if arm == "control" else "новый онбординг",
                pct(s["paired_3d_ios"], s["invite_sent_ios"]),
                pct(android_paired, android_inv),
            ]
        )
    tickets = {}
    for arm in ARMS_AB:
        installed = sums((df["arm"] == arm) & (df["child_age_group"] != "unknown"))["parent_app_installed"]
        rate = 0.045 if arm == "control" else 0.029
        tickets[arm] = (int(rng.poisson(rate * installed)), installed)

    def tickets_per_1000(arm: str) -> str:
        count, base = tickets[arm]
        return f"{1000 * count / base:.1f}".replace(".", ",")

    body = f"""# Протокол A/B-теста нового онбординга «Скворечника» (когорта сентября 2026)

> СИНТЕТИКА. Продукт, тест и цифры вымышлены для демонстрационного кейса. Данные —
> `onboarding_ab_2026-09_cohort.csv` в этой же папке.

Подготовлено аналитикой продукта 2 ноября 2026 года.

## Что проверяли

Гипотеза: если упростить подключение телефона ребёнка, больше родителей подключат ребёнка в первые три дня
и больше из них оплатят первый платный период.

## Как устроен тест

- Период: подключения с 1 по 30 сентября 2026 года; оплаты — по 31 октября 2026 года.
- Случайное распределение 50/50 по идентификатору подключения во всех трёх каналах.
- В новом онбординге: QR-код вместо шестизначного кода, пошаговая инструкция разрешений для iPhone
  (включая «Всегда»), напоминание через 24 часа, если ребёнок не подключён.
- Не менялись: цена, бесплатный период, тексты рекламы, детское приложение, порядок автоподключения по
  тарифу «Семейный». Экрана «договоритесь с ребёнком о правилах» в тесте не было.
- Основная метрика — первая оплата (M1) от подключений. Вспомогательная — подключили ребёнка за 3 дня.
  Ограничители — обращения в поддержку о подключении телефона ребёнка и возвраты.

## Результаты

{
        md_table(
            [
                "Срез",
                "Подключений, контроль",
                "Подключений, тест",
                "Подключили ребёнка, контроль, %",
                "Подключили ребёнка, тест, %",
                "M1, контроль, %",
                "M1, тест, %",
                "Разница M1, п. п. [95% ДИ]",
            ],
            result_rows,
        )
    }
Подключение ребёнка за 3 дня среди отправивших приглашение, по платформе телефона ребёнка:

{md_table(["Плечо", "iPhone, %", "Android, %"], ios_rows)}
Обращения о подключении телефона ребёнка на 1000 установивших приложение родителя: контроль —
{tickets_per_1000("control")}, новый онбординг — {tickets_per_1000("new_onboarding")}.

## Ограничения

- Видна только первая оплата; удержание на M2 и дальше станет известно не раньше декабря.
- Сентябрь — пик подключений перед школой; эффект в другие месяцы может отличаться.
- Возраст ребёнка известен только для установивших приложение родителя, поэтому в срезах по возрасту
  знаменатель — подключения с указанным возрастом; строка «Все каналы» включает и подключения без возраста.
"""
    write_md(
        NEW_EVIDENCE / "onboarding_ab_2026-09_protocol.md",
        "Протокол и итоги A/B-теста нового онбординга «Скворечника», когорта сентября 2026 (синтетика)",
        "document",
        "Аналитика продукта «Скворечник» (вымышленная команда)",
        "2026-11-02",
        "СИНТЕТИКА: продукт, тест и цифры вымышлены. Сводка посчитана из onboarding_ab_2026-09_cohort.csv.",
        body,
    )
    return df


# --------------------------------------------------------------------------------------------------
# Сводка для README (печатается, в файлы не пишется)
# --------------------------------------------------------------------------------------------------


def summarize(cells: list[Cell], cohorts: pd.DataFrame, survey: pd.DataFrame, info: dict) -> None:
    dash = info["dash"]

    def headline(months: list[str], key: str) -> float:
        rows = [r for r in dash if MONTHS[r["cohort"]] in months and r[key] is not None]
        return 100 * sum(r[key] for r in rows) / sum(r["connected"] for r in rows)

    def paired_rate(months: list[str]) -> float:
        rows = [r for r in dash if MONTHS[r["cohort"]] in months]
        return 100 * sum(r["paired"] for r in rows) / sum(r["connected"] for r in rows)

    y2025 = [m for m in MONTHS if m.startswith("2025")]
    spring = ["2026-03", "2026-04", "2026-05", "2026-06"]
    print(f"Дашборд M3: 2025 = {headline(y2025, 'm3'):.1f}%, весна 2026 = {headline(spring, 'm3'):.1f}%")
    print(
        f"Дашборд подключили ребёнка: 2025 = {paired_rate(y2025):.1f}%, 2026-03..08 = "
        f"{paired_rate(['2026-03', '2026-04', '2026-05', '2026-06', '2026-07', '2026-08']):.1f}%"
    )
    df = cohorts.copy()
    df["year"] = df["cohort_month"].str[:4]
    for label, mask in (
        ("без бандла", df["channel"] != "tariff_bundle"),
        ("app_store 6-10", (df["channel"] == "app_store") & (df["child_age_group"] == "6-10")),
        ("app_store 11-15", (df["channel"] == "app_store") & (df["child_age_group"] == "11-15")),
        ("operator_app", df["channel"] == "operator_app"),
        ("bundle", df["channel"] == "tariff_bundle"),
    ):
        for period, months in (("2025", y2025), ("весна 2026", spring)):
            part = df[mask & df["cohort_month"].isin(months) & df["paid_m3"].notna()]
            if len(part):
                print(f"  M3 {label} {period}: {100 * part['paid_m3'].sum() / part['connected'].sum():.1f}%")
    top = survey["main_reason"].value_counts(normalize=True).head(4)
    print("Опрос, топ причин:", {k: round(100 * v, 1) for k, v in top.items()}, "n =", len(survey))
    price = survey[survey["main_reason"] == R_PRICE]
    print(f"  «Дорого»: не подключали ребёнка {100 * (price['paired_child_device'] == 'no').mean():.0f}%")
    print(f"Экономика: { ({k: round(v) for k, v in info['eco'].items()}) }")


def main() -> None:
    rng = np.random.default_rng(SEED)
    MATERIALS.mkdir(parents=True, exist_ok=True)
    cells = simulate(rng)
    cohorts = write_cohorts(cells)
    write_funnel(cells, rng)
    write_support(cells, rng)
    survey = write_survey(cells, rng)
    write_interviews()
    info = write_passport(cells)
    write_new_evidence(cells, rng)
    summarize(cells, cohorts, survey, info)
    generated = sorted(p for p in [*MATERIALS.glob("internal_*"), *NEW_EVIDENCE.glob("*")] if p.is_file())
    for path in generated:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        print(f"{digest}  {path.relative_to(CASE_DIR)}")


if __name__ == "__main__":
    main()
