#!/usr/bin/env python3
"""
Estratégia de análise técnica para mercados Up/Down (5min e 15min).

Produz um score composto de 7 indicadores ponderados.
Score positivo = Up, negativo = Down.

Uso no bot:
- direction/confidence/score definem se e para que lado operar (safe, aggressive, only_hedge_plus).
- estimated_p_up e window_delta_pct são usados em only_hedge_plus para EV+ e margem dinâmica.
- Para máxima eficácia, analyze() deve receber candles 1m (bot passa 1m para btc/eth e btc15m).
"""

from dataclasses import dataclass
from typing import Optional

# Pesos dos indicadores (window delta domina; menos peso em sinais ruidosos de dia)
WEIGHT_WINDOW_DELTA = 7
WEIGHT_MICRO_MOMENTUM = 1.5   # Reduzido: últimos 2 candles muito voláteis de dia
WEIGHT_ACCELERATION = 1.2     # Reduzido: aceleração intrabar é ruidosa
WEIGHT_EMA_CROSS = 1.2        # Mantido: tendência de curto prazo
WEIGHT_RSI = 2
WEIGHT_VOLUME_SURGE = 1.2     # Só conta surtos mais fortes (limiar 2x)
WEIGHT_TICK_TREND = 1.2       # Reduzido: tick em tempo real muito ruidoso de dia

# Mínimo de candles 1m para que todos os indicadores (RSI, Volume Surge, Acceleration, Micro Momentum, EMA) sejam calculados
MIN_CANDLES_FOR_FULL_TA = 21


# Normalização: confiança e P(Up) usam score em [-MAX_SCORE, +MAX_SCORE]
# (window_delta sozinho pode contribuir ±7; demais indicadores somam no mesmo eixo)
MAX_SCORE = (
    WEIGHT_WINDOW_DELTA + WEIGHT_MICRO_MOMENTUM + WEIGHT_ACCELERATION
    + WEIGHT_EMA_CROSS + WEIGHT_RSI + WEIGHT_VOLUME_SURGE + WEIGHT_TICK_TREND
)


@dataclass
class AnalysisResult:
    """Resultado da análise com score, confiança e P(Up) estimada para EV+."""

    score: float
    confidence: float
    direction: str  # "up" ou "down"
    window_delta_pct: float
    details: dict
    estimated_p_up: float = 0.5  # P(Up) estimada para checagem EV+


def _ema(values: list[float], period: int) -> float:
    """Calcula EMA dos últimos `period` valores."""
    if not values or len(values) < period:
        return values[-1] if values else 0.0
    k = 2 / (period + 1)
    ema_val = sum(values[:period]) / period
    for v in values[period:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val


def _rsi(prices: list[float], period: int = 14) -> float:
    """RSI 14 períodos."""
    if len(prices) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    if len(gains) < period:
        return 50.0
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _window_delta_weight(delta_pct: float) -> float:
    """Peso do window delta baseado na magnitude (mais assertivo: ignora micro-ruído)."""
    abs_d = abs(delta_pct)
    if abs_d >= 0.10:
        return 7
    if abs_d >= 0.02:
        return 5
    if abs_d >= 0.005:
        return 3
    if abs_d >= 0.002:   # antes 0.001: evita peso 1 em oscilações mínimas de dia
        return 1
    return 0


def _rsi_weight(rsi: float) -> float:
    """Peso RSI: só extremos fortes (menos falsos de dia)."""
    if rsi >= 80:
        return -2   # Overbought forte = bearish
    if rsi <= 20:
        return 2    # Oversold forte = bullish
    return 0


def analyze(
    window_open_price: float,
    current_price: float,
    candles_1m: list[dict],
    tick_prices: Optional[list[float]] = None,
) -> AnalysisResult:
    """
    Analisa e retorna score composto usado pelo bot para direção e confiança.

    Utiliza sempre estes 7 sinais técnicos (quando há dados suficientes):
    1. Window Delta (preço vs abertura da janela)
    2. Micro Momentum (últimos 2 candles)
    3. Acceleration (aceleração do preço)
    4. EMA 9/21 (cruzamento)
    5. RSI 14
    6. Volume Surge (surtos >= 2x volume anterior)
    7. Tick Trend (tendência em tempo real, se tick_prices disponível)

    Para todos os indicadores 2–6 rodarem, candles_1m deve ter pelo menos
    MIN_CANDLES_FOR_FULL_TA candles com campo "v" (volume). O bot passa 30 candles 1m.

    Args:
        window_open_price: Preço de abertura da janela (5min ou 15min)
        current_price: Preço atual do ativo (BTC ou ETH)
        candles_1m: Candles [{"o","h","l","c","v"}]. Preferir 1m para todos os mercados.
        tick_prices: Preços de tick em tempo real (polling) para tendência intrabar.
    """
    details = {}
    score = 0.0

    # 1. Window Delta (dominante)
    delta_pct = (current_price - window_open_price) / window_open_price * 100
    wd_weight = _window_delta_weight(delta_pct)
    score += (1 if delta_pct >= 0 else -1) * wd_weight
    details["window_delta_pct"] = delta_pct
    details["window_delta_weight"] = wd_weight

    if not candles_1m or len(candles_1m) < 3:
        confidence = min(abs(score) / MAX_SCORE, 1.0)
        norm = max(-MAX_SCORE, min(MAX_SCORE, score))
        p_up = 0.5 + 0.4 * (norm / MAX_SCORE)
        p_up = max(0.1, min(0.9, p_up))
        return AnalysisResult(
            score=score,
            confidence=confidence,
            direction="up" if score >= 0 else "down",
            window_delta_pct=delta_pct,
            details=details,
            estimated_p_up=p_up,
        )

    prices = [c["c"] for c in candles_1m]
    volumes = [c.get("v", 0) for c in candles_1m]

    # 2. Micro Momentum (últimos 2 candles)
    if len(prices) >= 3:
        last_move = prices[-1] - prices[-3]
        mm = 1 if last_move > 0 else (-1 if last_move < 0 else 0)
        score += mm * WEIGHT_MICRO_MOMENTUM
        details["micro_momentum"] = mm

    # 3. Acceleration
    if len(prices) >= 4:
        recent = prices[-1] - prices[-2]
        prior = prices[-2] - prices[-3]
        acc = 1 if recent > prior else (-1 if recent < prior else 0)
        score += acc * WEIGHT_ACCELERATION
        details["acceleration"] = acc

    # 4. EMA 9/21
    if len(prices) >= 21:
        ema9 = _ema(prices, 9)
        ema21 = _ema(prices, 21)
        cross = 1 if ema9 > ema21 else (-1 if ema9 < ema21 else 0)
        score += cross * WEIGHT_EMA_CROSS
        details["ema_cross"] = cross

    # 5. RSI
    if len(prices) >= 15:
        rsi = _rsi(prices, 14)
        rsi_w = _rsi_weight(rsi)
        score += rsi_w
        details["rsi"] = rsi
        details["rsi_weight"] = rsi_w

    # 6. Volume Surge (mais assertivo: só surtos claros, 2x volume) — sempre avaliado com 6+ candles
    if len(volumes) >= 6:
        recent_avg = sum(volumes[-3:]) / 3
        prior_avg = sum(volumes[-6:-3]) / 3
        surge_dir = 0
        if prior_avg > 0 and recent_avg >= 2.0 * prior_avg:
            surge_dir = 1 if prices[-1] > prices[-2] else -1
            score += surge_dir * WEIGHT_VOLUME_SURGE
        details["volume_surge"] = surge_dir  # 0 = sem surto; ±1 = surto na direção

    # 7. Real-Time Tick Trend (mais assertivo: movimento e consistência maiores) — avaliado quando há 5+ ticks
    if tick_prices and len(tick_prices) >= 5:
        first = tick_prices[0]
        last = tick_prices[-1]
        move_pct = (last - first) / first * 100
        ups = sum(1 for i in range(1, len(tick_prices)) if tick_prices[i] > tick_prices[i - 1])
        consistency = ups / (len(tick_prices) - 1) if len(tick_prices) > 1 else 0.5
        tick_dir = 0
        if abs(move_pct) >= 0.008 and (consistency >= 0.65 or consistency <= 0.35):
            tick_dir = 1 if consistency >= 0.65 else -1
            score += tick_dir * WEIGHT_TICK_TREND
        details["tick_trend"] = tick_dir  # 0 = sem sinal; ±1 = tendência

    confidence = min(abs(score) / MAX_SCORE, 1.0)
    norm = max(-MAX_SCORE, min(MAX_SCORE, score))
    p_up = 0.5 + 0.4 * (norm / MAX_SCORE)
    p_up = max(0.1, min(0.9, p_up))
    return AnalysisResult(
        score=score,
        confidence=confidence,
        direction="up" if score >= 0 else "down",
        window_delta_pct=delta_pct,
        details=details,
        estimated_p_up=p_up,
    )
