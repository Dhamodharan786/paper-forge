from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config


class CapExceeded(RuntimeError):
    pass


@dataclass
class Call:
    label: str
    model: str
    input_tokens: int
    output_tokens: int
    searches: int
    usd: float


@dataclass
class CostTracker:
    cfg: Config
    calls: list[Call] = field(default_factory=list)

    @property
    def total(self) -> float:
        return sum(c.usd for c in self.calls)

    def price(self, model: str, in_tok: int, out_tok: int, searches: int) -> float:
        p = self.cfg.prices.get(model)
        if p is None:
            raise KeyError(f"No price for model {model!r} in forge.toml [prices]")
        return (in_tok * p["input"] + out_tok * p["output"]) / 1e6 + searches * self.cfg.web_search_usd

    def add(self, label: str, model: str, usage) -> Call:
        """usage is the `usage` object of an API response (or any object with the same attributes)."""
        # Cached input is billed differently; count it as normal input here, which slightly overstates.
        in_tok = (
            (getattr(usage, "input_tokens", 0) or 0)
            + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
            + (getattr(usage, "cache_read_input_tokens", 0) or 0)
        )
        out_tok = getattr(usage, "output_tokens", 0) or 0
        stu = getattr(usage, "server_tool_use", None)
        searches = (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
        call = Call(label, model, in_tok, out_tok, searches, self.price(model, in_tok, out_tok, searches))
        self.calls.append(call)
        return call

    def check(self) -> None:
        if self.total >= self.cfg.spend_cap_usd:
            raise CapExceeded(
                f"Spend ${self.total:.2f} reached the cap ${self.cfg.spend_cap_usd:.2f}. "
                "Finished work is saved. Raise limits.spend_cap_usd in forge.toml to continue."
            )

    def summary(self) -> str:
        lines = [f"{'step':<22}{'model':<20}{'in':>9}{'out':>9}{'search':>7}{'USD':>9}"]
        for c in self.calls:
            lines.append(f"{c.label:<22}{c.model:<20}{c.input_tokens:>9}{c.output_tokens:>9}{c.searches:>7}{c.usd:>9.4f}")
        lines.append(f"{'TOTAL':<22}{'':<20}{'':>9}{'':>9}{'':>7}{self.total:>9.4f}  (cap {self.cfg.spend_cap_usd:.2f})")
        return "\n".join(lines)
