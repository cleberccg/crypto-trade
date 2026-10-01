"""
Gerenciador de risco — validação de operações e cálculo dos níveis de stop/TP.

Decisão de projeto: o RiskManager é a única barreira pela qual toda operação
potencial deve passar. Ele aplica limites no nível da carteira e adiciona a cada
operação os níveis de stop-loss, take-profit e trailing stop.
"""
from __future__ import annotations

from dataclasses import dataclass

from config.settings import settings
from risk.position_sizer import PositionSizer
from utils.logger import get_logger
from utils.validators import validate_positive_float

logger = get_logger(__name__)


@dataclass
class TradeRiskParams:
    """
    Parâmetros de risco calculados para uma única operação potencial.

    Atributos:
        quantity: Tamanho da posição na moeda-base.
        stake_amount: Capital comprometido na moeda de cotação.
        stop_loss: Preço absoluto do stop-loss.
        take_profit: Preço absoluto do take-profit.
        trailing_stop_pct: Trailing stop como fração do preço (opcional).
        risk_amount: Perda monetária máxima caso o stop seja acionado.
        risk_pct: Perda máxima como fração da carteira.
        reward_amount: Lucro potencial caso o take-profit seja atingido.
        risk_reward_ratio: Relação retorno / risco.
    """

    quantity: float
    stake_amount: float
    stop_loss: float
    take_profit: float
    trailing_stop_pct: float | None
    risk_amount: float
    risk_pct: float
    reward_amount: float
    risk_reward_ratio: float
    quantity_suggested: float
    quantity_after_cap: float
    max_stake: float
    was_capped: bool


class RiskManager:
    """
    Valida operações e calcula parâmetros de risco.

    Todas as decisões são registradas para que cada verificação de risco seja auditável.
    """

    def __init__(self, sizer: PositionSizer | None = None) -> None:
        self._sizer = sizer or PositionSizer()

    @staticmethod
    def resolve_min_risk_reward_ratio(strategy: object | None) -> float | None:
        """Retorna o RR declarado pela estratégia, se disponível; caso contrário, None (usa o global)."""
        if strategy is None:
            return None
        raw_value = getattr(strategy, "_risk_reward_ratio", None)
        if raw_value is None:
            return None
        try:
            rr_value = float(raw_value)
        except (TypeError, ValueError):
            return None
        return rr_value if rr_value > 0.0 else None

    @staticmethod
    def infer_min_risk_reward_ratio_from_levels(
        entry_price: float | None,
        stop_loss: float | None,
        take_profit: float | None,
    ) -> float | None:
        """Infere o RR pelos níveis do sinal quando o RR da estratégia não está disponível."""
        if entry_price is None or stop_loss is None or take_profit is None:
            return None
        try:
            entry = float(entry_price)
            stop = float(stop_loss)
            take = float(take_profit)
        except (TypeError, ValueError):
            return None

        risk = entry - stop
        reward = take - entry
        if risk <= 0.0 or reward <= 0.0:
            return None

        rr_value = reward / risk
        return rr_value if rr_value > 0.0 else None

    def evaluate_trade(
        self,
        portfolio_value: float,
        entry_price: float,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        trailing_stop_pct: float | None = None,
        strategy_score: float = 1.0,
        min_risk_reward_ratio: float | None = None,
    ) -> TradeRiskParams:
        """
        Calcula e valida os parâmetros de risco de uma possível operação comprada.

        Os valores padrão de stop-loss / take-profit são carregados das configurações
        da aplicação quando não fornecidos pela estratégia. O tamanho da posição
        sempre é baseado no risco quando há um stop disponível; caso contrário,
        usa fração fixa.

        Argumentos:
            portfolio_value: Valor total atual da carteira (moeda de cotação).
            entry_price: Preço planejado de entrada.
            stop_loss: Preço absoluto opcional do stop-loss.
            take_profit: Preço absoluto opcional do take-profit.
            trailing_stop_pct: Fração opcional do trailing stop.
            strategy_score: Pontuação de confiança [0, 1] da estratégia.

        Retorna:
            TradeRiskParams com todos os valores calculados.

        Exceções:
            ValueError: Se a operação não passar em uma validação de risco.
        """
        portfolio_value = validate_positive_float(portfolio_value, "portfolio_value")
        entry_price = validate_positive_float(entry_price, "entry_price")

        cfg = settings.risk
        rr_minimum = float(cfg.risk_reward_ratio)
        if min_risk_reward_ratio is not None:
            try:
                rr_override = float(min_risk_reward_ratio)
            except (TypeError, ValueError):
                rr_override = 0.0
            if rr_override > 0.0:
                rr_minimum = rr_override

        logger.info(
            "RiskManager input - portfolio=%.2f entry=%.6f stop=%s take=%s "
            "risk_pct=%.4f score=%.4f",
            portfolio_value,
            entry_price,
            f"{stop_loss:.6f}" if stop_loss is not None else "None",
            f"{take_profit:.6f}" if take_profit is not None else "None",
            cfg.max_risk_per_trade_pct,
            strategy_score,
        )

        # --- Compute stop-loss ---
        if stop_loss is None:
            stop_loss = entry_price * (1.0 - cfg.default_stop_loss_pct)
        if stop_loss >= entry_price:
            raise ValueError(
                f"stop_loss ({stop_loss:.4f}) must be below entry_price ({entry_price:.4f})."
            )

        # --- Compute take-profit ---
        if take_profit is None:
            take_profit = entry_price * (1.0 + cfg.default_take_profit_pct)
        if take_profit <= entry_price:
            raise ValueError(
                f"take_profit ({take_profit:.4f}) must be above entry_price ({entry_price:.4f})."
            )

        # --- Trailing stop ---
        if trailing_stop_pct is None:
            trailing_stop_pct = cfg.default_trailing_stop_pct

        stop_distance = entry_price - stop_loss
        max_loss_value = portfolio_value * cfg.max_risk_per_trade_pct
        logger.info(
            "RiskManager sizing base - capital=%.2f max_risk_value=%.2f "
            "stop_distance=%.6f trailing_stop_pct=%.4f",
            portfolio_value,
            max_loss_value,
            stop_distance,
            trailing_stop_pct,
        )

        # --- Tamanho da posicao (baseado em risco) ---
        quantity = self._sizer.risk_based(
            portfolio_value=portfolio_value,
            risk_pct=cfg.max_risk_per_trade_pct,
            entry_price=entry_price,
            stop_loss_price=stop_loss,
        )

        logger.info(
            "RiskManager quantity from risk model - qty=%.8f",
            quantity,
        )

        # Ajusta o tamanho pela confiança da estratégia (mas mantém pelo menos o tamanho mínimo)
        quantity = quantity * max(strategy_score, 0.5)
        quantity_suggested = quantity
        stake_amount = quantity * entry_price

        logger.info(
            "RiskManager quantity after score factor - qty=%.8f stake=%.2f",
            quantity_suggested,
            stake_amount,
        )

        # --- Proteção: NUNCA envie mais que stake_amount_pct do portfólio ---
        max_stake = portfolio_value * settings.trading.stake_amount_pct
        was_capped = False
        if stake_amount > max_stake:
            logger.warning(
                "Stake amount %.2f exceeds max stake %.2f - capping quantity.",
                stake_amount,
                max_stake,
            )
            quantity = max_stake / entry_price
            stake_amount = max_stake
            was_capped = True

        logger.info(
            "RiskManager stake limit validation - max_stake=%.2f qty_after_cap=%.8f "
            "stake_after_cap=%.2f capped=%s",
            max_stake,
            quantity,
            stake_amount,
            was_capped,
        )

        # --- Risk/reward calculation ---
        risk_amount = (entry_price - stop_loss) * quantity
        reward_amount = (take_profit - entry_price) * quantity
        risk_pct = risk_amount / portfolio_value
        rr_ratio = reward_amount / risk_amount if risk_amount > 0 else 0.0

        # --- Valida risco/retorno minimo ---
        if rr_ratio + 1e-9 < rr_minimum:
            raise ValueError(
                f"Risk/reward ratio {rr_ratio:.2f} is below the configured "
                f"minimum of {rr_minimum:.2f}. Adjust stop/TP."
            )

        params = TradeRiskParams(
            quantity=quantity,
            stake_amount=stake_amount,
            stop_loss=stop_loss,
            take_profit=take_profit,
            trailing_stop_pct=trailing_stop_pct,
            risk_amount=risk_amount,
            risk_pct=risk_pct,
            reward_amount=reward_amount,
            risk_reward_ratio=rr_ratio,
            quantity_suggested=quantity_suggested,
            quantity_after_cap=quantity,
            max_stake=max_stake,
            was_capped=was_capped,
        )

        logger.info(
            "RiskManager.evaluate_trade - entry=%.4f sl=%.4f tp=%.4f "
            "qty=%.6f stake=%.2f rr=%.2f risk_pct=%.4f",
            entry_price,
            stop_loss,
            take_profit,
            quantity,
            stake_amount,
            rr_ratio,
            risk_pct,
        )
        return params

    def check_trailing_stop(
        self,
        entry_price: float,
        current_price: float,
        highest_price: float,
        trailing_stop_pct: float,
    ) -> bool:
        """
        Retorna True se o trailing stop tiver sido acionado.

        O nível do trailing stop é: highest_price * (1 - trailing_stop_pct).

        Argumentos:
            entry_price: Preço original de entrada (usado apenas no registro).
            current_price: Preço de mercado mais recente.
            highest_price: Preço mais alto atingido desde a entrada.
            trailing_stop_pct: Trailing stop como fração (por exemplo, 0.015).

        Retorna:
            True se current_price <= nível do trailing stop.
        """
        trailing_level = highest_price * (1.0 - trailing_stop_pct)
        triggered = current_price <= trailing_level

        if triggered:
            logger.info(
                "Trailing stop triggered - entry=%.4f high=%.4f "
                "level=%.4f current=%.4f",
                entry_price,
                highest_price,
                trailing_level,
                current_price,
            )
        return triggered
