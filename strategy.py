"""Strategy 3610: Aggressive late-game + cs4 inv 80 + throttle 0.30.

Based on strategy_3211. Varies sizing:
- Early 0.9x, Mid 1.0x, Late 1.15x, PnL boost +10%
"""
from __future__ import annotations
import math
from orderbook_pm_challenge.strategy import BaseStrategy
from orderbook_pm_challenge.types import CancelAll, PlaceOrder, Side, StepState


_SQRT2PI = math.sqrt(2.0 * math.pi)


def _inv_ncdf(p: float) -> float:
    if p <= 1e-10:
        return -6.3
    if p >= 1.0 - 1e-10:
        return 6.3
    t = math.sqrt(-2.0 * math.log(min(p, 1.0 - p)))
    r = t - (2.515517 + 0.802853 * t + 0.010328 * t * t) / (
        1.0 + 1.432788 * t + 0.189269 * t * t + 0.001308 * t * t * t
    )
    return r if p > 0.5 else -r


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT2PI


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _tick_vol(p: float, H: int, sigma: float = 0.02) -> float:
    if H <= 0:
        return 0.0
    p = max(0.02, min(0.98, p))
    tv = sigma * math.sqrt(H)
    return _npdf(_inv_ncdf(p)) / tv * sigma * 100.0


def _bid_size_mult(mid_est: float) -> float:
    p = max(0.03, min(0.97, mid_est / 100.0))
    scale = 0.50 / max(p, 0.05)
    if p > 0.75:
        return 1.0
    return max(1.0, min(15.0, scale))


def _ask_size_mult(mid_est: float) -> float:
    p = max(0.03, min(0.97, mid_est / 100.0))
    scale = 0.50 / p
    if p > 0.75:
        return 1.0
    return max(1.0, min(15.0, scale))


def _retail_sell_qty(tick: int, notional: float) -> float:
    return notional / max(0.05, tick / 100.0)


def _retail_buy_qty(tick: int, notional: float) -> float:
    return notional / max(0.01, tick / 100.0)


def _arb_hit_prob(order_ticks: float, mid_est: float, sigma_ticks: float, side: str) -> float:
    if sigma_ticks <= 0.01:
        return 0.0
    if side == 'bid':
        z = (order_ticks - mid_est) / sigma_ticks
        return _ncdf(z)
    else:
        z = (mid_est - order_ticks) / sigma_ticks
        return _ncdf(z)


# Per-CS parameters: (k, spread_mult, fill_correction, inv_limit, max_size)
CS_PARAMS = {
    2: (3.43, 1.5, 1.0, 60, 50),
    3: (8.0, 1.0, 0.5, 60, 80),
    4: (16.0, 1.0, 0.0, 80, 150),
}

ARB_LOSS_AMPLIFIER = 2.8
ARB_SIZE_FLOOR = 0.25
ARB_SIGMA_MULT = 1.6
ARB_PROB_THRESHOLD = 0.15

# p<5% floor exploitation parameters (from strategy_3020)
FLOOR_PRICE = 0.0424
FLOOR_ASK_BOOST = 3.0
FLOOR_BID_DAMPEN = 0.80
FLOOR_CASH_ASK_FRAC = 0.6
FLOOR_INV_LIMIT_MULT = 1.0
FLOOR_CAP_CS4 = 600

# Time-varying aggressiveness parameters
STARTING_CASH = 1000.0
TIME_EARLY_FRAC = 0.20   # first 20% of steps
TIME_LATE_FRAC = 0.80    # last 20% of steps (i.e., progress > 0.80)
TIME_EARLY_MULT = 0.90   # slightly cautious in learning phase
TIME_MID_MULT = 1.00     # normal in middle
TIME_LATE_MULT = 1.15    # slightly aggressive near settlement
PNL_BOOST = 0.10         # +10% when winning


class Strategy(BaseStrategy):
    def __init__(self):
        self.mid_est: float | None = None
        self.prev_cb: int | None = None
        self.prev_ca: int | None = None
        self.vol_ema: float = 0.7
        self.vol_mult: float = 1.0
        self.comp_spread_est: float = 2.0
        self.step_count: int = 0
        self.jump_freq_ema: float = 0.002
        self.steps_since_jump: int = 100
        self.last_jump_size: float = 0.0
        self.last_jump_direction: int = 0
        self.last_bid_qty: float = 0.0
        self.last_ask_qty: float = 0.0
        self.retail_fill_ema: float = 0.20
        self.quiet_steps: int = 0
        self.inv_limit: int = 60
        self.jump_cooldown: int = 5
        # Information-theoretic mid tracking (from 3050)
        self.info_mid: float | None = None
        self.last_own_bid_tick: int | None = None
        self.last_own_ask_tick: int | None = None
        self.recent_move_ema: float = 0.0
        self.total_steps: int | None = None  # computed on first step

    def _time_size_mult(self, state: StepState) -> float:
        """Compute time-varying size multiplier based on simulation progress and PnL."""
        if self.total_steps is None:
            self.total_steps = state.step + state.steps_remaining
        total = max(1, self.total_steps)
        progress = state.step / total

        # Time-based multiplier
        if progress < TIME_EARLY_FRAC:
            mult = TIME_EARLY_MULT
        elif progress > TIME_LATE_FRAC:
            mult = TIME_LATE_MULT
        else:
            mult = TIME_MID_MULT

        # PnL-based boost: if cash exceeds starting cash, we're ahead
        if state.cash > STARTING_CASH:
            mult *= (1.0 + PNL_BOOST)

        return mult

    def _is_arb_fill(self, filled: float, posted: float) -> bool:
        return filled > 0 and posted > 0.01 and filled >= posted * 0.95

    def _should_suppress(self, side: str) -> bool:
        if self.steps_since_jump >= self.jump_cooldown:
            return False
        if self.last_jump_direction > 0 and side == "ask":
            return True
        if self.last_jump_direction < 0 and side == "bid":
            return True
        return False

    def _get_params(self, cs: int):
        k, sm, fc, il, ms = CS_PARAMS.get(cs, CS_PARAMS[4])
        jf = max(0.0, min(1.0, self.jump_freq_ema / 0.08))
        if cs <= 2:
            sm += jf * 8.0
            k -= jf * 5.0
        else:
            sm += jf * 2.0
            k -= jf * 1.0
        rf = max(0.0, min(1.0, (self.retail_fill_ema - 0.05) / 0.40))
        fc -= rf * 0.2
        k = max(1.0, min(18.0, k))
        sm = max(0.6, min(5.0, sm))
        fc = max(0.4, min(2.0, fc))
        return k, sm, fc, il, ms

    # --- Floor regime helpers (from strategy_3020) ---

    def _in_floor_regime(self, p_est: float, cs: int = 3) -> bool:
        """Check if we're in the p<5% sell-quantity-floor regime.
        Only activate for cs>=3 where there's room to exploit."""
        return p_est < FLOOR_PRICE and cs >= 3

    def _floor_ask_multiplier(self, p_est: float, cs: int = 4) -> float:
        """Extra multiplier for ask size when p < floor.
        Ask captures retail BUYS = notional/price, which grows without bound below 5%.
        """
        if p_est >= FLOOR_PRICE:
            return 1.0
        ratio = FLOOR_PRICE / max(0.01, p_est)
        if cs >= 4:
            return min(FLOOR_ASK_BOOST, max(1.0, ratio))
        else:
            return min(1.8, max(1.0, ratio * 0.6))

    def _floor_bid_multiplier(self, p_est: float, cs: int = 4) -> float:
        """Dampen bid when p < floor since sell quantity is capped."""
        if p_est >= FLOOR_PRICE:
            return 1.0
        if cs >= 4:
            return FLOOR_BID_DAMPEN
        else:
            return 0.90

    # --- Info-theoretic mid estimation (from strategy_3050) ---

    def _estimate_mid_info_theoretic(self, state: StepState, cb, ca) -> float:
        """Information-theoretic mid estimation.

        Uses delta_ask/delta_bid signals to front-load lagged replenishment
        and info-weighted anchoring for better mid tracking.
        """
        both = cb is not None and ca is not None
        only_ask = cb is None and ca is not None
        only_bid = cb is not None and ca is None

        if both:
            comp_mid = (cb + ca) / 2.0
        elif only_ask:
            comp_mid = max(2.0, ca - self.comp_spread_est * 1.5)
        elif only_bid:
            comp_mid = min(98.0, cb + self.comp_spread_est * 1.5)
        else:
            comp_mid = self.info_mid if self.info_mid is not None else 50.0

        if self.info_mid is None:
            return comp_mid

        if self.prev_cb is None and self.prev_ca is None:
            return comp_mid

        # ---- SIGNAL EXTRACTION ----
        prev_comp_mid = self._prev_comp_mid()
        comp_mid_change = comp_mid - prev_comp_mid

        delta_ask = 0.0
        has_ask_signal = False
        if ca is not None and self.prev_ca is not None:
            delta_ask = float(ca - self.prev_ca)
            has_ask_signal = True
        elif ca is None and self.prev_ca is not None:
            delta_ask = float(99 - self.prev_ca)
            has_ask_signal = True
        elif ca is not None and self.prev_ca is None:
            delta_ask = -float(99 - ca)
            has_ask_signal = True

        delta_bid = 0.0
        has_bid_signal = False
        if cb is not None and self.prev_cb is not None:
            delta_bid = float(cb - self.prev_cb)
            has_bid_signal = True
        elif cb is None and self.prev_cb is not None:
            delta_bid = -float(self.prev_cb - 1)
            has_bid_signal = True
        elif cb is not None and self.prev_cb is None:
            delta_bid = float(cb - 1)
            has_bid_signal = True

        buy_arb = self._is_arb_fill(state.buy_filled_quantity, self.last_bid_qty)
        sell_arb = self._is_arb_fill(state.sell_filled_quantity, self.last_ask_qty)

        # ---- MID UPDATE ----
        new_mid = self.info_mid

        # Primary signal: comp mid change
        new_mid += comp_mid_change

        # Front-load correction for lagged replenishment
        if has_ask_signal and has_bid_signal:
            ask_driven = delta_ask - delta_bid
            if abs(ask_driven) > 1.5:
                lag_correction = ask_driven * 0.15 * 0.5
                lag_correction = max(-2.0, min(2.0, lag_correction))
                new_mid += lag_correction

        # Info-enhanced arb fill correction
        cs_est = max(1, min(4, round(self.comp_spread_est)))
        cs = max(2, min(4, cs_est))
        _, _, fc, _, _ = self._get_params(cs)

        rjb = 2.0 if self.steps_since_jump < 3 else 1.0
        fm_base = (0.05 + 0.5 * self.vol_mult) * fc * rjb

        comp_move_mag = abs(delta_ask) + abs(delta_bid)
        info_boost = 1.0 + min(2.0, comp_move_mag / 3.0) * 0.6

        fm = fm_base * info_boost

        if buy_arb:
            fill_correction = -min(5.0, state.buy_filled_quantity * fm)
            new_mid += fill_correction

        if sell_arb:
            fill_correction = min(5.0, state.sell_filled_quantity * fm)
            new_mid += fill_correction

        # Info-weighted anchoring
        total_change = abs(delta_ask) + abs(delta_bid)
        has_arb = buy_arb or sell_arb
        if total_change < 0.5 and not has_arb:
            anchor = 0.08
        else:
            anchor = 0.03
        new_mid = (1.0 - anchor) * new_mid + anchor * comp_mid

        return new_mid

    def _prev_comp_mid(self) -> float:
        if self.prev_cb is not None and self.prev_ca is not None:
            return (self.prev_cb + self.prev_ca) / 2.0
        elif self.prev_ca is not None:
            return max(2.0, self.prev_ca - self.comp_spread_est * 1.5)
        elif self.prev_cb is not None:
            return min(98.0, self.prev_cb + self.comp_spread_est * 1.5)
        else:
            return self.info_mid if self.info_mid is not None else 50.0

    def on_step(self, state: StepState):
        cb = state.competitor_best_bid_ticks
        ca = state.competitor_best_ask_ticks
        H = state.steps_remaining
        self.step_count += 1

        both = cb is not None and ca is not None
        only_ask = cb is None and ca is not None
        only_bid = cb is not None and ca is None
        neither = cb is None and ca is None

        # --- 1. INFORMATION-THEORETIC MID ESTIMATION (from 3050) ---
        new_mid = self._estimate_mid_info_theoretic(state, cb, ca)

        if self.info_mid is None:
            self.info_mid = new_mid
            self.mid_est = new_mid
            if both:
                self.comp_spread_est = max(1.0, min(4.0, (ca - cb) / 2.0))
                if ca - cb > 6:
                    self.vol_ema = 1.0
                elif ca - cb < 3:
                    self.vol_ema = 0.5
            else:
                self.comp_spread_est = 4.0
                self.vol_ema = 0.3
        else:
            self.info_mid = new_mid
            self.mid_est = new_mid

        # --- 2. VOLATILITY AND JUMP TRACKING (from 3050) ---
        if self.prev_cb is not None or self.prev_ca is not None:
            prev_comp_mid = self._prev_comp_mid()
            if both:
                comp_mid = (cb + ca) / 2.0
            elif only_ask:
                comp_mid = max(2.0, ca - self.comp_spread_est * 1.5)
            elif only_bid:
                comp_mid = min(98.0, cb + self.comp_spread_est * 1.5)
            else:
                comp_mid = self.mid_est
            mc = comp_mid - prev_comp_mid
            self.vol_ema = 0.97 * self.vol_ema + 0.03 * abs(mc)
            self.vol_mult = max(0.5, min(2.5, self.vol_ema / 0.7))
            self.recent_move_ema = 0.85 * self.recent_move_ema + 0.15 * abs(mc)
            alpha = 0.03 if self.step_count < 50 else 0.01
            is_jump = 1.0 if abs(mc) > 1.5 else 0.0
            self.jump_freq_ema = (1 - alpha) * self.jump_freq_ema + alpha * is_jump
            self.steps_since_jump += 1
            if abs(mc) > 1.5:
                self.steps_since_jump = 0
                self.last_jump_size = abs(mc)
                self.last_jump_direction = 1 if mc > 0 else -1
            if abs(mc) < 0.5:
                self.quiet_steps += 1
                had_fill = state.buy_filled_quantity > 0 or state.sell_filled_quantity > 0
                r_alpha = 0.06 if self.quiet_steps < 30 else 0.02
                self.retail_fill_ema = (1 - r_alpha) * self.retail_fill_ema + r_alpha * (1.0 if had_fill else 0.0)

        # --- 3. REGIME DETECTION ---
        if both:
            actual_gap = ca - cb
        else:
            actual_gap = max(3, int(self.comp_spread_est * 2.0))

        tv = _tick_vol(self.mid_est / 100.0, H)
        cs_est = max(1, min(4, round(self.comp_spread_est)))

        # cs=1 skip
        if cs_est <= 1 and not both:
            self.prev_cb, self.prev_ca = cb, ca
            self.last_bid_qty = self.last_ask_qty = 0.0
            self.last_own_bid_tick = self.last_own_ask_tick = None
            return [CancelAll()]

        # cs=2 vol gate
        if both and (actual_gap <= 2 or (actual_gap <= 4 and tv * self.vol_mult > 0.6)):
            self.prev_cb, self.prev_ca = cb, ca
            self.last_bid_qty = self.last_ask_qty = 0.0
            self.last_own_bid_tick = self.last_own_ask_tick = None
            return [CancelAll()]

        # --- 4. REGIME PARAMS ---
        cs = max(2, min(4, cs_est))
        if both:
            if actual_gap >= 8:
                cs = 4
            elif actual_gap >= 5:
                cs = max(cs, 3)

        k, sm, fc, il_base, ms = self._get_params(cs)

        # Save previous comp quotes BEFORE updating
        self.prev_cb, self.prev_ca = cb, ca

        # Gentle comp-bounded clamping
        if both:
            lo = cb + 0.5
            hi = ca - 0.5
            if self.mid_est < lo:
                self.mid_est += (lo - self.mid_est) * 0.5
            elif self.mid_est > hi:
                self.mid_est -= (self.mid_est - hi) * 0.5
        self.info_mid = self.mid_est

        # --- 5. INVENTORY MANAGEMENT ---
        p_est = max(0.02, min(0.98, self.mid_est / 100.0))
        extremity = min(p_est, 1.0 - p_est)
        in_floor = self._in_floor_regime(p_est, cs)

        # Enhanced inv_limit with floor regime support (from 3020)
        if cs >= 4 and extremity < 0.05:
            inv_limit = min(400, max(il_base, int(12.0 / max(extremity, 0.02))))
            if in_floor:
                inv_limit = min(600, int(inv_limit * FLOOR_INV_LIMIT_MULT))
        elif cs >= 4 and extremity < 0.12:
            inv_limit = min(200, max(il_base, int(9.24 / max(extremity, 0.04))))
        elif cs == 3 and extremity < 0.1:
            inv_limit = min(120, max(il_base, int(5.0 / max(extremity, 0.05))))
            if in_floor:
                inv_limit = min(180, int(inv_limit * FLOOR_INV_LIMIT_MULT))
        else:
            inv_limit = il_base
        self.inv_limit = inv_limit

        ni = state.yes_inventory - state.no_inventory
        tf = H / 2000.0
        iu = 1.0 + max(0, (0.2 - tf) * 10.0)
        ir = ni / self.inv_limit

        # Reduced skew at extreme prices
        if extremity < 0.10:
            skew = ir * 1.5598 * iu
        elif extremity < 0.20:
            skew = ir * abs(ir) * 6.0 * iu
        else:
            skew = ir * abs(ir) * 12.0 * iu

        mid = self.mid_est - skew
        ev = max(0.05, tv * self.vol_mult)

        # --- 6. QUOTE PLACEMENT ---

        # *** NONE-HANDLING: Both sides consumed ***
        if neither:
            bp, ap = 1, 99
            est_not = 5.0
            time_mult = self._time_size_mult(state)
            bid_q = _retail_sell_qty(bp, est_not) * 2.7 * time_mult
            ask_q = _retail_buy_qty(ap, est_not) * 2.7 * time_mult

            # Floor exploitation: boost ask, dampen bid (from 3020)
            if in_floor:
                ask_q *= self._floor_ask_multiplier(p_est, cs)
                bid_q *= self._floor_bid_multiplier(p_est, cs)

            bid_q = max(150, min(700 if in_floor else 500, bid_q))
            ask_q = max(150, min(700 if in_floor else 500, ask_q))

            avail = state.free_cash
            bpc = bp / 100.0
            acc = (100 - ap) / 100.0

            # Asymmetric cash allocation in floor regime (from 3020)
            if in_floor:
                bid_cash_frac = 1.0 - FLOOR_CASH_ASK_FRAC
                ask_cash_frac = FLOOR_CASH_ASK_FRAC
            else:
                bid_cash_frac = 0.5
                ask_cash_frac = 0.5

            if bpc > 0 and bid_q * bpc > avail * bid_cash_frac:
                bid_q = max(0.01, avail * bid_cash_frac / bpc)
            if acc > 0 and ask_q * acc > avail * ask_cash_frac:
                ask_q = max(0.01, avail * ask_cash_frac / acc)

            actions = [CancelAll()]
            if ni < self.inv_limit:
                actions.append(PlaceOrder(Side.BUY, bp, bid_q))
                self.last_bid_qty = bid_q
                self.last_own_bid_tick = bp
            else:
                self.last_bid_qty = 0.0
                self.last_own_bid_tick = None
            if ni > -self.inv_limit:
                actions.append(PlaceOrder(Side.SELL, ap, ask_q))
                self.last_ask_qty = ask_q
                self.last_own_ask_tick = ap
            else:
                self.last_ask_qty = 0.0
                self.last_own_ask_tick = None
            return actions

        # *** ONE-SIDE MISSING ***
        if only_ask:
            bp = 1
            ap = max(2, ca - 1)
            return self._place_one_missing(state, bp, ap, cs, ev, mid, p_est, "bid_missing")

        if only_bid:
            bp = min(98, cb + 1)
            ap = 99
            return self._place_one_missing(state, bp, ap, cs, ev, mid, p_est, "ask_missing")

        # *** BOTH SIDES PRESENT ***
        is_extreme_low = cs >= 3 and p_est < 0.20
        is_extreme_high = cs >= 3 and p_est > 0.80

        if is_extreme_low:
            bp, ap = self._extreme_low_quotes(cb, ca, mid, ev, sm, p_est)
        elif is_extreme_high:
            bp, ap = self._extreme_high_quotes(cb, ca, mid, ev, sm, p_est)
        else:
            spread = max(1, min(10, int(math.ceil(sm * ev))))
            # Info-theoretic spread adjustment (from 3050)
            if self.recent_move_ema > 1.0:
                info_widen = min(2, int(self.recent_move_ema / 1.5))
                spread = min(10, spread + info_widen)
            elif self.recent_move_ema < 0.3 and cs >= 3:
                spread = max(1, spread - 1)
            if self.steps_since_jump < 4:
                recency = 1.0 - self.steps_since_jump / 4
                if self.last_jump_size >= 5.0:
                    extra = max(1, int(3 * recency + 0.5))
                elif self.last_jump_size >= 2:
                    extra = max(1, int(2 * recency + 0.5))
                else:
                    extra = 1
                spread = min(spread + extra, 10)
            if cs <= 3 and self.mid_est < 20:
                spread = max(1, spread - 1)
            bp = max(1, min(cb + 1 if cb else int(mid - 1), int(mid - spread)))
            ap = min(99, max(ca - 1 if ca else int(mid + 1), int(mid + spread)))

        # Safety check
        if bp is not None and ap is not None and bp >= ap:
            bp = max(1, int(mid) - 1)
            ap = min(99, int(mid) + 1)
        if bp is not None and ap is not None and bp >= ap:
            self.last_bid_qty = self.last_ask_qty = 0.0
            self.last_own_bid_tick = self.last_own_ask_tick = None
            return [CancelAll()]

        # Jump direction suppression
        suppress_bid = self._should_suppress("bid")
        suppress_ask = self._should_suppress("ask")
        if suppress_bid:
            bp = None
        if suppress_ask:
            ap = None
        if bp is None and ap is None:
            self.last_bid_qty = self.last_ask_qty = 0.0
            self.last_own_bid_tick = self.last_own_ask_tick = None
            return [CancelAll()]

        # --- 7. SIZING ---
        base_size = min(ms, max(3.5, k / ev))

        # Time-varying aggressiveness
        time_mult = self._time_size_mult(state)
        base_size *= time_mult

        if self.steps_since_jump < 4:
            recency = 1.0 - self.steps_since_jump / 4
            reduction = max(0.25, 1.0 - self.last_jump_size / 12.0 * recency)
            base_size *= reduction

        if p_est >= 0.15:
            if ev > 0.7:
                base_size = min(base_size, 8.0)
            elif ev > 0.5:
                base_size = min(base_size, 25.0)

        me = self.mid_est
        if is_extreme_low or is_extreme_high:
            est_not = 5.0
            if is_extreme_low:
                if ap is not None:
                    expected_buy_qty = est_not / (ap / 100.0)
                    ask_base = max(base_size, expected_buy_qty * 2.7)
                    # Floor boost: buys are disproportionately large (from 3020)
                    if in_floor:
                        ask_base *= self._floor_ask_multiplier(p_est, cs)
                else:
                    ask_base = base_size
                if bp is not None:
                    expected_sell_qty = est_not / max(p_est, 0.05)
                    bid_base = max(base_size, expected_sell_qty * 2.7)
                    # Floor dampen: sells are capped (from 3020)
                    if in_floor:
                        bid_base *= self._floor_bid_multiplier(p_est, cs)
                else:
                    bid_base = base_size
            else:
                if bp is not None:
                    expected_sell_qty = est_not / max(p_est, 0.05)
                    bid_base = max(base_size, expected_sell_qty * 2.7)
                else:
                    bid_base = base_size
                if ap is not None:
                    expected_buy_qty = est_not / (ap / 100.0)
                    ask_base = max(base_size, expected_buy_qty * 2.7)
                else:
                    ask_base = base_size

            # Higher caps in floor regime (from 3020)
            if in_floor:
                cap = FLOOR_CAP_CS4 if cs >= 4 else 200
            else:
                cap = 456 if cs >= 4 else 150
            bid_base = min(cap, bid_base)
            ask_base = min(cap, ask_base)
        else:
            bm = _bid_size_mult(me)
            am = _ask_size_mult(me)
            if cs <= 3:
                bid_base = base_size * bm
                ask_base = base_size * am
            else:
                bid_base = base_size * max(1.0, min(2.5, 1.0 + (bm - 1.0) * 0.15))
                ask_base = base_size * max(1.0, min(2.5, 1.0 + (am - 1.0) * 0.15))
            cap = 456 if cs >= 4 else 150
            bid_base = min(cap, bid_base)
            ask_base = min(cap, ask_base)

        # One-sided boost
        if suppress_bid:
            ask_base *= 1.5
        if suppress_ask:
            bid_base *= 1.5

        # Inventory adjustment
        bid_qty = bid_base * max(0.15, 1.0 - ir * 0.8)
        ask_qty = ask_base * max(0.15, 1.0 + ir * 0.8)

        # High-probability throttle
        if cs >= 3 and p_est > 0.48:
            risk_scale = max(0.30, min(1.0, 1.0 - (p_est - 0.48) * 2.2))
            bid_qty *= risk_scale
            ask_qty *= risk_scale

        bid_qty = max(0.01, bid_qty)
        ask_qty = max(0.01, ask_qty)

        # Per-side arb probability sizing (cs>=4 only)
        sigma_ticks = tv * ARB_SIGMA_MULT
        if self.jump_freq_ema > 0.005:
            jump_contrib = self.jump_freq_ema * max(self.last_jump_size, 2.0) * 0.5
            sigma_ticks = math.sqrt(sigma_ticks**2 + jump_contrib**2)

        if cs >= 4 and sigma_ticks > 0.05:
            if bp is not None:
                bid_arb_prob = _arb_hit_prob(float(bp), self.mid_est, sigma_ticks, 'bid')
                if bid_arb_prob > ARB_PROB_THRESHOLD:
                    bid_arb_adj = max(ARB_SIZE_FLOOR, 1.0 - bid_arb_prob * ARB_LOSS_AMPLIFIER)
                    bid_qty *= bid_arb_adj
            if ap is not None:
                ask_arb_prob = _arb_hit_prob(float(ap), self.mid_est, sigma_ticks, 'ask')
                if ask_arb_prob > ARB_PROB_THRESHOLD:
                    ask_arb_adj = max(ARB_SIZE_FLOOR, 1.0 - ask_arb_prob * ARB_LOSS_AMPLIFIER)
                    ask_qty *= ask_arb_adj

        bid_qty = max(0.01, bid_qty)
        ask_qty = max(0.01, ask_qty)

        # --- 8. CASH CONSTRAINTS ---
        if self.mid_est < 15 or self.mid_est > 85:
            cash_frac = 0.88
        else:
            cash_frac = 0.45
        avail = state.free_cash * cash_frac

        # Asymmetric cash allocation in floor regime (from 3020)
        if in_floor and (is_extreme_low or neither):
            bid_avail = avail * (1.0 - FLOOR_CASH_ASK_FRAC)
            ask_avail = avail * FLOOR_CASH_ASK_FRAC
        else:
            bid_avail = avail
            ask_avail = avail

        if bp is not None:
            bpc = bp / 100.0
            if bpc > 0 and bid_qty * bpc > bid_avail:
                bid_qty = max(0.01, bid_avail / bpc)
        if ap is not None:
            acc = (100 - ap) / 100.0
            if acc > 0:
                if (is_extreme_low or is_extreme_high) and cs >= 3:
                    available_yes = max(0.0, state.yes_inventory)
                    uncovered = max(0.0, ask_qty - available_yes)
                    cash_needed = uncovered * acc
                    if cash_needed > ask_avail:
                        ask_qty = max(0.01, available_yes + ask_avail / acc)
                else:
                    if ask_qty * acc > ask_avail:
                        ask_qty = max(0.01, ask_avail / acc)

        # --- 9. PLACE ORDERS ---
        actions = [CancelAll()]
        if bp is not None and bid_qty > 0.01 and ni < self.inv_limit:
            actions.append(PlaceOrder(Side.BUY, bp, bid_qty))
            self.last_bid_qty = bid_qty
            self.last_own_bid_tick = bp
        else:
            self.last_bid_qty = 0.0
            self.last_own_bid_tick = None
        if ap is not None and ask_qty > 0.01 and ni > -self.inv_limit:
            actions.append(PlaceOrder(Side.SELL, ap, ask_qty))
            self.last_ask_qty = ask_qty
            self.last_own_ask_tick = ap
        else:
            self.last_ask_qty = 0.0
            self.last_own_ask_tick = None
        return actions

    def _place_one_missing(self, state, bp, ap, cs, ev, mid, p_est, mode):
        est_not = 5.0
        ni = state.yes_inventory - state.no_inventory
        in_floor = self._in_floor_regime(p_est, cs)
        time_mult = self._time_size_mult(state)

        if mode == "bid_missing":
            bid_q = _retail_sell_qty(bp, est_not) * 2.7 * time_mult
            # Floor dampen bid (from 3020)
            if in_floor:
                bid_q *= self._floor_bid_multiplier(p_est, cs)
            bid_q = max(150, min(500 if cs >= 4 else 150, bid_q))

            k, _, _, _, ms = self._get_params(cs)
            base = min(ms, max(3.5, k / ev)) * time_mult
            am = _ask_size_mult(self.mid_est)
            if cs <= 3:
                ask_q = base * am
            else:
                ask_q = base * max(1.0, min(2.5, 1.0 + (am - 1.0) * 0.15))
            if p_est < 0.10:
                ask_q *= 2.7
            elif p_est < 0.20:
                ask_q *= 1.8
            # Floor boost ask (from 3020)
            if in_floor:
                ask_q *= self._floor_ask_multiplier(p_est, cs)
            ask_q = max(0.5, min(700 if (cs >= 4 and in_floor) else (500 if cs >= 4 else 150), ask_q))
        else:
            ask_q = _retail_buy_qty(ap, est_not) * 2.7 * time_mult
            ask_q = max(150, min(500 if cs >= 4 else 150, ask_q))

            k, _, _, _, ms = self._get_params(cs)
            base = min(ms, max(3.5, k / ev)) * time_mult
            bm = _bid_size_mult(self.mid_est)
            if cs <= 3:
                bid_q = base * bm
            else:
                bid_q = base * max(1.0, min(2.5, 1.0 + (bm - 1.0) * 0.15))
            if p_est > 0.90:
                bid_q *= 2.7
            elif p_est > 0.80:
                bid_q *= 1.8
            bid_q = max(0.5, min(500 if cs >= 4 else 150, bid_q))

        avail = state.free_cash
        bpc = bp / 100.0
        acc = (100 - ap) / 100.0

        # Asymmetric cash allocation in floor regime (from 3020)
        if in_floor:
            bid_cash_frac = 0.30
            ask_cash_frac = 0.70
        else:
            bid_cash_frac = 0.55
            ask_cash_frac = 0.45

        if bpc > 0 and bid_q * bpc > avail * bid_cash_frac:
            bid_q = max(0.01, avail * bid_cash_frac / bpc)
        if acc > 0:
            if cs >= 3:
                available_yes = max(0.0, state.yes_inventory)
                uncovered = max(0.0, ask_q - available_yes)
                cash_needed = uncovered * acc
                if cash_needed > avail * ask_cash_frac:
                    ask_q = max(0.01, available_yes + avail * ask_cash_frac / acc)
            else:
                if ask_q * acc > avail * ask_cash_frac:
                    ask_q = max(0.01, avail * ask_cash_frac / acc)

        ir = ni / self.inv_limit
        bid_q *= max(0.15, 1.0 - ir * 0.8)
        ask_q *= max(0.15, 1.0 + ir * 0.8)

        s_bid = self._should_suppress("bid")
        s_ask = self._should_suppress("ask")

        actions = [CancelAll()]
        if not s_bid and bp >= 1 and bid_q > 0.01 and ni < self.inv_limit:
            actions.append(PlaceOrder(Side.BUY, bp, max(0.01, bid_q)))
            self.last_bid_qty = bid_q
            self.last_own_bid_tick = bp
        else:
            self.last_bid_qty = 0.0
            self.last_own_bid_tick = None
        if not s_ask and ap <= 99 and ask_q > 0.01 and ni > -self.inv_limit:
            actions.append(PlaceOrder(Side.SELL, ap, max(0.01, ask_q)))
            self.last_ask_qty = ask_q
            self.last_own_ask_tick = ap
        else:
            self.last_ask_qty = 0.0
            self.last_own_ask_tick = None
        return actions

    def _extreme_low_quotes(self, cb, ca, mid, ev, spread_mult, p_est):
        p_ticks = max(1, int(round(p_est * 100)))
        if ca is not None:
            ap = ca - 1
            min_safe_ask = p_ticks + 1
            ap = max(min_safe_ask, ap)
            if ap >= ca:
                ap = ca - 1
            if ap < min_safe_ask:
                ap = min_safe_ask
        else:
            ap = p_ticks + max(1, int(math.ceil(spread_mult * ev)))
        ap = max(2, min(99, ap))
        if cb is not None:
            bp = cb + 1
            max_safe_bid = p_ticks - 1
            if bp > max_safe_bid:
                bp = max_safe_bid
        else:
            max_safe_bid = p_ticks - 1
            bp = 1 if max_safe_bid >= 1 else None
        if bp is not None and bp < 1:
            bp = None
        if bp is not None and ap is not None and bp >= ap:
            bp = max(1, ap - 1) if ap > 1 else None
        return bp, ap

    def _extreme_high_quotes(self, cb, ca, mid, ev, spread_mult, p_est):
        p_ticks = max(1, min(99, int(round(p_est * 100))))
        if cb is not None:
            bp = cb + 1
            max_safe_bid = p_ticks - 1
            bp = min(bp, max_safe_bid)
            if bp <= cb:
                bp = cb + 1
            if bp > max_safe_bid:
                bp = max_safe_bid
        else:
            bp = p_ticks - max(1, int(math.ceil(spread_mult * ev)))
        bp = max(1, min(98, bp))
        if ca is not None:
            ap = ca - 1
            min_safe_ask = p_ticks + 1
            ap = max(min_safe_ask, ap)
            if ap >= ca:
                ap = ca - 1
            if ap < min_safe_ask:
                ap = min_safe_ask
        else:
            min_safe_ask = p_ticks + 1
            ap = 99 if min_safe_ask <= 99 else None
        if ap is not None and ap > 99:
            ap = None
        if bp is not None and ap is not None and bp >= ap:
            ap = min(99, bp + 1) if bp < 99 else None
        return bp, ap
