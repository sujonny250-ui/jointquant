# encoding: utf-8
"""
超短期EMA策略（自动交易版）

【运行逻辑】
  每日开盘前(before_open)选股，基于前一交易日数据，获取今日推荐。
  开盘时买入推荐前5只，每只20%仓位。
  收盘后15:35 计算是否卖出（仓位亏损/2ATR止损/5天周期未突破成本区），并计算整体收益率，若从最高点回撤10点则空仓10个交易日。
  ※ 收盘后计算需聚宽选择「分钟」回测频率

【筛选流程】
  1. 基础过滤：流通市值50~150亿，剔除ST/科创/创业/北交
  2. 趋势：前日收盘 > 15日EMA
  3. 波动：近4日振幅 <7%
  4. 流动性：20日日均成交额 >= 1.5亿
  5. 杠杆情绪：融资余额增加率 >5%
  6. 取前5只买入，每只20%
"""
from jqdata import *
from datetime import timedelta
import numpy as np
import talib

def initialize(context):
    set_benchmark('000300.XSHG')
    set_option('use_real_price', True)
    
    set_order_cost(OrderCost(
        open_tax=0,
        close_tax=0.001,
        open_commission=0.0003,
        close_commission=0.0003,
        min_commission=5
    ), type='stock')
    
    set_slippage(FixedSlippage(0.002))
    
    # 参数
    g.min_circ_mv = 50e8               # 最小流通市值
    g.max_circ_mv = 150e8             # 最大流通市值（小市值）
    g.min_avg_amount = 15000e4         # 最小平均成交额
    g.ema_period = 15                  # EMA周期
    g.vib_period = 4                  # 振幅周期（近4日）
    g.max_vib = 0.07                 # 近N日振幅 <7%（波动不宜过大）
    g.min_margin_increase = 0.05      # 融资余额增加率 >5%（杠杆情绪爆发）
    g.max_positions = 5               # 最大持仓数
    g.position_pct = 0.20             # 每只仓位20%
    g.loss_threshold_pct = 0.015      # 仓位亏损≥总仓1.5%则止损
    g.cycle_days = 5                  # 5天一个周期，验证是否突破成本区
    g.drawdown_empty_pct = 10         # 整体收益从最高点回撤10点则空仓
    g.empty_days = 10                  # 空仓10个交易日
    
    g.hold_days = {}                  # 持仓天数记录
    g.peak_return = 0                 # 整体收益峰值(%)
    g.empty_until = None              # 空仓截止日期(None=未空仓)
    g.hold_high = {}                  # 持有周期内最高价（用于2ATR止损）
    g.entry_atr = {}                  # 买入时ATR（用于成本区计算）
    g.to_sell = {}                    # 尾盘计算的待卖出 {stock: reason}
    g.candidates = []                 # 候选池
    
    run_daily(select_candidates, time='before_open', reference_security='000300.XSHG')
    run_daily(select_candidates, time='12:20', reference_security='000300.XSHG')   # 备用选股
    run_daily(calc_sell_list, time='15:35', reference_security='000300.XSHG')     # 收盘后计算待卖出
    run_daily(my_trade, time='open', reference_security='000300.XSHG')             # 早盘执行卖出+买入

def select_candidates(context):
    """开盘前执行：基于前一交易日数据，获取今日推荐"""
    ref_date = context.previous_date  # 前一交易日（开盘前无当日数据）
    log.info(f"=== 基于 {ref_date} 数据，今日推荐 ===")
    
    # 1. 获取所有股票基本信息，过滤小市值
    q = query(
        valuation.code, valuation.circulating_market_cap
    ).filter(
        valuation.circulating_market_cap.between(g.min_circ_mv / 1e8, g.max_circ_mv / 1e8)
    ).order_by(valuation.circulating_market_cap.asc())
    
    df_basic = get_fundamentals(q, date=ref_date)
    
    # 剔除ST、科创板、创业板、北交所
    def _exclude_board(code):
        if code.endswith('.BJ'):
            return True
        if code.startswith('688') or code.startswith('689'):
            return True
        if code.startswith('300') or code.startswith('301'):
            return True
        return False
    
    codes = df_basic['code'].tolist()
    codes = [c for c in codes if not _exclude_board(c)]
    if codes:
        df_st = get_extras('is_st', codes, start_date=ref_date, end_date=ref_date)
        st_set = set(df_st.columns[df_st.iloc[0] == True].tolist()) if not df_st.empty else set()
        codes = [c for c in codes if c not in st_set]
    df_basic = df_basic[df_basic['code'].isin(codes)].copy()
    
    log.info(f"[基础过滤] 小市值符合(已剔除ST/科创/创业/北交): {len(df_basic)} 只")
    
    if df_basic.empty:
        g.candidates = []
        return
    
    candidates = []
    filtered = {'数据不足': [], '收盘低于EMA': [], '振幅过大': [], '成交额不足': [], '融资数据不足': [], '融资增加率不足': []}
    for stock in df_basic['code']:
        # 2. 获取历史数据
        prices = get_price(stock, count=g.vib_period + g.ema_period, end_date=ref_date, 
                           frequency='daily', fields=['close', 'low', 'high', 'open', 'volume'])
        if len(prices) < g.vib_period + g.ema_period:
            filtered['数据不足'].append(stock)
            continue
        
        # 3. 当日收盘 > N日EMA（趋势向上）
        ema = talib.EMA(prices['close'], timeperiod=g.ema_period)[-1]
        close = prices['close'][-1]
        if close <= ema:
            filtered['收盘低于EMA'].append(f"{stock}(收盘{close:.2f}<=EMA{ema:.2f})")
            continue
        
        # 4. 近N日振幅 <7%（波动收敛，非剧烈震荡）
        recent_prices = prices[-g.vib_period:]
        max_high = recent_prices['high'].max()
        min_low = recent_prices['low'].min()
        vib = (max_high - min_low) / min_low
        if vib >= g.max_vib:
            filtered['振幅过大'].append(f"{stock}(振幅{vib:.1%})")
            continue
        
        # 5. 平均成交额过滤
        avg_amount = (prices['volume'][-20:] * prices['close'][-20:]).mean()
        if avg_amount < g.min_avg_amount:
            filtered['成交额不足'].append(f"{stock}(日均{avg_amount/1e4:.0f}万)")
            continue
        
        # 6. 融资余额增加率 >5%（融资数据T+1，用ref_date及前日）
        df_margin = get_mtss(stock, start_date=ref_date - timedelta(days=4), 
                             end_date=ref_date, fields=['fin_value'])
        if len(df_margin) < 2:
            filtered['融资数据不足'].append(stock)
            continue
        fin_last = df_margin['fin_value'].iloc[-1]   # 前一交易日
        fin_prev = df_margin['fin_value'].iloc[-2]   # 前前一交易日
        if fin_prev <= 0:
            filtered['融资数据不足'].append(stock)
            continue
        margin_increase = (fin_last - fin_prev) / fin_prev
        if margin_increase <= g.min_margin_increase:
            filtered['融资增加率不足'].append(f"{stock}({margin_increase:.1%})")
            continue
        
        candidates.append(stock)
    
    for reason, items in filtered.items():
        if items:
            log.info(f"[筛选] 淘汰-{reason}: {len(items)} 只 → {items[:8]}{'...' if len(items) > 8 else ''}")
    
    total_in = len(df_basic)
    total_out = sum(len(v) for v in filtered.values())
    log.info(f"[筛选漏斗] 基础{total_in} → 技术面{len(candidates)}(-{total_out}) → 取前5只买入")
    
    g.candidates = candidates[:5]  # 只取前5只用于买入
    log.info(f"[今日推荐] 共 {len(g.candidates)} 只（开盘买入）:")
    for code in g.candidates:
        try:
            name = get_security_info(code).display_name
            close = get_price(code, count=1, end_date=ref_date, fields=['close'])['close'][-1]
            log.info(f"  {name} ({code}) 前收: {close:.2f}")
        except Exception:
            log.info(f"  - ({code})")


def calc_sell_list(context):
    """收盘后15:35 计算待卖出列表，次日早盘执行。需分钟回测。"""
    # === 整体收益率与回撤空仓检查（每日收盘必算）===
    starting_cash = context.portfolio.starting_cash
    total_value = context.portfolio.total_value
    current_return = (total_value - starting_cash) / starting_cash * 100 if starting_cash > 0 else 0
    peak_return = getattr(g, 'peak_return', 0)
    g.peak_return = max(peak_return, current_return)
    drawdown = g.peak_return - current_return
    drawdown_pct = getattr(g, 'drawdown_empty_pct', 10)
    empty_days = getattr(g, 'empty_days', 10)
    empty_until = getattr(g, 'empty_until', None)
    today = context.current_dt.date()
    
    if empty_until is not None:
        if today >= empty_until:
            g.empty_until = None
            g.peak_return = current_return  # 空仓结束后重置峰值，从当前收益重新计算回撤
            log.info(f"[收盘] 整体收益{current_return:.2f}% 空仓期结束，峰值重置为{current_return:.2f}%，恢复正常")
        else:
            log.info(f"[收盘] 整体收益{current_return:.2f}% 峰值{g.peak_return:.2f}% 空仓中，截止{empty_until}")
    elif drawdown >= drawdown_pct:
        try:
            trade_days = get_trade_days(today, today + timedelta(days=30))
            g.empty_until = trade_days[min(empty_days, len(trade_days) - 1)] if len(trade_days) > 0 else today
        except Exception:
            g.empty_until = today + timedelta(days=14)
        log.info(f"[收盘] 整体收益{current_return:.2f}% 峰值{g.peak_return:.2f}% 回撤{drawdown:.2f}%>=10点 触发空仓至{g.empty_until}")
    
    positions = list(context.portfolio.positions.keys())
    g.to_sell = {}
    if not positions:
        return
    # 若本次触发回撤空仓，将全部持仓加入待卖出
    if g.empty_until is not None and drawdown >= drawdown_pct and positions:
        for stock in positions:
            g.to_sell[stock] = f"整体收益回撤{drawdown:.1f}%>={drawdown_pct}点空仓"
        log.info(f"[收盘后] 待卖出{len(g.to_sell)}只(回撤空仓): {list(g.to_sell.keys())}，明日早盘执行")
        return
    data = get_current_data()
    loss_threshold_pct = getattr(g, 'loss_threshold_pct', 0.015)
    today = context.current_dt
    log.info(f"========== 收盘后计算待卖出 持仓{len(positions)}只 ==========")
    for stock in positions:
        if stock not in g.hold_days:
            g.hold_days[stock] = 0
        g.hold_days[stock] += 1
        pos = context.portfolio.positions[stock]
        if data[stock].paused:
            continue
        if pos.closeable_amount <= 0:  # 当日买入T+1不可卖，明日早盘可卖
            continue
        cur_price = data[stock].last_price
        avg_cost = pos.avg_cost
        profit_pct = (cur_price - avg_cost) / avg_cost * 100 if avg_cost > 0 else 0
        total_value = context.portfolio.total_value
        loss_amount = (avg_cost - cur_price) * pos.total_amount
        threshold = total_value * loss_threshold_pct
        hold_days = g.hold_days[stock]
        # 计算2ATR止损位并记录
        today_bar = get_price(stock, count=1, end_date=today, frequency='daily',
                             fields=['high', 'low', 'close'])
        prices = get_price(stock, count=15, end_date=today, frequency='daily',
                          fields=['high', 'low', 'close'])
        stop_price = None
        if len(today_bar) >= 1 and len(prices) >= 14:
            today_high = today_bar['high'].iloc[-1]
            today_close = today_bar['close'].iloc[-1]
            hold_high = g.hold_high.get(stock, cur_price)
            hold_high = max(hold_high, today_high)
            g.hold_high[stock] = hold_high
            atr_arr = talib.ATR(prices['high'], prices['low'], prices['close'], timeperiod=14)
            atr_val = atr_arr[-1]
            if not (np.isnan(atr_val) or atr_val <= 0):
                stop_price = hold_high - 2 * atr_val
                log.info(f"  [{stock}] 第{hold_days}天 止损位={stop_price:.2f} (最高{hold_high:.2f}-2×ATR{atr_val:.2f}) 收盘{today_close:.2f} 盈亏{profit_pct:.2f}%")
        # 1. 仓位亏损≥总仓1.5%
        if loss_amount >= threshold:
            g.to_sell[stock] = f"仓位亏损{loss_amount:.0f}元≥{threshold:.0f}"
            log.info(f"  [{stock}] 标记卖出: 仓位亏损 盈亏{profit_pct:.2f}%")
            continue
        # 2. 持有周期内最高价-2×ATR止损：收盘价低于该点则卖
        if stop_price is not None and today_close < stop_price:
            g.to_sell[stock] = f"2ATR止损 收盘{today_close:.2f}<{stop_price:.2f}(最高{hold_high:.2f}-2×ATR{atr_val:.2f})"
            log.info(f"  [{stock}] 标记卖出: 2ATR止损 盈亏{profit_pct:.2f}%")
            continue
        # 3. 5天周期未突破成本区：成本区=买入价+2*N*ATR，周期内最高价<成本区则卖（需有hold_high数据）
        if stop_price is not None:
            cycle_days = getattr(g, 'cycle_days', 5)
            if hold_days > 0 and hold_days % cycle_days == 0:
                entry_atr_val = g.entry_atr.get(stock)
                if entry_atr_val is not None and entry_atr_val > 0:
                    cycle_num = hold_days // cycle_days  # 第N周期
                    cost_zone = avg_cost + 2 * cycle_num * entry_atr_val
                    broken = hold_high >= cost_zone
                    log.info(f"  [{stock}] 第{cycle_num}周期(第{hold_days}天) 成本区={cost_zone:.2f}(成本{avg_cost:.2f}+2×{cycle_num}×ATR{entry_atr_val:.2f}) 最高{hold_high:.2f} 突破={broken}")
                    if not broken:
                        g.to_sell[stock] = f"周期{cycle_num}未突破成本区 最高{hold_high:.2f}<{cost_zone:.2f}"
                        log.info(f"  [{stock}] 标记卖出: 第{cycle_num}周期未突破成本区 盈亏{profit_pct:.2f}%")
    if g.to_sell:
        log.info(f"[收盘后] 待卖出{len(g.to_sell)}只: {list(g.to_sell.keys())}，明日早盘执行")


def my_trade(context):
    """早盘09:30 执行：先卖（尾盘已计算）后买"""
    data = get_current_data()
    loss_threshold_pct = getattr(g, 'loss_threshold_pct', 0.015)
    to_sell = getattr(g, 'to_sell', {})
    
    # === 卖出逻辑：执行尾盘计算的待卖出 ===
    if to_sell:
        log.info(f"========== 早盘执行卖出 共{len(to_sell)}只（昨日收盘后已计算） ==========")
    for stock, reason in list(to_sell.items()):
        if stock not in context.portfolio.positions:
            continue
        cur_data = data[stock]
        if cur_data.paused:
            log.info(f"  [{stock}] 跳过: 停牌")
            continue
        pos = context.portfolio.positions[stock]
        if pos.closeable_amount <= 0:
            log.info(f"  [{stock}] 跳过: T+1无可卖数量")
            continue
        cur_price = cur_data.last_price
        avg_cost = pos.avg_cost
        profit_pct = (cur_price - avg_cost) / avg_cost * 100 if avg_cost > 0 else 0
        order_target(stock, 0)
        if stock in g.hold_days:
            del g.hold_days[stock]
        if stock in g.hold_high:
            del g.hold_high[stock]
        if stock in g.entry_atr:
            del g.entry_atr[stock]
        if stock in to_sell:
            del g.to_sell[stock]
        log.info(f"【卖出】{stock} {reason}, 盈亏{profit_pct:.2f}%")
    
    # === 买入逻辑 ===
    empty_until = getattr(g, 'empty_until', None)
    if empty_until is not None and context.current_dt.date() < empty_until:
        log.info(f"[早盘] 空仓期中，截止{empty_until}，仅卖不买")
        return
    if len(context.portfolio.positions) >= g.max_positions:
        return
    
    total_value = context.portfolio.total_value
    target_value_per = total_value * g.position_pct
    
    for stock in g.candidates[:g.max_positions]:
        if stock in context.portfolio.positions:
            continue
        if len(context.portfolio.positions) >= g.max_positions:
            break
        
        cur_data = data[stock]
        if cur_data.paused or cur_data.day_open == 0:
            log.info(f"[买入] {stock} 停牌或未开盘，跳过")
            continue
        
        cur_price = cur_data.last_price
        prices = get_price(stock, count=20, end_date=context.current_dt, frequency='daily',
                          fields=['high', 'low', 'close'])
        entry_atr = talib.ATR(prices['high'], prices['low'], prices['close'], timeperiod=14)[-1] if len(prices) >= 14 else 0
        if np.isnan(entry_atr) or entry_atr <= 0:
            entry_atr = 0
        # 按仓位1.5%止损计算：止损价=现价-(总仓×1.5%)/预估数量
        est_amount = target_value_per / cur_price if cur_price > 0 else 0
        stop_price = cur_price - (total_value * loss_threshold_pct) / est_amount if est_amount > 0 else 0
        
        order_target_value(stock, target_value_per)
        g.hold_days[stock] = 0
        g.hold_high[stock] = cur_price  # 买入时初始化持有周期最高价
        g.entry_atr[stock] = entry_atr  # 买入时ATR，用于成本区计算
        log.info(f"【买入】 {stock} 目标{target_value_per:.0f}(20%), 止损价={stop_price:.2f}(仓位亏损≥总仓{loss_threshold_pct:.1%}), ATR={entry_atr:.2f}")
        