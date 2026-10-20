"""Expose the public observation snapshot to Agent; private holdings stay local."""
import json

from src.agent.tools import BaseTool
from src.ashare.service import get_ashare_service


class AshareIntradayTool(BaseTool):
    name = "get_a_share_intraday"
    description = ("读取本地 A 股盘中监控快照：最新披露龙虎榜热门池、腾讯公开报价、时间戳、"
                   "行情质量、重点池新闻关联、原文依据、观察及失效条件。行业关联不等于公司订单，新闻摘要不等于事实核验。"
                   "休市报价只可复盘；不包含个人持仓/现金，不下单。"
                   "需在 A 股盯盘页面开启监控。不得将 stale/reference 报价称为实时买点。")
    parameters = {"type": "object", "properties": {}, "required": []}
    repeatable = True

    def execute(self, **kwargs) -> str:
        return json.dumps(get_ashare_service().public_snapshot(), ensure_ascii=False, allow_nan=False)
