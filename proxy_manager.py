#!/usr/bin/env python3
"""
macOS 系统代理管理 + mitmproxy CA 证书安装。

职责：
  - 检测当前活跃的网络服务（Wi-Fi / Ethernet 等）
  - 开启 / 关闭系统 HTTP+HTTPS 代理
  - 检查并安装 mitmproxy CA 证书到登录 Keychain
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent
MITMPROXY_CERT = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"
PROXY_HOST = "127.0.0.1"
VENV_PYTHON = PROJECT_DIR / ".venv" / "bin" / "python"
INSTALL_HINT = (
    f"{VENV_PYTHON} -m pip install -r requirements.txt"
    if VENV_PYTHON.exists()
    else "python3 -m pip install -r requirements.txt"
)


# ── 网络服务探测 ──────────────────────────────────────────────────────────────

def get_network_services() -> list[str]:
    """返回系统中所有启用的网络服务名称列表。"""
    r = subprocess.run(
        ["networksetup", "-listnetworkserviceorder"],
        capture_output=True, text=True,
    )
    services = []
    for line in r.stdout.splitlines():
        # 格式：(1) Wi-Fi  或  (*) Wi-Fi（* 表示禁用）
        m = re.match(r"^\((\d+)\) (.+)$", line.strip())
        if m:
            services.append(m.group(2).strip())
    return services


def get_primary_service() -> str | None:
    """返回当前默认路由所用的网络服务名，找不到时返回 None。"""
    # 找到默认路由对应的网络接口（如 en0）
    r = subprocess.run(["route", "get", "default"], capture_output=True, text=True)
    iface = None
    for line in r.stdout.splitlines():
        m = re.search(r"interface:\s+(\S+)", line)
        if m:
            iface = m.group(1)
            break
    if not iface:
        return None

    # 从硬件端口列表中找到对应服务名
    r2 = subprocess.run(
        ["networksetup", "-listallhardwareports"],
        capture_output=True, text=True,
    )
    current_service = None
    for line in r2.stdout.splitlines():
        hw_m = re.match(r"Hardware Port:\s+(.+)", line.strip())
        dev_m = re.match(r"Device:\s+(\S+)", line.strip())
        if hw_m:
            current_service = hw_m.group(1).strip()
        if dev_m and dev_m.group(1) == iface:
            return current_service
    return None


# ── 代理操作 ──────────────────────────────────────────────────────────────────

class ProxyManager:
    def __init__(self, port: int = 8080):
        self.port = port
        self._services: list[str] = []

    def enable(self):
        """在所有活跃网络服务上开启 HTTP + HTTPS 代理。"""
        services = get_network_services()
        if not services:
            print("警告：未找到任何网络服务，代理设置可能不生效。")
            return
        self._services = services
        for svc in services:
            self._set_proxy(svc, enable=True)
        primary = get_primary_service()
        display = primary or services[0]
        print(f"代理已开启（{display}  →  {PROXY_HOST}:{self.port}）")

    def disable(self):
        """关闭代理（仅还原我们开启过的服务）。"""
        for svc in self._services:
            self._set_proxy(svc, enable=False)
        if self._services:
            print("代理已关闭。")

    def _set_proxy(self, service: str, enable: bool):
        state = "on" if enable else "off"
        args_http  = ["networksetup", "-setwebproxy", service, PROXY_HOST, str(self.port)]
        args_https = ["networksetup", "-setsecurewebproxy", service, PROXY_HOST, str(self.port)]
        args_http_state  = ["networksetup", "-setwebproxystate",       service, state]
        args_https_state = ["networksetup", "-setsecurewebproxystate", service, state]

        if enable:
            subprocess.run(args_http,  capture_output=True)
            subprocess.run(args_https, capture_output=True)
        subprocess.run(args_http_state,  capture_output=True)
        subprocess.run(args_https_state, capture_output=True)


# ── CA 证书管理 ───────────────────────────────────────────────────────────────

def ensure_mitmproxy_cert_generated():
    """如果 mitmproxy CA 证书不存在，运行一次 mitmdump 让它自动生成。"""
    if MITMPROXY_CERT.exists():
        return
    print("正在生成 mitmproxy CA 证书（首次）...")
    mitmdump = shutil.which("mitmdump")
    if not mitmdump:
        print(f"错误：未找到 mitmdump，请先安装依赖: {INSTALL_HINT}")
        sys.exit(1)
    # 运行 mitmdump 0.5 秒即可生成证书，然后立即退出
    proc = subprocess.Popen(
        [mitmdump, "--listen-port", "18999", "-q"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    import time
    time.sleep(1.5)
    proc.terminate()
    proc.wait()
    if MITMPROXY_CERT.exists():
        print(f"证书生成成功：{MITMPROXY_CERT}")
    else:
        print("警告：证书生成失败，请手动运行 mitmdump 一次后重试。")


def is_cert_trusted() -> bool:
    """检查 mitmproxy CA 是否已在 Keychain 中受信任。"""
    if not MITMPROXY_CERT.exists():
        return False
    r = subprocess.run(
        ["security", "find-certificate", "-c", "mitmproxy", "-a"],
        capture_output=True, text=True,
    )
    return "mitmproxy" in r.stdout.lower()


def install_cert():
    """将 mitmproxy CA 证书添加到登录 Keychain（需要用户授权）。"""
    ensure_mitmproxy_cert_generated()

    if is_cert_trusted():
        print("mitmproxy CA 证书已安装，无需重复操作。")
        return

    print(f"\n正在安装 CA 证书到 Keychain：{MITMPROXY_CERT}")
    print("系统会弹出授权框，请输入登录密码（仅此一次）。\n")

    login_keychain = Path.home() / "Library" / "Keychains" / "login.keychain-db"
    r = subprocess.run(
        [
            "security", "add-trusted-cert",
            "-r", "trustRoot",
            "-k", str(login_keychain),
            str(MITMPROXY_CERT),
        ],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        print("✓ CA 证书安装成功！")
    else:
        print(f"证书安装失败（code={r.returncode}）：{r.stderr.strip()}")
        print(
            "\n可以手动安装：\n"
            f"  双击打开 {MITMPROXY_CERT}\n"
            "  在 Keychain Access 中将其设置为「始终信任」"
        )
