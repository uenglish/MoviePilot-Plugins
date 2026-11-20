"""
NexusPHP站点邀请系统解析器基类
"""
import re
from abc import ABCMeta, abstractmethod
from typing import Dict, Optional, Any

import requests
from bs4 import BeautifulSoup
from urllib.parse import parse_qs, urljoin, urlsplit

from app.sdk.logging import logger


class _ISiteHandler(metaclass=ABCMeta):
    """
    站点邀请系统处理的基类，所有站点处理类都需要继承此类
    """
    # 站点类型标识
    site_schema = ""

    @classmethod
    @abstractmethod
    def match(cls, site_url: str) -> bool:
        """
        判断是否匹配该站点处理类
        :param site_url: 站点URL
        :return: 是否匹配
        """
        pass

    @abstractmethod
    def parse_invite_page(self, site_info: Dict[str, Any], session: requests.Session) -> Dict[str, Any]:
        """
        解析站点邀请页面
        :param site_info: 站点信息
        :param session: 已配置好的请求会话
        :return: 解析结果
        """
        pass

    @staticmethod
    def _user_id_from_html(html: str, site_url: str = '') -> Optional[str]:
        """Other users' links in threads or the online list are never identity evidence."""
        soup = BeautifulSoup(html or '', 'html.parser')
        if soup.select_one('input[type="password"]'):
            return None
        base = urlsplit(site_url)
        def own_link(link, page):
            url = urlsplit(urljoin(site_url, link.get('href') or ''))
            return (url.scheme, url.netloc) == (base.scheme, base.netloc) and url.path.rsplit('/', 1)[-1] == page
        panels = soup.select('#info_block, #info-bar, #user-info-panel, #userinfo, #user-info, .userinfo, .user-info, .user-menu, [data-user-menu]')
        for page in ('invite.php', 'userdetails.php'):
            for panel in panels:
                for link in panel.select('a[href]'):
                    if own_link(link, page):
                        value = parse_qs(urlsplit(link['href']).query).get('id', [''])[0]
                        if value.isascii() and value.isdecimal():
                            return value
        if any(own_link(link, 'logout.php') for link in soup.select('a[href]')):
            return ''
        return None

    @classmethod
    def _get_user_id(cls, session: requests.Session, site_url: str,
                     prefetched_html: Optional[str] = None) -> Optional[str]:
        account_id = cls._user_id_from_html(prefetched_html or '', site_url)
        if account_id is not None:
            return account_id
        try:
            response = session.get(urljoin(site_url, 'usercp.php'), timeout=(5, 15))
            response.raise_for_status()
            return cls._user_id_from_html(response.text, site_url)
        except requests.RequestException:
            return None

    @staticmethod
    def _convert_size_to_bytes(size_str: str) -> float:
        """
        将大小字符串转换为字节数
        :param size_str: 大小字符串
        :return: 字节数
        """
        if not size_str or size_str.strip() == '':
            logger.warning(f"空的大小字符串")
            return 0

        # 处理特殊情况
        if size_str.lower() == 'inf.' or size_str.lower() == 'inf' or size_str == '∞':
            logger.info(f"识别到无限大值: {size_str}")
            return 1e20  # 使用一个非常大的数值代替无穷大

        try:
            # 标准化字符串，替换逗号为点
            size_str = size_str.replace(',', '.')

            # 分离数字和单位
            # 正则表达式匹配数字部分和单位部分
            matches = re.match(
                r'([\d.]+)\s*([KMGTPEZY]?i?B)', size_str, re.IGNORECASE)

            if not matches:
                # 尝试匹配仅有数字的情况
                try:
                    return float(size_str)
                except ValueError:
                    logger.warning(f"无法解析大小字符串: {size_str}")
                    return 0

            size_num, unit = matches.groups()

            # 尝试转换数字
            try:
                size_value = float(size_num)
            except ValueError:
                logger.warning(f"无法转换大小值为浮点数: {size_num}")
                return 0

            # 单位转换
            unit = unit.upper()

            units = {
                'B': 1,
                'KB': 1024,
                'KIB': 1024,
                'MB': 1024 ** 2,
                'MIB': 1024 ** 2,
                'GB': 1024 ** 3,
                'GIB': 1024 ** 3,
                'TB': 1024 ** 4,
                'TIB': 1024 ** 4,
                'PB': 1024 ** 5,
                'PIB': 1024 ** 5,
                'EB': 1024 ** 6,
                'EIB': 1024 ** 6,
                'ZB': 1024 ** 7,
                'ZIB': 1024 ** 7,
                'YB': 1024 ** 8,
                'YIB': 1024 ** 8
            }

            # 处理简写单位
            if unit in ['K', 'M', 'G', 'T', 'P', 'E', 'Z', 'Y']:
                unit = unit + 'B'

            if unit not in units:
                logger.warning(f"未知的大小单位: {unit}")
                return size_value  # 假设是字节

            return size_value * units[unit]

        except Exception as e:
            logger.warning(f"转换大小字符串到字节时出错 '{size_str}': {str(e)}")
            return 0

    @staticmethod
    def _calculate_ratio(uploaded: str, downloaded: str) -> str:
        """
        计算分享率
        :param uploaded: 上传量
        :param downloaded: 下载量
        :return: 分享率字符串
        """
        try:
            up_bytes = _ISiteHandler._convert_size_to_bytes(uploaded)
            down_bytes = _ISiteHandler._convert_size_to_bytes(downloaded)

            if down_bytes == 0:
                return "∞" if up_bytes > 0 else "0"

            ratio = up_bytes / down_bytes
            return f"{ratio:.3f}"
        except Exception as e:
            logger.error(f"计算分享率失败: {str(e)}")
            return "0"
