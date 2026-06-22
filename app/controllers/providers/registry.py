from app.controllers.providers.base import ProviderClient
from app.controllers.providers.google.gmail_client import GmailClient
from app.controllers.providers.imap_adapter import ImapProviderAdapter
from app.controllers.providers.microsoft.graph_client import GraphClient
from app.models.account import Account, AccountProvider


class ProviderRegistry:
    """Resolves the provider client for an account."""

    def __init__(
        self,
        gmail_client: GmailClient,
        graph_client: GraphClient,
        imap_adapter: ImapProviderAdapter,
    ) -> None:
        self._clients: dict[AccountProvider, ProviderClient] = {
            AccountProvider.google: gmail_client,
            AccountProvider.microsoft: graph_client,
            AccountProvider.imap: imap_adapter,
        }

    def get_client(self, account: Account) -> ProviderClient:
        return self._clients[account.provider]

    def get_gmail(self) -> GmailClient:
        client = self._clients[AccountProvider.google]
        assert isinstance(client, GmailClient)
        return client

    def get_graph(self) -> GraphClient:
        client = self._clients[AccountProvider.microsoft]
        assert isinstance(client, GraphClient)
        return client
