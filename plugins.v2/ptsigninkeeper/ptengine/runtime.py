"""PT 站点签到插件（MoviePilot V3）

双引擎：
  1. HTTP 引擎 —— 直接请求站点签到接口（如 NexusPHP attendance.php），轻量快速；
  2. Browserless 引擎 —— 通过 Browserless BrowserQL 打开真实浏览器完成
     图片验证码、Cloudflare / Turnstile 挑战、弹窗签到与 Altcha 验证。

引擎默认按站点规则自动判定；未显式声明引擎的站点在 HTTP 直连撞上 Cloudflare
或验证码时，只有在 Browserless 站点白名单内才允许回退。

站点登录凭证直接读 MoviePilot「站点管理」里该站的「附加信息」(note)；没有账号密码时
复用站点管理里保存的 Cookie。每次签到前先用账号密码真实登录站点、换回新的 Cookie 再
交给签到引擎；签到结束后再打开站点页面读取
站点展示的「最近动向」（含站点自带的相对时间，如 2026-10-16 23:44:21 ( < 1分前 )），
写入结果页与通知。

参考 tmzg0000/moviepilot-plugins 的插件规范编写。
"""

from __future__ import annotations

import importlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Event, Lock, Timer
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from apscheduler.triggers.cron import CronTrigger
from app.log import logger
from app.db.site_oper import SiteOper

from app.schemas import NotificationType

from .core import has_account_evidence, login_page, plain_text
from .rewards import normalize_reward, daily_header_reward, SITE_UNITS
from .quiz import AssistantSolver
from .tokens import browserless_tokens
from .browserless_policy import browserless_site_ids
from .core import (
    DEFAULT_BROWSERLESS_URL,
    BrowserlessPool,
    BrowserlessSigner,
    LoginResult,
    SignResult,
    account_from_site,
    account_ready,
    fallback_rule,
    DEFAULT_INDEX_PATH,
    decode_entities,
    discover_signin_target,
    fetch_page,
    http_check_api_key,
    generic_api_key_rule,
    has_custom_rule,
    http_sign_auto,
    is_api_key_rule,
    playwright_sign,
    playwright_read_activity,
    run_site_module,
    run_site_module_login,
    is_login_record,
    is_signin_record,
    login as site_login,
    message_bonus,
    normalize_site_ids,
    parse_rules,
    read_activity,
    record_meta,
    resolve_rule,
    site_record_counts,
    site_module_for,
    site_state_key,
    summarize_records,
    target_url,
    normalize_host,
)

# 日志里的失败原因最多保留多少字。
LOG_TEXT_LIMIT = 120


def mask_proxy(host: str) -> str:
    """代理地址可能带账号密码，日志里把凭据打码。"""
    return re.sub(r"//[^@/]+@", "//***@", str(host or ""))


def compact_text(value: Any, limit: int = LOG_TEXT_LIMIT) -> str:
    """
    把日志文本压成单行，并限制最大长度。

    :param value: 待压缩的日志内容
    :param limit: 单条日志允许保留的最大字符数
    :return: 去除多余空白并截断后的单行文本
    """
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 1)].rstrip() + "…"



class RuleEngine:
    """Single-site operations only; the enhanced plugin owns all lifecycle and storage."""

    def __init__(self, config):
        self._stop_event = Event()
        self.quiz_solver = AssistantSolver(config.get("quiz_ai_enabled", False), config.get("quiz_ai_timeout", 60))
        self.quiz_claim = None
        self._site_rules = str(config.get("site_rules") or "")
        self._timeout = max(5, min(int(config.get("timeout") or 30), 120))
        self._read_activity = bool(config.get("read_activity", True))
        self._token_list = browserless_tokens(config)
        self._browserless_sites = frozenset(str(value) for value in
                                           browserless_site_ids(config.get('browserless_sites')))

    def stop(self):
        """Invalidate network work when the owning plugin reloads."""
        self._stop_event.set()

    def _browserless_tokens(self) -> List[str]:
        """返回任意数量的有效 Token，保留顺序并去空去重。"""
        return list(self._token_list)

    def _browserless_block_reason(self, info: Mapping[str, Any]) -> str:
        """Check every cloud-browser path, even when a shared signer already exists."""
        if not self._is_run_current(None):
            return "任务已停止，未调用 Browserless"
        ids = browserless_site_ids([info.get('id')])
        if not ids or str(ids[0]) not in self._browserless_sites:
            return "站点不在 Browserless 白名单中，已禁止调用"
        return ""

    def _sign_browserless(self, info, rule, signer, *, api=False) -> SignResult:
        """The only entry to the Browserless pool; rules cannot override this policy."""
        reason = self._browserless_block_reason(info)
        if reason:
            return SignResult("failed", reason)
        if signer is None:
            return SignResult("failed", "未配置 Browserless 地址或 Token")
        if api:
            return signer.sign_api(info, rule, timeout=max(120, self._timeout * 2))
        return signer.sign(info, rule)


    @staticmethod
    def _site_id(info: Dict[str, Any]) -> Optional[int]:
        """返回正整数站点 ID；字段异常时按未选择处理。"""
        try:
            site_id = int(str(info.get("id") or "").strip())
        except (TypeError, ValueError):
            return None
        return site_id if site_id > 0 else None


    @staticmethod
    def _key_only_site(info: Dict[str, Any]) -> bool:
        """站点只有 API Key / Token、没有 Cookie。是否可执行由显式 API 规则决定。"""
        cookie = str(info.get("cookie") or "").strip()
        credential = (str(info.get("apikey") or "").strip()
                      or str(info.get("token") or "").strip())
        return bool(credential and not cookie)


    def _is_run_current(self, run_id: Optional[int]) -> bool:
        """配置重载会递增 run_id；旧批次不得继续登录、签到或保存。"""
        event = getattr(self, "_stop_event", None)
        if event is not None and event.is_set():
            return False
        return run_id is None or run_id == getattr(self, "_run_id", run_id)


    def _site_guard(self, info: Dict[str, Any], run_id: Optional[int],
                    allowed_site_ids: Optional[Set[int]], action: str) -> str:
        """统一校验轮次与站点白名单；API Key 是否可执行由 _sign_one/_login_one 判断。"""
        if not self._is_run_current(run_id):
            return "任务已停止或被新配置取代，未执行" + action
        if allowed_site_ids is not None and self._site_id(info) not in allowed_site_ids:
            return f"站点不在本次{action}选择中，已跳过"
        return ""


    def _site_result(self, info: Dict[str, Any], *, engine: str, status: str,
                     message: str, login: str = "", activity: str = "",
                     activity_source: str = "", bonus: str = "", dual: bool = False,
                     login_status: str = "", logged_in: bool = False) -> Dict[str, Any]:
        """
        统一构造站点结果。

        :param info: MoviePilot 站点信息
        :param engine: 实际执行的签到引擎
        :param status: 签到或登录状态
        :param message: 结果说明
        :param login: 登录补充说明
        :param activity: 最近动向
        :param activity_source: 最近动向来源
        :param bonus: 奖励信息
        :param dual: 是否同时执行登录和签到
        :param login_status: 双任务站点的登录状态
        :return: 可保存和展示的统一结果字典
        """
        return {"site": str(info.get("name") or info.get("url") or "未知站点"),
                "site_id": str(info.get("id") or ""), "state_key": site_state_key(info),
                "engine": engine, "status": status, "message": message,
                "login": login, "activity": activity, "activity_source": activity_source,
                "ran_at": self._now(), "bonus": bonus, "dual": dual,
                "login_status": login_status, "logged_in": logged_in}


    @staticmethod
    def _log_failure(site_name: str, message: str) -> None:
        """
        输出单行失败日志。

        :param site_name: 站点名称
        :param message: 失败原因，完整内容仍保存在结果页
        """
        logger.warning(f"PT签到管家：{site_name} 失败：{compact_text(message)}")


    def _skip_result(self, info: Dict[str, Any], reason: str) -> Dict[str, Any]:
        return self._site_result(info, engine="-", status="failed", message=reason,
                                 dual=bool(info.get("dual")),
                                 login_status="login_failed" if info.get("dual") else "")


    def _sign_with_engine(self, info: Dict[str, Any], rule: Dict[str, Any], engine: str,
                              signer: Optional[BrowserlessSigner],
                              site_name: str) -> Optional[SignResult]:
        """按规则里的 engine 执行一次签到（站点模块失败后的回落路径）。"""
        try:
            if engine == "playwright":
                outcome = playwright_sign(info, rule, timeout=max(90, self._timeout * 2),
                                          proxies=self._proxies_for(info))
                if outcome is not None and (outcome.auth_required or outcome.status in {"success", "already"}):
                    return outcome
                # 本机成功时绝不消耗 Browserless 额度；只有浏览器助手缺失或本机流程失败，
                # 且规则允许时才回退。
                fallback_setting = rule.get("browserless_fallback")
                allow_fallback = (bool(fallback_setting) if fallback_setting is not None
                                  else outcome is None)
                if allow_fallback:
                    blocked = self._browserless_block_reason(info)
                    if blocked:
                        reason = "本机未提供 Playwright" if outcome is None else outcome.message
                        return SignResult("failed", f"{reason}；{blocked}")
                if allow_fallback and signer is not None:
                    reason = ("本机未提供 Playwright" if outcome is None
                              else f"本机 Playwright 失败：{outcome.message}")
                    logger.info(f"PT签到管家：{site_name} {reason}，改用 Browserless")
                    browser_rule = fallback_rule(
                        rule, outcome.challenge if outcome is not None else "", "")
                    fallback = self._sign_browserless(info, browser_rule, signer)
                    if fallback.status in {"success", "already"}:
                        return SignResult(fallback.status,
                                          f"{reason}；Browserless：{fallback.message}")
                    return SignResult("failed",
                                      f"{reason}；Browserless 回退：{fallback.message}")
                if outcome is not None:
                    if allow_fallback:
                        return SignResult(outcome.status,
                                          f"{outcome.message}；未配置 Browserless，无法回退")
                    return outcome
                if rule.get("browser_auth_mode"):
                    return SignResult(
                        "failed",
                        "该站点需要本机 Playwright 完成浏览器内登录，"
                        "当前 MoviePilot 环境没有可用的浏览器助手")
                return SignResult("failed", "本机缺少 Playwright，且未配置 Browserless")
            if engine == "browserless":
                return self._sign_browserless(info, rule, signer)
            outcome = http_sign_auto(info, rule, timeout=self._timeout,
                                     proxies=self._proxies_for(info))
            if outcome.status == "failed" and self._should_fallback(rule, outcome):
                outcome, _ = self._maybe_fallback(info, rule, outcome, signer)
            return outcome
        except ValueError as error:
            return SignResult("failed", str(error))


    def _should_init_signer(self, infos: List[Dict[str, Any]],
                           rules: Dict[str, Dict[str, Any]]) -> bool:
        """按最终生效的站点规则判断是否需要初始化 Browserless 引擎。

        彩虹岛等 quiz 规则只走一次 HTTP 提交与回读确认，失败后禁止改用
        Browserless 或通用表单，避免重复答题。显式 API Key 规则只访问固定
        端点；遇到 Cloudflare / 验证码时可用 Browserless 在浏览器上下文内重试。
        """
        for info in infos:
            if self._browserless_block_reason(info):
                continue
            try:
                rule = resolve_rule(info, rules)
            except ValueError:
                continue
            if is_api_key_rule(rule):
                if rule.get("browserless_fallback", True):
                    return True
                continue
            if self._key_only_site(info):
                module = site_module_for(str(info.get("url") or ""))
                if module is None or not hasattr(module, "signin"):
                    return True
                continue
            if str(rule.get("form_mode") or "").lower() == "quiz":
                continue
            if str(rule.get("engine")) == "browserless":
                return True
            if rule.get("auto_fallback") or rule.get("browserless_fallback"):
                return True
        return False


    def _sign_api_key_one(self, info: Dict[str, Any], rule: Dict[str, Any],
                          run_id: Optional[int], allowed_site_ids: Optional[Set[int]],
                          signer: Optional[Any] = None) -> Dict[str, Any]:
        """
        执行显式 API Key 签到。

        仅访问规则声明的端点，不触发站点登录或站点模块。
        API Key 规则可选 HTTP、本机 Playwright 或 Browserless 防护回退；
        HTTP / 本机 Playwright 检测到 Cloudflare 或验证码时，可由 Browserless
        在同一浏览器上下文内带上 API Key 重试。

        :param info: MoviePilot 站点信息
        :param rule: 已解析的 API Key 规则
        :param run_id: 当前任务轮次
        :param allowed_site_ids: 本轮允许访问的站点 ID
        :return: 统一签到结果
        """
        site_name = str(info.get("name") or info.get("url") or "未知站点")
        guard = self._site_guard(info, run_id, allowed_site_ids, "签到")
        if guard:
            logger.debug(f"PT签到管家：{site_name} 跳过（{guard}）")
            return self._skip_result(info, guard)
        dual = bool(info.get("dual"))
        engine = str(rule.get("engine") or "http").strip().lower()
        try:
            if engine == "playwright":
                outcome = playwright_sign(info, rule, timeout=max(90, self._timeout * 2),
                                          proxies=self._proxies_for(info))
                if outcome is None:
                    outcome = SignResult(
                        "failed",
                        "当前 MoviePilot 环境没有可用的本机 Playwright，"
                        "无法先通过 Cloudflare 再调用 API Key 接口")
            elif engine == "http":
                outcome = http_sign_auto(info, rule, timeout=self._timeout,
                                         proxies=self._proxies_for(info))
            else:
                outcome = SignResult("failed", "API Key 规则引擎只能是 http 或 playwright")
        except Exception:
            # 异常文本可能带站点回显，不把异常内容写进日志或结果。
            logger.error(f"PT签到管家：{site_name} API Key 签到异常")
            outcome = SignResult("failed", "API Key 签到出现未预期错误")
        blocked = self._browserless_block_reason(info)
        if outcome.status not in {"success", "already"} and outcome.challenge and blocked:
            outcome = SignResult(outcome.status, f"{outcome.message}；{blocked}",
                                 challenge=outcome.challenge)
        elif (outcome.status not in {"success", "already"} and outcome.challenge
                and signer is not None and hasattr(signer, "sign_api")):
            previous = outcome
            fallback = self._sign_browserless(info, rule, signer, api=True)
            if fallback.status in {"success", "already"}:
                outcome = SignResult(
                    fallback.status,
                    f"{previous.message}；Browserless：{fallback.message}",
                )
                engine = "browserless"
            else:
                outcome = SignResult(
                    fallback.status,
                    f"{previous.message}；Browserless：{fallback.message}",
                    challenge=fallback.challenge or previous.challenge,
                    transient=fallback.transient,
                )
                engine = "browserless"
        self._log_success(site_name, outcome.status, outcome.message, engine)
        if outcome.status not in {"success", "already"}:
            self._log_failure(site_name, outcome.message)
        activity_text, activity_source = (
            self._read_site_activity(info, rule, run_id, allowed_site_ids)
            if (outcome.status in {"success", "already"} and self._read_activity) else ("", ""))
        return self._site_result(
            info, engine=engine, status=outcome.status, message=outcome.message,
            login="API Key 已用于签到" if outcome.status in {"success", "already"}
            else "API Key 签到失败",
            activity=activity_text, activity_source=activity_source,
            bonus=message_bonus(outcome.message), dual=dual,
            login_status=(("login" if outcome.status in {"success", "already"}
                           else "login_failed") if dual else ""))


    @staticmethod
    def _log_success(site_name: str, status: str, message: str, engine: str = "-") -> None:
        """输出成功或已签到日志，便于在运行日志里确认每个站点的最终结果。"""
        labels = {"success": "签到成功", "already": "今日已签到", "login": "登录成功"}
        label = labels.get(str(status or ""))
        if not label:
            return
        detail = compact_text(message) if str(message or "").strip() else label
        logger.info(f"PT签到管家：{site_name} {label}（{engine or '-'}）：{detail}")

    def _sign_one(self, info: Dict[str, Any], rules: Dict[str, Dict[str, Any]],
                  signer: Optional[BrowserlessSigner], run_id: Optional[int] = None,
                  allowed_site_ids: Optional[Set[int]] = None) -> Dict[str, Any]:
        """
        执行单个站点的登录、签到和最近动向读取。

        :param info: MoviePilot 站点信息
        :param rules: 已解析的全部自定义规则
        :param signer: Browserless 执行器
        :param run_id: 当前任务轮次
        :param allowed_site_ids: 本轮允许访问的站点 ID
        :return: 统一签到结果
        """
        site_name = str(info.get("name") or info.get("url") or "未知站点")
        guard = self._site_guard(info, run_id, allowed_site_ids, "签到")
        if guard:
            logger.debug(f"PT签到管家：{site_name} 跳过（{guard}）")
            return self._skip_result(info, guard)

        try:
            rule = resolve_rule(info, rules)
            rule["_quiz_solver"] = self.quiz_solver
            if self.quiz_claim:
                rule["_quiz_claim"] = lambda: self.quiz_claim(str(info.get("id") or normalize_host(info.get("url", ""))))
        except ValueError as error:
            reason = f"API Key 规则无效，未发起请求：{error}" if self._key_only_site(info) else str(error)
            self._log_failure(site_name, reason)
            return self._skip_result(info, reason)

        if is_api_key_rule(rule):
            return self._sign_api_key_one(info, rule, run_id, allowed_site_ids, signer=signer)
        module_for_site = site_module_for(str(info.get("url") or ""))
        if self._key_only_site(info) and (
                module_for_site is None or not hasattr(module_for_site, "signin")):
            # 没有专用签到模块的纯 Key 站点使用通用 HTTP 兼容逻辑；
            # 有专用 signin 模块的站点仍优先走模块。
            try:
                generic_rule = generic_api_key_rule(info, rule)
            except ValueError as error:
                reason = f"通用 API Key 规则无效：{error}"
                self._log_failure(site_name, reason)
                return self._skip_result(info, reason)
            return self._sign_api_key_one(info, generic_rule, run_id, allowed_site_ids, signer=signer)

        engine = "http"
        logger.info(f"PT签到管家：开始处理 {site_name}（{str(rule.get('engine') or 'http')}）")
        login_text = ""
        login_broken = False
        login_proven = False
        dual = bool(info.get("dual"))
        account = account_from_site(info)
        browser_auth = str(rule.get("browser_auth_mode") or "").strip().lower()
        playwright_info: Dict[str, Any] = info

        if browser_auth:
            if account:
                playwright_info = dict(info)
                playwright_info.update(account)
            if account_ready(account):
                login_text = "浏览器内自动登录"
            elif account:
                login_text = "账号信息不完整，将尝试复用站点管理 Cookie"
            elif str(info.get("cookie") or "").strip():
                login_text = "复用站点管理 Cookie" if dual else ""
            else:
                login_text = "未配置账号密码或 Cookie"
        elif account_ready(account):
            try:
                login_result = site_login(info, account, timeout=self._timeout,
                                          proxies=self._proxies_for(info))
            except Exception as error:
                logger.error(f"PT签到管家：{site_name} 登录异常：{compact_text(error)}")
                login_text = "异常：真实登录出现未预期错误"
            else:
                if login_result.status == "success":
                    info = dict(info, cookie=login_result.cookie)
                    playwright_info = info
                    login_text = "成功（已换用新 Cookie）"
                    login_proven = True
                else:
                    login_text = "失败：" + login_result.message
                    login_broken = True
                    self._log_failure(site_name, login_text)
        elif account:
            login_text = "未提供可用的账号密码"
            login_broken = True
        elif str(info.get("cookie") or "").strip():
            login_text = "复用站点管理 Cookie" if dual else ""

        guard = self._site_guard(info, run_id, allowed_site_ids, "签到")
        if guard:
            logger.debug(f"PT签到管家：{site_name} 跳过（{guard}）")
            return self._skip_result(info, guard)

        try:
            engine = str(rule["engine"])
            quiz_mode = str(rule.get("form_mode") or "").lower() == "quiz"
            module_outcome = (None if quiz_mode else
                              run_site_module(info, timeout=self._timeout,
                                              proxies=self._proxies_for(info), logger=logger,
                                              rule=rule))
            if module_outcome is not None and module_outcome.status != "failed":
                outcome = module_outcome
                engine = "module"
            elif module_outcome is not None:
                module_for_site = site_module_for(str(info.get("url") or ""))
                if (getattr(module_for_site, "authoritative", False)
                        or str(info.get("apikey") or "").strip()):
                    outcome = module_outcome
                    engine = "module"
                else:
                    module_note = f"模块：{module_outcome.message}"
                    outcome = self._sign_with_engine(playwright_info, rule, engine, signer, site_name)
                    if outcome is None:
                        outcome = module_outcome
                        engine = "module"
                    elif outcome.status in {"success", "already"}:
                        outcome = SignResult(outcome.status,
                                             f"{outcome.message}（{module_note} 后由规则引擎完成）")
                        engine = str(rule["engine"])
                    else:
                        outcome = SignResult(outcome.status,
                                             f"{module_note}；规则引擎：{outcome.message}")
                        engine = str(rule["engine"])
            elif engine == "playwright":
                outcome = self._sign_with_engine(playwright_info, rule, engine, signer, site_name)
                engine = "playwright"
            elif engine == "browserless":
                blocked = self._browserless_block_reason(info)
                host = normalize_host(str(info.get("url") or ""))
                if (blocked and self._is_run_current(run_id) and not has_custom_rule(info, rules)
                        and host in {"hhanclub.net", "ptsbao.club", "hdkyl.in"}):
                    from ..sites.nexusphp_attendance import NexusPhpAttendance
                    engine = "http"
                    ok, message = NexusPhpAttendance.ordinary_receipt(info, personal_receipt=True)
                    if ok:
                        outcome = SignResult("already" if message.startswith("今日已签到") else "success", message)
                    else:
                        outcome = SignResult("failed", f"{message}；{blocked}")
                else:
                    outcome = self._sign_browserless(info, rule, signer)
            elif (normalize_host(str(info.get("url") or "")) in {"haidan.cc", "haidan.video"}
                  and not has_custom_rule(info, rules)):
                from .haidan import sign_in
                outcome = sign_in(info, rule, self._timeout, self._proxies_for(info))
            else:
                outcome = http_sign_auto(info, rule, timeout=self._timeout,
                                         proxies=self._proxies_for(info))
                if outcome.status == "failed" and self._should_fallback(rule, outcome):
                    outcome, engine = self._maybe_fallback(info, rule, outcome, signer)
        except ValueError as error:
            outcome = SignResult("failed", str(error))
        except Exception as error:
            logger.error(f"PT签到管家：{site_name} 异常：{compact_text(error)}")
            outcome = SignResult("failed", "签到执行出现未预期错误")

        self._log_success(site_name, outcome.status, outcome.message, engine)
        if outcome.status == "pending":
            logger.info(f"PT签到管家：{site_name} {compact_text(outcome.message)}")
        elif outcome.status not in {"success", "already"}:
            self._log_failure(site_name, outcome.message)
        if browser_auth and dual:
            if outcome.status in {"success", "already"}:
                login_text = ("浏览器内登录成功" if account_ready(account)
                              else "浏览器会话可用")
            else:
                login_broken = True

        guard = self._site_guard(info, run_id, allowed_site_ids, "签到")
        if guard:
            logger.debug(f"PT签到管家：{site_name} 跳过（{guard}）")
            return self._skip_result(info, guard)
        read_activity = (self._read_activity
                         and (outcome.status in {"success", "already"} or dual))
        activity_text, activity_source = (
            self._read_site_activity(info, rule, run_id, allowed_site_ids)
            if read_activity else ("", ""))
        bonus = (normalize_reward(message_bonus(outcome.message), str(info.get("url") or ""), outcome.message)
                 if outcome.status in {"success", "already"} else "")
        if outcome.status in {"success", "already"} and not rule.get('bonus'):
            bonus = self.read_header_reward(info) or bonus
        row = self._site_result(
            info, engine=engine, status=outcome.status, message=outcome.message,
            login=login_text, activity=activity_text, activity_source=activity_source,
            bonus=bonus, dual=dual,
            login_status=("login_failed" if login_broken else "login") if dual else "",
            logged_in=login_proven or outcome.status in {"success", "already"} or bool(activity_text))
        # 回退资格只来自服务明确拒绝首次预检的结构化证据；模块或登录已运行时不传递。
        row['browserless_not_started'] = bool(
            engine == 'browserless' and outcome.not_started and module_for_site is None
            and not account_ready(account) and not browser_auth and not dual)
        return row


    def _login_one(self, info: Dict[str, Any], run_id: Optional[int] = None,
                   allowed_site_ids: Optional[Set[int]] = None) -> Dict[str, Any]:
        """
        执行仅登录站点。

        真实登录或复用 Cookie 后读取最近动向，不触发签到。

        :param info: MoviePilot 站点信息
        :param run_id: 当前任务轮次
        :param allowed_site_ids: 本轮允许访问的站点 ID
        :return: 统一登录结果
        """
        site_name = str(info.get("name") or info.get("url") or "未知站点")
        guard = self._site_guard(info, run_id, allowed_site_ids, "仅登录")
        if guard:
            logger.debug(f"PT签到管家：{site_name} 跳过（{guard}）")
            return self._skip_result(info, guard)

        try:
            login_rule = resolve_rule(info, parse_rules(self._site_rules))
        except ValueError as error:
            if self._key_only_site(info):
                return self._skip_result(
                    info, f"API Key 规则无效，未发起请求：{error}")
            login_rule = {}

        module_login = run_site_module_login(
            info, timeout=self._timeout, proxies=self._proxies_for(info),
            logger=logger, rule=login_rule)
        if module_login is not None:
            login_ok = module_login.status == "success"
            login_text = module_login.message if login_ok else "失败：" + module_login.message
            if login_ok and module_login.cookie:
                info = dict(info, cookie=module_login.cookie)
            if not login_ok:
                self._log_failure(site_name, login_text)
            guard = self._site_guard(info, run_id, allowed_site_ids, "仅登录")
            if guard:
                return self._skip_result(info, guard)
            activity_text, activity_source = (
                self._read_site_activity(info, login_rule, run_id, allowed_site_ids)
                if (login_ok and self._read_activity) else ("", ""))
            return self._site_result(
                info, engine="http", status="login" if login_ok else "login_failed",
                message=login_text, activity=activity_text,
                activity_source=activity_source)

        if is_api_key_rule(login_rule):
            try:
                check = http_check_api_key(info, login_rule, timeout=self._timeout,
                                           proxies=self._proxies_for(info))
            except Exception:
                # 不记录异常原文，避免站点回显 API Key 时进入日志。
                logger.error(f"PT签到管家：{site_name} API Key 验证异常")
                check = LoginResult("failed", "API Key 验证出现未预期错误")
            login_ok = check.status == "success"
            if not login_ok:
                self._log_failure(site_name, check.message)
            guard = self._site_guard(info, run_id, allowed_site_ids, "仅登录")
            if guard:
                return self._skip_result(info, guard)
            activity_text, activity_source = (
                self._read_site_activity(info, login_rule, run_id, allowed_site_ids)
                if (login_ok and self._read_activity) else ("", ""))
            return self._site_result(
                info, engine="http", status="login" if login_ok else "login_failed",
                message=check.message, activity=activity_text,
                activity_source=activity_source)
        if self._key_only_site(info):
            return self._skip_result(
                info, "站点仅有 API Key / Token，但未声明 auth=api_key 登录规则，已跳过且未发起请求")

        login_text = ""
        login_ok = False
        account = account_from_site(info)
        if account_ready(account):
            try:
                login_result = site_login(info, account, timeout=self._timeout,
                                          proxies=self._proxies_for(info))
            except Exception as error:
                logger.error(f"PT签到管家：{site_name} 登录异常：{compact_text(error)}")
                login_text = "异常：真实登录出现未预期错误"
            else:
                if login_result.status == "success":
                    info = dict(info, cookie=login_result.cookie)
                    login_ok = True
                    login_text = "成功（已换用新 Cookie）"
                else:
                    login_text = "失败：" + login_result.message
        elif account:
            login_text = "未提供可用的账号密码"
        elif str(info.get("cookie") or "").strip():
            login_ok = True
        else:
            login_text = "未配置账号密码或 Cookie"
        if not login_ok:
            self._log_failure(site_name, login_text)

        guard = self._site_guard(info, run_id, allowed_site_ids, "仅登录")
        if guard:
            return self._skip_result(info, guard)
        activity_text, activity_source = (
            self._read_site_activity(info, run_id=run_id,
                                     allowed_site_ids=allowed_site_ids)
            if (login_ok and self._read_activity) else ("", ""))
        return self._site_result(
            info, engine="http", status="login" if login_ok else "login_failed",
            message=login_text, activity=activity_text, activity_source=activity_source)


    def _read_site_activity(self, info: Dict[str, Any],
                            rule: Optional[Dict[str, Any]] = None,
                            run_id: Optional[int] = None,
                            allowed_site_ids: Optional[Set[int]] = None) -> Tuple[str, str]:
        """用登录态 Cookie 打开站点页面，返回（「最近动向」原文, 来源页面）。

        来源页面写进记录；读不到时把原因（含试过的页面）也带回去，
        方便按站点用 activity_path 指定真正的动向页面。网络请求前统一校验
        轮次与站点白名单，避免配置重载或误选后继续访问站点。
        """
        guard = self._site_guard(info, run_id, allowed_site_ids, "读取最近动向")
        if guard:
            return "", guard
        rule = rule or {}
        if not rule:
            try:
                rule = resolve_rule(info, parse_rules(self._site_rules))
            except ValueError:
                rule = {}
        api_mode = is_api_key_rule(rule)
        cookie = str(info.get("cookie") or "").strip()
        activity_engine = str(rule.get("activity_engine") or "").strip().lower()
        account = account_from_site(info)
        # 某些站点可以直接用站点管理里的账号密码启动本机浏览器，
        # 不要求另外保存一份可能已经失效的 Cookie。
        if not api_mode and not cookie and not (
                activity_engine == "playwright" and account_ready(account)):
            return "", ""
        site_name = str(info.get("name") or info.get("url") or "未知站点")
        if activity_engine == "playwright":
            outcome = playwright_read_activity(
                info, rule, timeout=max(90, self._timeout * 2),
                proxies=self._proxies_for(info))
            if outcome is not None:
                if outcome.status == "success":
                    return outcome.last_seen or outcome.snippet or "已读取", outcome.source
                logger.debug(f"PT签到管家：{site_name} 本机浏览器读取最近动向失败"
                             f"（{compact_text(outcome.message)}）")
                return "", outcome.message
        # 站点常把「最近动向」放在签到页 / 校验页（例如观众 attendance.php），一并作为候选
        try:
            activity = read_activity(info, cookie, timeout=self._timeout,
                                     proxies=self._proxies_for(info),
                                     path=str(account.get("activity_path") or "") or None,
                                     rule=rule)
        except Exception:
            # 异常原文可能包含站点回显，API Key 模式下尤其不能写进日志。
            logger.debug(f"PT签到管家：{site_name} 读取最近动向异常")
            return "", ""
        if activity.status != "success":
            logger.debug(f"PT签到管家：{site_name} 未读到最近动向（{compact_text(activity.message)}）")
            return "", activity.message
        return activity.last_seen or activity.snippet or "已读取", activity.source

    def read_header_reward(self, info):
        """只读账号首页的当日奖励；回读失败不改变签到成功状态。"""
        host = normalize_host(str(info.get('url') or ''))
        if not info.get('cookie') or host in {'u2.dmhy.org', 'yemapt.org'}:
            return ''
        try:
            if host in {'rousi.pro', 'www.rousi.pro'}:
                from .sites import SignContext
                from .rewards import rousi_daily_reward
                from datetime import datetime
                ctx = SignContext(info, timeout=self._timeout, proxies=self._proxies_for(info))
                response = ctx._session().get(target_url(str(info.get('url') or ''), '/api/v1/me/attendance'),
                                              timeout=self._timeout, proxies=ctx.proxies, allow_redirects=False)
                try:
                    if response.status_code == 200:
                        return rousi_daily_reward(ctx.response_json(response), datetime.now().date().isoformat())
                finally:
                    ctx.close_response(response)
                return ''
            path = '/attendance.php?type=list' if host in {'pttime.org', 'www.pttime.org'} else '/index.php'
            header = fetch_page(target_url(str(info.get('url') or ''), path),
                                str(info.get('cookie') or ''), self._timeout,
                                self._proxies_for(info), str(info.get('ua') or ''))
            if has_account_evidence(header) and not login_page(header):
                if host in {'pttime.org', 'www.pttime.org'}:
                    from .rewards import pttime_daily_reward
                    from datetime import datetime
                    return pttime_daily_reward(header, datetime.now().date().isoformat())
                return daily_header_reward(plain_text(header), str(info.get('url') or ''))
        except Exception:
            pass
        return ''


    @staticmethod
    def _should_fallback(rule: Dict[str, Any], outcome: SignResult) -> bool:
        """这一轮失败要不要改用浏览器再试一次。

        两种情况：① 撞上 Cloudflare / 验证码；② 拿到了页面但没匹配到成功标志——
        后者常见于只能靠浏览器点击的站点，未写规则的站点也给它一次机会。
        404（找不到签到入口）、Cookie 失效这类重试没用，不折腾。
        """
        if outcome.auth_required or is_api_key_rule(rule):
            return False
        if str(rule.get("form_mode") or "").lower() == "quiz":
            # quiz 的提交结果只能靠回读确认；重试或换引擎会重复提交，必须禁用。
            return False
        if outcome.challenge:
            return True
        if not rule.get("auto_fallback") or outcome.missing_path:
            return False
        return "未匹配到" in str(outcome.message or "")


    def _maybe_fallback(self, info: Dict[str, Any], rule: Dict[str, Any], outcome: SignResult,
                            signer: Optional[BrowserlessSigner]) -> Tuple[SignResult, str]:
        """HTTP 失败后按需改用浏览器重试。

        默认回退到 Browserless；高清杜比这类站点可声明 ``local_browser_fallback``，
        优先用 MoviePilot 所在网络的本机 Playwright 点击；助手不可用时再尝试 Browserless。
        返回（结果, 实际使用的引擎）。
        """
        # 自动回退恒为开启（设置页不再提供开关）；用户在规则里显式写了 engine 的站点不参与
        if is_api_key_rule(rule):
            return outcome, "http"
        if str(rule.get("form_mode") or "").lower() == "quiz":
            return outcome, "http"
        if not rule.get("auto_fallback"):
            return outcome, "http"
        label = ("Cloudflare 验证" if outcome.challenge == "cloudflare"
                 else "验证码" if outcome.challenge
                 else "未匹配到签到结果")
        site_name = str(info.get("name") or info.get("url") or "未知站点")

        local_note = ""
        local_engine = "http"
        if rule.get("local_browser_fallback"):
            logger.info(f"PT签到管家：{site_name} 检测到{label}，先改用本机 Playwright 重试")
            local_engine = "playwright"
            local_result = playwright_sign(
                info, rule, timeout=max(90, self._timeout * 2),
                proxies=self._proxies_for(info))
            if local_result is not None and local_result.status in {"success", "already"}:
                return SignResult(
                    local_result.status,
                    f"{local_result.message}（HTTP 未确认，已改用本机 Playwright）",
                ), "playwright"
            if local_result is None:
                local_note = "本机未提供 Playwright"
            else:
                # A browser may already have clicked. Do not follow it with another submission.
                return local_result, "playwright"

        blocked = self._browserless_block_reason(info)
        if blocked:
            prefix = f"{outcome.message}；{local_note}；" if local_note else f"{outcome.message}；"
            return SignResult("failed", prefix + blocked), local_engine
        if signer is None:
            suffix = f"；{local_note}" if local_note else ""
            return SignResult(
                "failed",
                f"{outcome.message}；检测到{label}{suffix}；未配置 Browserless 地址或 Token",
            ), local_engine
        logger.info(f"PT签到管家：{site_name} 检测到{label}，自动改用 Browserless 重试")
        try:
            # 验证码站点要按页面推导选择器交给 Browserless 的 solveImageCaptcha
            page_html = ""
            if outcome.challenge == "captcha":
                page_html = fetch_page(target_url(str(info.get("url") or "").rstrip("/"),
                                                  str(rule.get("path") or DEFAULT_HTTP_PATH)),
                                       str(info.get("cookie") or ""), self._timeout,
                                       self._proxies_for(info), str(info.get("ua") or ""))
            fallback = fallback_rule(rule, outcome.challenge, page_html)
        except ValueError as error:
            prefix = f"{outcome.message}；{local_note}；" if local_note else f"{outcome.message}；"
            return SignResult("failed", f"{prefix}回退规则无效：{error}"), local_engine
        # 撞盾时未必知道该站真正的签到入口（首页挂「每日签到」表单的站点没有独立签到页）：
        # 顺手从首页找一次，找到就把它交给浏览器，别让浏览器去开一个 404 的地址
        base_url = str(info.get("url") or "").rstrip("/")
        discovered = discover_signin_target(
            fetch_page(target_url(base_url, DEFAULT_INDEX_PATH), str(info.get("cookie") or ""),
                       self._timeout, self._proxies_for(info), str(info.get("ua") or "")),
            base_url)
        if discovered.get("path"):
            fallback = dict(fallback, path=str(discovered["path"]))
            logger.info(f"PT签到管家：{site_name} 的签到入口改为 {discovered['path']}")
        retry = self._sign_browserless(info, fallback, signer)
        prefix = f"{outcome.message}；{local_note}；" if local_note else ""
        if retry.status in {"success", "already"}:
            return SignResult(
                retry.status,
                f"{prefix}{retry.message}；已自动改用 Browserless 通过{label}",
            ), "browserless"
        return SignResult(
            "failed",
            f"{prefix}Browserless 重试未通过：{retry.message}",
        ), "browserless"


    def _proxies_for(self, info: Optional[Mapping[str, Any]] = None
                     ) -> Optional[Dict[str, str]]:
        """这个站点是否走代理——直接用 MoviePilot「站点管理」里的「使用代理访问」开关。

        站点记录勾了才走 MoviePilot 的全局代理（设置 → 系统 → 代理）；勾了但全局代理为空时写一条
        日志说明本次仍直连，避免出现「勾了却看不出效果」。Browserless 引擎走它自己的出口。
        """
        if not (info or {}).get("proxy"):
            return None
        host = self._proxy_host()
        if not host:
            if not getattr(self, "_proxy_missing_logged", False):
                self._proxy_missing_logged = True
                logger.warning("PT签到管家：站点勾选代理，但 MoviePilot 未配置代理，本次直连")
            return None
        self._proxy_missing_logged = False
        if host != getattr(self, "_proxy_logged", ""):
            self._proxy_logged = host
            logger.info(f"PT签到管家：站点勾选「使用代理访问」，HTTP 引擎走代理 {mask_proxy(host)}")
        return {"http": host, "https": host}


    @staticmethod
    def _proxy_host() -> str:
        """取 MoviePilot 的全局代理地址：不同版本的模块路径与配置项名都试一遍，最后看环境变量。"""
        for module_name in ("app.sdk.config", "app.core.config", "app.core.settings",
                            "app.core.config.base"):
            try:
                module = importlib.import_module(module_name)
            except Exception:
                continue
            for holder_name in ("settings", "config", "Config"):
                holder = getattr(module, holder_name, None)
                if holder is None:
                    continue
                for key in ("PROXY_HOST", "PROXY", "proxy_host", "proxy"):
                    value = getattr(holder, key, None)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
        for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            value = str(os.environ.get(key) or "").strip()
            if value:
                return value
        return ""


    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
