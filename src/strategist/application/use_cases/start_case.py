"""StartCase: создать кейс из брифа и зарегистрировать материалы (неизменяемые копии с хешами)."""

from __future__ import annotations

from strategist.application.context import AppContext
from strategist.application.ports import MaterialFile
from strategist.domain.entities import DecisionCase, Source, SourceKind
from strategist.domain.services.reliability import source_prior

KIND_MAP = {
    "table": SourceKind.TABLE,
    "web_excerpt": SourceKind.WEB,
    "web_snapshot": SourceKind.WEB,
    "web": SourceKind.WEB,
    "interview": SourceKind.INTERVIEW,
    "note": SourceKind.NOTE,
    "user_input": SourceKind.USER_INPUT,
}


def source_from_material(case: DecisionCase, material: MaterialFile) -> Source:
    source = Source(
        id=case.new_id("source"),
        title=material.title,
        kind=KIND_MAP.get(material.kind, SourceKind.DOCUMENT),
        path=material.path,
        url=material.source_url,
        publisher=material.publisher,
        published=material.published,
        retrieved=material.retrieved,
        sha256=material.sha256,
        synthetic=material.synthetic,
        synthetic_kind=(
            material.meta.get("synthetic_kind") or ("simulated_data" if material.synthetic else None)
        ),
        added_at=material.added_at,
        note=material.note,
    )
    source.reliability = source_prior(source)
    return source


def register_materials(
    ctx: AppContext, case: DecisionCase, paths: list[str], *, subdir: str = ""
) -> list[Source]:
    added: list[Source] = []
    known = {s.sha256 for s in case.sources}
    for path in paths:
        material = ctx.materials.add_file(case.id, path, subdir=subdir)
        if material.sha256 in known:
            continue
        source = source_from_material(case, material)
        case.sources.append(source)
        known.add(material.sha256)
        added.append(source)
        ctx.emit(
            "material.registered",
            case.id,
            source_id=source.id,
            path=material.path,
            synthetic=source.synthetic,
        )
    return added


def start_case(
    ctx: AppContext, *, case_id: str, title: str, brief: str, material_paths: list[str]
) -> DecisionCase:
    if ctx.cases.exists(case_id):
        raise ValueError(f"Кейс {case_id} уже существует — используйте resume")
    now = ctx.clock.now()
    case = DecisionCase(id=case_id, title=title, brief=brief.strip(), created_at=now, updated_at=now)
    register_materials(ctx, case, material_paths)
    case.log(now, "Кейс создан", [f"материалов: {len(case.sources)}"])
    ctx.cases.create(case)
    ctx.emit("case.created", case_id, sources=len(case.sources))
    return case
