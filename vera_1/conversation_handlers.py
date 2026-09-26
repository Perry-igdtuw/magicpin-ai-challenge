"""Optional standalone multi-turn handler for evaluators importing the project."""

from bot import reply_action


def respond(state: dict, merchant_message: str) -> dict:
    """Adapt the HTTP reply contract to a small in-process state object."""
    state.setdefault("turns", 0)
    state["turns"] += 1
    return reply_action({
        "conversation_id": state.setdefault("conversation_id", "local-conversation"),
        "merchant_id": state.get("merchant_id"),
        "customer_id": state.get("customer_id"),
        "from_role": "merchant",
        "message": merchant_message,
        "turn_number": state["turns"],
    })
