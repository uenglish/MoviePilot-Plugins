import re
from threading import Lock
from .integration import SignInBridge, merge_config, extend_form, migrate_history, selection_union
from .result import SiteResult, has_signin_evidence, has_login_evidence, has_login_form
from .notification import notification_result_changed, notification_batch, queue_notification
from .dashboard import render_dashboard
from . import ui, ui_actions
from .identity_migration import migrate_identity
import traceback
from datetime import datetime, timedelta
from multiprocessing import TimeoutError as PoolTimeoutError
from multiprocessing.pool import ThreadPool
from typing import Any, List, Dict, Tuple, Optional
from urllib.parse import urljoin

import pytz
from requests import exceptions as request_errors
from app import schemas
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.db.site_oper import SiteOper
from app.helper.browser import PlaywrightHelper
from app.helper.cloudflare import under_challenge
from app.helper.module import ModuleHelper
from app.helper.sites import SitesHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType
from app.utils.http import RequestUtils
from app.utils.site import SiteUtils
from app.utils.string import StringUtils
from app.utils.timer import TimerUtils
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from ruamel.yaml import CommentedMap


# 通用签到页的“已签到”特征：只有出现明确特征才认为签到完成，避免站点只看登录态就误报签到成功。
# 同时兼容简体、繁体与英文站点（NexusPHP 签到页会跟随用户语言显示文案）。
_SIGNED_PAGE_PATTERNS = (
    r"[签簽][到到]成功",
    r"[签簽][到到]已得",
    r"已[经經][签簽][到到]",
    r"今日已[签簽][到到]",
    r"[请請]不要重[复複]刷新",
    r"[请請]不要重[复複][签簽][到到]",
    r"已[连連][续續][签簽][到到]",
    r"本次[签簽][到到][获獲]得",
    r"此次[签簽][到到]您[获獲]得",
    r"Already attendance",
    r"Attendance success",
)

# 通用签到页中需要验证码的表单特征
_CAPTCHA_PAGE_PATTERNS = (
    r"name=[\"']imagehash[\"']",
    r"name=[\"']imagestring[\"']",
    r"regimage",
)


def _is_signed_page(html_text: str) -> bool:
    """
    判断通用签到页面是否已出现签到完成的明确特征
    :param html_text: 页面源码
    :return: 是否已签到
    """
    if not html_text:
        return False
    return has_signin_evidence(html_text)


def _need_captcha_page(html_text: str) -> bool:
    """
    判断通用签到页面是否是需要验证码的表单（此时访问页面不会完成签到）
    :param html_text: 页面源码
    :return: 是否需要验证码
    """
    if not html_text:
        return False
    return any(re.search(pattern, html_text, re.IGNORECASE) for pattern in _CAPTCHA_PAGE_PATTERNS)
class PTSignInKeeper(_PluginBase):
    """按配置执行站点签到和模拟登录，并记录结果与发送通知。"""

    # 插件名称
    plugin_name = "PT 签到管家"
    # 插件描述
    plugin_desc = "PT 站点签到、登录检测与奖励记录，支持访问记录核验、失败重试和历史查询。"
    # 插件图标
    plugin_icon = "../api/v1/plugin/file/PTSignInKeeper/assets/pt-signin-keeper-v1.webp"
    # 插件版本
    plugin_version = "1.4.5"
    # 插件作者
    plugin_author = "serendipity"
    # 作者主页
    author_url = "https://github.com/uenglish"
    # 插件配置项ID前缀
    plugin_config_prefix = "ptsigninkeeper_"
    # 加载顺序
    plugin_order = 0
    # 可使用的用户级别
    auth_level = 2

    # 持久化全选标记，执行时展开，自动包含后续新增站点。
    _ALL_SITES = "all"

    # 历史记录保留天数（详情页展示最近 7 天，这里保留 14 天便于回溯）
    _history_days = 14
    _HISTORY_DAYS = 14
    _rich_notify = True
    _run_lock = Lock()
    _generation = 0
    _bridge = None
    _extra_config = {}

    # 单个站点最长执行时间（秒）：浏览器仿真等外部依赖可能永久阻塞，超时不再等待
    _site_timeout = 300

    # 失败重试时间（HH:MM）：用于 U2 这类 9 点后才开放签到的站点，留空则关闭
    _retry_time: str = "09:01"

    # 插件改名前的旧插件ID，用于自动迁移历史与配置
    _legacy_plugin_id = "AutoSignIn"

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None
    # 加载的模块
    _site_schema: list = []

    # 配置属性
    _enabled: bool = False
    _cron: str = ""
    _main_trigger = None
    _onlyonce: bool = False
    _notify: bool = False
    _queue_cnt: int = 5
    _sign_sites: list = []
    _login_sites: list = []
    _retry_keyword = None
    _clean: bool = False
    _start_time: int = None
    _end_time: int = None
    _auto_cf: int = 0

    def init_plugin(self, config: dict = None):
        """加载配置并保留动态全选标记，注册需要立即执行的任务。"""

        # Invalidate in-flight results before replacing configuration.
        if "_run_lock" not in self.__dict__:
            self._run_lock = Lock()
        self._generation += 1
        previous_bridge = self._bridge
        self.stop_service()
        current = config if config is not None else (self.get_config() or {})
        current = migrate_identity(self, current, "AutoSignInPlus")
        if self.get_data(key="consolidation_config_v1"):
            current["consolidation_version"] = 1
        config = merge_config(current, self.get_config(plugin_id="AutoSignIn") or {},
                              self.get_config(plugin_id="PTSiteSignIn") or {})
        self._extra_config = dict(config)
        self._rich_notify = bool(config.get("rich_notify", True))
        self._bridge = SignInBridge(config)
        self._bridge.engine.quiz_claim = self._claim_quiz_attempt
        if previous_bridge:
            # A worker may outlive its timeout; keep site locks across reloads.
            self._bridge._site_locks = previous_bridge._site_locks
            self._bridge._lock = previous_bridge._lock
        self._short_retry_attempt = 0
        self._allow_once = bool(config.get("onlyonce"))
        migrate_history(self)


        # 兼容插件改名：自动继承旧插件的历史记录与配置
        self.__migrate_legacy_data()
        if not config:
            config = self.__migrate_legacy_config()

        # 配置
        if config:
            self._enabled = config.get("enabled")
            period = config.get("cron")
            self._cron = "" if period is None else str(period).strip()
            self._main_trigger = None
            self._onlyonce = config.get("onlyonce")
            self._notify = config.get("notify")
            self._queue_cnt = max(1, min(int(config.get("queue_cnt") or 5), 20))
            self._sign_sites = selection_union(config.get("sign_sites"))
            self._login_sites = selection_union(config.get("login_sites"))
            self._retry_keyword = config.get("retry_keyword")
            self._auto_cf = config.get("auto_cf")
            # 未配置时使用默认重试时间（09:01），显式留空表示关闭
            retry_time = config.get("retry_time")
            self._retry_time = "09:01" if retry_time is None else str(retry_time).strip()
            self._clean = config.get("clean")

            # 过滤掉已删除的站点
            all_sites = [site.id for site in SiteOper().list_order_by_pri()] + [site.get("id") for site in
                                                                                self.__custom_sites()]
            self._sign_sites = ([self._ALL_SITES] if self._ALL_SITES in self._sign_sites else
                                [site_id for site_id in all_sites if site_id in self._sign_sites])
            self._login_sites = ([self._ALL_SITES] if self._ALL_SITES in self._login_sites else
                                [site_id for site_id in all_sites if site_id in self._login_sites])
            # 保存配置
            self.__update_config()
            self.save_data(key="consolidation_config_v1", value=True)

        # 加载模块
        if self._enabled or self._onlyonce:
            if not self._sign_sites and not self._login_sites:
                logger.warning("未选择签到或登录站点，本次不执行；请在站点范围中选择全部站点或指定站点")
                self._onlyonce = False
                self._allow_once = False
                self.__update_config()
                return

            self._site_schema = ModuleHelper.load('app.plugins.ptsigninkeeper.sites',
                                                  filter_func=lambda _, obj: hasattr(obj, 'match'))

            # 立即运行一次
            if self._onlyonce:
                # 定时服务
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info("PT 签到管家服务启动，立即运行一次")
                self._scheduler.add_job(func=self.sign_in, trigger='date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="PT 签到管家")

                # 关闭一次性开关
                self._onlyonce = False
                # 保存配置
                self.__update_config()

            # 失败重试任务（U2 等 9 点后开放签到的站点）
            if self._enabled:
                self.__register_retry_job()

            # 启动任务
            if self._scheduler and self._scheduler.get_jobs() and not self._scheduler.running:
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    def __register_retry_job(self):
        """
        注册每日失败重试任务

        部分站点（如 U2）需要等到固定时间后才开放签到，定时任务可能早于该时间；
        这里按配置的重试时间再跑一次，只会处理当日未签到/命中的站点。
        """
        self._scheduler = self._scheduler or BackgroundScheduler(timezone=settings.TZ)
        try:
            self._scheduler.remove_job("ptsigninkeeper_retry")
        except Exception:
            pass
        retry_time = str(self._retry_time or "").strip()
        if not retry_time:
            logger.info("未配置失败重试时间，跳过注册重试任务")
            return
        try:
            hour, minute = (int(part) for part in retry_time.split(":")[:2])
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError(f"时间超出范围 {retry_time}")
        except Exception as err:
            logger.error(f"失败重试时间格式不正确（{retry_time}）：{str(err)}，应为 HH:MM")
            return
        self._scheduler.add_job(func=self.__retry, trigger="cron", hour=hour, minute=minute,
                                id="ptsigninkeeper_retry", replace_existing=True,
                                name="PT 签到管家失败重试")
        logger.info(f"已注册PT 签到管家失败重试任务：每天 {hour:02d}:{minute:02d}")

    def __retry(self):
        """重试当日失败或未签到的站点"""
        logger.info("PT 签到管家失败重试任务启动 ...")
        try:
            self.sign_in()
        except Exception as err:
            logger.error(f"失败重试任务异常：{str(err)}")

    def __run_sites(self, func, sites: list, type_str: str) -> list:
        """
        并行执行站点任务，单个站点卡住不再拖死整轮执行。

        multiprocessing 的 map 会一直等待全部任务返回，而浏览器仿真等外部依赖可能永久阻塞，
        一旦卡住本轮结果就无法落库、插件也无法停止。这里改为逐站点收集结果，超时按失败处理，
        且单个卡住的任务不会占用全部并发位。
        """
        status = []
        bridge, generation = self._bridge, self._generation
        def execute(site):
            # Queued work must never switch to a newer configuration's bridge.
            if generation != self._generation:
                return SiteResult(site.get("name"), f"{type_str}失败：配置已重载", False)
            logger.info(f"开始站点{type_str}：{site.get('name')}（ID：{site.get('id')}）")
            if getattr(func, "__name__", "") in {"signin_site", "login_site"}:
                return bridge.perform(self._normalize_site_info(site), type_str, self.__login_base)
            return func(site)
        pool = ThreadPool(min(len(sites), int(self._queue_cnt)))
        try:
            futures = [(site, pool.apply_async(execute, (site,))) for site in sites]
            for site, future in futures:
                site_name = site.get("name")
                try:
                    status.append(future.get(timeout=self._site_timeout))
                except (TimeoutError, PoolTimeoutError):
                    logger.error(f"{site_name} {type_str}失败：执行超过 {self._site_timeout} 秒无响应，"
                                 f"本轮跳过，将在下次执行时重试")
                    status.append(SiteResult(site_name, f"{type_str}失败：执行超时，已跳过", False))
                except Exception as err:
                    logger.error(f"{site_name} {type_str}异常：{str(err)}")
                    status.append(SiteResult(site_name, f"{type_str}失败：执行异常", False))
        finally:
            # 卡住的任务无法中断，终止线程池以免影响插件停止与后续执行
            pool.terminate()
        return status

    def __migrate_legacy_data(self):
        """插件改名后自动继承旧插件的历史记录，避免详情页历史为空。"""
        try:
            if self.get_data() or not self._legacy_plugin_id:
                return
            legacy_data = self.get_data(plugin_id=self._legacy_plugin_id)
            if not legacy_data:
                return
            # 无 key 查询返回的是数据对象列表，兼容字典形态的返回
            if isinstance(legacy_data, dict):
                items = list(legacy_data.items())
            else:
                items = [(getattr(item, "key", None), getattr(item, "value", None))
                         for item in legacy_data]
            migrated = 0
            for key, value in items:
                if not key:
                    continue
                self.save_data(key=key, value=value)
                migrated += 1
            if migrated:
                logger.info(f"已从旧插件 {self._legacy_plugin_id} 迁移 {migrated} 条历史记录")
        except Exception as err:
            logger.error(f"迁移旧插件历史记录失败：{str(err)}")

    def __migrate_legacy_config(self) -> Optional[dict]:
        """插件改名后自动继承旧插件配置（仅当当前插件还没有配置时）。"""
        try:
            if not self._legacy_plugin_id:
                return None
            legacy_config = self.get_config(plugin_id=self._legacy_plugin_id)
            if not legacy_config:
                return None
            self.update_config(legacy_config)
            logger.info(f"已从旧插件 {self._legacy_plugin_id} 迁移插件配置")
            return legacy_config
        except Exception as err:
            logger.error(f"迁移旧插件配置失败：{str(err)}")
            return None

    def __update_config(self):
        """保存原始站点选择，避免全选退化为固定站点列表。"""
        # 保存配置
        self.update_config(
            {
                **self._extra_config,
                "rich_notify": self._rich_notify,
                "enabled": self._enabled,
                "notify": self._notify,
                "cron": self._cron,
                "onlyonce": self._onlyonce,
                "queue_cnt": self._queue_cnt,
                "sign_sites": self._sign_sites,
                "login_sites": self._login_sites,
                "retry_keyword": self._retry_keyword,
                "auto_cf": self._auto_cf,
                "retry_time": self._retry_time,
                "clean": self._clean,
            }
        )

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return [{
            "cmd": "/site_signin",
            "event": EventType.PluginAction,
            "desc": "站点签到",
            "category": "站点",
            "data": {
                "action": "site_signin"
            }
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API
        [{
            "path": "/xx",
            "endpoint": self.xxx,
            "methods": ["GET", "POST"],
            "summary": "API说明"
        }]
        """
        return [{
            "path": "/signin_by_domain",
            "endpoint": self.signin_by_domain,
            "methods": ["GET"],
            "summary": "站点签到",
            "description": "使用站点域名签到站点",
        }] + self._ui_routes()

    def get_service(self) -> List[Dict[str, Any]]:
        """向宿主提供主任务。重复查询应返回同一个计划。"""
        if not self._enabled or not (self._sign_sites or self._login_sites):
            return []
        if self._main_trigger is None:
            period = str(self._cron or "").strip()
            tz = pytz.timezone(settings.TZ)
            now = datetime.now(tz)
            self._start_time = self._end_time = None
            try:
                trigger, plan = self._build_main_trigger(period, tz, now)
                next_run = trigger.get_next_fire_time(None, now)
                if next_run is None:
                    raise ValueError("计划没有下一次触发时间")
            except (TypeError, ValueError, OverflowError) as err:
                logger.error(f"签到主任务计划无效（{period}）：{err}")
                return []
            self._main_trigger = trigger
            logger.info(f"签到主任务计划：{plan}，下次触发 {next_run:%Y-%m-%d %H:%M:%S}（{settings.TZ}）")
        return [{"id": "PTSignInKeeper", "name": "PT 签到管家服务", "trigger": self._main_trigger,
                 "func": self._scheduled_sign_in, "kwargs": {}}]

    def _build_main_trigger(self, period, tz, now):
        """根据已保存的执行周期生成主任务触发器和日志说明。"""
        if not period:
            times = TimerUtils.random_scheduler(num_executions=2, begin_hour=9, end_hour=23,
                                                min_interval=2 * 60, max_interval=6 * 60)
            # 宿主会用 | 之前的 ID 替换旧任务，两个时刻需要注册在同一个触发器上。
            trigger = OrTrigger([CronTrigger(hour=t.hour, minute=t.minute, timezone=tz) for t in times])
            return trigger, "每日随机 " + "、".join(t.strftime("%H:%M") for t in times)
        if len(period.split()) == 5:
            return CronTrigger.from_crontab(period, timezone=tz), f"Cron {period}"

        amount, separator, window = period.partition("/")
        interval = timedelta(hours=float(amount))
        if interval <= timedelta(0):
            raise ValueError("小时间隔必须大于零")
        first_run = now + interval
        if separator:
            start, end = map(int, window.split("-"))
            if not (0 <= start <= 23 and start <= end <= 24):
                raise ValueError("执行时段应为起止小时，例如 0-8 或 9-23")
            if not start <= first_run.astimezone(tz).hour <= end:
                opening = CronTrigger(hour=start, minute=0, timezone=tz)
                first_run = opening.get_next_fire_time(None, first_run)
            self._start_time, self._end_time = start, end
        trigger = IntervalTrigger(seconds=interval.total_seconds(), start_date=first_run, timezone=tz)
        return trigger, f"小时间隔 {period}"

    def _scheduled_sign_in(self):
        """按主计划触发签到，小时窗口仅约束定时主任务。"""
        if self._start_time is not None:
            hour = datetime.now(pytz.timezone(settings.TZ)).hour
            if not self._start_time <= hour <= self._end_time:
                logger.info(f"当前 {hour} 点在主任务时段 {self._start_time}-{self._end_time} 点以外，跳过本次调度")
                return
        logger.info("签到主任务按执行计划触发")
        self.sign_in()

    def _legacy_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        # 站点的可选项（内置站点 + 自定义站点）
        customSites = self.__custom_sites()

        site_options = ([{"title": "全部", "value": self._ALL_SITES}]
                        + [{"title": site.name, "value": site.id}
                         for site in SiteOper().list_order_by_pri()]
                        + [{"title": site.get("name"), "value": site.get("id")}
                           for site in customSites])
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '发送通知',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'clean',
                                            'label': '清理本日缓存',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'retry_time',
                                            'label': '失败重试时间',
                                            'placeholder': 'HH:MM，如 09:01，留空关闭',
                                            'hint': '对当日失败/未签到站点再跑一次，用于 9 点后才开放签到的站点',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '5位cron表达式，留空自动'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'queue_cnt',
                                            'label': '队列数量'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'retry_keyword',
                                            'label': '重试关键词',
                                            'placeholder': '支持正则表达式，命中才重签'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'auto_cf',
                                            'label': '自动优选',
                                            'placeholder': '命中重试关键词次数（0-关闭）'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'chips': True,
                                            'multiple': True,
                                            'model': 'sign_sites',
                                            'label': '签到站点',
                                            'items': site_options,
                                            'hint': '选择全部后自动包含后续新增站点',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'chips': True,
                                            'multiple': True,
                                            'model': 'login_sites',
                                            'label': '登录站点',
                                            'items': site_options,
                                            'hint': '选择全部后自动包含后续新增站点',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '执行周期支持：'
                                                    '1、5位cron表达式；'
                                                    '2、配置间隔（小时），如2.3/9-23（9-23点之间每隔2.3小时执行一次）；'
                                                    '3、周期不填默认9-23点随机执行2次。'
                                                    '每天首次全量执行，其余执行命中重试关键词的站点。'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '自动优选：0-关闭，命中重试关键词次数大于该数量时自动执行Cloudflare IP优选（需要开启且则正确配置Cloudflare IP优选插件和自定义Hosts插件）'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'warning',
                                            'variant': 'tonal',
                                            'text': '不是所有的站点都会把程序自动登录/签到定义为用户活跃（比如馒头），提示签到/登录成功仍然存在掉号风险！请结合站点公告说明自行把握。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "notify": True,
            "cron": "",
            "auto_cf": 0,
            "onlyonce": False,
            "clean": False,
            "queue_cnt": 5,
            "sign_sites": [],
            "login_sites": [],
            "retry_keyword": "错误|失败"
        }

    def _resolve_site_ids(self, selected: list) -> list:
        """为详情页展开全选标记，包含内置站点和已启用的自定义站点。"""
        if self._ALL_SITES in selected:
            return [site.id for site in SiteOper().list_order_by_pri()] + [
                site.get("id") for site in self.__custom_sites()
            ]
        return selected

    def __custom_sites(self) -> List[Any]:
        """读取已启用的自定义站点插件配置。"""
        custom_sites = []
        custom_sites_config = self.get_config("CustomSites")
        if custom_sites_config and custom_sites_config.get("enabled"):
            custom_sites = custom_sites_config.get("sites")
        return custom_sites

    def _task_sites(self) -> Tuple[List[dict], set]:
        """Use configured accounts, independently of the host's search-indexer rules.

        SitesHelper omits domains absent from its resource bundle (including
        uhddolby.com on V2). Sign-in has its own rules and must use the same
        database as the site selector, retaining credentials and connection options.
        """
        fields = ('id', 'name', 'domain', 'url', 'cookie', 'ua', 'apikey', 'token',
                  'proxy', 'render', 'public', 'note', 'timeout', 'is_active')
        configured = [{key: getattr(site, key) for key in fields if hasattr(site, key)}
                      for site in SiteOper().list_order_by_pri()]
        builtin_ids = {site['id'] for site in configured if site.get('id') is not None}
        seen = {str(sid) for sid in builtin_ids}
        for site in self.__custom_sites() or []:
            sid = site.get('id')
            if sid is not None and str(sid) not in seen:
                configured.append(dict(site))
                seen.add(str(sid))
        sites = [site for site in configured if site.get('id') is not None
                 and str(site.get('is_active', True)).lower() not in ('false', '0')
                 and str(site.get('public', False)).lower() not in ('true', '1')]
        return sites, builtin_ids

    def _legacy_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """
        # 获取最近14天的日期数组
        date_list = [(datetime.now() - timedelta(days=i)).date() for i in range(14)]

        # 获取所有数据，包括签到和登录历史
        all_data = {
            "signin": [],  # 签到数据
            "login": []  # 登录数据
        }
        sites_info = self._build_sites_info()

        # 获取常规日期格式数据
        for day in date_list:
            day_str = f"{day.month}月{day.day}日"
            day_formatted = day.strftime('%Y-%m-%d')

            # 获取"月日"格式数据
            day_data = self.get_data(day_str)
            if day_data:
                # 添加日期信息到每条记录
                if isinstance(day_data, list):
                    for record in day_data:
                        if isinstance(record, dict):
                            record["date"] = day_str
                            record["day_obj"] = day
                            # 区分签到和登录数据
                            if "登录" in record.get("status", ""):
                                all_data["login"].append(record)
                            else:
                                all_data["signin"].append(record)

            # 获取"签到-yyyy-mm-dd"和"登录-yyyy-mm-dd"格式数据
            signin_history = self.get_data(key="签到-" + day_formatted)
            if signin_history and isinstance(signin_history, dict):
                # 获取完成签到的站点ID列表
                done_sites = signin_history.get("do", [])
                retry_sites = signin_history.get("retry", [])

                # 为所有已完成签到的站点创建记录
                for site_id in done_sites:
                    site_name = self._get_site_display_name(site_id=site_id, sites_info=sites_info)
                    if not site_name:
                        continue

                    # 跳过需要重试的站点
                    if site_id in retry_sites:
                        status_text = "需要重试"
                    else:
                        status_text = "已签到"
                    all_data["signin"].append({
                        "site": site_name,
                        "status": status_text,
                        "date": day_str,
                        "day_obj": day,
                        "site_id": site_id
                    })

            # 获取登录历史数据
            login_history = self.get_data(key="登录-" + day_formatted)
            if login_history and isinstance(login_history, dict):
                # 获取完成登录的站点ID列表
                done_sites = login_history.get("do", [])
                retry_sites = login_history.get("retry", [])

                # 为所有已完成登录的站点创建记录
                for site_id in done_sites:
                    site_name = self._get_site_display_name(site_id=site_id, sites_info=sites_info)
                    if not site_name:
                        continue

                    # 跳过需要重试的站点
                    if site_id in retry_sites:
                        status_text = "登录需要重试"
                    else:
                        status_text = "登录成功"
                    all_data["login"].append({
                        "site": site_name,
                        "status": status_text,
                        "date": day_str,
                        "day_obj": day,
                        "site_id": site_id
                    })

        # 如果没有数据且没有配置站点，显示提示信息
        if not all_data["signin"] and not all_data["login"] and not self._sign_sites and not self._login_sites:
            return [{
                'component': 'VAlert',
                'props': {
                    'type': 'info',
                    'text': '暂无签到记录：插件会在设定时间自动执行，也可在插件配置中开启「立即运行一次」生成记录',
                    'variant': 'tonal',
                    'class': 'mt-4',
                    'prepend-icon': 'mdi-information'
                }
            }]

        # 按站点分组并去重数据
        signin_site_data = {}
        login_site_data = {}

        # 处理签到数据 - 每个站点每天只保留一条最新记录
        site_day_records = {}  # 用于去重: {site}_{date} -> record
        for data in all_data["signin"]:
            site_name = data.get("site", "未知站点")
            date_str = data.get("date", "")
            site_day_key = f"{site_name}_{date_str}"

            # 存储或更新记录（如有多条取最新）
            site_day_records[site_day_key] = data

        # 整理去重后的数据
        for key, record in site_day_records.items():
            site_name = record.get("site", "未知站点")
            if site_name not in signin_site_data:
                signin_site_data[site_name] = []
            signin_site_data[site_name].append(record)

        # 处理登录数据 - 同样去重
        site_day_records = {}  # 重置去重字典
        for data in all_data["login"]:
            site_name = data.get("site", "未知站点")
            date_str = data.get("date", "")
            site_day_key = f"{site_name}_{date_str}"

            # 存储或更新记录
            site_day_records[site_day_key] = data

        # 整理去重后的数据
        for key, record in site_day_records.items():
            site_name = record.get("site", "未知站点")
            if site_name not in login_site_data:
                login_site_data[site_name] = []
            login_site_data[site_name].append(record)

        # 补齐已配置但暂无历史记录的站点，详情页能直接看出未记录项。
        for site_id in self._resolve_site_ids(self._sign_sites):
            site_name = self._get_site_display_name(site_id=site_id, sites_info=sites_info)
            if not site_name:
                continue
            signin_site_data.setdefault(site_name, [])
        for site_id in self._resolve_site_ids(self._login_sites):
            site_name = self._get_site_display_name(site_id=site_id, sites_info=sites_info)
            if not site_name:
                continue
            login_site_data.setdefault(site_name, [])

        display_dates = date_list[:7]
        today_label = self._date_label(day=date_list[0])
        signin_stats = self._calculate_day_stats(site_data=signin_site_data, date_label=today_label)
        login_stats = self._calculate_day_stats(site_data=login_site_data, date_label=today_label)

        # 添加紧凑状态矩阵样式
        return [
            {
                'component': 'style',
                'text': """
                .autosignin-page {
                    display: flex;
                    flex-direction: column;
                    gap: 12px;
                }
                .autosignin-summary {
                    display: grid;
                    grid-template-columns: repeat(4, minmax(0, 1fr));
                    gap: 8px;
                }
                .autosignin-stat {
                    min-width: 0;
                    padding: 10px 12px;
                }
                .autosignin-stat__head {
                    display: flex;
                    align-items: center;
                    gap: 6px;
                    min-width: 0;
                    color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
                    font-size: .75rem;
                    font-weight: 600;
                    line-height: 1.25;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                }
                .autosignin-stat__head .v-icon {
                    color: rgb(var(--app-card-accent-rgb));
                }
                .autosignin-stat__value {
                    margin-top: 8px;
                    font-size: 1.25rem;
                    font-weight: 700;
                    line-height: 1;
                    letter-spacing: 0;
                }
                .autosignin-stat__meta {
                    margin-top: 4px;
                    color: rgba(var(--v-theme-on-surface), .56);
                    font-size: .72rem;
                    line-height: 1.25;
                    white-space: nowrap;
                    overflow: hidden;
                    text-overflow: ellipsis;
                }
                .autosignin-section {
                    min-width: 0;
                }
                .autosignin-section-head {
                    display: flex;
                    align-items: center;
                    gap: 8px;
                    min-height: 30px;
                    margin-bottom: 6px;
                }
                .autosignin-section-title {
                    font-size: .95rem;
                    font-weight: 700;
                    letter-spacing: 0;
                }
                .autosignin-table-wrap {
                    overflow-x: auto;
                    border: 1px solid rgba(var(--v-theme-on-surface), .08);
                    border-radius: 8px;
                }
                .autosignin-table {
                    min-width: 620px;
                }
                html[data-theme="transparent"] .autosignin-table-wrap,
                .v-theme--transparent .autosignin-table-wrap {
                    backdrop-filter: blur(var(--transparent-blur, 10px));
                    background-color: rgba(var(--v-theme-surface), 0) !important;
                }
                html[data-theme="transparent"] .autosignin-table,
                html[data-theme="transparent"] .autosignin-table .v-table__wrapper,
                html[data-theme="transparent"] .autosignin-table table,
                html[data-theme="transparent"] .autosignin-table tbody tr,
                .v-theme--transparent .autosignin-table,
                .v-theme--transparent .autosignin-table .v-table__wrapper,
                .v-theme--transparent .autosignin-table table,
                .v-theme--transparent .autosignin-table tbody tr {
                    background-color: transparent !important;
                }
                .autosignin-table th {
                    height: 34px !important;
                    padding: 0 8px !important;
                    color: rgba(var(--v-theme-on-surface), .62);
                    font-size: .75rem;
                    font-weight: 600 !important;
                    white-space: nowrap;
                }
                .autosignin-table td {
                    height: 38px !important;
                    padding: 0 8px !important;
                    vertical-align: middle;
                }
                .autosignin-table tbody tr:last-child td {
                    border-bottom: 0 !important;
                }
                .autosignin-site-name {
                    max-width: 160px;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                    font-weight: 600;
                    line-height: 1.2;
                }
                .autosignin-site-meta {
                    margin-top: 2px;
                    color: rgba(var(--v-theme-on-surface), .52);
                    font-size: .68rem;
                    line-height: 1.1;
                }
                .autosignin-status-cell {
                    min-width: 92px;
                }
                .autosignin-dot-cell {
                    width: 40px;
                    text-align: center;
                }
                .autosignin-dot {
                    width: 22px;
                    height: 22px;
                    display: inline-flex;
                    align-items: center;
                    justify-content: center;
                    border-radius: 999px;
                    border: 1px solid transparent;
                    font-weight: 700;
                }
                .autosignin-dot .v-icon {
                    opacity: 1;
                }
                .autosignin-dot--success {
                    color: rgb(var(--v-theme-success));
                    background: rgba(var(--v-theme-success), .24);
                    border-color: rgba(var(--v-theme-success), .38);
                }
                .autosignin-dot--warning {
                    color: rgb(var(--v-theme-warning));
                    background: rgba(var(--v-theme-warning), .30);
                    border-color: rgba(var(--v-theme-warning), .48);
                }
                .autosignin-dot--error {
                    color: rgb(var(--v-theme-error));
                    background: rgba(var(--v-theme-error), .26);
                    border-color: rgba(var(--v-theme-error), .42);
                }
                .autosignin-dot--none {
                    color: rgba(var(--v-theme-on-surface), .68);
                    background: rgba(var(--v-theme-on-surface), .14);
                    border-color: rgba(var(--v-theme-on-surface), .22);
                }
                @media (max-width: 720px) {
                    .autosignin-page {
                        gap: 10px;
                    }
                    .autosignin-summary {
                        grid-template-columns: repeat(2, minmax(0, 1fr));
                    }
                    .autosignin-stat {
                        padding: 8px 10px;
                    }
                    .autosignin-stat__value {
                        font-size: 1.08rem;
                    }
                    .autosignin-table {
                        min-width: 560px;
                    }
                    .autosignin-table th,
                    .autosignin-table td {
                        padding-left: 6px !important;
                        padding-right: 6px !important;
                    }
                    .autosignin-site-name {
                        max-width: 126px;
                    }
                    .autosignin-dot-cell {
                        width: 34px;
                    }
                }
                """
            },
            {
                'component': 'div',
                'props': {
                    'class': 'autosignin-page'
                },
                'content': [
                    self._build_summary(signin_stats=signin_stats, login_stats=login_stats, days=len(date_list)),
                    self._build_legend(),
                    self._build_status_section(
                        title="签到状态",
                        icon="mdi-calendar-check",
                        site_data=signin_site_data,
                        display_dates=display_dates,
                        empty_text="暂无签到数据"
                    ),
                    self._build_status_section(
                        title="登录状态",
                        icon="mdi-login-variant",
                        site_data=login_site_data,
                        display_dates=display_dates,
                        empty_text="暂无登录数据"
                    )
                ]
            }
        ]

    @staticmethod
    def _add_site_info(sites_info: dict, site_id: Any, site_name: Any) -> None:
        """
        记录站点ID到名称的映射，兼容历史记录中ID类型不一致的情况。
        """
        if site_id is None or not site_name:
            return
        sites_info[site_id] = site_name
        sites_info[str(site_id)] = site_name

    def _build_sites_info(self) -> dict:
        """
        汇总系统站点、索引器站点和自定义站点名称，供详情页历史记录反查。
        """
        sites_info = {}
        for site in SitesHelper().get_indexers():
            if not site.get("public"):
                self._add_site_info(
                    sites_info=sites_info,
                    site_id=site.get("id"),
                    site_name=site.get("name")
                )
        for site in SiteOper().list_order_by_pri():
            self._add_site_info(
                sites_info=sites_info,
                site_id=getattr(site, "id", None),
                site_name=getattr(site, "name", None)
            )
        for site in self.__custom_sites():
            self._add_site_info(
                sites_info=sites_info,
                site_id=site.get("id"),
                site_name=site.get("name")
            )
        return sites_info

    @staticmethod
    def _get_site_display_name(site_id, sites_info: dict) -> Optional[str]:
        """
        根据站点ID获取详情页中展示的站点名称，查不到时返回空值便于跳过。
        """
        site_id_str = str(site_id)
        return sites_info.get(site_id_str) or sites_info.get(site_id)

    @staticmethod
    def _status_meta(status_text: str) -> dict:
        """
        将签到或登录状态文本转换为页面展示需要的颜色、图标和排序权重。
        """
        status_text = str(status_text or "").strip()
        if "Cookie已失效" in status_text or "失效" in status_text:
            return {
                "level": "error",
                "color": "error",
                "icon": "mdi-cookie-off",
                "label": status_text or "Cookie失效",
                "sort": 0
            }
        if "失败" in status_text or "错误" in status_text:
            return {
                "level": "error",
                "color": "error",
                "icon": "mdi-alert-circle",
                "label": status_text or "失败",
                "sort": 0
            }
        if "重试" in status_text:
            return {
                "level": "warning",
                "color": "warning",
                "icon": "mdi-refresh",
                "label": status_text or "需要重试",
                "sort": 1
            }
        if "未检测到签到结果" in status_text:
            return {
                "level": "warning",
                "color": "warning",
                "icon": "mdi-help-circle-outline",
                "label": status_text,
                "sort": 1
            }
        if "无签到功能" in status_text or "无签到 API" in status_text:
            return {
                "level": "none",
                "color": "grey",
                "icon": "mdi-information-outline",
                "label": status_text,
                "sort": 2
            }
        if "成功" in status_text or "已签到" in status_text:
            return {
                "level": "success",
                "color": "success",
                "icon": "mdi-check-circle",
                "label": status_text or "成功",
                "sort": 3
            }
        if status_text:
            return {
                "level": "none",
                "color": "grey",
                "icon": "mdi-help-circle-outline",
                "label": status_text,
                "sort": 2
            }
        return {
            "level": "none",
            "color": "grey",
            "icon": "mdi-minus-circle-outline",
            "label": "未记录",
            "sort": 2
        }

    @staticmethod
    def _latest_record(records: list, date_label: str = None) -> dict:
        """
        获取指定日期或整个记录列表中的最新一条记录。
        """
        filtered_records = records
        if date_label:
            filtered_records = [record for record in records if record.get("date") == date_label]
        if not filtered_records:
            return {}
        try:
            return sorted(
                filtered_records,
                key=lambda item: item.get("day_obj", datetime.min.date()),
                reverse=True
            )[0]
        except Exception as e:
            logger.debug(f"获取最新记录失败: {str(e)}")
            return filtered_records[0]

    @staticmethod
    def _date_label(day) -> str:
        """
        将日期对象格式化为历史记录使用的月日标签。
        """
        return f"{day.month}月{day.day}日"

    @classmethod
    def _calculate_day_stats(cls, site_data: dict, date_label: str) -> dict:
        """
        统计指定日期下各站点的成功、异常和未记录数量。
        """
        stats = {
            "total": len(site_data),
            "recorded": 0,
            "success": 0,
            "warning": 0,
            "error": 0,
            "none": 0
        }
        for records in site_data.values():
            record = cls._latest_record(records=records, date_label=date_label)
            if not record:
                stats["none"] += 1
                continue
            stats["recorded"] += 1
            level = cls._status_meta(record.get("status", "")).get("level")
            if level in stats:
                stats[level] += 1
        return stats

    @staticmethod
    def _build_stat_item(label: str, value: str, meta: str, color: str, icon: str) -> dict:
        """
        构建顶部紧凑统计块。
        """
        return {
            'component': 'div',
            'props': {
                'class': 'autosignin-stat app-card-shell app-card-colorful',
                'style': f'--app-card-accent-rgb: var(--v-theme-{color});'
            },
            'content': [
                {
                    'component': 'div',
                    'props': {
                        'class': 'autosignin-stat__head'
                    },
                    'content': [
                        {
                            'component': 'VIcon',
                            'props': {
                                'size': 'x-small',
                                'color': color
                            },
                            'text': icon
                        },
                        {
                            'component': 'span',
                            'text': label
                        }
                    ]
                },
                {
                    'component': 'div',
                    'props': {
                        'class': f'autosignin-stat__value text-{color}'
                    },
                    'text': value
                },
                {
                    'component': 'div',
                    'props': {
                        'class': 'autosignin-stat__meta'
                    },
                    'text': meta
                }
            ]
        }

    @classmethod
    def _build_legend(cls) -> dict:
        """构建状态图例，解释状态矩阵中圆点颜色的含义。"""
        items = [
            ("success", "成功 / 已签到"),
            ("warning", "未确认签到结果（需关注）"),
            ("error", "失败 / Cookie 失效"),
            ("none", "未记录 / 无签到功能"),
        ]
        return {
            'component': 'div',
            'props': {
                'class': 'd-flex flex-wrap align-center ga-3 mb-3'
            },
            'content': [
                {
                    'component': 'div',
                    'props': {'class': 'd-flex align-center ga-1'},
                    'content': [
                        {
                            'component': 'span',
                            'props': {'class': f"autosignin-dot autosignin-dot--{level}"}
                        },
                        {
                            'component': 'span',
                            'props': {'class': 'text-caption text-medium-emphasis'},
                            'text': label
                        }
                    ]
                }
                for level, label in items
            ]
        }

    @classmethod
    def _build_summary(cls, signin_stats: dict, login_stats: dict, days: int) -> dict:
        """
        构建详情页顶部的签到和登录概要。
        """
        signin_total = signin_stats.get("total") or 0
        login_total = login_stats.get("total") or 0
        signin_problem_count = (signin_stats.get("error") or 0) + (signin_stats.get("warning") or 0)
        signin_missing_count = signin_stats.get("none") or 0
        signin_color = "info"
        if signin_total:
            signin_color = "success" if not signin_problem_count and not signin_missing_count else "warning"
        if signin_total and signin_stats.get("error"):
            signin_color = "error"

        return {
            'component': 'div',
            'props': {
                'class': 'autosignin-summary'
            },
            'content': [
                cls._build_stat_item(
                    label="今日签到",
                    value=f"{signin_stats.get('success') or 0}/{signin_total}",
                    meta=f"异常 {signin_problem_count} · 未记录 {signin_missing_count}",
                    color=signin_color,
                    icon="mdi-calendar-check"
                ),
                cls._build_stat_item(
                    label="异常重试",
                    value=str(signin_problem_count),
                    meta=f"失败 {signin_stats.get('error') or 0} · 重试 {signin_stats.get('warning') or 0}",
                    color="error" if signin_stats.get("error") else "warning",
                    icon="mdi-alert-circle-outline"
                ),
                cls._build_stat_item(
                    label="今日登录",
                    value=f"{login_stats.get('success') or 0}/{login_total}",
                    meta=f"异常 {(login_stats.get('error') or 0) + (login_stats.get('warning') or 0)} · 未记录 {login_stats.get('none') or 0}",
                    color="success" if login_total and not login_stats.get("error") and not login_stats.get("warning") else "info",
                    icon="mdi-login-variant"
                ),
                cls._build_stat_item(
                    label="历史范围",
                    value=f"{days}天",
                    meta="矩阵显示最近7天",
                    color="info",
                    icon="mdi-history"
                )
            ]
        }

    @classmethod
    def _build_status_section(cls, title: str, icon: str, site_data: dict, display_dates: list, empty_text: str) -> dict:
        """
        构建签到或登录状态区块，使用按站点排列的紧凑矩阵展示最近状态。
        """
        return {
            'component': 'div',
            'props': {
                'class': 'autosignin-section'
            },
            'content': [
                {
                    'component': 'div',
                    'props': {
                        'class': 'autosignin-section-head'
                    },
                    'content': [
                        {
                            'component': 'VIcon',
                            'props': {
                                'size': 'small',
                                'color': 'primary'
                            },
                            'text': icon
                        },
                        {
                            'component': 'span',
                            'props': {
                                'class': 'autosignin-section-title'
                            },
                            'text': title
                        },
                        {
                            'component': 'VSpacer'
                        },
                        {
                            'component': 'VChip',
                            'props': {
                                'size': 'x-small',
                                'variant': 'tonal',
                                'color': 'primary'
                            },
                            'text': f"{len(site_data)} 个站点"
                        }
                    ]
                },
                cls._build_status_table(
                    site_data=site_data,
                    display_dates=display_dates,
                    empty_text=empty_text
                )
            ]
        }

    @classmethod
    def _build_status_table(cls, site_data: dict, display_dates: list, empty_text: str) -> dict:
        """
        构建按站点和日期交叉展示的状态表格。
        """
        if not site_data:
            return {
                'component': 'VAlert',
                'props': {
                    'type': 'info',
                    'text': empty_text,
                    'variant': 'tonal',
                    'density': 'compact',
                    'prepend-icon': 'mdi-information'
                }
            }

        table_headers = [
            {
                'component': 'th',
                'props': {
                    'class': 'text-start'
                },
                'text': '站点'
            },
            {
                'component': 'th',
                'props': {
                    'class': 'text-start'
                },
                'text': '今日'
            }
        ]
        for day in display_dates:
            table_headers.append({
                'component': 'th',
                'props': {
                    'class': 'text-center'
                },
                'text': f"{day.month}/{day.day}"
            })

        sorted_sites = sorted(
            site_data.items(),
            key=lambda item: cls._site_sort_key(site_name=item[0], records=item[1], display_dates=display_dates)
        )
        table_rows = []
        for site_name, records in sorted_sites:
            table_rows.append(cls._build_status_row(site_name=site_name, records=records, display_dates=display_dates))

        return {
            'component': 'div',
            'props': {
                'class': 'autosignin-table-wrap'
            },
            'content': [
                {
                    'component': 'VTable',
                    'props': {
                        'hover': True,
                        'density': 'compact',
                        'class': 'autosignin-table'
                    },
                    'content': [
                        {
                            'component': 'thead',
                            'content': [
                                {
                                    'component': 'tr',
                                    'content': table_headers
                                }
                            ]
                        },
                        {
                            'component': 'tbody',
                            'content': table_rows
                        }
                    ]
                }
            ]
        }

    @classmethod
    def _site_sort_key(cls, site_name: str, records: list, display_dates: list) -> tuple:
        """
        生成站点行排序键，让今日异常和未记录站点优先展示。
        """
        today_label = cls._date_label(day=display_dates[0]) if display_dates else ""
        today_record = cls._latest_record(records=records, date_label=today_label)
        latest_record = cls._latest_record(records=records)
        status_meta = cls._status_meta(today_record.get("status", "") if today_record else "")
        latest_day = latest_record.get("day_obj", datetime.min.date()) if latest_record else datetime.min.date()
        return status_meta.get("sort", 2), -latest_day.toordinal(), site_name

    @classmethod
    def _build_status_row(cls, site_name: str, records: list, display_dates: list) -> dict:
        """
        构建单个站点在状态矩阵中的一行。
        """
        today_label = cls._date_label(day=display_dates[0]) if display_dates else ""
        today_record = cls._latest_record(records=records, date_label=today_label)
        today_status = today_record.get("status", "") if today_record else ""
        today_meta = cls._status_meta(today_status)
        row_cells = [
            {
                'component': 'td',
                'content': [
                    {
                        'component': 'div',
                        'props': {
                            'class': 'autosignin-site-name',
                            'title': site_name
                        },
                        'text': site_name
                    },
                    {
                        'component': 'div',
                        'props': {
                            'class': 'autosignin-site-meta'
                        },
                        'text': f"{len(records)} 条记录" if records else "暂无记录"
                    }
                ]
            },
            {
                'component': 'td',
                'props': {
                    'class': 'autosignin-status-cell'
                },
                'content': [
                    {
                        'component': 'VChip',
                        'props': {
                            'size': 'x-small',
                            'variant': 'tonal',
                            'color': today_meta.get("color"),
                            'prepend-icon': today_meta.get("icon")
                        },
                        'text': today_meta.get("label")
                    }
                ]
            }
        ]
        for day in display_dates:
            date_label = cls._date_label(day=day)
            record = cls._latest_record(records=records, date_label=date_label)
            row_cells.append({
                'component': 'td',
                'props': {
                    'class': 'autosignin-dot-cell'
                },
                'content': [
                    cls._build_status_dot(record=record, date_label=date_label)
                ]
            })
        return {
            'component': 'tr',
            'content': row_cells
        }

    @classmethod
    def _build_status_dot(cls, record: dict, date_label: str) -> dict:
        """
        构建矩阵中单日状态的图标点。
        """
        status_text = record.get("status", "") if record else ""
        status_meta = cls._status_meta(status_text)
        return {
            'component': 'span',
            'props': {
                'class': f"autosignin-dot autosignin-dot--{status_meta.get('level')}",
                'title': f"{date_label} {status_meta.get('label')}"
            },
            'content': [
                {
                    'component': 'VIcon',
                    'props': {
                        'size': 'x-small'
                    },
                    'text': status_meta.get("icon")
                }
            ]
        }

    @eventmanager.register(EventType.PluginAction)
    def sign_in(self, event: Event = None):
        """One shared run for cron, retries, API and commands; never overlap."""
        if event and (event.event_data or {}).get("action") != "site_signin":
            return
        if not (self._enabled or getattr(self, "_allow_once", False)):
            return
        if not self._run_lock.acquire(blocking=False):
            logger.info("增强版签到：上一轮仍在执行，本次跳过")
            return
        try:
            with notification_batch(self, datetime.today().strftime('%Y-%m-%d')):
                self._run_signin(event)
            self._schedule_short_retry()
        finally:
            self._allow_once = False
            self._run_lock.release()

    def _run_signin(self, event: Event = None):
        """
        自动签到|模拟登录
        """
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "site_signin":
                return
        if not self._sign_sites and not self._login_sites:
            logger.warning("未选择签到或登录站点，本次不执行；请先设置站点范围")
            return
        # 日期
        today = datetime.today()
        generation = self._generation
        if self._clean:
            for action in ("签到", "登录"):
                self.del_data(key=f"{action}-{today:%Y-%m-%d}")
            self.del_data(key=f"{today.month}月{today.day}日")
            self._clean = False
            self.__update_config()
        if event:
            logger.info("收到命令，开始站点签到 ...")
            self.post_message(channel=event.event_data.get("channel"),
                              title="PT 签到管家 · 开始执行签到与登录任务",
                              userid=event.event_data.get("user"))

        signin_results = {}
        if self._sign_sites:
            signin_results = self.__do(today=today, type_str="签到", do_sites=self._sign_sites, event=event) or {}
        if generation == self._generation and self._login_sites:
            self.__do(today=today, type_str="登录", do_sites=self._login_sites, event=event,
                      signin_results=signin_results)


    def __do(self, today: datetime, type_str: str, do_sites: list, event: Event = None,
             signin_results: dict = None) -> dict:
        """执行并保存实际结果；登录复用本轮签到验证，通知仅列出独立执行的站点。"""
        generation = self._generation
        self._clean_history(today)

        # 查看今天有没有签到|登录历史
        today = today.strftime('%Y-%m-%d')
        today_history = self.get_data(key=type_str + "-" + today)
        results = (today_history or {}).get("results") or {}

        # 查询所有站点
        all_sites, builtin_site_ids = self._task_sites()
        # 过滤掉没有选中的站点
        if do_sites and self._ALL_SITES not in do_sites:
            selected = {str(site_id) for site_id in do_sites}
            do_sites = [site for site in all_sites if str(site.get("id")) in selected]
        else:
            do_sites = all_sites

        # 记录本轮真实站点ID，历史、重试与通知不能使用全选标记。
        selected_site_ids = [site.get("id") for site in do_sites]

        # 今日没数据
        if not today_history or self._clean:
            logger.info(f"今日 {today} 未{type_str}，开始{type_str}已选站点")
            if self._clean:
                # 关闭开关
                self._clean = False
        else:
            # 需要重试站点
            retry_sites = today_history.get("retry") or []
            # 今天已签到|登录站点
            already_sites = today_history.get("do") or []

            # 今日未签|登录站点
            no_sites = [site for site in do_sites if not self._action_completed(
                results.get(str(site.get("id")), {}), type_str, site)]

            if not no_sites:
                logger.info(f"今日 {today} 已{type_str}，无重新{type_str}站点，本次任务结束")
                return {str(site_id): results[str(site_id)] for site_id in selected_site_ids
                        if str(site_id) in results}

            # 任务站点 = 需要重试+今日未do
            do_sites = no_sites
            logger.info(f"今日 {today} 已执行{type_str}，重试未确认登录或命中关键词的站点")

        if not do_sites:
            logger.info(f"没有需要{type_str}的站点")
            return {}

        # 执行签到
        logger.info(f"开始执行{type_str}任务 ...")
        reused_ids = set()
        if type_str == "签到":
            status = self.__run_sites(self.signin_site, do_sites, type_str)
        else:
            # 只有签到成功或实际登录证据可以复用；签到失败不能代表登录失败。
            reused = signin_results or {}
            reused_ids = {site.get("id") for site in do_sites if str(site.get("id")) in reused}
            # A missing check-in endpoint says nothing about this account's session.
            # Read only the configured homepage. The sign-in result remains retryable.
            entry_missing = [site for site in do_sites if site.get('id') in reused_ids
                             and not reused[str(site['id'])].get('success')
                             and not reused[str(site['id'])].get('logged_in')
                             and re.search(r'HTTP\s*[:：=]?\s*404|没找到可用的签到入口|打开签到页失败', str(reused[str(site['id'])].get('message', '')))]
            entry_ids = {site['id'] for site in entry_missing}
            reused_ids = {sid for sid in reused_ids if reused[str(sid)].get('success')
                          or reused[str(sid)].get('logged_in')}
            login_sites = [site for site in do_sites if site.get('id') not in reused_ids | entry_ids]
            login_results = {}
            if login_sites:
                login_results = dict(zip([site.get("id") for site in login_sites],
                    self.__run_sites(self.login_site, login_sites, type_str)))
            if entry_missing:
                login_results.update(zip([site['id'] for site in entry_missing],
                                         self.__run_sites(self._read_account_home, entry_missing, type_str)))
            status = []
            for site in do_sites:
                if site.get("id") not in reused_ids:
                    status.append(login_results[site.get("id")])
                    continue
                result = reused[str(site.get("id"))]
                logged_in = bool(result.get("success") or result.get("logged_in"))
                status.append(SiteResult(site.get("name"), "登录成功（签到任务已确认）" if logged_in else result["message"],
                                         logged_in, logged_in, activity=result.get('activity', ''),
                                         engine=result.get('engine', '')))

        if generation != self._generation:
            return {}
        if status:
            logger.info(f"站点{type_str}任务完成！")
            notification_changed = False
            # 获取今天的日期
            key = f"{datetime.now().month}月{datetime.now().day}日"
            today_data = self.get_data(key)
            if today_data:
                if not isinstance(today_data, list):
                    today_data = [today_data]
                for site, s in zip(do_sites, status):
                    today_data.append({
                        "site": s[0],
                        "status": s[1],
                        "site_id": site.get("id"),
                        "type": type_str,
                        "success": s.success,
                        "logged_in": s.logged_in, "bonus": s.bonus, "activity": s.activity, "engine": s.engine,
                        "signin_supported": s.signin_supported,
                    })
            else:
                today_data = [{
                    "site": s[0],
                    "status": s[1],
                    "site_id": site.get("id"),
                    "type": type_str,
                    "success": s.success,
                    "logged_in": s.logged_in, "bonus": s.bonus, "activity": s.activity, "engine": s.engine,
                    "signin_supported": s.signin_supported,
                } for site, s in zip(do_sites, status)]
            # 保存数据
            self.save_data(key, today_data)

            # 命中重试词的站点id
            retry_sites = []
            # 命中重试词的站点签到msg
            retry_msg = []
            # 登录成功
            login_success_msg = []
            # 签到成功
            sign_success_msg = []
            # 已签到
            already_sign_msg = []
            # 仿真签到成功
            fz_sign_msg = []
            # 失败｜错误
            failed_msg = []

            for site, s in zip(do_sites, status):
                site_name = s[0]
                # ThreadPool.map 保持输入顺序，直接绑定 ID，兼容同名和自定义站点。
                site_id = site.get("id")
                current_result = {"success": s.success, "message": s.message,
                    "logged_in": s.logged_in, "bonus": s.bonus, "activity": s.activity,
                    "engine": s.engine, "signin_supported": s.signin_supported}
                if type_str == '签到' and s.success and self._is_u2(site):
                    from .ptengine.sites.u2 import current_time
                    current_result['signin_day'] = current_time().date().isoformat()
                notification_changed = (notification_changed or notification_result_changed(
                    results.get(str(site_id)), current_result))
                results[str(site_id)] = current_result

                if site_id not in reused_ids and not s.logged_in and 'Cookie已失效' in s.message and site_id in builtin_site_ids:
                    # 触发自动登录插件登录
                    logger.info(f"触发站点 {site_name} 自动登录更新Cookie和Ua")
                    self.eventmanager.send_event(EventType.PluginAction,
                                                 {
                                                     "site_id": site_id,
                                                     "action": "site_refresh"
                                                 })
                # 记录本次命中重试关键词的站点
                if not s.completed:
                    retry_sites.append(site_id)
                if self._retry_keyword and not s.completed:
                    if site_id:
                        try:
                            match = re.search(self._retry_keyword, s[1])
                        except re.error:
                            match = None
                        if match:
                            logger.debug(f"站点 {site_name} 命中重试关键词 {self._retry_keyword}")
                            if site_id not in retry_sites:
                                retry_sites.append(site_id)
                            # 命中的站点
                            if site_id not in reused_ids:
                                retry_msg.append(s)
                            continue

                if site_id in reused_ids:
                    continue
                if not s.completed:
                    failed_msg.append(s)
                elif not s.success or "登录成功" in s.message:
                    login_success_msg.append(s)
                elif "仿真签到成功" in str(s):
                    fz_sign_msg.append(s)
                    continue
                elif "签到成功" in str(s):
                    sign_success_msg.append(s)
                elif '已签到' in str(s):
                    already_sign_msg.append(s)
                else:
                    failed_msg.append(s)

            logger.debug(f"下次{type_str}重试站点 {retry_sites}")

            # 存入历史
            self.save_data(key=type_str + "-" + today,
                           value={
                               "do": list(dict.fromkeys((today_history or {}).get("do", []) + selected_site_ids)),
                               "retry": retry_sites,
                               "results": results,
                           })

            # 自动Cloudflare IP优选
            if self._auto_cf and int(self._auto_cf) > 0 and retry_msg and len(retry_msg) >= int(self._auto_cf):
                self.eventmanager.send_event(EventType.PluginAction, {
                    "action": "cloudflare_speedtest"
                })

            # 发送通知
            notify_rows = [(site, result) for site, result in zip(do_sites, status)
                           if site.get("id") not in reused_ids]
            queue_notification(self, today, type_str, status,
                               [site.get('id') for site in do_sites], selected_site_ids,
                               retry_sites, notification_changed)
            if event and notify_rows:
                self.post_message(channel=event.event_data.get("channel"),
                                  title=f"PT 签到管家 · {type_str}任务执行结束", userid=event.event_data.get("user"))
        else:
            logger.error(f"站点{type_str}任务失败！")
            if event:
                self.post_message(channel=event.event_data.get("channel"),
                                  title=f"PT 签到管家 · {type_str}任务失败", userid=event.event_data.get("user"))
        # 保存配置
        self.__update_config()
        return {str(site_id): results[str(site_id)] for site_id in selected_site_ids if str(site_id) in results}


    def __build_class(self, url) -> Any:
        """按站点地址匹配专用签到处理器。"""
        for site_schema in self._site_schema:
            try:
                if site_schema.match(url):
                    return site_schema
            except Exception as e:
                logger.error("站点模块加载失败：%s" % str(e))
        return None

    def signin_by_domain(self, url: str, apikey: str) -> schemas.Response:
        """The domain API shares daily deduplication and returns actual sign-in success."""
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")
        from .ptengine.core import normalize_host
        domain = normalize_host(url if '://' in url else 'https://' + url)
        sites, _ = self._task_sites()
        site = next((item for item in sites if domain and
                     normalize_host(item.get('url') or '') == domain), None)
        if not site:
            return schemas.Response(success=False, message="站点不存在")
        if not self._enabled or not self._bridge:
            return schemas.Response(success=False, message="插件未启用")
        if not self._run_lock.acquire(blocking=False):
            return schemas.Response(success=False, message="签到任务正在运行")
        try:
            results = self.__do(datetime.today(), "签到", [site.get("id")])
            result = results.get(str(site.get("id")), {})
            return schemas.Response(success=bool(result.get("success")),
                message=result.get("message") or "没有可执行的站点",
                data={"signin_success": bool(result.get("success")),
                      "login_success": bool(result.get("logged_in"))})
        finally:
            self._run_lock.release()

    @staticmethod
    def _normalize_site_info(site_info: CommentedMap) -> CommentedMap:
        """
        规整站点信息中的 Cookie。

        CookieCloud 同步的 Cookie 可能带换行，放进请求头会被 requests 判为
        非法而静默失败，这里统一清理后再交给站点模块与通用流程使用。
        """
        cookie = site_info.get("cookie")
        if not cookie or not re.search(r"[\r\n\t]", cookie):
            return site_info
        normalized = dict(site_info)
        normalized["cookie"] = re.sub(r"[\r\n\t]+", "", cookie)
        return normalized

    def _legacy_signin_site(self, site_info: CommentedMap) -> Tuple[str, str]:
        """
        签到一个站点
        """
        site_info = self._normalize_site_info(site_info)
        site_module = self.__build_class(site_info.get("url"))
        # 开始记时
        start_time = datetime.now()
        if site_module and hasattr(site_module, "signin"):
            try:
                state, message = site_module().signin(site_info)
            except Exception as e:
                traceback.print_exc()
                state, message = False, f"签到失败：{str(e)}"
        else:
            state, message = self.__signin_base(site_info)
        # 统计
        seconds = (datetime.now() - start_time).seconds
        domain = StringUtils.get_url_domain(site_info.get('url'))
        if state:
            SiteOper().success(domain=domain, seconds=seconds)
        else:
            SiteOper().fail(domain)
        return site_info.get("name"), message

    @staticmethod
    def __signin_base(site_info: CommentedMap) -> Tuple[bool, str]:
        """
        通用签到处理
        :param site_info: 站点信息
        :return: 签到结果信息
        """
        if not site_info:
            return False, ""
        site = site_info.get("name")
        site_url = site_info.get("url")
        site_cookie = site_info.get("cookie")
        ua = site_info.get("ua")
        render = site_info.get("render")
        proxies = settings.PROXY if site_info.get("proxy") else None
        proxy_server = settings.PROXY_SERVER if site_info.get("proxy") else None
        timeout = site_info.get("timeout") or 60
        if not site_url or not site_cookie:
            logger.warn(f"未配置 {site} 的站点地址或Cookie，无法签到")
            return False, ""
        # 模拟登录
        try:
            # 访问链接
            checkin_url = site_url
            if site_url.find("attendance.php") == -1:
                # 拼登签到地址
                checkin_url = urljoin(site_url, "attendance.php")
            logger.info(f"开始站点签到：{site}，地址：{checkin_url}...")
            if render:
                page_source = PlaywrightHelper().get_page_source(url=checkin_url,
                                                                 cookies=site_cookie,
                                                                 ua=ua,
                                                                 proxies=proxy_server,
                                                                 timeout=timeout)
                if not SiteUtils.is_logged_in(page_source):
                    if under_challenge(page_source):
                        return False, f"无法通过Cloudflare！"
                    return False, f"仿真登录失败，Cookie已失效！"
                else:
                    # 判断是否已签到
                    if re.search(r'已签|签到已得', page_source, re.IGNORECASE) \
                            or SiteUtils.is_checkin(page_source):
                        return True, f"签到成功"
                    return True, "仿真签到成功"
            else:
                res = RequestUtils(cookies=site_cookie,
                                   ua=ua,
                                   proxies=proxies,
                                   timeout=timeout
                                   ).get_res(url=checkin_url)
                if not res and site_url != checkin_url:
                    logger.info(f"开始站点模拟登录：{site}，地址：{site_url}...")
                    res = RequestUtils(cookies=site_cookie,
                                       ua=ua,
                                       proxies=proxies,
                                       timeout=timeout
                                       ).get_res(url=site_url)
                # 判断登录状态
                if res and res.status_code in [200, 500, 403]:
                    if not SiteUtils.is_logged_in(res.text):
                        if under_challenge(res.text):
                            msg = "站点被Cloudflare防护，请打开站点浏览器仿真"
                        elif res.status_code == 200:
                            msg = "Cookie已失效"
                        else:
                            msg = f"状态码：{res.status_code}"
                        logger.warn(f"{site} 签到失败，{msg}")
                        return False, f"签到失败，{msg}！"
                    elif _is_signed_page(res.text):
                        logger.info(f"{site} 签到成功")
                        return True, f"签到成功"
                    elif _need_captcha_page(res.text):
                        msg = "签到需要验证码，请手动签到或为该站点适配专用签到"
                        logger.warn(f"{site} 签到失败，{msg}")
                        return False, f"签到失败，{msg}！"
                    else:
                        # 只能确认登录态，无法确认签到结果，不再直接上报签到成功
                        logger.warn(f"{site} 未能确认签到结果，仅确认模拟登录成功")
                        return True, "模拟登录成功（未检测到签到结果）"
                elif res is not None:
                    logger.warn(f"{site} 签到失败，状态码：{res.status_code}")
                    return False, f"签到失败，状态码：{res.status_code}！"
                else:
                    logger.warn(f"{site} 签到失败，无法打开网站")
                    return False, f"签到失败，无法打开网站！"
        except Exception as e:
            logger.warn("%s 签到失败：%s" % (site, str(e)))
            traceback.print_exc()
            return False, f"签到失败：{str(e)}！"

    def _legacy_login_site(self, site_info: CommentedMap) -> Tuple[str, str]:
        """
        模拟登录一个站点
        """
        site_info = self._normalize_site_info(site_info)
        site_module = self.__build_class(site_info.get("url"))
        # 开始记时
        start_time = datetime.now()
        if site_module and hasattr(site_module, "login"):
            try:
                state, message = site_module().login(site_info)
            except Exception as e:
                traceback.print_exc()
                state, message = False, f"模拟登录失败：{str(e)}"
        else:
            state, message = self.__login_base(site_info)
        # 统计
        seconds = (datetime.now() - start_time).seconds
        domain = StringUtils.get_url_domain(site_info.get('url'))
        if state:
            SiteOper().success(domain=domain, seconds=seconds)
        else:
            SiteOper().fail(domain)
        return site_info.get("name"), message

    @staticmethod
    def __login_base(site_info: CommentedMap) -> Tuple[bool, str]:
        """
        模拟登录通用处理
        :param site_info: 站点信息
        :return: 签到结果信息
        """
        if not site_info:
            return False, "模拟登录失败，缺少站点信息"
        site = site_info.get("name")
        site_url = site_info.get("url")
        site_cookie = site_info.get("cookie")
        ua = site_info.get("ua")
        render = site_info.get("render")
        proxies = settings.PROXY if site_info.get("proxy") else None
        proxy_server = settings.PROXY_SERVER if site_info.get("proxy") else None
        timeout = site_info.get("timeout") or 60
        if not site_url or not site_cookie:
            logger.warn(f"未配置 {site} 的站点地址或Cookie，无法登录")
            return False, "模拟登录失败，未配置站点地址或Cookie"
        # 模拟登录
        try:
            # 访问链接
            if "attendance.php" in site_url:
                site_url = urljoin(site_url, "./")
            logger.info(f"开始站点模拟登录：{site}")
            if render:
                page_source = PlaywrightHelper().get_page_source(url=site_url,
                                                                 cookies=site_cookie,
                                                                 ua=ua,
                                                                 proxies=proxy_server,
                                                                 timeout=timeout)
            else:
                res = RequestUtils(cookies=site_cookie,
                                   ua=ua,
                                   proxies=proxies,
                                   timeout=timeout
                                   ).get_res(url=site_url, raise_exception=True)
                if res is None:
                    return False, f"模拟登录失败，未收到 HTTP 响应（站点超时设置 {timeout} 秒）"
                if under_challenge(res.text):
                    return False, "模拟登录失败，站点被Cloudflare防护，请打开站点浏览器仿真"
                if res.status_code != 200:
                    return False, f"模拟登录失败，状态码：{res.status_code}！"
                page_source = res.text
            if not page_source:
                return False, "模拟登录失败，站点返回空页面"
            if under_challenge(page_source):
                return False, "模拟登录失败，无法通过Cloudflare！"
            if not has_login_evidence(page_source):
                if has_login_form(page_source):
                    return False, "模拟登录失败，Cookie已失效"
                return False, "模拟登录失败，未识别到登录状态"
            logger.info(f"{site} 模拟登录成功")
            return True, "模拟登录成功"
        except (request_errors.Timeout, TimeoutError):
            reason = f"请求超时（站点超时设置 {timeout} 秒）"
        except request_errors.ProxyError:
            reason = "无法通过站点配置的代理连接"
        except request_errors.SSLError:
            reason = "TLS 连接失败，请检查站点证书和网络"
        except (request_errors.ConnectionError, ConnectionError):
            reason = "站点连接失败或被中断"
        except Exception as error:
            reason = f"请求异常（{type(error).__name__}）"
        logger.warn(f"{site} 模拟登录失败：{reason}")
        return False, f"模拟登录失败：{reason}"


    def stop_service(self):
        """
        退出插件
        """
        self._generation += 1
        if self._bridge:
            self._bridge.stop()
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown(wait=False)
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))

    @eventmanager.register(EventType.SiteDeleted)
    def site_deleted(self, event):
        """
        删除对应站点选中
        """
        site_id = event.event_data.get("site_id")
        config = self.get_config()
        if config:
            self._sign_sites = self.__remove_site_id(config.get("sign_sites") or [], site_id)
            self._login_sites = self.__remove_site_id(config.get("login_sites") or [], site_id)
            # 保存配置
            self.__update_config()

    def __remove_site_id(self, do_sites, site_id):
        """移除已删除站点；全选模式保留以便自动纳入后续新增站点。"""
        if self._ALL_SITES in do_sites:
            return [self._ALL_SITES]
        if do_sites:
            if isinstance(do_sites, str):
                do_sites = [do_sites]

            # 删除对应站点
            if site_id:
                do_sites = [site for site in do_sites if int(site) != int(site_id)]
            else:
                # 清空
                do_sites = []

            # 若无站点，则停止
            if len(do_sites) == 0:
                self._enabled = False

        return do_sites

    def _clean_history(self, today: datetime):
        """清除14天窗口外的签到、登录和月日明细，兼顾停跑后的过期记录。"""
        first_day = today.date() - timedelta(days=self._HISTORY_DAYS - 1)
        retained_days = {
            self._date_label(today.date() - timedelta(days=offset))
            for offset in range(self._HISTORY_DAYS)
        }
        for data in self.get_data() or []:
            key = data.key
            if re.fullmatch(r"(?:签到|登录)-\d{4}-\d{2}-\d{2}", key):
                try:
                    day = datetime.strptime(key.split("-", 1)[1], "%Y-%m-%d").date()
                except ValueError:
                    continue
                if day < first_day:
                    self.del_data(key=key)
            elif re.fullmatch(r"\d{1,2}月\d{1,2}日", key) and key not in retained_days:
                # 旧明细键不含年份，按窗口内的月日保留，避免跨年误删。
                self.del_data(key=key)

    @staticmethod
    def _site_result(site_name: str, state: bool, message: str, action: str) -> SiteResult:
        """保留明确布尔结果；空结果或失败分支中的成功字样不能伪装成成功通知。"""
        message = str(message or "").strip()
        success = state is True and bool(message)
        if not message:
            message = f"{action}失败，未返回可确认的结果"
        elif not success and "失败" not in message:
            message = f"{action}失败：{message}"
        if success and action == "签到" and "登录成功" in message:
            return SiteResult(site_name, f"{message}（未执行签到）", False, True)
        return SiteResult(site_name, message, success, success)



    def get_dashboard_meta(self) -> Optional[List[Dict[str, str]]]:
        return [{'key': 'ptsigninkeeper_dashboard', 'name': 'PT 签到管家'}]

    def get_dashboard(self, key: str, **kwargs):
        if key != 'ptsigninkeeper_dashboard':
            return None
        from .native_dashboard import render
        return ({'cols': 12, 'md': 6}, {'refresh': 300, 'title': '签到与登录'}, render(self))

    @staticmethod
    def get_render_mode() -> Tuple[str, str]:
        return 'vue', 'dist/assets'

    def get_sidebar_nav(self) -> List[Dict[str, Any]]:
        """只注册插件主入口，设置由页面内部打开。"""
        return [{'nav_key': 'main', 'title': 'PT 签到管家', 'icon': 'mdi-calendar-check-outline',
                 'section': 'organize', 'permission': 'manage', 'order': 40}]

    def _ui_routes(self):
        routes = [('/data', self.ui_data, 'GET'), ('/detail', self.ui_detail, 'GET'),
                  ('/options', self.ui_options, 'GET'), ('/export', self.ui_export, 'GET'),
                  ('/run', self.ui_run, 'POST'),
                  ('/browserless-quota', self.ui_browserless_quota, 'POST'),
                  ('/notification-preview', self.ui_notification_preview, 'POST')]

        return [{'path': '/ui' + path, 'endpoint': endpoint, 'methods': [method],
                 'auth': 'bear', 'summary': '增强版 ' + path[1:], 'response_model': dict}
                for path, endpoint, method in routes]

    def ui_notification_preview(self) -> dict:
        from .notification import send_preview
        return send_preview(self)

    def ui_browserless_quota(self, payload: dict) -> dict:
        from .browserless_quota import lookup_quota
        return lookup_quota(payload)

    def ui_run(self, scope: str = 'pending', site: str = '') -> dict:
        result = ui_actions.start_run(self, 'signin', scope=scope, site=site)
        return {**result, 'data': {'message': result['message'], 'task': dict(getattr(self, '_ui_task', {}))}}

    def ui_data(self, query: str = '', category: str = 'all', sort: str = 'priority',
                page: int = 1, page_size: int = 20, day: str = '', include_inactive: bool = False) -> dict:
        result = ui.list_sites(self, query=query, category=category, sort=sort, page=page,
                               page_size=page_size, day=day, include_inactive=include_inactive)
        return {**result, 'task': dict(getattr(self, '_ui_task', {}))}

    def ui_detail(self, site: str, day: str = '') -> dict:
        return ui.site_detail(self, site=site, day=day)

    def _claim_quiz_attempt(self, site_id: str) -> bool:
        """Persist before POST so retries and reloads cannot repeat an uncertain answer."""
        from datetime import datetime
        key = 'quiz-attempt-' + str(site_id)
        today = datetime.today().date().isoformat()
        if (self.get_data(key=key) or {}).get('day') == today:
            return False
        self.save_data(key=key, value={'day': today})
        return (self.get_data(key=key) or {}).get('day') == today

    def ui_options(self) -> dict:
        from .ptengine.quiz import AssistantSolver
        config = self.get_config() or {}
        return {'sites': [{'id': item['id'], 'name': item['name'] + ('' if item['active'] else '（已停用）')}
                          for item in ui._metadata(self).values()],
                'quiz_ai': AssistantSolver(True, config.get('quiz_ai_timeout', 60)).status()}

    def ui_export(self, query: str = '', category: str = 'all', day: str = '',
                  include_inactive: bool = False) -> dict:
        return {'items': ui.export_rows(self, query=query, category=category, day=day,
                                        include_inactive=include_inactive)}

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        from .integration import EXTRA_DEFAULTS
        defaults = {**EXTRA_DEFAULTS, 'enabled': False, 'notify': True, 'cron': '',
                    'onlyonce': False, 'clean': False, 'queue_cnt': 5,
                    'sign_sites': [], 'login_sites': [], 'retry_keyword': '错误|失败',
                    'retry_time': '09:01', 'auto_cf': 0}
        return [], {**defaults, **(self.get_config() or {})}

    def get_page(self) -> List[dict]:
        """Vue fetches one page at a time through the read-only UI API."""
        return []

    def _schedule_short_retry(self):
        """Retry only failed daily records, without occupying a worker while waiting."""
        if not self._enabled or not self._bridge or self._bridge.stopped:
            return
        limit = max(0, min(int(self._extra_config.get("retry_count", 1)), 5))
        if self._short_retry_attempt >= limit:
            self._short_retry_attempt = 0
            return
        today = datetime.today().strftime("%Y-%m-%d")
        pending = any((self.get_data(key=f"{action}-{today}") or {}).get("retry")
                      for action in ("签到", "登录"))
        if not pending:
            self._short_retry_attempt = 0
            return
        self._short_retry_attempt += 1
        minutes = max(1, min(int(self._extra_config.get("retry_delay_minutes", 10)), 1440))
        self._scheduler = self._scheduler or BackgroundScheduler(timezone=settings.TZ)
        self._scheduler.add_job(self.sign_in, trigger="date",
            run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(minutes=minutes),
            id="ptsigninkeeper_short_retry", replace_existing=True, name="增强版失败重试")
        if not self._scheduler.running:
            self._scheduler.start()

    @staticmethod
    def _action_completed(result, action, site=None):
        """A failed check-in is retryable even when the session is authenticated."""
        if action == '签到' and PTSignInKeeper._is_u2(site or {}) and result.get('success'):
            from .ptengine.sites.u2 import current_time
            return result.get('signin_day') == current_time().date().isoformat()
        if result.get("success"):
            return True
        return bool(result.get("logged_in") and (
            action == "登录" or result.get("signin_supported") is False))

    @staticmethod
    def _is_u2(site):
        from .ptengine.core import normalize_host
        return normalize_host(str(site.get('url') or site.get('domain') or '')) == 'u2.dmhy.org'


    def signin_site(self, site_info: CommentedMap) -> SiteResult:
        """Select one implementation, retaining enhanced-specific site behavior."""
        return self._bridge.perform(self._normalize_site_info(site_info), "签到",
                                    self.__login_base)

    def _read_account_home(self, site_info: CommentedMap) -> SiteResult:
        from .sites.nexusphp_attendance import NexusPhpAttendance
        ok, message = NexusPhpAttendance.ordinary_receipt(self._normalize_site_info(site_info), '登录')
        activity = ''
        if ok and self._extra_config.get('read_activity') and self._bridge:
            activity, _ = self._bridge.engine._read_site_activity(self._normalize_site_info(site_info))
        return SiteResult(site_info.get('name'), message, ok, ok, activity=activity, engine='站点 HTTP')

    def login_site(self, site_info: CommentedMap) -> SiteResult:
        """Login-only uses the same adapters and never submits a check-in."""
        return self._bridge.perform(self._normalize_site_info(site_info), "登录",
                                    self.__login_base)








def record_to_row(record):
    """辅助函数：将记录转换为表格行"""
    status = record.get("status", "")

    # 确定状态图标和颜色
    icon = "mdi-check-circle"
    color = "success"

    if "失败" in status or "错误" in status:
        icon = "mdi-alert-circle"
        color = "error"
    elif "Cookie已失效" in status:
        icon = "mdi-cookie-off"
        color = "error"
    elif "已签到" in status:
        icon = "mdi-check"
        color = "grey"
    elif "成功" in status:
        icon = "mdi-check-circle"
        color = "success"

    return {
        'component': 'tr',
        'props': {
            'class': 'text-sm'
        },
        'content': [
            {
                'component': 'td',
                'props': {
                    'class': 'text-start'
                },
                'text': record.get("date", "")
            },
            {
                'component': 'td',
                'props': {
                    'class': 'text-start'
                },
                'text': status
            },
            {
                'component': 'td',
                'props': {
                    'class': 'text-center'
                },
                'content': [
                    {
                        'component': 'VIcon',
                        'props': {
                            'color': color,
                            'size': 'small'
                        },
                        'text': icon
                    }
                ]
            }
        ]
    }
