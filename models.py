from dataclasses import dataclass
from typing import Sequence

from app.models import Account


@dataclass
class WorkerConfig:
    worker_id: int
    accounts: Sequence[Account]
    max_connections_per_provider: int = 50
