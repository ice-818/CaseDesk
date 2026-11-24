from app.tools.factory import build_tool_registry


def test_registry_has_seventeen_tools() -> None:
    class _Retriever:
        pass

    registry = build_tool_registry(_Retriever())  # type: ignore[arg-type]
    assert len(registry.names()) == 17
    assert "get_order" in registry.names()
    assert "escalate_to_human" in registry.names()
    assert "process_refund" in registry.names()
    assert "process_return" in registry.names()
