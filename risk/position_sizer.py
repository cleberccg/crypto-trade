"""
Cálculos do dimensionamento de posições.

Decisão de projeto: separar o dimensionamento de posições do gerenciador de risco
mantém cada classe focada em uma única responsabilidade. Todos os métodos de
dimensionamento são funções puras, sem efeitos colaterais.
"""
from __future__ import annotations

from utils.logger import get_logger
from utils.validators import validate_positive_float, validate_percentage

logger = get_logger(__name__)


class PositionSizer:
    """
     Calcula o tamanho da posição de uma operação.

     Oferece suporte a dois métodos de dimensionamento:
     1. **Fração fixa**: aloca uma porcentagem fixa da carteira total.
     2. **Baseado em risco**: determina o tamanho para que o acionamento do stop-loss
         corresponda a uma porcentagem fixa da carteira total (abordagem Kelly/risco fixo).
    """

    def fixed_fractional(
        self,
        portfolio_value: float,
        stake_pct: float,
        price: float,
    ) -> float:
        """
        Dimensiona uma posição como fração fixa da carteira.

        Argumentos:
            portfolio_value: Valor total da carteira na moeda de cotação.
            stake_pct: Fração a alocar por operação (por exemplo, 0.02 para 2%).
            price: Preço atual do ativo.

        Retorna:
            Quantidade a comprar na moeda-base.
        """
        portfolio_value = validate_positive_float(portfolio_value, "portfolio_value")
        stake_pct = validate_percentage(stake_pct, "stake_pct")
        price = validate_positive_float(price, "price")

        stake_amount = portfolio_value * stake_pct
        quantity = stake_amount / price

        logger.debug(
            "fixed_fractional - portfolio=%.2f stake_pct=%.4f price=%.4f "
            "stake=%.2f qty=%.6f",
            portfolio_value,
            stake_pct,
            price,
            stake_amount,
            quantity,
        )
        return quantity

    def risk_based(
        self,
        portfolio_value: float,
        risk_pct: float,
        entry_price: float,
        stop_loss_price: float,
    ) -> float:
        """
        Dimensiona uma posição para que o acionamento do stop-loss cause uma perda
        exatamente igual a *risk_pct* da carteira.

        Fórmula: qty = (portfolio * risk_pct) / (entry - stop_loss)

        Argumentos:
            portfolio_value: Valor total da carteira na moeda de cotação.
            risk_pct: Fração máxima de perda por operação (por exemplo, 0.01 para 1%).
            entry_price: Preço planejado de entrada.
            stop_loss_price: Preço planejado do stop-loss.

        Retorna:
            Quantidade a comprar na moeda-base.

        Exceções:
            ValueError: Se stop_loss_price >= entry_price (não há margem para perda).
        """
        portfolio_value = validate_positive_float(portfolio_value, "portfolio_value")
        risk_pct = validate_percentage(risk_pct, "risk_pct")
        entry_price = validate_positive_float(entry_price, "entry_price")
        stop_loss_price = validate_positive_float(stop_loss_price, "stop_loss_price")

        risk_per_unit = entry_price - stop_loss_price
        if risk_per_unit <= 0:
            raise ValueError(
                f"stop_loss_price ({stop_loss_price}) must be less than "
                f"entry_price ({entry_price})."
            )

        max_loss = portfolio_value * risk_pct
        quantity = max_loss / risk_per_unit

        logger.info(
            "PositionSizer.risk_based details - portfolio=%.2f risk_pct=%.4f "
            "entry=%.6f stop=%.6f risk_per_unit=%.6f max_loss=%.2f qty=%.8f",
            portfolio_value,
            risk_pct,
            entry_price,
            stop_loss_price,
            risk_per_unit,
            max_loss,
            quantity,
        )

        logger.debug(
            "risk_based - portfolio=%.2f risk_pct=%.4f entry=%.4f sl=%.4f "
            "risk_per_unit=%.4f max_loss=%.2f qty=%.6f",
            portfolio_value,
            risk_pct,
            entry_price,
            stop_loss_price,
            risk_per_unit,
            max_loss,
            quantity,
        )
        return quantity
