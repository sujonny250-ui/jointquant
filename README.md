# jointquant

聚宽（JoinQuant）量化策略代码仓库。按策略风格分子目录管理，脚本可直接复制到聚宽研究/回测/模拟环境运行。

根目录 README 只做**层级索引与策略摘要**；各风格目录下的 `README.md` 维护该目录脚本的详细说明。

## 目录结构

```text
jointquant/
└── joinquant_strategy/                    # 聚宽策略根目录
    ├── small_cap/                         # 小市值风格
    │   ├── README.md
    │   └── margin_leverage_strategy.py
    └── right_side_morphology/             # 右侧形态学选股
        ├── README.md
        └── weekly_select.py
```

### 分层说明

| 层级 | 路径 | 含义 |
|------|------|------|
| 仓库根 | `jointquant/` | Git 仓库根目录 |
| 平台域 | `joinquant_strategy/` | 面向聚宽平台的策略集合；后续若有其他平台可并列新增目录 |
| 风格域 | `small_cap/`、`right_side_morphology/` 等 | 同一交易风格的策略集合 |
| 策略脚本 | `*.py` | 单文件策略，含选股、交易、风控与日志 |
| 目录文档 | 各风格目录下的 `README.md` | 该目录脚本的调度、参数、流程等详细说明 |

命名约定：

- 目录优先用英文风格标签（如 `small_cap`、`right_side_morphology`）
- 策略文件用功能关键词组合（如 `margin_leverage_strategy`、`weekly_select`）

## 策略一览

### `joinquant_strategy/small_cap/` — 小市值

主板小市值股票相关策略。详见 [small_cap/README.md](joinquant_strategy/small_cap/README.md)。

| 脚本 | 摘要 |
|------|------|
| `margin_leverage_strategy.py` | 小市值 + 短期趋势/流动性/融资余额情绪选股；等权持仓；多层止损与组合回撤空仓（自动交易） |

### `joinquant_strategy/right_side_morphology/` — 右侧形态学选股

锚定放量阳线后的缩量整理形态，偏右侧确认选股。详见 [right_side_morphology/README.md](joinquant_strategy/right_side_morphology/README.md)。

| 脚本 | 摘要 |
|------|------|
| `weekly_select.py` | 每周第一个交易日晚选股；锚定日放量上涨后缩量整理；默认只输出推荐池，不自动下单 |

## 使用方式

1. 打开聚宽「策略」编辑器  
2. 将对应 `.py` 全文粘贴并保存  
3. 回测频率按各脚本目录文档要求选择（日 / 分钟）  
4. 模拟/实盘更新代码后如遇 `g` 属性缺失，需重启策略使 `initialize` 重新执行  

## 后续扩展

可在 `joinquant_strategy/` 下按风格继续新增目录，例如：

```text
joinquant_strategy/
├── small_cap/
├── right_side_morphology/
├── mid_cap/            # 中市值（预留）
└── index_enhance/      # 指数增强（预留）
```

每个风格目录保持「一策略一文件」，并在该目录 `README.md` 中补充详细说明。
