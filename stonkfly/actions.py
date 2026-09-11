"""The public invoke() method is called directly by the fixed neural decoder.
No LLM, general wallet tools, or hidden profit-based action selection.
"""

import time
from typing import Literal

from pydantic import BaseModel, ConfigDict


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: str
    side: Literal["BUY", "SELL"]


class StonkflyActions:
    def __init__(self, guard, broker):
        self.guard = guard
        self.broker = broker
        self.quotes = {}

    def invoke(self, args):
        p = Proposal.model_validate(args)
        plan = self.guard.plan(p.product, p.side, self.quotes)
        plan["neural_observation"] = self.guard.l.get("observation")
        plan["checkpoint"] = self.guard.l.get("checkpoint")
        plan = self.guard.l.reserve(plan, time.time())
        return self.broker.execute(plan, self.guard.before_submit)
