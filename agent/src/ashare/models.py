"""Validated operator input for the local A-share workbench."""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


def normalize_symbol(value: str) -> str:
    value = value.strip().upper()
    if re.fullmatch(r"(?:SH|SZ|BJ)\d{6}", value):
        value = value[2:] + "." + value[:2]
    if re.fullmatch(r"\d{6}", value):
        suffix = "SH" if value.startswith("6") else "BJ" if value.startswith(("4", "8", "92")) else "SZ"
        value += "." + suffix
    if not re.fullmatch(r"(?:6\d{5}\.SH|[03]\d{5}\.SZ|(?:[48]\d{5}|92\d{4})\.BJ)", value):
        raise ValueError("请输入沪深京 A 股代码，例如 000001.SZ；不接受指数、基金或任意 URL")
    return value


Symbol = Annotated[str, AfterValidator(normalize_symbol)]


class Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class WatchSettings(Inputs):
    enabled: bool = True
    recurring_news_enabled: bool = False
    market_news_enabled: bool = False
    poll_seconds: int = Field(30, ge=30, le=300, strict=True)
    pool_size: int = Field(30, ge=5, le=60, strict=True)
    watchlist: list[Symbol] = Field(default_factory=lambda: ["000001.SZ"], max_length=50)
    max_single_position_pct: float = Field(20, gt=0, le=30)
    warning_drawdown_pct: float = Field(3, gt=0, lt=100)
    restrict_drawdown_pct: float = Field(4, gt=0, lt=100)
    max_drawdown_pct: float = Field(5, gt=0, lt=100)

    @model_validator(mode="after")
    def unique_watchlist(self):
        self.watchlist = list(dict.fromkeys(self.watchlist))
        if not self.warning_drawdown_pct < self.restrict_drawdown_pct < self.max_drawdown_pct:
            raise ValueError("回撤阈值必须满足：0 < 预警 < 暂停新增 < 最大回撤阈值 < 100%")
        return self


class Holding(Inputs):
    symbol: Symbol
    quantity: int = Field(ge=1, strict=True)
    available_quantity: int = Field(ge=0, strict=True)
    cost_price: float | None = Field(None, gt=0)

    @model_validator(mode="after")
    def available_not_above_quantity(self):
        if self.available_quantity > self.quantity:
            raise ValueError("可卖股数不能超过持仓股数")
        return self


class AccountInput(Inputs):
    submission_id: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    cash: float = Field(ge=0)
    positions: list[Holding] = Field(default_factory=list, max_length=100)
    historical_peak_equity: float = Field(gt=0)
    peak_as_of: datetime
    # Must include deposits (+) / withdrawals (-), never trading proceeds.
    external_flow: float = 0

    @model_validator(mode="after")
    def unique_holdings(self):
        symbols = [p.symbol for p in self.positions]
        if len(symbols) != len(set(symbols)):
            raise ValueError("同一股票只能录入一行，请合并持仓")
        if self.peak_as_of.tzinfo is None:
            raise ValueError("净值高点时间必须带时区")
        return self


class AckAlerts(Inputs):
    ids: list[str] = Field(max_length=200)


class NewsMonitoringInput(Inputs):
    enabled: bool
    market_wide: bool | None = None


class RiskPeriodInput(Inputs):
    submission_id: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    expected_account_submission_id: str = Field(min_length=8, max_length=80)
    confirm_account_complete: Literal[True]


class ResearchPublishInput(Inputs):
    expected_hash: str = Field(pattern=r'^[a-f0-9]{64}$')


class PaperStartInput(Inputs):
    expected_plan_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    initial_cash: float = Field(default=900000, ge=1000, le=100000000)


class PaperStatusInput(Inputs):
    enabled: bool


class PaperRotationInput(Inputs):
    mode: Literal['notify','confirm','auto']
    expected_plan_hash: str = Field(pattern=r'^[a-f0-9]{64}$')


class PaperCandidateInput(Inputs):
    symbol: Symbol
    source_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    action: Literal['review','confirm']


class PriceRules(Inputs):
    breakout: float = Field(gt=0)
    hold: float = Field(gt=0)
    invalid_below: float = Field(gt=0)
    strong_invalid_below: float | None = Field(None, gt=0)
    max_vwap_deviation_pct: float = Field(2, gt=0, le=2)
    breakout_volume_ratio: float = Field(1.5, ge=1.5)
    retest_tolerance_pct: float = Field(0.2, ge=0, le=0.5)

    @model_validator(mode="after")
    def ordered_levels(self):
        if not self.invalid_below <= self.hold <= self.breakout:
            raise ValueError("失效位、回踩承接位、突破位顺序无效")
        if self.strong_invalid_below and self.strong_invalid_below > self.invalid_below:
            raise ValueError("强失效位不能高于失效位")
        return self


class FocusStock(Inputs):
    symbol: Symbol
    name: str = Field(min_length=1, max_length=40)
    reference_close: float = Field(gt=0)
    reference_vwap: float = Field(gt=0)
    disclosure_status: Literal["pending", "reviewed", "caution", "blocked"] = "pending"
    disclosure_summary: str = Field("待核验公告", max_length=1800)
    reviewed_ids: list[str] = Field(default_factory=list, max_length=500)
    source_urls: list[str] = Field(default_factory=list, max_length=30)
    reason: str = Field("", max_length=1200)
    risk_points: list[str] = Field(default_factory=list, max_length=20)
    catalyst_published_at: datetime | None = None
    rules: PriceRules | None = None


class FocusPlan(Inputs):
    enabled: bool = True
    news_enabled: bool = False
    session_date: date
    reference_date: date
    review_since: date
    reviewed_at: datetime
    stocks: list[FocusStock] = Field(min_length=1, max_length=20)
    max_open_gap_pct: float = Field(3, gt=0, le=7)
    plan_id: str | None = Field(None, pattern=r"^[a-z0-9-]{8,80}$")
    version: int = Field(1, ge=1, strict=True)
    rules_version: Literal["legacy-v1", "breakout-retest-m5-v1"] = "legacy-v1"
    research_cutoff: datetime | None = None
    published_at: datetime | None = None
    content_hash: str | None = Field(None, pattern=r"^[a-f0-9]{64}$")
    screening: dict | None = None

    @model_validator(mode="after")
    def valid_plan(self):
        if self.reference_date >= self.session_date or self.review_since > self.reference_date:
            raise ValueError("重点观察计划日期范围无效")
        if self.reviewed_at.tzinfo is None:
            raise ValueError("公告核验时间必须带时区")
        if len({p.symbol for p in self.stocks}) != len(self.stocks):
            raise ValueError("重点股票不能重复")
        if self.plan_id:
            import re
            if self.rules_version != "breakout-retest-m5-v1" or not self.research_cutoff or self.research_cutoff.tzinfo is None:
                raise ValueError("发布计划必须包含规则版本与带时区的研究截止时间")
            if not all(s.rules and re.fullmatch(r"(?:60[0135]\d{3}\.SH|00[0123]\d{3}\.SZ)", s.symbol) for s in self.stocks):
                raise ValueError("发布范围只允许有结构化观察条件的沪深主板股票")
            if self.research_cutoff > self.reviewed_at:
                raise ValueError("研究截止时间不能晚于审阅时间")
        if self.screening is not None:
            from .screening import validate_audit
            try:
                audit = validate_audit(self.screening)
            except (KeyError, TypeError, AttributeError) as exc:
                raise ValueError('冻结研究结构无效') from exc
            if (not self.plan_id or audit['target_date'] != str(self.session_date)
                or audit['reference_date'] != str(self.reference_date)
                or datetime.fromisoformat(audit['cutoff']) != self.research_cutoff
                or audit['selected_symbols'] != [s.symbol for s in self.stocks]):
                raise ValueError('观察计划必须与冻结研究的日期、截止时间和入选顺序一致')
        for stamp in (self.published_at, *(s.catalyst_published_at for s in self.stocks)):
            if stamp and stamp.tzinfo is None:
                raise ValueError("计划来源时间必须带时区")
        return self
