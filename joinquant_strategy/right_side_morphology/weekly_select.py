# encoding: utf-8
"""
右侧形态学每周选股 / Right-Side Morphology Weekly Select（仅选股，不自动下单）

【运行逻辑】
  每周第一个交易日晚上20:00运行一次（通常为周一 night），
  基于当日数据筛选股票，输出本周推荐池。默认只打日志，不自动下单。
  ※ 聚宽 run_weekly 的 weekday = 本周第几个交易日（1=本周第1个交易日，通常周一），不是日历星期几。

【基础过滤】
  1. 流通市值 >= 下限（暂不设上限）+ 剔除ST/科创/创业/北交
  2. 近20日日均成交额 >= 2亿（剔除僵尸股）

【核心选股】
  1. 锚定日T：倒退前10个交易日（不含最新日），须有一天涨幅>5%且成交量>60日均量×2；否则剔除
     ※ T不能是最新交易日，否则后续缩量/收盘约束无法检验
  2. T日之后至数据日：每日收盘 >= T日开盘；每日量能 < T日量能×70%；每日涨幅 <= 2%

【使用说明】
  - 回测/模拟：频率选「日」即可
  - run_weekly(weekday=1)：本周第一个交易日；time='night'：晚上20:00执行
"""
from jqdata import *
from datetime import timedelta
import numpy as np
import talib


def initialize(context):
    set_benchmark('000300.XSHG')
    set_option('use_real_price', True)

    # ===== 基础参数 =====
    g.min_circ_mv = 50e8               # 最小流通市值
    g.max_circ_mv = None              # 最大流通市值（None=不设上限）
    g.min_avg_amount = 2e8            # 近20日日均成交额下限（2亿，剔除僵尸股）
    g.avg_amount_days = 20            # 成交额统计天数
    g.top_n = None                    # 输出只数上限（None=全部打印）

    # ===== 核心选股参数 =====
    g.anchor_lookback = 10            # 锚定日回看交易日数
    g.anchor_gain_min = 0.05          # 锚定日涨幅下限 >5%
    g.vol_ma_days = 60                # 量能均线天数
    g.anchor_vol_mult = 2.0           # 锚定日量能 > 均量 × 倍数
    g.post_vol_max_ratio = 0.70       # T日后每日量能 < T日量能 × 70%
    g.post_gain_max = 0.02            # T日后每日涨幅上限 <=2%

    # ===== 运行状态 =====
    g.candidates = []                 # 本周选股结果（代码列表）
    g.candidate_info = {}             # {code: {anchor_date, open_t, ...}}
    g.last_select_date = None         # 上次选股日期

    # 每周第一个交易日晚上20:00选股（通常周一；weekday=1=本周第1个交易日，night=20:00）
    run_weekly(weekly_select, weekday=1, time='night', reference_security='000300.XSHG')


def weekly_select(context):
    """每周第一个交易日晚上：执行选股并输出结果"""
    today = context.current_dt.date()
    # night/after_close 时当日行情已可用
    ref_date = context.current_dt.date()
    weekday_name = ['一', '二', '三', '四', '五', '六', '日'][context.current_dt.weekday()]
    log.info("=" * 60)
    log.info(f"【每周选股】运行日={today}(周{weekday_name}) "
             f"时刻={context.current_dt.strftime('%H:%M')} 数据日={ref_date}")
    log.info("=" * 60)

    # 1. 基础股票池
    stock_pool = get_stock_pool(ref_date)
    log.info(f"[基础池] {len(stock_pool)} 只")
    if not stock_pool:
        g.candidates = []
        g.candidate_info = {}
        g.last_select_date = today
        return

    # 2. 筛选（全部入选结果都打印，不截断）
    candidates = filter_stocks(stock_pool, ref_date)
    top_n = getattr(g, 'top_n', None)
    g.candidates = candidates[:top_n] if top_n else candidates
    g.last_select_date = today

    # 3. 输出结果：全部列出 + 按申万一级行业分类打印
    log.info(f"[选股结果] 共 {len(g.candidates)} 只（全部列出）:")
    for i, code in enumerate(g.candidates, 1):
        try:
            name = get_security_info(code).display_name
            label = f"{code}{name}"
            close = get_price(code, count=1, end_date=ref_date, fields=['close'])['close'][-1]
            info = g.candidate_info.get(code, {})
            t_date = info.get('anchor_date', '-')
            t_open = info.get('open_t', 0)
            t_gain = info.get('gain_t', 0)
            log.info(f"  {i:02d}. {label} 前收:{close:.2f} 锚定日:{t_date} "
                     f"T开盘:{t_open:.2f} T涨幅:{t_gain:.1%}")
        except Exception:
            log.info(f"  {i:02d}. {code}")

    # 4. 按行业分类打印
    print_by_industry(g.candidates, ref_date)

    # 额外汇总一行，方便复制
    labels = []
    for code in g.candidates:
        try:
            labels.append(f"{code}{get_security_info(code).display_name}")
        except Exception:
            labels.append(code)
    log.info(f"[选股汇总] {', '.join(labels)}")
    log.info("=" * 60)


def print_by_industry(candidates, ref_date):
    """按申万一级行业分类打印：规整列表 + 流通市值，并标记过小/过大市值"""
    if not candidates:
        return

    # 批量取行业
    try:
        ind_map = get_industry(security=candidates, date=ref_date)
    except Exception as e:
        log.info(f"[行业分类] 获取失败: {e}")
        return

    # 批量取流通市值（聚宽单位：亿元）
    mv_map = {}
    try:
        q = query(
            valuation.code, valuation.circulating_market_cap
        ).filter(
            valuation.code.in_(candidates)
        )
        df_mv = get_fundamentals(q, date=ref_date)
        if df_mv is not None and not df_mv.empty:
            for _, row in df_mv.iterrows():
                mv_map[row['code']] = float(row['circulating_market_cap'])
    except Exception as e:
        log.info(f"[行业分类] 流通市值获取失败: {e}")

    # industry_name -> [dict, ...]
    groups = {}
    for code in candidates:
        name = code
        try:
            name = get_security_info(code).display_name
        except Exception:
            pass
        label = f"{code}{name}"

        industry_name = '未知行业'
        info = ind_map.get(code) if isinstance(ind_map, dict) else None
        if info:
            for key in ('sw_l1', 'jq_l1', 'zjw'):
                if key in info and info[key]:
                    industry_name = info[key].get('industry_name', industry_name)
                    break

        circ_mv = mv_map.get(code)  # 亿元
        mark = ''
        if circ_mv is not None:
            if circ_mv < 50:
                mark = ' [流通市值<50亿]'
            elif circ_mv > 500:
                mark = ' [流通市值>500亿]'

        groups.setdefault(industry_name, []).append({
            'code': code,
            'label': label,
            'circ_mv': circ_mv,
            'mark': mark,
        })

    # 行业按数量降序；行业内按流通市值升序
    sorted_industries = sorted(groups.items(), key=lambda x: (-len(x[1]), x[0]))

    log.info("-" * 60)
    log.info(f"[行业分类] 共 {len(candidates)} 只 / {len(sorted_industries)} 个申万一级行业")
    log.info("-" * 60)

    for industry_name, stocks in sorted_industries:
        stocks_sorted = sorted(
            stocks,
            key=lambda s: (s['circ_mv'] is None, s['circ_mv'] if s['circ_mv'] is not None else 0)
        )
        log.info(f"【{industry_name}】共 {len(stocks_sorted)} 只")
        for i, s in enumerate(stocks_sorted, 1):
            if s['circ_mv'] is not None:
                mv_str = f"{s['circ_mv']:.2f}亿"
            else:
                mv_str = "N/A"
            # 对齐：序号 | 代码名称 | 流通市值 | 标记
            log.info(f"  {i:02d}. {s['label']:<22}  流通市值:{mv_str:>10}{s['mark']}")
        log.info("")  # 行业之间空一行，更规整

    log.info("-" * 60)


def get_stock_pool(ref_date):
    """基础股票池：流通市值下限（暂不设上限）+ 剔除ST/科创/创业/北交"""
    min_mv = g.min_circ_mv / 1e8  # 聚宽估值单位：亿元
    q = query(
        valuation.code, valuation.circulating_market_cap
    ).filter(
        valuation.circulating_market_cap >= min_mv
    )
    if getattr(g, 'max_circ_mv', None) is not None:
        q = q.filter(valuation.circulating_market_cap <= g.max_circ_mv / 1e8)
    q = q.order_by(valuation.circulating_market_cap.asc())

    df = get_fundamentals(q, date=ref_date)
    if df is None or df.empty:
        return []

    codes = df['code'].tolist()
    codes = [c for c in codes if not _exclude_board(c)]

    if codes:
        df_st = get_extras('is_st', codes, start_date=ref_date, end_date=ref_date)
        st_set = set(df_st.columns[df_st.iloc[0] == True].tolist()) if not df_st.empty else set()
        codes = [c for c in codes if c not in st_set]

    return codes


def _exclude_board(code):
    """剔除北交所 / 科创板 / 创业板"""
    if code.endswith('.BJ'):
        return True
    if code.startswith('688') or code.startswith('689'):
        return True
    if code.startswith('300') or code.startswith('301'):
        return True
    return False


def filter_stocks(stock_pool, ref_date):
    """
    选股筛选
    基础：近20日日均成交额 >= 2亿
    核心：锚定日T + T日后收敛约束
    """
    candidates = []
    g.candidate_info = {}
    filtered = {
        '数据不足': [],
        '日均成交额不足': [],
        '无锚定日': [],
        'T日后破位/放量/大涨': [],
    }
    amount_days = getattr(g, 'avg_amount_days', 20)
    min_avg = getattr(g, 'min_avg_amount', 2e8)
    lookback = getattr(g, 'anchor_lookback', 10)
    vol_ma_days = getattr(g, 'vol_ma_days', 60)
    # 至少需要：60日均量 + 前收(涨幅) + 回看10日
    need_bars = vol_ma_days + lookback + 1

    for stock in stock_pool:
        prices = get_price(
            stock, count=need_bars, end_date=ref_date, frequency='daily',
            fields=['open', 'close', 'volume']
        )
        if len(prices) < need_bars:
            filtered['数据不足'].append(stock)
            continue

        # ----- 基础：近20日日均成交额 -----
        recent = prices.iloc[-amount_days:]
        avg_amount = (recent['volume'] * recent['close']).mean()
        if avg_amount < min_avg:
            filtered['日均成交额不足'].append(f"{stock}(日均{avg_amount/1e8:.2f}亿)")
            continue

        # ----- 核心：找锚定日并校验T日后条件 -----
        ok, reason, info = check_anchor_and_hold(prices, stock)
        if not ok:
            filtered[reason].append(info if isinstance(info, str) else stock)
            continue

        candidates.append(stock)
        g.candidate_info[stock] = info

    for reason, items in filtered.items():
        if items:
            log.info(f"[筛选] 淘汰-{reason}: {len(items)} 只 → {items[:8]}{'...' if len(items) > 8 else ''}")

    log.info(f"[筛选漏斗] 基础{len(stock_pool)} → 通过{len(candidates)}(-{sum(len(v) for v in filtered.values())})")
    return candidates


def check_anchor_and_hold(prices, stock):
    """
    核心规则：
    1) 前10个交易日中找锚定日T：涨幅>5% 且 量>60日均量×2
       （多个候选时从近到远尝试，取第一个同时满足T日后条件的）
    2) T日之后至最新：收盘>=T开盘；量能<T日量×70%；涨幅<=2%
    返回: (ok, fail_reason_key, info_or_msg)
    """
    lookback = getattr(g, 'anchor_lookback', 10)
    gain_min = getattr(g, 'anchor_gain_min', 0.05)
    vol_ma_days = getattr(g, 'vol_ma_days', 60)
    vol_mult = getattr(g, 'anchor_vol_mult', 2.0)
    post_vol_ratio = getattr(g, 'post_vol_max_ratio', 0.70)
    post_gain_max = getattr(g, 'post_gain_max', 0.02)

    n = len(prices)
    opens = prices['open'].values
    closes = prices['close'].values
    volumes = prices['volume'].values
    # 前10个交易日窗口（含最新日），但锚定日T不能是最新日，否则后续缩量判断无意义
    # 候选T索引： [n-lookback, ..., n-2]
    window_start = n - lookback
    last_idx = n - 1

    # 收集窗口内所有锚定日候选（从近到远，排除最新日）
    anchor_idxs = []
    for i in range(last_idx - 1, window_start - 1, -1):
        if i < 1:
            continue
        gain = (closes[i] - closes[i - 1]) / closes[i - 1] if closes[i - 1] > 0 else 0
        if gain <= gain_min:
            continue
        # 60日均量：T日之前60根（不含T）
        if i < vol_ma_days:
            continue
        vol_ma = volumes[i - vol_ma_days:i].mean()
        if vol_ma <= 0 or volumes[i] <= vol_mult * vol_ma:
            continue
        anchor_idxs.append(i)

    if not anchor_idxs:
        return False, '无锚定日', stock

    # 从近到远：第一个通过T日后约束的锚定日
    for t_idx in anchor_idxs:
        passed, detail = _check_post_anchor(opens, closes, volumes, t_idx, post_vol_ratio, post_gain_max)
        if passed:
            gain_t = (closes[t_idx] - closes[t_idx - 1]) / closes[t_idx - 1]
            anchor_date = prices.index[t_idx]
            if hasattr(anchor_date, 'date'):
                anchor_date = anchor_date.date()
            info = {
                'anchor_date': anchor_date,
                'open_t': float(opens[t_idx]),
                'close_t': float(closes[t_idx]),
                'vol_t': float(volumes[t_idx]),
                'gain_t': float(gain_t),
            }
            return True, '', info

    return False, 'T日后破位/放量/大涨', f"{stock}({detail})"


def _check_post_anchor(opens, closes, volumes, t_idx, post_vol_ratio, post_gain_max):
    """校验 T+1 ~ 最新：收盘>=T开盘、量<T量×70%、涨幅<=2%；T不能是最新日"""
    open_t = opens[t_idx]
    vol_t = volumes[t_idx]
    if open_t <= 0 or vol_t <= 0:
        return False, 'T日数据异常'

    # T必须早于最新日，至少有一天用于检验缩量等条件
    if t_idx >= len(closes) - 1:
        return False, '锚定日为最新日'

    vol_cap = vol_t * post_vol_ratio
    for j in range(t_idx + 1, len(closes)):
        if closes[j] < open_t:
            return False, f"收盘{closes[j]:.2f}<T开盘{open_t:.2f}"
        if volumes[j] >= vol_cap:
            return False, f"量{volumes[j]:.0f}>=T量70%{vol_cap:.0f}"
        prev = closes[j - 1]
        gain_j = (closes[j] - prev) / prev if prev > 0 else 0
        if gain_j > post_gain_max:
            return False, f"涨幅{gain_j:.1%}>2%"

    return True, ''