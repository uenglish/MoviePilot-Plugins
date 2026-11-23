"""
NexusPHP站点邀请系统管理插件
"""
import pytz
import os
import re
import json
import time
import threading
from typing import Any, List, Dict, Tuple, Optional
from datetime import datetime, timedelta
import traceback

import requests
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.plugins.ptinvitekeeper.host_compat import _PluginBase
from app.log import logger
from app.schemas import Response
from app.schemas.types import NotificationType, EventType
from app.db.site_oper import SiteOper
from app.helper.sites import SitesHelper

from .data import DataManager
from .migration import merge_config, merge_data
from .identity_migration import migrate_identity
from .utils import NotificationHelper, SiteHelper
from .module_loader import ModuleLoader
from .parsing import sanitize_invitees
from .site_access import (
    COOKIE_EXPIRED_REASON,
    CF_CHALLENGE_REASON as site_access_cf_reason,
    has_user_id as site_access_has_user_id,
    classify as classify_site_response,
    detect_schema,
    normalize_cookie,
    normalize_ua,
    swap_www,
)
from . import categories as site_categories
from . import trend as site_trend
from . import ui_extra
from . import ui, ui_actions
from .notification import build_refresh_content, notification_plain_text

# MoviePilot V2/V3 的插件完整模块名不同，动态重载必须使用运行时包名。
_PLUGIN_PACKAGE = __package__ or __name__.rsplit('.', 1)[0]

class Prescription():
    def __init__(self):
        self._cache = {}

    def _tag(self,site_name,key,value):
        if site_name not in self._cache:
            self._cache[site_name] = {}
        self._cache[site_name][key] = value
    def setP(self,site_name,value):
        self._tag(site_name,"p",value)
    def setT(self,site_name,value):
        self._tag(site_name,"t",value)
    def setCBP(self,site_name,value):
        self._tag(site_name,"cbp",value)
    def setCBT(self,site_name,value):
        self._tag(site_name,"cbt",value)
    def setCanInvite(self,site_name,value):
        self._tag(site_name,"can_invite",value)
    def setMTBuyable(self,site_name,value):
        self._tag(site_name,"mt_buyable",value)
    def setFailed(self,site_name,value):
        self._tag(site_name,"error",value or "获取失败")

    def _export(self):
        med_list = []
        failed_list = []
        total_remain = 0
        total_can_buy = 0

        for k in self._cache:
            site_name = k
            site_error = self._cache[k].get('error')
            if site_error:
                failed_list.append({
                    "site": site_name,
                    "remain": 0,
                    "can_buy": 0,
                    "error": str(site_error)
                })
                continue

            site_remain = self._cache[k].get('p', 0) + self._cache[k].get('t', 0)
            # 合并普通可购买和MT可购买的数量
            site_can_buy = 0
            if 'cbp' in self._cache[k]:
                site_can_buy += self._cache[k].get("cbp", 0) + self._cache[k].get("cbt", 0)
            if 'mt_buyable' in self._cache[k]:
                site_can_buy += self._cache[k].get("mt_buyable", 0)

            if site_remain + site_can_buy > 0:
                site_content = {
                    "site": site_name,
                    "remain": site_remain,
                    "can_buy": site_can_buy,
                    "error": ""
                }
                med_list.append(site_content)
                total_remain += site_remain
                total_can_buy += site_can_buy

        return {
            "total": {
                "remain": total_remain,
                "can_buy": total_can_buy,
                "failed": len(failed_list)
            },
            "details": (
                sorted(med_list, key=lambda x: (-x['remain'], -x['can_buy'], x['site'])) +
                sorted(failed_list, key=lambda x: x['site'])
            )
        }

    def getExportText(self):
        med_data = self._export()
        med_text = ""
        for site in med_data["details"]:
            if site.get("error"):
                med_text += f"站点[{site['site']}]: 获取失败[{site['error']}]\r\n"
            else:
                med_text += f"站点[{site['site']}]: 剩余[{site['remain']}]个. 可购买[{site['can_buy']}]个\r\n"
        return med_text

    def getComponent(self):
        med_data = self._export()
        if not med_data["details"]:
            return None

        # 生成药单内容字符串 - 保持统一的格式
        med_text = self.getExportText()

        # 使用 json.dumps 来安全地将 Python 字符串嵌入 JS 字符串
        js_safe_med_text = json.dumps(med_text)

        # 构建健壮的 onclick JavaScript 代码
        onclick_js = f"""
        (function(button) {{
            button.disabled = true; // 禁用按钮防止重复点击
            const originalText = button.textContent;
            const textToCopy = {js_safe_med_text};
            navigator.clipboard.writeText(textToCopy).then(() => {{
                button.textContent = '已复制';
                setTimeout(() => {{ button.textContent = originalText; button.disabled = false; }}, 1500);
            }}).catch(err => {{
                console.error('Clipboard API 失败:', err);
                const textArea = document.createElement('textarea');
                textArea.value = textToCopy;
                textArea.style.position = 'fixed'; textArea.style.left = '-9999px';
                document.body.appendChild(textArea);
                textArea.focus(); textArea.select();
                try {{
                    const successful = document.execCommand('copy');
                    if (successful) {{
                        button.textContent = '已复制(FB)';
                        setTimeout(() => {{ button.textContent = originalText; button.disabled = false; }}, 1500);
                    }} else {{
                        alert('复制失败: execCommand 未成功'); button.disabled = false;
                    }}
                }} catch (err) {{
                    console.error('Fallback 复制失败:', err);
                    alert('复制失败: ' + err); button.disabled = false;
                }}
                document.body.removeChild(textArea);
            }});
        }})(this);
        """

        # 生成药单容器
        return {
            "component": "VCard",
            "props": {
                "variant": "flat",
                "class": "mt-4"
            },
            "content": [
                {
                    "component": "VCardItem",
                    "content": [
                        {
                            "component": "VCardTitle",
                            "props": {
                                "class": "text-h6"
                            },
                            "text": "药单信息"
                        }
                    ]
                },
                {
                    "component": "VCardText",
                    "content": [
                        {
                            "component": "VRow",
                            "props": {
                                "justify": "space-around",
                                "align": "center",
                                "class": "mb-2",
                                "dense": True
                            },
                            "content": [
                                {
                                    "component": "VCol",
                                    "props": {
                                        "cols": "auto"
                                    },
                                    "content": [
                                        {
                                            "component": "VChip",
                                            "props": {
                                                "color": "primary",
                                                "variant": "flat",
                                                "size": "default",
                                                "prepend-icon": "mdi-package-variant-closed"
                                            },
                                            "text": f"总剩余: {med_data['total']['remain']}"
                                        }
                                    ]
                                },
                                {
                                    "component": "VCol",
                                    "props": {
                                        "cols": "auto"
                                    },
                                    "content": [
                                        {
                                            "component": "VChip",
                                            "props": {
                                                "color": "success",
                                                "variant": "flat",
                                                "size": "default",
                                                "prepend-icon": "mdi-cart-plus"
                                            },
                                            "text": f"总可购买: {med_data['total']['can_buy']}"
                                        }
                                    ]
                                },
                                {
                                    "component": "VCol",
                                    "props": {
                                        "cols": "auto"
                                    },
                                    "content": [
                                        {
                                            "component": "VBtn",
                                            "props": {
                                                "color": "primary",
                                                "size": "default",
                                                "variant": "tonal",
                                                "prepend-icon": "mdi-content-copy",
                                                "onclick": onclick_js
                                            },
                                            "text": "复制药单"
                                        }
                                    ]
                                }
                            ]
                        },
                        {
                            "component": "VExpansionPanels",
                            "props": {
                                "variant": "accordion",
                                "class": "mt-2"
                            },
                            "content": [
                                {
                                    "component": "VExpansionPanel",
                                    "content": [
                                        {
                                            "component": "VExpansionPanelTitle",
                                            # 模仿站点卡片样式添加图标
                                            "content": [
                                                {
                                                    "component": "VIcon",
                                                    "props": {
                                                        "start": True,
                                                        "icon": "mdi-pill",
                                                        "color": "blue-grey"
                                                    }
                                                },
                                                {
                                                    "component": "span",
                                                    "text": "药单详情"
                                                }
                                            ]
                                        },
                                        {
                                            "component": "VExpansionPanelText",
                                            # 移除内边距以使表格更紧凑
                                            "props": {
                                                "class": "pa-0"
                                            },
                                            "content": [
                                                {
                                                    "component": "VTable",
                                                    "props": {
                                                        "density": "compact",
                                                        "hover": True
                                                    },
                                                    "content": [
                                                        {
                                                            "component": "thead",
                                                            "content": [
                                                                {
                                                                    "component": "tr",
                                                                    "content": [
                                                                        {
                                                                            "component": "th",
                                                                            "text": "站点"
                                                                        },
                                                                        {
                                                                            "component": "th",
                                                                            "text": "剩余"
                                                                        },
                                                                        {
                                                                            "component": "th",
                                                                            "text": "可购买"
                                                                        }
                                                                    ]
                                                                }
                                                            ]
                                                        },
                                                        {
                                                            "component": "tbody",
                                                            "content": [
                                                                {
                                                                    "component": "tr",
                                                                    "content": [
                                                                        {
                                                                            "component": "td",
                                                                            "text": site["site"]
                                                                        },
                                                                        {
                                                                            "component": "td",
                                                                            "text": "获取失败" if site.get("error") else str(site["remain"])
                                                                        },
                                                                        {
                                                                            "component": "td",
                                                                            "text": site.get("error") or str(site["can_buy"])
                                                                        }
                                                                    ]
                                                                } for site in med_data["details"]
                                                            ]
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]
        }

def get_nested_value(data_dict: dict, key_path: List[str], default: Any = None) -> Any:
    """
    递归获取嵌套字典中的值
    :param data_dict: 要查询的字典
    :param key_path: 键路径列表
    :param default: 默认返回值
    :return: 找到的值或默认值
    """
    if not data_dict or not isinstance(data_dict, dict):
        return default

    if len(key_path) == 1:
        return data_dict.get(key_path[0], default)

    next_dict = data_dict.get(key_path[0], {})
    return get_nested_value(next_dict, key_path[1:], default)


def get_site_error(cache: dict) -> str:
    """
    从缓存包装或站点数据中提取刷新失败信息。
    """
    if not cache or not isinstance(cache, dict):
        return ""

    candidates = [cache]
    data = cache.get("data", {})
    if isinstance(data, dict):
        candidates.append(data)
        nested_data = data.get("data", {})
        if isinstance(nested_data, dict):
            candidates.append(nested_data)

    for candidate in candidates:
        for key in ("error", "fetch_error"):
            error = candidate.get(key)
            if error:
                return str(error)

        if candidate.get("fetch_failed"):
            invite_status = candidate.get("invite_status", {})
            if isinstance(invite_status, dict):
                reason = invite_status.get("reason")
                if reason:
                    return str(reason)
            return "获取失败"

    return ""


class PTInviteKeeper(_PluginBase):
    # 插件名称
    plugin_name = "PT 邀请管家"
    # 插件描述
    plugin_desc = "PT 邀请资格、名额和成员健康管理，支持分类筛选、历史查询和异常重试。"
    # 插件图标
    plugin_icon = "../api/v1/plugin/file/PTInviteKeeper/assets/pt-invite-keeper-v1.webp"
    # 插件版本
    plugin_version = "1.4.0"
    # 插件作者
    plugin_author = "serendipity"
    # 作者主页
    author_url = "https://github.com/uenglish"
    # 插件配置项ID前缀
    plugin_config_prefix = "ptinvitekeeper_"
    # 加载顺序
    plugin_order = 21
    # 可使用的用户级别
    auth_level = 2

    # 旧插件ID（madrays 的 nexusinvitee）：改名后自动迁移配置与历史数据
    _legacy_plugin_id = "nexusinvitee"

    # 详情页站点名关键词过滤（服务端过滤，相当于页面搜索）
    _page_filter = ""

    # 不使用浏览器仿真的站点（如站点明确禁止仿真登录）
    _no_browser_sites: list = []

    # 站点选择中的「全部」标记：持久化后，后续新增站点会自动纳入，无需重新保存配置
    _ALL_SITES = "all"

    # 私有属性
    _enabled = False
    _notify = False
    _cron = "0 9 * * *"  # 默认每天早上9点检查一次
    _onlyonce = False
    _nexus_sites = []  # 支持多选的站点列表（含 "all" 全部标记）
    _all_sites: bool = False  # 是否选择了「全部（含后续新增站点）」

    # 站点助手
    sites: SitesHelper = None
    siteoper: SiteOper = None

    # 配置和数据管理器
    data_manager: DataManager = None

    # 通知助手
    notify_helper: NotificationHelper = None

    # 站点处理器列表
    _site_handlers = []

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    presc : Prescription = None

    def __browser_disabled(self, site_name: str) -> bool:
        """站点是否配置为不使用浏览器仿真。"""
        return site_name in (self._no_browser_sites or [])

    def __site_priorities(self) -> dict:
        """站点优先级映射：沿用 MoviePilot 站点列表顺序（越靠前优先级越高）。"""
        try:
            return {site.get("name"): idx for idx, site in enumerate(self.sites.get_indexers())}
        except Exception as err:
            logger.error(f"读取站点优先级失败: {str(err)}")
            return {}

    def __init__(self):
        """初始化独立的刷新锁，统一定时、手动与 API 的执行入口。"""
        super().__init__()
        self._refresh_lock = threading.Lock()
        self._extra_config = {}

    def __migrate_legacy(self, config):
        """合并普通版配置与文件，保留增强版身份和原始历史归档。"""
        current = config if config is not None else super().get_config()
        legacy = super().get_config(plugin_id=self._legacy_plugin_id) or {}
        # 兼容还未迁入 V3 插件实例表的旧配置。
        if not legacy:
            legacy = self.systemconfig.get(f"plugin.{self._legacy_plugin_id}") or {}
        if current is None:
            current = self.systemconfig.get(f"plugin.{self.__class__.__name__}") or {}
        try:
            merge_data(self.get_data_path(), self.get_data_path(plugin_id=self._legacy_plugin_id))
            return merge_config(current, legacy)
        except Exception as err:
            logger.error(f"合并旧插件数据失败，原数据已保留：{err}")
            return dict(current or {})

    def init_plugin(self, config=None):
        """
        插件初始化
        """
        # 动态重载模块
        self.__reload_modules()

        # 兼容插件改名：继承旧插件的配置与数据（无配置时使用迁移结果）
        config = migrate_identity(self, config, "NexusInviteePlus")
        config = self.__migrate_legacy(config)
        self._extra_config = dict(config)

        self.sites = SitesHelper()
        self.siteoper = SiteOper()
        self.presc = Prescription()

        # 获取数据目录
        data_path = self.get_data_path()

        # 确保目录存在
        if not os.path.exists(data_path):
            try:
                os.makedirs(data_path)
            except Exception as e:
                logger.error(f"创建数据目录失败: {str(e)}")

        # 初始化数据管理器（仅保留数据存储，移除配置存储）
        self.data_manager = DataManager(data_path)

        # 初始化通知助手
        self.notify_helper = NotificationHelper(self)

        # 加载站点处理器
        self._site_handlers = ModuleLoader.load_site_handlers()
        logger.info(f"加载了 {len(self._site_handlers)} 个站点处理器")

        # 停止现有服务
        self.stop_service()

        # 处理传入的配置参数
        if config:
            self._enabled = config.get("enabled", False)
            self._notify = config.get("notify", False)
            self._cron = config.get("cron", "0 9 * * *")
            self._onlyonce = config.get("onlyonce", False)

            # 详情页关键词过滤
            self._page_filter = (config.get("page_filter") or "").strip()

            # 不使用浏览器仿真的站点名单
            raw_no_browser = config.get("no_browser_sites") or []
            if isinstance(raw_no_browser, str):
                raw_no_browser = [x.strip() for x in raw_no_browser.replace("，", ",").split(",")]
            self._no_browser_sites = [str(x).strip() for x in raw_no_browser if str(x).strip()]

            # 处理站点ID
            raw_site_ids = config.get("site_ids") or []
            self._all_sites = any(str(item).lower() == self._ALL_SITES for item in raw_site_ids)
            self._nexus_sites = []
            for site_id in raw_site_ids:
                try:
                    if isinstance(site_id, str) and site_id.isdigit():
                        self._nexus_sites.append(int(site_id))
                    elif isinstance(site_id, int):
                        self._nexus_sites.append(site_id)
                except Exception:
                    pass
            if self._all_sites:
                # 保留「全部」标记本身，执行时展开为所有站点（含后续新增）
                self._nexus_sites = [self._ALL_SITES]
            # 保存配置
            self.__update_config()

        # 如果启用了插件
        if self._enabled:
            # 检查是否配置了站点
            if self._all_sites:
                logger.info("站点选择为「全部（含后续新增站点）」，后续新增站点会自动纳入")
            elif not self._nexus_sites:
                logger.info("未选择任何站点，将使用所有站点")
            else:
                logger.info(f"PT 邀请管家初始化完成，已选择 {len(self._nexus_sites)} 个站点")

        # 立即运行一次
        if self._onlyonce:
            try:
                # 定时服务
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.debug("立即运行一次开关已开启，将在3秒后执行刷新")
                self._scheduler.add_job(func=self.refresh_all_sites, trigger='date',
                                      run_date=datetime.now(pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                      name="PT 邀请管家")

                # 关闭一次性开关
                self._onlyonce = False
                # 保存配置
                self.__update_config()

                # 启动任务
                if self._scheduler and self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()
            except Exception as e:
                logger.error(f"启动一次性任务失败: {str(e)}")

    def __reload_modules(self):
        """
        动态重载所有模块，确保更新时能加载最新代码
        """
        try:
            import sys
            import importlib

            # 记录开始重载
            logger.debug("PT 邀请管家开始动态重载模块...")

            # 1. 清理模块缓存 - 从sys.modules中删除相关模块
            modules_to_reload = []
            package_name = _PLUGIN_PACKAGE
            for module_name in list(sys.modules.keys()):
                if module_name.startswith(f'{package_name}.') and module_name != package_name:
                    modules_to_reload.append(module_name)
                    # 从sys.modules中删除模块以强制重新导入
                    del sys.modules[module_name]
                    logger.info(f"从缓存中移除模块: {module_name}")

            # 2. 重新导入核心模块
            logger.debug("重新导入核心模块...")
            importlib.import_module(f'{package_name}.data')
            importlib.import_module(f'{package_name}.utils')
            importlib.import_module(f'{package_name}.module_loader')

            # 3. 更新全局引用以确保使用的是最新版本
            logger.debug("更新全局模块引用...")
            global DataManager, NotificationHelper, ModuleLoader
            try:
                from .data import DataManager
                from .utils import NotificationHelper
                from .module_loader import ModuleLoader
                logger.debug("核心模块引用更新成功")
            except Exception as e:
                logger.error(f"更新核心模块引用失败: {str(e)}")

            # 记录完成信息
            logger.info(f"PT 邀请管家成功重载 {len(modules_to_reload)} 个模块")
        except Exception as e:
            logger.error(f"动态重载模块失败: {str(e)}")
            import traceback
            logger.error(f"错误详情: {traceback.format_exc()}")

    def __update_config(self):
        """
        更新配置到MoviePilot系统
        """
        # 保存配置到MP
        config = {
            **self._extra_config,
            "enabled": self._enabled,
            "notify": self._notify,
            "cron": self._cron,
            "onlyonce": self._onlyonce,
            "site_ids": self._nexus_sites,
            "page_filter": self._page_filter,
            "no_browser_sites": self._no_browser_sites
        }
        # 使用父类的update_config方法而不是自己的方法，避免递归
        super().update_config(config)

    def get_state(self) -> bool:
        """
        获取插件状态
        """
        return self._enabled

    def get_command(self) -> List[Dict[str, Any]]:
        """
        注册插件命令
        """
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API
        """
        return [{
            "path": "/config",
            "endpoint": self.api_get_config,
            "methods": ["GET"],
            "summary": "获取配置",
            "description": "获取PT 邀请管家配置数据",
        }, {
            "path": "/update_config",
            "endpoint": self.api_update_config,
            "methods": ["POST"],
            "summary": "更新配置",
            "description": "更新PT 邀请管家配置数据",
        }, {
            "path": "/get_invitees",
            "endpoint": self.get_invitees,
            "methods": ["GET"],
            "summary": "获取被邀请人列表",
            "description": "获取所有站点的被邀请人列表及状态",
        }, {
            "path": "/refresh_data",
            "endpoint": self.refresh_data,
            "methods": ["GET"],
            "summary": "刷新数据",
            "description": "强制刷新所有站点数据",
        }, {
            "path": "/refresh_now",
            "endpoint": self.refresh_now,
            "methods": ["GET"],
            "summary": "立即刷新",
            "description": "后台立即刷新站点数据，scope 支持 all/problem/stale",
        }, {
            "path": "/export_data",
            "endpoint": self.export_data,
            "methods": ["GET"],
            "summary": "导出数据",
            "description": "导出后宫数据为 CSV/JSON",
        }] + self._ui_routes()

    def get_dashboard_meta(self) -> Optional[List[Dict[str, str]]]:
        """
        获取插件仪表盘元信息
        """
        return [{
            "key": "ptinvitekeeper_dashboard",
            "name": "PT 邀请管家"
        }]

    def get_dashboard(self, key: str, **kwargs):
        if key != 'ptinvitekeeper_dashboard':
            return None
        return ({'cols': 12, 'md': 6}, {'refresh': 3600, 'title': '邀请管理'}, [])


    @staticmethod
    def get_render_mode() -> Tuple[str, str]:
        return 'vue', 'dist/assets'

    def get_sidebar_nav(self) -> List[Dict[str, Any]]:
        """只注册插件主入口，设置由页面内部打开。"""
        return [{'nav_key': 'main', 'title': 'PT 邀请管家', 'icon': 'mdi-email-plus-outline',
                 'section': 'organize', 'permission': 'manage', 'order': 42}]

    def _ui_routes(self):
        routes = [('/data', self.ui_data, 'GET'), ('/detail', self.ui_detail, 'GET'),
                  ('/options', self.ui_options, 'GET'), ('/export', self.ui_export, 'GET'),
                  ('/run', self.ui_run, 'POST'),
                  ('/notification-preview', self.ui_notification_preview, 'POST')]
        routes.append(('/overview', self.ui_overview, 'GET'))
        return [{'path': '/ui' + path, 'endpoint': endpoint, 'methods': [method],
                 'auth': 'bear', 'summary': '增强版 ' + path[1:], 'response_model': dict}
                for path, endpoint, method in routes]

    def ui_notification_preview(self) -> dict:
        from .notification import send_preview
        return send_preview(self)

    def ui_run(self, scope: str = 'pending', site: str = '') -> dict:
        result = ui_actions.start_run(self, 'invitee', scope=scope, site=site)
        return {**result, 'data': {'message': result['message'], 'task': dict(getattr(self, '_ui_task', {}))}}

    def ui_data(self, query: str = '', category: str = 'all', sort: str = 'priority',
                page: int = 1, page_size: int = 20) -> dict:
        result = ui.list_sites(self, query=query, category=category, sort=sort, page=page, page_size=page_size)
        return {**result, 'task': dict(getattr(self, '_ui_task', {})), 'default_query': self._page_filter or ''}

    def ui_detail(self, site: str, member_query: str = '', member_status: str = 'all',
                  page: int = 1, page_size: int = 20) -> dict:
        return ui.site_detail(self, site=site, member_query=member_query,
                              member_status=member_status, page=page, page_size=page_size)

    def ui_options(self) -> dict:
        return {'sites': [{'id': site.get('id'), 'name': site.get('name', '')}
                          for site in self.sites.get_indexers()]}

    def ui_export(self, query: str = '', category: str = 'all') -> dict:
        return {'items': ui.export_rows(self, query=query, category=category)}

    def ui_overview(self) -> dict:
        result = ui.list_sites(self, page_size=10)
        return {'summary': result['summary'], 'items': result['items'][:3], 'updated_at': result['updated_at']}

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        defaults = {'enabled': False, 'notify': False, 'cron': '0 9 * * *',
                    'onlyonce': False, 'site_ids': ['all'], 'page_filter': '', 'no_browser_sites': []}
        return [], {**defaults, **(self.get_config() or {})}

    def _is_nexusphp(self, site_url: str) -> bool:
        """
        判断是否为NexusPHP站点
        """
        # 简单判断，后续可以添加更多特征
        return "php" in site_url.lower()

    def get_page(self) -> List[dict]:
        """Vue fetches one page at a time through the read-only UI API."""
        return []

    def stop_service(self):
        """
        停止现有服务
        """
        try:
            if hasattr(self, '_scheduler') and self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
                logger.info("PT 邀请管家服务已停止")
        except Exception as e:
            logger.error(f"停止PT 邀请管家服务失败: {str(e)}")

    def _get_site_invite_data(self, site_name):
        """
        获取站点邀请页面数据
        """
        try:
            # 获取站点信息
            site_info = None
            for indexer in self.sites.get_indexers():
                if indexer.get("name") == site_name:
                    site_info = indexer
                    break

            if not site_info:
                logger.error(f"站点 {site_name} 信息不存在")
                return {
                    "error": "站点信息不存在",
                    "invite_status": {
                        "can_invite": False,
                        "permanent_count": 0,
                        "temporary_count": 0,
                        "reason": "站点信息不存在"
                    }
                }

            site_url = site_info.get("url", "").strip()
            # CookieCloud 同步的 Cookie 常带换行，直接写进请求头会被 requests 判为非法
            site_cookie = normalize_cookie(site_info.get("cookie"))
            # 未配置 UA 时使用默认 UA，避免站点直接拒绝请求
            ua = normalize_ua(site_info.get("ua"))
            site_id = site_info.get("id", "")

            # 检查是否是M-Team站点
            is_mteam = False
            site_url_lower = site_url.lower()
            mteam_features = ["m-team", "api.m-team.cc", "api.m-team.io"]
            for feature in mteam_features:
                if feature in site_url_lower:
                    is_mteam = True
                    logger.info(f"站点 {site_name} 匹配到M-Team特征: {feature}")
                    break

            # 如果是M-Team站点，检查API认证信息
            if is_mteam:
                api_key = site_info.get("apikey", "").strip()
                token = site_info.get("token", "").strip()

                if not all([site_url, api_key, token, ua]):
                    missing_fields = []
                    if not site_url:
                        missing_fields.append("站点URL")
                    if not api_key:
                        missing_fields.append("API Key")
                    if not token:
                        missing_fields.append("Authorization Token")
                    if not ua:
                        missing_fields.append("User-Agent")

                    error_msg = f"M-Team API认证信息不完整: {', '.join(missing_fields)}"
                    logger.error(f"站点 {site_name} {error_msg}")
                    return {
                        "error": error_msg,
                        "invite_status": {
                            "can_invite": False,
                            "permanent_count": 0,
                            "temporary_count": 0,
                            "reason": error_msg
                        }
                    }
            # 对于非M-Team站点，检查Cookie
            elif not site_url or not ua or (not site_cookie and not site_info.get("apikey")):
                missing_fields = []
                if not site_url:
                    missing_fields.append("站点URL")
                if not site_cookie:
                    missing_fields.append("Cookie/API密钥")
                if not ua:
                    missing_fields.append("User-Agent")

                error_msg = f"站点信息不完整: {', '.join(missing_fields)}"
                logger.error(f"站点 {site_name} {error_msg}")
                return {
                    "error": error_msg,
                    "invite_status": {
                        "can_invite": False,
                        "permanent_count": 0,
                        "temporary_count": 0,
                        "reason": error_msg
                    }
                }

            # 先验证此站点是否在用户选择的站点列表中
            if (not self._all_sites) and self._nexus_sites and \
                    str(site_id) not in [str(x) for x in self._nexus_sites]:
                logger.warning(f"站点 {site_name} 不在用户选择的站点列表中，跳过处理")
                return {
                    "error": "站点未被选择",
                    "invite_status": {
                        "can_invite": False,
                        "permanent_count": 0,
                        "temporary_count": 0,
                        "reason": "站点未被选择"
                    }
                }

            from urllib.parse import urlsplit
            account_schemas = {'hxpt.org': 'hxpt', 'www.hxpt.org': 'hxpt',
                               'rousi.pro': 'rousipro', 'www.rousi.pro': 'rousipro',
                               'yemapt.org': 'yema', 'web.yemapt.org': 'yema', 'www.yemapt.org': 'yema'}
            schema = account_schemas.get((urlsplit(site_url).hostname or '').lower().rstrip('.'))
            account_handler = ModuleLoader.get_handler_for_schema(schema, self._site_handlers) if schema else None
            # 构建请求Session
            session = requests.Session()
            if site_info.get("proxy") and settings.PROXY:
                session.proxies.update(settings.PROXY)

            # 根据站点类型设置不同的请求头
            if is_mteam:
                # M-Team站点使用API认证方式
                session.headers.update({
                    "Content-Type": "application/json",
                    "User-Agent": ua,
                    "Accept": "application/json, text/plain, */*",
                    "Authorization": token,
                    "API-Key": api_key,
                    "Referer": site_url
                })

                # 测试API认证是否有效
                test_url = site_url
                test_response = session.get(test_url, timeout=(10, 30))
                if test_response.status_code >= 400:
                    logger.error(f"站点 {site_name} API认证测试失败，状态码: {test_response.status_code}")
                    return {
                        "error": f"API认证失败，请检查Token是否有效，状态码: {test_response.status_code}",
                        "invite_status": {
                            "can_invite": False,
                            "permanent_count": 0,
                            "temporary_count": 0,
                            "reason": f"API认证失败，请检查Token是否有效，状态码: {test_response.status_code}"
                        }
                    }
            else:
                # 普通站点使用Cookie认证
                session.headers.update({
                    'User-Agent': ua,
                    'Cookie': site_cookie,
                    'Referer': site_url,
                    'Accept': 'application/json, text/plain, */*',
                    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                    'Cache-Control': 'no-cache',
                    'Pragma': 'no-cache',
                    'sec-ch-ua': '"Chromium";v="134", "Not:A-Brand";v="24"',
                    'sec-ch-ua-mobile': '?0',
                    'sec-ch-ua-platform': '"Windows"',
                    'sec-fetch-dest': 'empty',
                    'sec-fetch-mode': 'cors',
                    'sec-fetch-site': 'same-origin'
                })

                # Personal-account APIs authenticate their own requests. An unrelated
                # marketing homepage is not a prerequisite for this data.
                test_response = None
                if account_handler is None:
                    # 选择可用基址：部分站点裸域名与 www 域名的会话不通用（如 PT时间），
                    # 依次探测候选域名，优先使用能确认登录态（含数字用户ID）的那个。
                    candidate_bases = [site_url]
                    alt_base = swap_www(site_url)
                    if alt_base and alt_base != site_url:
                        candidate_bases.append(alt_base)
                    test_response = None
                    access_reason = None
                    loose_candidate = None
                    error_candidate = None
                    for candidate in candidate_bases:
                        try:
                            candidate_response = session.get(candidate, timeout=(10, 30))
                        except Exception as request_err:
                            # 备用域名可能无法解析/连接，不能因此丢掉主域名的真实原因
                            logger.warning(f"站点 {site_name} 访问 {candidate} 失败: {str(request_err)[:80]}")
                            error_candidate = error_candidate or (
                                None, f"无法访问站点: {str(request_err)[:120]}")
                            continue
                        candidate_reason = classify_site_response(candidate_response)
                        if candidate_reason == site_access_cf_reason or candidate_response.status_code in (401, 403, 429):
                            test_response, access_reason = candidate_response, candidate_reason or f'站点拒绝访问（HTTP {candidate_response.status_code}）'
                            break
                        if candidate_reason is None and site_access_has_user_id(candidate_response.text):
                            test_response = candidate_response
                            if candidate != site_url:
                                logger.info(f"站点 {site_name} 改用备用域名访问成功: {candidate}")
                                site_url = candidate
                                site_info = dict(site_info)
                                site_info["url"] = candidate
                            break
                        if candidate_reason is None:
                            loose_candidate = loose_candidate or candidate_response
                        elif error_candidate is None or error_candidate[0] is None:
                            # 有响应的错误原因优先于“连接异常”兜底
                            error_candidate = (candidate_response, candidate_reason)
                    if test_response is None:
                        if loose_candidate is not None:
                            test_response = loose_candidate
                        elif error_candidate is not None:
                            test_response, access_reason = error_candidate
                    if access_reason:
                        logger.error(f"站点 {site_name} 访问验证失败: {access_reason}")
                        return {
                            "error": access_reason,
                            "invite_status": {
                                "can_invite": False,
                                "permanent_count": 0,
                                "temporary_count": 0,
                                "reason": access_reason
                            }
                        }

            # 使用站点处理器
            logger.info(f"站点 {site_name} 开始处理邀请数据")

            # 根据站点类型选择不同的处理器
            if is_mteam:
                logger.info(f"站点 {site_name} 使用M-Team处理器")
                from .sites.mteam import MTeamHandler
                handler = MTeamHandler()
            elif account_handler is not None:
                handler = account_handler
            elif ModuleLoader.get_handler_for_schema(
                detect_schema(test_response.text, site_url), self._site_handlers
            ):
                schema = detect_schema(test_response.text, site_url)
                handler = ModuleLoader.get_handler_for_schema(schema, self._site_handlers)
                logger.info(f"站点 {site_name} 根据页面指纹使用 {schema} 处理器")
            elif "hdchina" in site_url.lower():
                # 仓库未提供 HDChina 专用处理器，使用通用 NexusPHP 处理器兜底。
                logger.info(f"站点 {site_name} 使用通用 NexusPHP 处理器")
                from .sites.nexusphp import NexusPhpHandler
                handler = NexusPhpHandler()
            else:
                # 查找匹配的处理器
                handler = ModuleLoader.get_handler_for_site(site_url, self._site_handlers)
                if not handler:
                    # 如果找不到合适的处理器，使用通用NexusPHP处理器
                    logger.info(f"站点 {site_name} 未找到专用处理器，使用默认NexusPHP处理器")
                    from .sites.nexusphp import NexusPhpHandler
                    handler = NexusPhpHandler()

            # 使用处理器解析邀请页面
            site_info = dict(site_info)
            site_info['_prefetched_html'] = test_response.text if test_response is not None else ''
            site_data = handler.parse_invite_page(site_info, session)
            if isinstance(site_data, dict) and isinstance(site_data.get("invitees"), list):
                site_data["invitees"] = sanitize_invitees(site_data["invitees"])


            # 检查站点数据结构是否正确
            if "invite_status" in site_data:
                # 检查临时邀请数量
                temp_count = site_data["invite_status"].get("temporary_count", 0)
                if temp_count > 0:
                    logger.info(f"站点 {site_name} 有 {temp_count} 个临时邀请")

                # 确保不可邀请原因也被正确处理和显示
                if not site_data["invite_status"].get("can_invite", False):
                    reason = site_data["invite_status"].get("reason", "")
                    if reason:
                        logger.info(f"站点 {site_name} 不可邀请原因: {reason}")

            return site_data

        except Exception as e:
            logger.error(f"获取站点 {site_name} 邀请数据失败: {str(e)}")
            return {
                "error": f"获取站点邀请数据失败: {str(e)}",
                "invite_status": {
                    "can_invite": False,
                    "permanent_count": 0,
                    "temporary_count": 0,
                    "reason": f"获取站点邀请数据失败: {str(e)}"
                }
            }

    def get_invitees(self, apikey: str = None, site_name: str = None) -> dict:
        """
        获取后宫成员API接口
        """
        if apikey and apikey != settings.API_TOKEN:
            return {"code": 1, "message": "API令牌错误!"}

        try:
            # 从数据文件获取站点数据
            site_data = self.data_manager.get_site_data(site_name)

            # 获取最后更新时间
            last_update = self.data_manager.get_last_update_time()

            if not site_data:
                if site_name:
                    return {"code": 1, "message": f"站点 {site_name} 数据不存在"}
                else:
                    return {"code": 1, "message": "暂无站点数据"}

            return {
                "code": 0,
                "message": "获取成功",
                "data": {
                    "sites": site_data,
                    "last_update": last_update
            }
            }
        except Exception as e:
            logger.error(f"获取后宫成员失败: {str(e)}")
            return {"code": 1, "message": f"获取后宫成员失败: {str(e)}"}

    def refresh_data(self, apikey: str = None) -> dict:
        """
        强制刷新所有站点数据API接口
        """
        if apikey and apikey != settings.API_TOKEN:
            return {"code": 1, "message": "API令牌错误!"}

        try:
            # 重新加载站点处理器以确保使用最新的处理逻辑
            self._site_handlers = ModuleLoader.load_site_handlers()
            logger.info(f"已重新加载 {len(self._site_handlers)} 个站点处理器")

            # 调用refresh_all_sites方法刷新数据
            result = self.refresh_all_sites()

            if result and result.get("success", 0) > 0:
                # 获取最新的更新时间和站点数据
                site_data = self.data_manager.get_site_data()
                last_update = self.data_manager.get_last_update_time()

                return {
                    "code": 0,
                    "message": f"增量数据刷新成功: {result.get('success')}个站点, 失败: {result.get('error')}个站点",
                    "data": {
                        "last_update": last_update,
                        "site_count": len(site_data),
                        "success": result.get("success", 0),
                        "error": result.get("error", 0)
                    }
                }
            else:
                return {"code": 1, "message": "数据刷新失败，没有成功刷新的站点"}

        except Exception as e:
            logger.error(f"强制刷新数据失败: {str(e)}")
            return {"code": 1, "message": f"强制刷新数据失败: {str(e)}"}

    def refresh_now(self, apikey: str = None, scope: str = "all") -> dict:
        """
        立即刷新站点数据（详情页按钮调用）。

        :param scope: all=全部；problem=仅异常站点；stale=仅数据超期的站点
        """
        if apikey != settings.API_TOKEN:
            return {"success": False, "message": "API密钥验证失败"}
        if getattr(self, "_refreshing", False):
            return {"success": False, "message": "刷新已在进行中，请稍候"}

        targets = None
        if scope in ("problem", "stale"):
            sites = self.data_manager.get_site_data()
            summary = site_categories.summarize(sites)
            if scope == "problem":
                targets = [item["name"] for item in summary["action_sites"]]
            else:
                stale_seconds = 12 * 3600
                now = time.time()
                targets = [name for name, record in sites.items()
                           if now - (record.get("last_update") or 0) > stale_seconds]
            if not targets:
                return {"success": True, "message": "没有需要刷新的站点 🎉"}

        threading.Thread(target=self.refresh_all_sites, kwargs={"site_names": targets},
                         daemon=True).start()
        scope_text = {"all": "全部站点", "problem": "异常站点", "stale": "数据超期站点"}.get(scope, scope)
        return {"success": True,
                "message": f"已开始后台刷新{scope_text}{('（%d 个）' % len(targets)) if targets else ''}，稍后刷新本页查看结果"}

    def export_data(self, apikey: str = None, format: str = "csv") -> dict:
        """导出当前后宫数据（CSV 写入插件数据目录，JSON 直接返回数据）。"""
        if apikey != settings.API_TOKEN:
            return {"success": False, "message": "API密钥验证失败"}
        try:
            rows = ui_extra.build_export_rows(self.data_manager.get_site_data())
            if format == "json":
                return {"success": True, "message": f"已生成 {len(rows)} 条数据", "data": rows}
            import csv as csv_module
            target = self.get_data_path() / f"harem_export_{time.strftime('%Y%m%d_%H%M%S')}.csv"
            with open(target, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv_module.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else [])
                if rows:
                    writer.writeheader()
                    writer.writerows(rows)
            logger.info(f"后宫数据已导出: {target}")
            return {"success": True, "message": f"已导出 {len(rows)} 行到 {target}"}
        except Exception as err:
            logger.error(f"导出后宫数据失败: {str(err)}")
            return {"success": False, "message": f"导出失败: {str(err)}"}

    def _get_user_id(self, session: requests.Session, site_info: Dict[str, Any]) -> str:
        """
        从站点获取用户ID
        :param session: 请求会话
        :param site_info: 站点信息
        :return: 用户ID
        """
        try:
            site_url = site_info.get("url", "").strip()
            site_name = site_info.get("name", "")

            # 访问个人页面提取ID
            profile_url = urljoin(site_url, "index.php")
            response = session.get(profile_url, timeout=(5, 15))
            response.raise_for_status()
            html_content = response.text

            # 尝试从多种常见格式中提取用户ID
            # 方法1：从class="searchrecord td"中提取
            soup = BeautifulSoup(html_content, 'html.parser')

            # 查找欢迎语中的用户名和ID链接
            welcome_text = soup.select_one('.welcome')
            if welcome_text:
                user_link = welcome_text.select_one('a[href*="userdetails.php"]')
                if user_link:
                    href = user_link.get('href', '')
                    id_match = re.search(r'id=(\d+)', href)
                    if id_match:
                        return id_match.group(1)

            # 方法2：从个人资料链接中提取
            for pattern in [
                r'userdetails\.php\?id=(\d+)',
                r'getusertorrentlistajax\.php\?userid=(\d+)',
                r'<input[^>]*name=["\']passkey["\'][^>]*value=["\']([a-zA-Z0-9]+)["\']',
                r'passkey=([a-zA-Z0-9]+)',
                r'usercp\.php\?action=personal&userid=(\d+)',
                r'id=(\d+)',
                r'uid=(\d+)'
            ]:
                matches = re.search(pattern, html_content)
                if matches:
                    return matches.group(1)

            # 方法3：尝试通过访问具体的用户资料页面
            try:
                user_link = None
                # 找到包含用户名的链接
                for a_tag in soup.select('a[href*="userdetails.php"], a[href*="user.php"]'):
                    if a_tag.get_text().strip():
                        user_link = a_tag
                        break

                if user_link:
                    href = user_link.get('href', '')
                    if 'id=' in href:
                        id_match = re.search(r'id=(\d+)', href)
                        if id_match:
                            return id_match.group(1)

                    # 访问用户链接，从重定向或内容中提取用户ID
                    user_response = session.get(urljoin(site_url, href), timeout=(5, 15))
                    user_response.raise_for_status()
                    user_content = user_response.text

                    # 在返回的内容中搜索用户ID
                    for pattern in [r'userdetails\.php\?id=(\d+)', r'passkey=([a-zA-Z0-9]+)', r'id=(\d+)', r'uid=(\d+)']:
                        matches = re.search(pattern, user_content)
                        if matches:
                            return matches.group(1)
            except Exception as e:
                logger.warning(f"通过用户资料页面获取用户ID失败: {str(e)}")

            # 方法4：尝试从邀请页面获取
            try:
                invite_url = urljoin(site_url, "invite.php")
                invite_response = session.get(invite_url, timeout=(5, 15))
                invite_response.raise_for_status()
                invite_content = invite_response.text

                # 搜索邀请页面中的用户ID
                for pattern in [r'id=(\d+)', r'uid=(\d+)', r'user(?:id|_id)=(\d+)']:
                    matches = re.search(pattern, invite_content)
                    if matches:
                        return matches.group(1)
            except Exception as e:
                logger.warning(f"通过邀请页面获取用户ID失败: {str(e)}")

            # 未能从任何途径获取用户ID
            logger.error(f"无法从站点 {site_name} 获取用户ID")
            return ""

        except Exception as e:
            logger.error(f"获取用户ID失败: {str(e)}")
            return ""

    def refresh_all_sites(self, site_names: List[str] = None) -> Dict[str, int]:
        """统一刷新入口，阻止定时与手动刷新同时请求并写入同一份缓存。"""
        if not self._refresh_lock.acquire(blocking=False):
            return {"success": 0, "error": 0, "message": "刷新已在进行中"}
        try:
            return self._refresh_selected_sites(site_names)
        finally:
            self._refresh_lock.release()

    def _refresh_selected_sites(self, site_names: List[str] = None) -> Dict[str, int]:
        """
        刷新站点数据

        :param site_names: 指定只刷新这些站点（None 表示按配置刷新全部）
        """
        try:
            # 设置刷新标志防止重复刷新
            if hasattr(self, '_refreshing') and self._refreshing:
                logger.warning("PT 邀请管家数据刷新已在进行中，跳过重复刷新")
                return {"success": 0, "error": 0, "message": "刷新已在进行中"}

            self._refreshing = True

            # 记录刷新开始 - 说明是增量更新模式
            logger.info("开始增量刷新站点数据，只更新选择的站点，失败时保留旧数据")

            # 重新加载站点处理器
            self._site_handlers = ModuleLoader.load_site_handlers()
            logger.info(f"加载了 {len(self._site_handlers)} 个站点处理器")

            # 获取所有站点配置
            all_sites = self.sites.get_indexers()
            if site_names is not None:
                wanted = set(site_names)
                all_sites = [site for site in all_sites if site.get("name") in wanted]
                logger.info(f"本次仅刷新指定站点: {', '.join(sorted(wanted))}")

            # 筛选站点配置 - 「全部」标记或未选择站点时选择所有站点（含后续新增）
            selected_sites = []
            if self._all_sites:
                logger.info(f"站点选择为「全部」，本次刷新覆盖 {len(all_sites)} 个站点")
                selected_sites = all_sites
            elif not self._nexus_sites:
                logger.info("未选择任何站点，将使用所有站点")
                selected_sites = all_sites
            else:
                for site in all_sites:
                    site_id = site.get("id")
                    # 转换为字符串进行比较
                    site_id_str = str(site_id)
                    nexus_sites_str = [str(x) for x in self._nexus_sites]

                    # 调试输出当前站点ID
                    logger.debug(f"检查站点ID: {site_id}，类型: {type(site_id)}")

                    if site_id_str in nexus_sites_str:
                        selected_sites.append(site)
                        logger.debug(f"匹配到站点: {site.get('name')} (ID: {site_id})")

            if selected_sites:
                logger.debug(f"将刷新 {len(selected_sites)} 个站点的数据: {', '.join([site.get('name', '') for site in selected_sites])}")
            else:
                logger.warning("没有发现可供刷新的站点，请检查站点选择配置")
                logger.debug(f"所有站点ID: {[site.get('id') for site in all_sites]}")
                logger.debug(f"选择的站点ID: {self._nexus_sites}")
                return {"success": 0, "error": 0, "message": "没有发现可供刷新的站点"}

            # 统计成功/失败站点数
            success_count = 0
            error_count = 0
            error_details = []

            # 获取现有数据
            existing_data = self.data_manager.get_site_data()

            # 逐个刷新站点数据
            for site in selected_sites:
                site_name = site.get("name", "")

                logger.debug(f"开始获取站点 {site_name} 的后宫数据...")

                site_data = self._get_site_invite_data(site_name)

                # --- 修改开始: 增强失败判断逻辑 ---
                is_successful = True
                error_msg = ""

                if "error" in site_data:
                    # 情况1: _get_site_invite_data 内部捕获到异常
                    is_successful = False
                    error_msg = site_data.get('error', '未知错误')
                else:
                    # 情况2: 检查 parse_invite_page 返回的 reason 是否表明失败
                    invite_status = site_data.get("invite_status", {})
                    reason = invite_status.get("reason", "")

                    # 定义表明失败的关键字或模式 (即使没有异常)
                    # 使用 r 前缀确保是原始字符串，避免反斜杠转义问题
                    failure_indicators = [
                        r"访问邀请页面失败",
                        r"无法获取用户ID",
                        r"未登录或Cookie已失效",
                        r"初始化失败",
                        r"网络错误",
                        r"发生错误",
                        r"解析站点.*时发生意外错误",
                        r"站点信息不完整", # 加入对站点信息不完整的检查
                    ]

                    # 使用正则表达式匹配，因为 "解析站点..." 包含变量
                    if reason and any(re.search(indicator, reason, re.IGNORECASE) for indicator in failure_indicators):
                        is_successful = False
                        error_msg = reason # 使用 handler 返回的具体原因作为错误消息

                # --- 修改结束 ---

                if not is_successful:
                    if not error_msg: # 确保总有一个错误消息
                        error_msg = "未知原因导致刷新失败"
                    logger.error(f"站点 {site_name} 数据刷新失败: {error_msg}")
                    error_count += 1
                    error_details.append({"site_name": site_name, "msg": error_msg})

                    # 保留旧数据逻辑 (保持不变)
                    old_data = existing_data.get(site_name, {}).get("data", {})
                    if old_data:
                        old_invitees = old_data.get("invitees", [])
                        old_status = old_data.get("invite_status", {})
                        logger.info(f"站点 {site_name} 保留旧数据: {len(old_invitees)}人, "
                                   f"永久邀请:{old_status.get('permanent_count', 0)}个, "
                                   f"临时邀请:{old_status.get('temporary_count', 0)}个")
                    else:
                        logger.info(f"站点 {site_name} 无旧数据可保留")

                    failed_site_data = dict(old_data) if isinstance(old_data, dict) else {}
                    failed_status = failed_site_data.get("invite_status", {})
                    failed_status = dict(failed_status) if isinstance(failed_status, dict) else {}
                    failed_status.update({
                        "can_invite": False,
                        "reason": error_msg
                    })
                    failed_site_data["invite_status"] = failed_status
                    failed_site_data.setdefault("invitees", old_data.get("invitees", []) if isinstance(old_data, dict) else [])
                    failed_site_data["error"] = error_msg
                    prior_update = existing_data.get(site_name, {}).get('last_update')
                    failed_site_data['last_success_at'] = (old_data.get('last_success_at') if old_data.get('fetch_failed') else prior_update)
                    failed_site_data["fetch_failed"] = True
                    self.data_manager.update_site_data(site_name, failed_site_data)
                else:
                    # 成功逻辑 (保持不变)
                    invite_status = site_data.get("invite_status", {})
                    invitees = site_data.get("invitees", [])
                    perm_count = invite_status.get("permanent_count", 0)
                    temp_count = invite_status.get("temporary_count", 0)
                    can_invite = invite_status.get("can_invite", False)
                    reason = invite_status.get("reason", "")

                    logger.info(f"站点 {site_name} 数据刷新成功，已邀请 {len(invitees)} 人，永久邀请 {perm_count} 个，临时邀请 {temp_count} 个")

                    # 在成功时也记录一下原因（例如 可购买邀请、具体原因）
                    if reason:
                        if can_invite:
                            logger.info(f"站点 {site_name} 可邀请原因: {reason}")
                        else:
                            logger.info(f"站点 {site_name} 不可邀请原因: {reason}")

                    # 保存站点数据 (保持不变)
                    self.data_manager.update_site_data(site_name, site_data)
                    success_count += 1

            # 记录本次快照并对比上一次，得到变化趋势
            try:
                latest_data = self.data_manager.get_site_data()
                snapshot = site_trend.build_snapshot(latest_data, site_categories.classify, time.time())
                history = self.data_manager.load_history()
                self._last_diff = site_trend.diff_snapshots(history[-1] if history else None, snapshot)
                self.data_manager.append_history(snapshot)
            except Exception as trend_err:
                logger.error(f"记录刷新历史失败: {str(trend_err)}")
                self._last_diff = {}

            # 发送通知
            if self._notify:
                self._send_refresh_notification(success_count, error_count, error_details,
                                                site_names=[site.get('name', '') for site in selected_sites],
                                                targeted=site_names is not None,
                                                previous_records=existing_data)

            logger.info(f"增量刷新完成: 成功 {success_count} 个站点, 失败 {error_count} 个站点")

            return {"success": success_count, "error": error_count}

        finally:
            # 清除刷新标志
            self._refreshing = False

    def _send_refresh_notification(self, success_count, error_count, error_details=None,
                                   site_names=None, targeted=False, previous_records=None):
        """保留通知渠道，仅发送与本次刷新范围一致的邀请结果。"""
        if not success_count and not error_count:
            return
        try:
            content = build_refresh_content(
                self.data_manager.get_site_data(), success_count, error_count, error_details,
                site_names=site_names, targeted=targeted, previous_records=previous_records,
            )
            self.post_message(mtype=NotificationType.SiteMessage, title=content.title,
                              text=notification_plain_text(content), rich_content=content)
            logger.info(f"{content.title}：成功 {success_count} 站，失败 {error_count} 站")
        except Exception as err:
            logger.error(f"PT 邀请管家通知发送失败: {err}")

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        """
        if self._enabled and self._cron:
            try:
                # 检查是否为5位cron表达式
                if str(self._cron).strip().count(" ") == 4:
                    return [{
                        "id": "ptinvitekeeper",
                        "name": "PT 邀请管家",
                        "trigger": CronTrigger.from_crontab(self._cron),
                        "func": self.refresh_all_sites,
                        "kwargs": {}
                    }]
                else:
                    logger.error("cron表达式格式错误")
                    return []
            except Exception as err:
                logger.error(f"定时任务配置错误：{str(err)}")
                return []
        return []

    @staticmethod
    def get_api_handlers():
        """
        获取API接口
        """
        return {
            "/get_invitees": {"func": PTInviteKeeper.get_invitees, "methods": ["GET"], "desc": "获取所有站点邀请数据"},
            "/refresh": {"func": PTInviteKeeper.refresh_data, "methods": ["GET"], "desc": "强制刷新站点数据"}
        }

    def api_update_config(self, request: dict) -> Response:
        """
        更新插件配置
        """
        try:
            # 读取配置
            self._enabled = request.get("enabled", False)
            self._notify = request.get("notify", False)
            self._cron = request.get("cron", "0 9 * * *")
            self._onlyonce = request.get("onlyonce", False)

            # 获取选中站点列表
            raw_site_ids = request.get("site_ids") or []
            self._all_sites = any(str(item).lower() == self._ALL_SITES for item in raw_site_ids)
            self._nexus_sites = []
            for site_id in raw_site_ids:
                try:
                    if isinstance(site_id, str) and site_id.isdigit():
                        self._nexus_sites.append(int(site_id))
                    elif isinstance(site_id, int):
                        self._nexus_sites.append(site_id)
                except Exception:
                    pass
            if self._all_sites:
                self._nexus_sites = [self._ALL_SITES]

            # 不使用浏览器仿真的站点
            raw_no_browser = request.get("no_browser_sites") or []
            if isinstance(raw_no_browser, str):
                raw_no_browser = [x.strip() for x in raw_no_browser.replace("，", ",").split(",")]
            self._no_browser_sites = [str(x).strip() for x in raw_no_browser if str(x).strip()]
            self._page_filter = (request.get("page_filter") or "").strip()

            # 记录站点ID，用于调试
            logger.info(f"已选择站点ID: {self._nexus_sites}"
                        f"{'（全部，含后续新增站点）' if self._all_sites else ''}")

            # 保存配置
            self.__update_config()

            # 如果开启了立即运行一次
            if self._onlyonce:
                try:
                    # 定时服务
                    if hasattr(self, '_scheduler') and self._scheduler:
                        self.stop_service()  # 先停止已有服务

                    self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                    logger.debug("立即运行一次开关被打开，将在3秒后执行刷新")
                    self._scheduler.add_job(func=self.refresh_all_sites, trigger='date',
                                          run_date=datetime.now(pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                          name="PT 邀请管家")

                    # 关闭一次性开关
                    self._onlyonce = False
                    # 保存配置
                    self.__update_config()

                    # 启动任务
                    if self._scheduler and self._scheduler.get_jobs():
                        self._scheduler.print_jobs()
                        self._scheduler.start()

                    return {"code": 0, "msg": "配置已更新，将在3秒后执行刷新"}
                except Exception as e:
                    logger.error(f"启动一次性任务失败: {str(e)}")
                    return {"code": 1, "msg": f"配置已更新，但启动任务失败: {str(e)}"}

            return {"code": 0, "msg": "配置已更新"}

        except Exception as e:
            logger.error(f"更新配置失败: {str(e)}")
            return {"code": 1, "msg": f"更新配置失败: {str(e)}"}

    def _calculate_statistics(self, invitees):
        """
        计算用户统计数据
        """
        banned_count = sum(1 for i in invitees if i.get('enabled', '').lower() == 'no')
        low_ratio_count = sum(1 for i in invitees if i.get('ratio_health') in ['warning', 'danger'])
        no_data_count = sum(1 for i in invitees if i.get('ratio_health') == 'neutral')

        return {
            'banned': banned_count,
            'low_ratio': low_ratio_count,
            'no_data': no_data_count
        }

    def api_get_config(self, apikey: str) -> Response:
        """
        获取配置
        """
        if apikey != settings.API_TOKEN:
            return Response(success=False, message="API令牌错误!")

        try:
            # 直接返回当前配置，不再从文件读取
            config = {
                "enabled": self._enabled,
                "notify": self._notify,
                "cron": self._cron,
                "onlyonce": self._onlyonce,
                "site_ids": self._nexus_sites,
                "page_filter": self._page_filter,
                "no_browser_sites": self._no_browser_sites
            }
            return Response(success=True, message="获取成功", data=config)
        except Exception as e:
            logger.error(f"获取配置失败: {str(e)}")
            return Response(success=False, message=f"获取配置失败: {str(e)}")


# 插件类导出
plugin_class = PTInviteKeeper
