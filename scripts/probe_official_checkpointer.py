"""Probe: does langgraph-checkpoint-redis work against plain redis:7 (no modules)?"""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class S(TypedDict):
    n: int


def inc(s: S) -> S:
    return {"n": s["n"] + 1}


g = StateGraph(S)
g.add_node("inc", inc)
g.add_edge(START, "inc")
g.add_edge("inc", END)

try:
    from langgraph.checkpoint.redis import RedisSaver

    with RedisSaver.from_conn_string("redis://127.0.0.1:16379/0") as saver:
        saver.setup()
        app = g.compile(checkpointer=saver)
        print(app.invoke({"n": 1}, {"configurable": {"thread_id": "probe"}}))
        print("OFFICIAL REDIS CHECKPOINTER: OK")
except Exception as e:
    print("OFFICIAL REDIS CHECKPOINTER FAILED:", type(e).__name__, str(e)[:300])
