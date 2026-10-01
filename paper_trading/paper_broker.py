"""
Corretora simulada: simula a execução de ordens sem interagir com a exchange real.

Decisão de projeto: PaperBroker replica a interface esperada por qualquer
executor de ordens, de modo que a troca de paper trading para operações ao vivo
não exija mudanças nas camadas superiores — basta injetar outra corretora.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from utils.helpers import utc_now
from utils.logger import get_logger
from utils.validators import validate_positive_float

logger = get_logger(__name__)

_TAKER_FEE = 0.001  # 0.1%


@dataclass
class PaperOrder:
    """Representa uma ordem simulada."""

    order_id: str
    symbol: str
    side: str  # buy | sell
    order_type: str  # mercado | limite
    quantity: float
    price: float
    filled_quantity: float = 0.0
    status: str = "open"  # open | filled | cancelled
    fee: float = 0.0
    timestamp: datetime = field(default_factory=utc_now)


@dataclass
class PaperBalance:
    """Saldos atuais da carteira simulada."""

    cash: float
    positions: dict[str, float] = field(default_factory=dict)

    @property
    def total_value(self) -> float:
        """Valor total incluindo o caixa (as posições são avaliadas pelo custo de aquisição)."""
        return self.cash


class PaperBroker:
    """
    Simula a execução de ordens da exchange em paper trading.

    Args:
        initial_capital: Saldo inicial em caixa na moeda de cotação.
        fee_pct: Taxa taker simulada por execução (padrão 0.1%).
    """

    def __init__(
        self,
        initial_capital: float = 10_000.0,
        fee_pct: float = _TAKER_FEE,
    ) -> None:
        validate_positive_float(initial_capital, "initial_capital")
        self._balance = PaperBalance(cash=initial_capital)
        self._fee_pct = fee_pct
        self._orders: dict[str, PaperOrder] = {}
        self._order_counter = 0

        logger.info(
            "PaperBroker initialised - capital=%.2f fee=%.4f",
            initial_capital,
            fee_pct,
        )

    # ------------------------------------------------------------------
    # Balance
    # ------------------------------------------------------------------

    @property
    def fee_pct(self) -> float:
        return float(self._fee_pct)

    def get_balance(self) -> PaperBalance:
        """Retorna uma cópia do estado atual do saldo."""
        return PaperBalance(
            cash=self._balance.cash,
            positions=dict(self._balance.positions),
        )

    def export_runtime_state(self) -> dict[str, Any]:
        """Exporta o estado do saldo da corretora para retomada da execução."""
        return {
            "cash": float(self._balance.cash),
            "positions": {asset: float(qty) for asset, qty in self._balance.positions.items()},
        }

    def import_runtime_state(self, state: dict[str, Any] | None) -> None:
        """Restaura o estado do saldo da corretora para retomada da execução."""
        if not isinstance(state, dict):
            return

        cash = float(state.get("cash", self._balance.cash))
        raw_positions = state.get("positions")
        positions: dict[str, float] = {}
        if isinstance(raw_positions, dict):
            for asset, qty in raw_positions.items():
                token = str(asset or "").strip()
                value = float(qty)
                if token and value > 0.0:
                    positions[token] = value

        self._balance = PaperBalance(cash=max(0.0, cash), positions=positions)

    def get_position_quantity(self, symbol: str) -> float:
        """Retorna a quantidade do ativo-base atualmente mantida para um ativo de negociação."""
        base_asset = symbol.split("/")[0]
        return float(self._balance.positions.get(base_asset, 0.0))

    def get_portfolio_value(self, prices: dict[str, float]) -> float:
        """
        Calcula o valor total da carteira usando os preços de mercado atuais.

        Args:
                prices: Dict que associa a moeda base ao preço atual (por exemplo,
                    ``{"BTC": 42000.0}``).

        Returns:
            Valor total na moeda de cotação.
        """
        position_value = sum(
            qty * prices.get(asset, 0.0)
            for asset, qty in self._balance.positions.items()
        )
        return self._balance.cash + position_value

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    def create_market_buy(self, symbol: str, quantity: float, price: float) -> PaperOrder:
        """
        Simula uma ordem de compra a mercado.

        Args:
            symbol: Par de negociação (por exemplo, ``BTC/USDT``).
            quantity: Quantidade na moeda base.
            price: Preço simulado de execução (normalmente o fechamento do candle).

        Returns:
            PaperOrder executada.
        """
        cost = quantity * price
        fee = cost * self._fee_pct
        total_cost = cost + fee

        if total_cost > self._balance.cash:
            raise ValueError(
                f"Insufficient cash: need {total_cost:.2f}, "
                f"have {self._balance.cash:.2f}."
            )

        self._balance.cash -= total_cost
        base_asset = symbol.split("/")[0]
        self._balance.positions[base_asset] = (
            self._balance.positions.get(base_asset, 0.0) + quantity
        )

        order = self._make_order(symbol, "buy", "market", quantity, price, fee)
        logger.info(
            "PaperBroker BUY - %s qty=%.6f @ %.4f fee=%.4f cash=%.2f",
            symbol,
            quantity,
            price,
            fee,
            self._balance.cash,
        )
        return order

    def create_market_sell(self, symbol: str, quantity: float, price: float) -> PaperOrder:
        """
        Simula uma ordem de venda a mercado.

        Args:
            symbol: Par de negociação.
            quantity: Quantidade na moeda base a vender.
            price: Preço simulado de execução.

        Returns:
            PaperOrder executada.
        """
        base_asset = symbol.split("/")[0]
        available = self._balance.positions.get(base_asset, 0.0)

        if quantity > available:
            raise ValueError(
                f"Insufficient {base_asset}: need {quantity:.6f}, "
                f"have {available:.6f}."
            )

        proceeds = quantity * price
        fee = proceeds * self._fee_pct
        net_proceeds = proceeds - fee

        self._balance.positions[base_asset] = available - quantity
        if self._balance.positions[base_asset] <= 0:
            del self._balance.positions[base_asset]

        self._balance.cash += net_proceeds

        order = self._make_order(symbol, "sell", "market", quantity, price, fee)
        logger.info(
            "PaperBroker SELL - %s qty=%.6f @ %.4f fee=%.4f cash=%.2f",
            symbol,
            quantity,
            price,
            fee,
            self._balance.cash,
        )
        return order

    # ------------------------------------------------------------------
    # Auxiliares privados
    # ------------------------------------------------------------------

    def _make_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: float,
        fee: float,
    ) -> PaperOrder:
        self._order_counter += 1
        order = PaperOrder(
            order_id=f"paper_{self._order_counter:06d}",
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            filled_quantity=quantity,
            status="filled",
            fee=fee,
        )
        self._orders[order.order_id] = order
        return order
