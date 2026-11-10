from factories import empty_case, grounded_fact
from fakes import make_context
from strategist.application.use_cases.knowledge import case_documents, index_case


def test_case_documents_cover_sources_quotes_and_claims():
    case = empty_case("c1")
    claim = grounded_fact(case, "Удержание M3 снизилось до 33%")
    docs, links = case_documents(case)
    kinds = {d.kind for d in docs}
    assert kinds == {"material", "claim"}
    claim_doc = next(d for d in docs if d.kind == "claim")
    assert claim_doc.meta["type"] == "fact" and claim_doc.meta["support"] == "supported"
    assert (f"c1:{claim.id}", f"c1:{claim.evidence_ids[0]}", "supported_by") in links


def test_index_case_upserts_and_links():
    ctx = make_context()
    case = empty_case("c1")
    grounded_fact(case)
    assert index_case(ctx, case) == 3
    assert ctx.knowledge.search("удержание")
    assert ctx.knowledge.links and ctx.events.of("knowledge.indexed")
