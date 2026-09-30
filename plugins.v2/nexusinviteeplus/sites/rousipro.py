"""Rousi Pro 处理器：站点使用个人 API Key（api-token 请求头），没有 Cookie 登录态。

站点未提供邀请/后宫数据接口，这里用签到接口校验 API Key 是否有效，
并明确告知邀请信息需在站点内查看，避免误报「站点信息不完整」。
"""
from typing import Any, Dict

from app.log import logger

from . import _ISiteHandler


class RousiProHandler(_ISiteHandler):
    site_schema = "rousipro"
    _api_host = "https://rousi.pro"
    _verify_api = "/api/points/attendance"

    @classmethod
    def match(cls, url: str) -> bool:
        return "rousi.pro" in (url or "").lower()

    def parse_invite_page(self, site_info: Dict[str, Any], session) -> Dict[str, Any]:
        site_name = site_info.get("name", "")
        result = {
            "invite_status": {
                "can_invite": False,
                "reason": "",
                "permanent_count": 0,
                "temporary_count": 0,
                "bonus": 0,
                "permanent_invite_price": 0,
                "temporary_invite_price": 0,
            },
            "invitees": [],
        }
        apikey = str(site_info.get("apikey") or "").strip()
        if not apikey:
            reason = "站点未配置 API Key（个人 API 密钥）"
            result["invite_status"]["reason"] = reason
            result["error"] = reason
            return result

        try:
            response = session.post(self._api_host + self._verify_api,
                                    headers={"api-token": apikey,
                                             "Content-Type": "application/json",
                                             "Accept": "application/json",
                                             "Origin": self._api_host,
                                             "Referer": self._api_host + "/"},
                                    json={}, timeout=(10, 30))
        except Exception as err:
            reason = f"访问站点 API 失败: {str(err)[:80]}"
            logger.error(f"站点 {site_name} {reason}")
            result["invite_status"]["reason"] = reason
            result["error"] = reason
            return result

        text = (response.text or "").strip()
        if response.status_code in (401, 403):
            reason = "API Key 无效或已过期，请在站点重新生成"
        elif response.status_code == 404:
            reason = "站点 API 地址不可用（请在站点内确认接口变更）"
        elif not text.startswith("{"):
            reason = f"API 返回异常（HTTP {response.status_code}）"
        else:
            # 接口返回业务码即说明鉴权通过（例如“今日已签到”）
            result["invite_status"]["reason"] = "API Key 校验通过；站点未提供邀请数据接口，邀请信息请在站点内查看"
            logger.info(f"站点 {site_name} API Key 校验通过：{text[:60]}")
            return result

        logger.error(f"站点 {site_name} {reason}")
        result["invite_status"]["reason"] = reason
        result["error"] = reason
        return result
