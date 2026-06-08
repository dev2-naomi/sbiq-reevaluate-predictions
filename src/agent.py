from typing import TypedDict, Annotated
import operator

from langchain_anthropic import ChatAnthropic
from langgraph.graph import StateGraph, END


class State(TypedDict):
    messages: Annotated[list, operator.add]
    preconditions: list[str]
    evaluation_result: str


model = ChatAnthropic(model="claude-opus-4-5")


def evaluate_preconditions(state: State) -> State:
    preconditions = state.get("preconditions", [])
    messages = state.get("messages", [])

    prompt = (
        "You are evaluating whether the following preconditions are still valid "
        "given the current context.\n\n"
        f"Preconditions:\n" + "\n".join(f"- {p}" for p in preconditions) + "\n\n"
        "Assess each precondition and return a structured evaluation."
    )

    response = model.invoke(messages + [{"role": "user", "content": prompt}])

    return {
        "messages": [response],
        "evaluation_result": response.content,
        "preconditions": preconditions,
    }


def should_continue(state: State) -> str:
    if state.get("evaluation_result"):
        return "end"
    return "evaluate"


builder = StateGraph(State)
builder.add_node("evaluate", evaluate_preconditions)
builder.set_entry_point("evaluate")
builder.add_conditional_edges("evaluate", should_continue, {"end": END, "evaluate": "evaluate"})

graph = builder.compile()
