"""图表指标注册表 — 日 K 图可渲染指标的单一权威。

与 ``app/factors/registry.py``(评分/因子元数据)**解耦**: 本表只描述**图表渲染**所需
(id / 面板位 / 参数 schema / 输出列与颜色 / Y 轴范围), 只**引用**既有列 id 与 ``ta_*`` id,
不重复定义计算口径 —— 计算仍由 ``pipeline`` / ``indicators.custom`` / ``ta_factors`` 负责。

新增一个通用指标 = 在本表加一条描述符 → ``GET /api/indicators/catalog`` 自动下发 → 前端出现。
见 ``docs/adr/ADR-006-chart-indicator-registry.md``。

边界:
  - ``col_template`` 必须与 ``indicators.custom._alias`` 的命名**逐字一致**(别名列契约, ADR-005);
    由 ``tests/test_indicator_registry.py`` 与 custom.py 交叉校验锁定。
  - 成交量(量比标注)以 ``special="volume"`` 标记, 由前端特例渲染。
  - Heikin-Ashi(4 条 OHLC 线)暂不纳入(需专用渲染), 不在本表。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

Pane = Literal["overlay", "panel"]
ParamKind = Literal["scalar", "list"]
OutputType = Literal["line", "bar"]

# 与前端既有配色保持一致 (MA_COLORS / RSI_COLORS)。
MA_PALETTE = ("#A1A1AA", "#3B82F6", "#F97316", "#8B5CF6", "#06B6D4", "#EF4444")
RSI_PALETTE = ("#FACC15", "#3B82F6", "#8B5CF6", "#06B6D4")


@dataclass(frozen=True)
class ParamSpec:
    """可参数化指标的参数声明 (参数偏离默认时按 col_template 走别名列)。"""

    key: str
    label: str
    kind: ParamKind
    default: tuple[int, ...]
    min: int = 2
    max: int = 500


@dataclass(frozen=True)
class OutputSpec:
    """一条输出序列的渲染声明。

    expand_param 非空 = 按该 list 参数的每个值展开成 N 条输出 (如 MA 的多周期均线),
    此时用 label_template / canonical_template / col_template / palette;
    为空 = 固定输出, 用 label / canonical_col / color。
    """

    key: str
    type: OutputType = "line"
    expand_param: str | None = None
    # 固定输出
    label: str = ""
    color: str | None = None
    canonical_col: str | None = None
    # 展开输出
    label_template: str = ""
    canonical_template: str | None = None
    palette: tuple[str, ...] = ()
    # 别名列模板 (None = 不可参数化)
    col_template: str | None = None


@dataclass(frozen=True)
class ChartIndicator:
    """一个可渲染的图表指标。"""

    id: str
    label: str
    pane: Pane
    outputs: tuple[OutputSpec, ...]
    params: tuple[ParamSpec, ...] = ()
    y_axis: dict | None = None
    source: Literal["builtin", "ta"] = "builtin"
    special: str | None = None


_BUILTIN: tuple[ChartIndicator, ...] = (
    ChartIndicator(
        id="ma", label="均线 MA", pane="overlay",
        params=(ParamSpec("p", "周期", "list", (5, 10, 20, 60)),),
        outputs=(OutputSpec(
            key="p", type="line", expand_param="p",
            label_template="MA{p}", canonical_template="ma{p}", col_template="ma_{p}_c",
            palette=MA_PALETTE,
        ),),
    ),
    ChartIndicator(
        id="ema", label="均线 EMA", pane="overlay",
        params=(ParamSpec("p", "周期", "list", (5, 10, 20, 30, 60)),),
        outputs=(OutputSpec(
            key="p", type="line", expand_param="p",
            label_template="EMA{p}", canonical_template="ema{p}", col_template="ema_{p}_c",
            palette=MA_PALETTE,
        ),),
    ),
    ChartIndicator(
        id="boll", label="布林带 BOLL", pane="overlay",
        params=(ParamSpec("period", "周期", "scalar", (20,)), ParamSpec("std", "倍数", "scalar", (2,), min=1, max=10)),
        outputs=(
            OutputSpec(key="upper", label="BOLL上", color="#E879F9", canonical_col="boll_upper",
                       col_template="boll_{period}_{std}_upper_c"),
            OutputSpec(key="lower", label="BOLL下", color="#E879F9", canonical_col="boll_lower",
                       col_template="boll_{period}_{std}_lower_c"),
        ),
    ),
    ChartIndicator(
        id="macd", label="MACD", pane="panel",
        params=(ParamSpec("fast", "快线", "scalar", (12,)), ParamSpec("slow", "慢线", "scalar", (26,)),
                ParamSpec("signal", "信号", "scalar", (9,))),
        outputs=(
            OutputSpec(key="dif", label="DIF", color="#FACC15", canonical_col="macd_dif",
                       col_template="macd_{fast}_{slow}_{signal}_dif_c"),
            OutputSpec(key="dea", label="DEA", color="#8B5CF6", canonical_col="macd_dea",
                       col_template="macd_{fast}_{slow}_{signal}_dea_c"),
            OutputSpec(key="hist", label="MACD", color="#C74040", type="bar", canonical_col="macd_hist",
                       col_template="macd_{fast}_{slow}_{signal}_hist_c"),
        ),
    ),
    ChartIndicator(
        id="rsi", label="RSI", pane="panel", y_axis={"min": 0, "max": 100},
        params=(ParamSpec("p", "周期", "list", (6, 14, 24)),),
        outputs=(OutputSpec(
            key="p", type="line", expand_param="p",
            label_template="RSI{p}", canonical_template="rsi_{p}", col_template="rsi_{p}_c",
            palette=RSI_PALETTE,
        ),),
    ),
    ChartIndicator(
        id="kdj", label="KDJ", pane="panel",
        params=(ParamSpec("n", "周期", "scalar", (9,)),),
        outputs=(
            OutputSpec(key="k", label="K", color="#FACC15", canonical_col="kdj_k", col_template="kdj_{n}_k_c"),
            OutputSpec(key="d", label="D", color="#3B82F6", canonical_col="kdj_d", col_template="kdj_{n}_d_c"),
            OutputSpec(key="j", label="J", color="#8B5CF6", canonical_col="kdj_j", col_template="kdj_{n}_j_c"),
        ),
    ),
    ChartIndicator(
        id="vol", label="成交量", pane="panel", y_axis={"min": 0}, special="volume",
        outputs=(OutputSpec(key="vol", label="成交量", color="#8E8E96", type="bar", canonical_col="volume"),),
    ),
)


def _ta_catalog() -> tuple[ChartIndicator, ...]:
    """TA 描述符 (仅在可选 extra `ta` 安装时非空); 只引用 ta_* 列, 无参数。"""
    from app.factors import ta_factors

    if not ta_factors.available():
        return ()
    return (
        ChartIndicator(
            id="ta_adx", label="ADX", pane="panel", source="ta", y_axis={"min": 0, "max": 100},
            outputs=(OutputSpec(key="adx", label="ADX", color="#FACC15", canonical_col="ta_adx_14"),),
        ),
        ChartIndicator(
            id="ta_cci", label="CCI", pane="panel", source="ta",
            outputs=(OutputSpec(key="cci", label="CCI", color="#3B82F6", canonical_col="ta_cci_14"),),
        ),
        ChartIndicator(
            id="ta_willr", label="W%R", pane="panel", source="ta", y_axis={"min": -100, "max": 0},
            outputs=(OutputSpec(key="willr", label="W%R", color="#F97316", canonical_col="ta_willr_14"),),
        ),
        ChartIndicator(
            id="ta_stoch", label="STOCH", pane="panel", source="ta", y_axis={"min": 0, "max": 100},
            outputs=(
                OutputSpec(key="k", label="K", color="#FACC15", canonical_col="ta_stoch_k"),
                OutputSpec(key="d", label="D", color="#8B5CF6", canonical_col="ta_stoch_d"),
            ),
        ),
        ChartIndicator(
            id="ta_aroon", label="AROON", pane="panel", source="ta", y_axis={"min": 0, "max": 100},
            outputs=(
                OutputSpec(key="up", label="UP", color="#C74040", canonical_col="ta_aroon_up"),
                OutputSpec(key="down", label="DOWN", color="#2D9B65", canonical_col="ta_aroon_down"),
            ),
        ),
        ChartIndicator(
            id="ta_obv", label="OBV", pane="panel", source="ta",
            outputs=(OutputSpec(key="obv", label="OBV", color="#06B6D4", canonical_col="ta_obv"),),
        ),
        ChartIndicator(
            id="ta_supertrend", label="Supertrend", pane="overlay", source="ta",
            outputs=(OutputSpec(key="st", label="ST", color="#22C55E", canonical_col="ta_supertrend"),),
        ),
        ChartIndicator(
            id="ta_hma", label="HMA", pane="overlay", source="ta",
            outputs=(OutputSpec(key="hma", label="HMA", color="#EAB308", canonical_col="ta_hma_20"),),
        ),
    )


def all_indicators() -> tuple[ChartIndicator, ...]:
    """内置 + TA (TA 未安装时为空); 顺序即前端选择器顺序。"""
    return _BUILTIN + _ta_catalog()


def catalog() -> list[dict]:
    """只读目录 (供 /api/indicators/catalog); 每项含 available 供前端门控。"""
    ta_ok = True
    from app.factors import ta_factors

    ta_ok = ta_factors.available()
    out: list[dict] = []
    for ind in all_indicators():
        item = asdict(ind)
        item["available"] = ta_ok if ind.source == "ta" else True
        out.append(item)
    return out


def render_template(template: str, values: dict[str, object]) -> str:
    """把 ``ma{p}_c`` 之类的模板按参数值渲染 (与前端同一套占位规则)。"""
    return template.format(**values)
