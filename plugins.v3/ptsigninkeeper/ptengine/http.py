"""插件 HTTP 会话兼容层。

V3 优先使用 ``app.sdk.network.RequestUtils``；V2 使用宿主自带的
``app.utils.http.RequestUtils``；两者都不存在时才回退到 ``requests.Session``。
业务代码只依赖 ``get`` / ``post`` / ``headers`` / ``cookies`` 这组最小会话接口。
"""

from __future__ import annotations

import inspect
from http.cookies import SimpleCookie
from types import SimpleNamespace
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit


class _CookieSnapshot:
    """把普通 Cookie 字典包装成 requests 风格的可迭代对象。"""

    def __init__(self, values: Mapping[str, Any]) -> None:
        self._values = dict(values)

    def __iter__(self) -> Iterator[SimpleNamespace]:
        for name, value in self.get_dict().items():
            yield SimpleNamespace(name=name, value=value)

    def get_dict(self) -> dict[str, str]:
        """返回当前 Cookie 快照。"""
        return {str(name): str(value) for name, value in self._values.items()}


class _SdkCookies:
    """
    把 RequestUtils 的 Cookie 快照适配成可迭代对象。

    :param client: RequestUtils 实例
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def __iter__(self) -> Iterator[SimpleNamespace]:
        for name, value in self.get_dict().items():
            yield SimpleNamespace(name=name, value=value)

    def get_dict(self) -> dict[str, str]:
        """
        返回当前会话 Cookie。

        :return: Cookie 名值字典
        """
        getter = getattr(self._client, "get_cookies", None)
        return dict(getter() if callable(getter) else {})


class _SdkSession:
    """
    新版 RequestUtils 的最小会话兼容包装。

    :param client: RequestUtils 实例
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self.headers: dict[str, str] = {}

    @property
    def cookies(self) -> _SdkCookies:
        """
        返回当前持久会话的 Cookie 视图。

        :return: 可迭代 Cookie 视图
        """
        return _SdkCookies(self._client)

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        """
        发送请求。

        :param method: HTTP 方法
        :param url: 请求地址
        :param kwargs: requests 兼容请求参数
        :return: HTTP 响应
        :raises Exception: request 失败且 SDK 要求抛出时透传异常
        """
        headers = dict(self.headers)
        extra = kwargs.pop("headers", None)
        if isinstance(extra, dict):
            headers.update(extra)
        if headers:
            kwargs["headers"] = headers
        raise_exception = bool(kwargs.pop("raise_exception", True))
        return self._client.request(method=method, url=url,
                                    raise_exception=raise_exception, **kwargs)

    def get(self, url: str, **kwargs: Any) -> Any:
        """发送 GET 请求。"""
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        """发送 POST 请求。"""
        return self.request("POST", url, **kwargs)

    def close(self) -> None:
        """释放 RequestUtils 持有的会话。"""
        closer = getattr(self._client, "close", None)
        if callable(closer):
            closer()


class _LegacySdkSession:
    """MoviePilot V2 的 RequestUtils 兼容包装。

    V2 的 ``RequestUtils`` 主要提供 ``get_res`` / ``post_res``，不一定支持
    ``request(raise_exception=...)``。这里把插件统一使用的 requests 风格接口转换成
    V2 宿主接口，同时维护请求头和响应 Cookie 快照。
    """

    def __init__(self, client: Any, transport: Any = None) -> None:
        self._client = client
        self._transport = transport
        self._cookie_header = None
        self._seeded_cookies: list[tuple[str, str]] = []
        self.headers: dict[str, str] = {}
        self._cookies: dict[str, str] = {}
        if transport is not None:
            transport.hooks['response'].append(self._accept_response_cookies)
        getter = getattr(client, "get_cookies", None)
        if callable(getter):
            try:
                self._cookies.update({str(k): str(v) for k, v in getter().items()})
            except Exception:
                pass

    @property
    def cookies(self) -> _CookieSnapshot:
        """返回当前持久会话的 Cookie 视图。"""
        if self._transport is not None:
            return _CookieSnapshot(self._transport.cookies.get_dict())
        return _CookieSnapshot(self._cookies)

    def _prepare_cookies(self, url: str, headers: dict[str, str]) -> None:
        """将初始 Cookie 放入 CookieJar，让响应更新、跳转和路径限制继续生效。"""
        raw = None
        for key in list(headers):
            if key.lower() == 'cookie':
                raw = headers.pop(key)
        if raw is None or raw == self._cookie_header:
            return
        parsed = urlsplit(url)
        if not parsed.hostname:
            return
        jar = self._transport.cookies
        # 显式换用另一份登录 Cookie 时，旧会话和 CSRF 也必须一并失效。
        if self._cookie_header is not None:
            jar.clear()
        self._seeded_cookies = []
        for part in str(raw).split(';'):
            name, separator, value = part.strip().partition('=')
            if separator and name:
                jar.set(name, value, domain=parsed.hostname, path='/', secure=parsed.scheme == 'https')
                self._seeded_cookies.append((parsed.hostname, name))
        # 同一原始 Cookie 只注入一次，不能覆盖站点轮换或删除后的值。
        self._cookie_header = raw

    def _accept_response_cookies(self, response: Any, **kwargs: Any) -> None:
        """站点 Set-Cookie 接管同名初始值，避免 Domain 形式不同造成双重身份 Cookie。"""
        request = response.request
        target = urlsplit(request.url)
        host = target.hostname or ''
        source = getattr(getattr(response, 'raw', None), 'headers', None)
        getter = getattr(source, 'getlist', None)
        values = getter('Set-Cookie') if callable(getter) else [response.headers.get('Set-Cookie', '')]
        for value in values:
            incoming = SimpleCookie()
            try:
                incoming.load(value)
            except Exception:
                continue
            for name, cookie in incoming.items():
                domain = str(cookie['domain'] or host).lower().lstrip('.')
                path = cookie['path'] or (target.path.rpartition('/')[0] or '/')
                if path != '/' or not domain or not (host == domain or host.endswith('.' + domain)):
                    continue
                for seeded in list(self._seeded_cookies):
                    seed_host, seed_name = seeded
                    if name != seed_name or not (seed_host == domain or seed_host.endswith('.' + domain)):
                        continue
                    # requests 在响应钩子之后保存 Set-Cookie；同时清理已准备请求的
                    # CookieJar，确保本次 302 的下一跳也不会重新合并旧值。
                    for jar in (self._transport.cookies, getattr(request, '_cookies', None)):
                        if jar is not None:
                            try:
                                jar.clear(domain=seed_host, path='/', name=seed_name)
                            except KeyError:
                                pass
                    self._seeded_cookies.remove(seeded)

    def _remember_cookies(self, response: Any) -> None:
        if self._transport is not None:
            return  # requests.Session 已保留包括中间跳转在内的 CookieJar。
        cookies = getattr(response, "cookies", None)
        getter = getattr(cookies, "get_dict", None)
        if not callable(getter):
            return
        try:
            self._cookies.update({str(k): str(v) for k, v in getter().items()})
        except Exception:
            return

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        """通过 V2 的 get_res / post_res 发送请求。"""
        raise_exception = bool(kwargs.pop("raise_exception", True))
        headers = dict(self.headers)
        extra = kwargs.pop("headers", None)
        if isinstance(extra, dict):
            headers.update({str(k): str(v) for k, v in extra.items()})
        if self._transport is not None:
            self._prepare_cookies(url, headers)
        if headers:
            kwargs["headers"] = headers
        if self._transport is None and self._cookies:
            kwargs.setdefault("cookies", dict(self._cookies))

        method_upper = str(method or "GET").upper()
        getter = getattr(self._client, "get_res", None)
        poster = getattr(self._client, "post_res", None)
        if method_upper == "GET" and callable(getter):
            sender = getter
        elif method_upper == "POST" and callable(poster):
            sender = poster
        else:
            generic = getattr(self._client, "request", None)
            if not callable(generic):
                raise ValueError(f"V2 RequestUtils 不支持 {method_upper} 请求")
            response = generic(method=method, url=url, raise_exception=raise_exception, **kwargs)
            self._remember_cookies(response)
            return response

        try:
            response = sender(url, raise_exception=raise_exception, **kwargs)
        except TypeError as error:
            # 老版 V2 RequestUtils 不声明 raise_exception，去掉该参数后重试。
            if "raise_exception" not in str(error):
                raise
            response = sender(url, **kwargs)
        if response is None:
            if raise_exception:
                raise ConnectionError("MoviePilot V2 RequestUtils 未返回响应")
            return None
        self._remember_cookies(response)
        return response

    def get(self, url: str, **kwargs: Any) -> Any:
        """发送 GET 请求。"""
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        """发送 POST 请求。"""
        return self.request("POST", url, **kwargs)

    def close(self) -> None:
        """释放 V2 RequestUtils 持有的会话。"""
        closer = getattr(self._client, "close", None)
        try:
            if callable(closer):
                closer()
        finally:
            if self._transport is not None:
                self._transport.close()


def _legacy_session(request_utils: Any) -> _LegacySdkSession:
    """V2 继续由宿主发送请求，同时显式提供其公开接口支持的持久会话。"""
    try:
        supports_session = 'session' in inspect.signature(request_utils).parameters
    except (TypeError, ValueError):
        supports_session = False
    if not supports_session:
        return _LegacySdkSession(request_utils())
    import requests

    transport = requests.Session()
    try:
        return _LegacySdkSession(request_utils(session=transport), transport)
    except Exception:
        transport.close()
        raise


def _request_utils_session(request_utils: Any) -> Any:
    """Choose the adapter supported by this MoviePilot RequestUtils version."""
    try:
        client = request_utils(use_session=True)
    except TypeError as error:
        message = str(error)
        if "unexpected keyword argument" not in message or "use_session" not in message:
            raise
        return _legacy_session(request_utils)
    return _SdkSession(client)


def new_http_session() -> Any:
    """
    创建插件 HTTP 会话。

    优先使用当前 MoviePilot 版本提供的 RequestUtils；V2 / V3 都缺失时才回退到
    requests.Session，避免 V2 绕过宿主已有的代理、证书与 TLS 兼容处理。

    :return: 带 requests 兼容接口的 HTTP 会话
    """
    try:
        from app.sdk.network import RequestUtils
    except ImportError:
        try:
            from app.utils.http import RequestUtils as LegacyRequestUtils
        except ImportError:
            import requests

            return requests.Session()
        return _legacy_session(LegacyRequestUtils)
    return _request_utils_session(RequestUtils)
