from dataclasses import dataclass


@dataclass
class Allocation:
    multiplier: float
    reason: str


class PortfolioAllocator:
    """
    Adjusts exposure based on system confidence + risk state.
    """

    def get_multiplier(self, signal_strength: float, volatility: float = 0.5) -> Allocation:

        if signal_strength > 0.8:
            return Allocation(1.5, "high_conviction")

        if signal_strength > 0.6:
            return Allocation(1.0, "normal")

        if signal_strength > 0.45:
            return Allocation(0.6, "weak_signal")

        return Allocation(0.0, "ignore")