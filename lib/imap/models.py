from dataclasses import dataclass


@dataclass
class AccountConfig:
    email: str
    username: str
    password: str
    provider: str
    webhook_url: str

    def __post_init__(self) -> None:
        if not all([self.email, self.username, self.password, self.provider, self.webhook_url]):
            raise ValueError("All account fields are required")
