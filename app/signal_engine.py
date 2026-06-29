from __future__ import annotations



from dataclasses import dataclass

from typing import List



from app.polymarket.data_client import SourceTrade





@dataclass

class Signal:

    token_id: str

    action: str  # BUY / SELL

    strength: float  # 0–1

    reason: str





class SignalEngine:

    """

    Converts raw trades into structured trading signals.

    This is where actual edge extraction starts.

    """



    def generate(self, fill: SourceTrade) -> List[Signal]:

        signals: List[Signal] = []



        if fill.side != "BUY":

            return signals



        strength = self._base_strength(fill)



        if strength < 0.4:

            return signals



        # Momentum signal

        if fill.price > 0.65 and fill.usd_size > 25:

            signals.append(

                Signal(

                    token_id=fill.token_id,

                    action="BUY",

                    strength=strength + 0.15,

                    reason="momentum_breakout",

                )

            )



        # Conviction whale signal

        if fill.usd_size > 100:

            signals.append(

                Signal(

                    token_id=fill.token_id,

                    action="BUY",

                    strength=strength + 0.2,

                    reason="whale_conviction",

                )

            )



        # Early positioning signal

        if 0.3 < fill.price < 0.5:

            signals.append(

                Signal(

                    token_id=fill.token_id,

                    action="BUY",

                    strength=strength,

                    reason="early_value_zone",

                )

            )



        return signals



    def _base_strength(self, fill: SourceTrade) -> float:

        size_score = min(fill.usd_size / 75.0, 1.0)

        price_score = abs(fill.price - 0.5) * 2.0



        return 0.5 * size_score + 0.5 * price_score