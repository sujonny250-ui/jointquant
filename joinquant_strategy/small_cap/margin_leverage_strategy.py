# encoding: utf-8
"""
小市值融资杠杆策略 / Small-Cap Margin Leverage（自动交易版）

【运行逻辑】
  每日开盘前(before_open)选股，基于前一交易日数据，获取今日推荐。
  开盘时买入推荐前5只，每只20%仓位。
  收盘后15:35 计算是否卖出（仓位亏损/2ATR止损/5天周期未突破成本区），并计算整体收益率，若从最高点回撤10点则空仓10个交易日。
  ※ 收盘后计算需聚宽选择「分钟」回测频率

【筛选流程】
  1. 基础过滤：流通市值50~150亿，剔除ST/科创/创业/北交
  2. 数据充足：价格序列长度足够
  3. 趋势：前日收盘 > 15日EMA
  4. 流动性：20日日均成交额 >= 1.5亿
  5. 杠杆情绪：融资余额增加率 >5%
  6. 波动：近4日振幅 <7%
  7. 取前5只买入，每只20%
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

def _log_section_banner(context, name):
    """时段大标题。聚宽日志新在上，故应在本时段逻辑末尾调用，阅读时标题在上。

    注意：不能用换行拼成一条 log。多行时只有首行带时间戳前缀，横线会对不齐。
    每行单独 log.info，前缀长度一致，横线才整齐。
    """
    # 单行内容宽度：总宽约100 - 前缀约30；每行单独打，避免换行导致横线错位
    # 倒序打点：适配聚宽「新在上」，阅读时为 顶栏 → 标题 → 时间 → 底栏
    bar = '#' * 40
    log.info(bar)
    log.info(f"时间 {context.current_dt.strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"【{name}】")
    log.info(bar)


def _select_section_name(context):
    """区分 before_open / 12:20 两次选股"""
    t = context.current_dt.time()
    if t.hour < 12:
        return '开盘前选股 before_open'
    return '午间选股 12:20'


def _log_recommend_cards(codes, metrics, date):
    """最终入选个股：逐票打印全部考察指标（每行单独 log，避免被截断）"""
    if not codes:
        log.info('今日推荐：无')
        return
    try:
        ind_data = get_industry(list(codes), date=date)
    except Exception:
        ind_data = {}

    def _f(v, fmt):
        return fmt.format(v) if v is not None else '-'

    log.info(f"今日推荐：共{len(codes)}只（以下为全部考察指标）")
    for i, code in enumerate(codes, 1):
        m = metrics.get(code, {})
        try:
            name = get_security_info(code).display_name
        except Exception:
            name = m.get('name', code)
        industry = ind_data.get(code, {}).get('sw_l1', {}).get('industry_name', '未知行业')
        close = m.get('close')
        ema = m.get('ema')
        avg_amount = m.get('avg_amount')
        margin = m.get('margin_increase')
        vib = m.get('vib')
        circ_mv = m.get('circ_mv')
        rel_ema = ((close - ema) / ema) if (close is not None and ema not in (None, 0)) else None
        avg_yi = (avg_amount / 1e8) if avg_amount is not None else None

        log.info(f"---- [{i}/{len(codes)}] {name} {code} ----")
        log.info(f"  一级行业: {industry} | 流通市值: {_f(circ_mv, '{:.1f}亿')}")
        log.info(
            f"  收盘: {_f(close, '{:.2f}')} | EMA15: {_f(ema, '{:.2f}')} | 相对EMA: {_f(rel_ema, '{:+.2%}')}"
        )
        log.info(
            f"  日均额: {_f(avg_yi, '{:.2f}亿')} | 融资增幅: {_f(margin, '{:+.2%}')} | 4日振幅: {_f(vib, '{:.2%}')}"
        )


def select_candidates(context):
    """开盘前执行：基于前一交易日数据，获取今日推荐"""
    ref_date = context.previous_date  # 前一交易日（开盘前无当日数据）
    metrics = {}  # code -> 考察指标（供今日推荐卡片使用）
    
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
    basic_count = len(df_basic)
    circ_mv_map = dict(zip(df_basic['code'], df_basic['circulating_market_cap']))
    
    if df_basic.empty:
        g.candidates = []
        log.info('[筛选漏斗] 基础0 → 取前0只')
        _log_recommend_cards([], {}, ref_date)
        log.info(f"选股基准日(前一交易日): {ref_date}")
        _log_section_banner(context, _select_section_name(context))
        return
    
    remaining = list(df_basic['code'])
    
    # 2. 数据充足
    price_cache = {}
    need_bars = max(20, g.vib_period + g.ema_period)
    next_remaining = []
    drop_data = 0
    for stock in remaining:
        prices = get_price(stock, count=need_bars, end_date=ref_date,
                           frequency='daily', fields=['close', 'low', 'high', 'open', 'volume'])
        if len(prices) < need_bars:
            drop_data += 1
            continue
        price_cache[stock] = prices
        next_remaining.append(stock)
    remaining = next_remaining
    after_data = len(remaining)
    
    # 3. 收盘 > EMA
    next_remaining = []
    drop_ema = 0
    for stock in remaining:
        prices = price_cache[stock]
        ema = talib.EMA(prices['close'], timeperiod=g.ema_period)[-1]
        close = prices['close'][-1]
        if close <= ema:
            drop_ema += 1
            continue
        metrics.setdefault(stock, {})
        metrics[stock].update({
            'close': float(close),
            'ema': float(ema),
            'circ_mv': float(circ_mv_map.get(stock)) if stock in circ_mv_map else None,
        })
        next_remaining.append(stock)
    remaining = next_remaining
    after_ema = len(remaining)
    
    # 4. 平均成交额
    next_remaining = []
    drop_amount = 0
    for stock in remaining:
        prices = price_cache[stock]
        avg_amount = (prices['volume'][-20:] * prices['close'][-20:]).mean()
        if avg_amount < g.min_avg_amount:
            drop_amount += 1
            continue
        metrics.setdefault(stock, {})['avg_amount'] = float(avg_amount)
        next_remaining.append(stock)
    remaining = next_remaining
    after_amount = len(remaining)
    
    # 5. 融资余额增加率 >5%
    next_remaining = []
    drop_no_data, drop_rate = 0, 0
    for stock in remaining:
        df_margin = get_mtss(stock, start_date=ref_date - timedelta(days=4),
                             end_date=ref_date, fields=['fin_value'])
        if len(df_margin) < 2:
            drop_no_data += 1
            continue
        fin_last = df_margin['fin_value'].iloc[-1]
        fin_prev = df_margin['fin_value'].iloc[-2]
        if fin_prev <= 0:
            drop_no_data += 1
            continue
        margin_increase = (fin_last - fin_prev) / fin_prev
        if margin_increase <= g.min_margin_increase:
            drop_rate += 1
            continue
        metrics.setdefault(stock, {})['margin_increase'] = float(margin_increase)
        next_remaining.append(stock)
    remaining = next_remaining
    after_margin = len(remaining)
    
    # 6. 近N日振幅 <7%
    next_remaining = []
    drop_vib = 0
    for stock in remaining:
        prices = price_cache[stock]
        recent_prices = prices[-g.vib_period:]
        max_high = recent_prices['high'].max()
        min_low = recent_prices['low'].min()
        vib = (max_high - min_low) / min_low if min_low > 0 else 999
        if vib >= g.max_vib:
            drop_vib += 1
            continue
        metrics.setdefault(stock, {})['vib'] = float(vib)
        next_remaining.append(stock)
    remaining = next_remaining
    
    candidates = remaining
    g.candidates = candidates[:5]
    
    log.info(
        f"[筛选漏斗] 基础{basic_count}"
        f" → 数据{after_data}(-{drop_data})"
        f" → EMA{after_ema}(-{drop_ema})"
        f" → 成交额{after_amount}(-{drop_amount})"
        f" → 融资{after_margin}(无数据-{drop_no_data}/增幅不足-{drop_rate})"
        f" → 振幅{len(candidates)}(-{drop_vib})"
        f" → 取前{len(g.candidates)}只"
    )
    # 最终入选个股：打印名称/代码/行业及全部筛选指标
    _log_recommend_cards(g.candidates, metrics, ref_date)
    log.info(f"选股基准日(前一交易日): {ref_date}")
    _log_section_banner(context, _select_section_name(context))


def calc_sell_list(context):
    """收盘后15:35 计算待卖出列表，次日早盘执行。需分钟回测。"""
    try:
        _calc_sell_list_body(context)
    finally:
        _log_section_banner(context, '收盘后风控 15:35 计算待卖出')


def _calc_sell_list_body(context):
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
        log.info('[收盘后] 当前无持仓，无需计算卖出')
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
    log.info(f"[收盘后] 开始逐票检查 持仓{len(positions)}只")
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
    try:
        _my_trade_body(context)
    finally:
        _log_section_banner(context, '早盘交易 09:30 卖出+买入')


def _my_trade_body(context):
    data = get_current_data()
    loss_threshold_pct = getattr(g, 'loss_threshold_pct', 0.015)
    to_sell = getattr(g, 'to_sell', {})
    
    # === 卖出逻辑：执行尾盘计算的待卖出 ===
    if to_sell:
        log.info(f"[早盘] 执行待卖出 共{len(to_sell)}只（昨日收盘后已计算）")
    else:
        log.info('[早盘] 无待卖出')
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
        log.info(f"[早盘] 已满仓({g.max_positions})，跳过买入")
        return
    
    total_value = context.portfolio.total_value
    target_value_per = total_value * g.position_pct
    cand = getattr(g, 'candidates', [])
    if not cand:
        log.info('[早盘] 无候选股，跳过买入')
        return
    
    for stock in cand[:g.max_positions]:
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
