"""Application entry into the graph, separate from HTTP and durable workers."""

from app.graphs.campaign.graph import build_graph
from app.graphs.campaign.state import AgentState
from app.config import get_settings
from app.services.adapters.smartproposal import SmartProposalAdapter


class WorkflowService:
    def __init__(self) -> None:
        self.business_services = SmartProposalAdapter(get_settings())
        self.graph = build_graph(self.business_services)

    async def process(self, state: AgentState) -> AgentState:
        # Durable event acceptance, authorization and lease acquisition are a
        # later substage. Selection routes only inspect and edit local state;
        # existing business and recommendation routes have separate gates.
        return await self.graph.ainvoke(state)
