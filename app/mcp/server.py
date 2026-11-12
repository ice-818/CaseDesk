"""Standalone demo MCP server. stdout is reserved for protocol messages."""

from typing import Literal

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("CaseDesk store hours")


@mcp.tool()
def mcp_store_hours(day: Literal["all", "weekday", "weekend"] = "all") -> dict:
    """查询客服营业时间与夜间退款审核（演示数据）。

    客服几点上班、周末时段、夜间退款会不会审用本工具。
    仓库截单用 mcp_warehouse_cutoff；退款规则用 search_knowledge；
    退款进度用 get_refund_status。day 可省略，默认返回全部时段。
    night_refund_review=false 表示夜间不审退款，不要承诺夜间立即退款。
    """
    result = {
        "weekday": "09:00-21:00",
        "weekend": "10:00-18:00",
        "night_refund_review": False,
        "source": "mcp_stdio_demo",
    }
    if day != "all":
        result["hours"] = result[day]
    return result


if __name__ == "__main__":
    mcp.run(transport="stdio")
