"""Identity and current-brand authorization ports; production providers are not configured."""

from typing import Protocol

from app.schemas.business_api import ActorScope


class ActorAuthenticator(Protocol):
    async def authenticate(self, bearer_token: str) -> ActorScope:
        """Validate a credential using a trusted provider, never client-supplied identity fields."""


class CurrentBrandAuthorizer(Protocol):
    def authorize(self, actor: ActorScope) -> bool:
        """Consult current trusted permissions; old session scopes are not authorization."""
