"""Persistent fan-out/fan-in workflow; dependencies are injected for testing."""

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app.config import Settings, load_policy
from app.graph.nodes import AuditNodes, audited
from app.graph.state import AuditState
from app.repo.repositories import Repository
from app.rules.registry import RULE_GROUPS


def build_graph(checkpointer, repo=None, extraction=None, explanation=None, policy=None):
    if repo is None or extraction is None or explanation is None:
        from app.services.explanation import ExplanationService
        from app.services.extraction import ExtractionService
        from app.services.llm_client import LLMClient

        settings = Settings.from_env()
        repo = repo or Repository(settings.db_path)
        client = LLMClient(
            mode=settings.llm_mode,
            cache_dir=settings.cache_dir,
            model=settings.llm_model,
            api_key=settings.anthropic_api_key,
        )
        extraction = extraction or ExtractionService(client)
        explanation = explanation or ExplanationService(client)
    nodes = AuditNodes(repo, extraction, explanation, policy or load_policy())
    graph = StateGraph(AuditState)
    for name in (
        "register_run",
        "extract_invoice",
        "load_context",
        "confidence_gate",
        "aggregate_findings",
        "explain_findings",
        "route",
        "finalize",
    ):
        graph.add_node(name, audited(repo, name, getattr(nodes, name)))
    graph.add_node("dispatch_rules", audited(repo, "dispatch_rules", lambda state: {}))
    for group in RULE_GROUPS:
        graph.add_node(group, audited(repo, group, nodes.check(group)))
        graph.add_edge(group, "aggregate_findings")
    for name in (
        "field_verify",
        "ap_review",
        "level1_approval",
        "level2_approval",
        "wait_for_info",
    ):
        graph.add_node(name, audited(repo, name, nodes.review(name)))
        graph.add_conditional_edges(
            name,
            lambda state: state["next_step"],
            {
                "load_context": "load_context",
                "extract_invoice": "extract_invoice",
                "wait_for_info": "wait_for_info",
                "level2_approval": "level2_approval",
                "finalize": "finalize",
            },
        )
    graph.add_edge(START, "register_run")
    graph.add_edge("register_run", "extract_invoice")
    graph.add_edge("extract_invoice", "load_context")
    graph.add_edge("load_context", "confidence_gate")
    graph.add_conditional_edges(
        "confidence_gate",
        lambda state: state["next_step"],
        {"field_verify": "field_verify", "dispatch_rules": "dispatch_rules"},
    )
    graph.add_conditional_edges(
        "dispatch_rules",
        lambda state: [Send(name, state) for name in RULE_GROUPS],
        list(RULE_GROUPS),
    )
    graph.add_edge("aggregate_findings", "explain_findings")
    graph.add_edge("explain_findings", "route")
    graph.add_conditional_edges(
        "route",
        lambda state: state["next_step"],
        {
            "field_verify": "field_verify",
            "ap_review": "ap_review",
            "level1_approval": "level1_approval",
            "finalize": "finalize",
        },
    )
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)
