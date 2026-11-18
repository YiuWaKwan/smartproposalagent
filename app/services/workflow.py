"""Application entry into the graph, separate from HTTP and durable workers."""

from app.graphs.campaign.graph import build_graph
from app.graphs.campaign.state import AgentState


class WorkflowService:
    def __init__(self) -> None:
        self.graph = build_graph()

    async def process(self, state: AgentState) -> AgentState:
        # TODO(execution, design §§4.3, 6.1): accept only a persisted event,
        # reauthorize, acquire conversation lease, then start/resume the graph.
        return await self.graph.ainvoke(state)
