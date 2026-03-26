"""
VS Code 扩展管理器后端
提供 VS Code 扩展的搜索、安装、卸载等功能
"""
import os
import subprocess
import json
import shutil
from dataclasses import dataclass
from typing import List, Optional, Tuple
from PyQt6.QtCore import QObject, pyqtSignal, QThread
import urllib.request
import urllib.parse


@dataclass
class VSCodeExtension:
    """VS Code 扩展数据类"""
    identifier: str          # 扩展 ID (publisher.name)
    display_name: str        # 显示名称
    publisher: str           # 发布者
    short_description: str   # 简短描述
    version: str             # 版本号
    install_count: int = 0   # 安装数
    rating: float = 0.0      # 评分
    icon_url: str = ""       # 图标 URL
    is_installed: bool = False  # 是否已安装


class SearchWorker(QThread):
    """搜索扩展的工作线程"""
    finished = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, query: str, parent=None):
        super().__init__(parent)
        self.query = query

    def run(self):
        try:
            extensions = self._search_marketplace(self.query)
            self.finished.emit(extensions)
        except Exception as e:
            self.error.emit(str(e))

    def _search_marketplace(self, query: str) -> List[VSCodeExtension]:
        """搜索 VS Code 扩展市场"""
        # VS Code Marketplace API
        url = "https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery"

        # 构建请求体
        request_body = {
            "filters": [{
                "criteria": [
                    {"filterType": 8, "value": "Microsoft.VisualStudio.Code"},
                    {"filterType": 10, "value": query},
                ],
                "pageNumber": 1,
                "pageSize": 50,
                "sortBy": 0,
                "sortOrder": 0
            }],
            "assetTypes": [],
            "flags": 914  # 包含统计信息
        }

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json;api-version=3.0-preview.1",
            "User-Agent": "Mozilla/5.0"
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(request_body).encode('utf-8'),
            headers=headers,
            method='POST'
        )

        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode('utf-8'))

        extensions = []
        results = data.get("results", [])
        if results:
            for ext in results[0].get("extensions", []):
                # 获取统计信息
                install_count = 0
                rating = 0.0
                statistics = ext.get("statistics", [])
                for stat in statistics:
                    if stat.get("statisticName") == "install":
                        install_count = int(stat.get("value", 0))
                    elif stat.get("statisticName") == "averagerating":
                        rating = float(stat.get("value", 0))

                # 获取图标 URL
                icon_url = ""
                versions = ext.get("versions", [])
                if versions:
                    for file in versions[0].get("files", []):
                        if file.get("assetType") == "Microsoft.VisualStudio.Services.Icons.Default":
                            icon_url = file.get("source", "")
                            break

                extensions.append(VSCodeExtension(
                    identifier=f"{ext.get('publisher', {}).get('publisherName', '')}.{ext.get('extensionName', '')}",
                    display_name=ext.get("displayName", ext.get("extensionName", "")),
                    publisher=ext.get("publisher", {}).get("displayName", ""),
                    short_description=ext.get("shortDescription", ""),
                    version=versions[0].get("version", "") if versions else "",
                    install_count=install_count,
                    rating=rating,
                    icon_url=icon_url
                ))

        return extensions


class InstallWorker(QThread):
    """安装/卸载扩展的工作线程"""
    finished = pyqtSignal(bool, str)  # success, message

    def __init__(self, extension_id: str, install: bool = True, code_path: str = "code", parent=None):
        super().__init__(parent)
        self.extension_id = extension_id
        self.install = install
        self.code_path = code_path

    def run(self):
        try:
            if self.install:
                result = subprocess.run(
                    [self.code_path, "--install-extension", self.extension_id],
                    capture_output=True,
                    text=True,
                    timeout=120
                )
            else:
                result = subprocess.run(
                    [self.code_path, "--uninstall-extension", self.extension_id],
                    capture_output=True,
                    text=True,
                    timeout=60
                )

            if result.returncode == 0:
                action = "安装" if self.install else "卸载"
                self.finished.emit(True, f"{action}成功: {self.extension_id}")
            else:
                self.finished.emit(False, result.stderr or result.stdout)
        except subprocess.TimeoutExpired:
            self.finished.emit(False, "操作超时")
        except Exception as e:
            self.finished.emit(False, str(e))


class VSCodeManager(QObject):
    """VS Code 扩展管理器"""

    # 信号
    search_completed = pyqtSignal(list)     # 搜索完成
    install_completed = pyqtSignal(bool, str)  # 安装/卸载完成
    error_occurred = pyqtSignal(str)        # 错误发生
    installed_list_updated = pyqtSignal(list)  # 已安装列表更新

    def __init__(self, parent=None):
        super().__init__(parent)
        self._code_path = self._find_code_cli()
        self._search_worker: Optional[SearchWorker] = None
        self._install_worker: Optional[InstallWorker] = None
        self._installed_extensions: List[str] = []

    def _find_code_cli(self) -> str:
        """查找 VS Code CLI 路径"""
        # 常见路径
        possible_paths = [
            "code",  # 在 PATH 中
            "/usr/local/bin/code",
            "/usr/bin/code",
            "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
            os.path.expanduser("~/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"),
        ]

        for path in possible_paths:
            if shutil.which(path) or os.path.isfile(path):
                return path

        return "code"  # 默认使用 PATH 中的

    def is_vscode_available(self) -> bool:
        """检查 VS Code 是否可用"""
        try:
            result = subprocess.run(
                [self._code_path, "--version"],
                capture_output=True,
                text=True,
                timeout=10
            )
            return result.returncode == 0
        except Exception:
            return False

    def get_vscode_version(self) -> str:
        """获取 VS Code 版本"""
        try:
            result = subprocess.run(
                [self._code_path, "--version"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                return result.stdout.strip().split('\n')[0]
        except Exception:
            pass
        return "未知"

    def search_extensions(self, query: str):
        """搜索扩展（异步）"""
        if self._search_worker and self._search_worker.isRunning():
            self._search_worker.terminate()
            self._search_worker.wait()

        self._search_worker = SearchWorker(query, self)
        self._search_worker.finished.connect(self._on_search_finished)
        self._search_worker.error.connect(lambda e: self.error_occurred.emit(f"搜索失败: {e}"))
        self._search_worker.start()

    def _on_search_finished(self, extensions: List[VSCodeExtension]):
        """搜索完成回调"""
        # 标记已安装的扩展
        for ext in extensions:
            ext.is_installed = ext.identifier.lower() in [e.lower() for e in self._installed_extensions]
        self.search_completed.emit(extensions)

    def get_installed_extensions(self) -> List[str]:
        """获取已安装的扩展列表"""
        try:
            result = subprocess.run(
                [self._code_path, "--list-extensions"],
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode == 0:
                self._installed_extensions = [
                    ext.strip() for ext in result.stdout.strip().split('\n')
                    if ext.strip()
                ]
                return self._installed_extensions
        except Exception as e:
            self.error_occurred.emit(f"获取已安装扩展失败: {e}")
        return []

    def refresh_installed(self):
        """刷新已安装列表"""
        extensions = self.get_installed_extensions()
        self.installed_list_updated.emit(extensions)

    def install_extension(self, extension_id: str):
        """安装扩展（异步）"""
        if self._install_worker and self._install_worker.isRunning():
            self.error_occurred.emit("有安装任务正在进行中")
            return

        self._install_worker = InstallWorker(extension_id, True, self._code_path, self)
        self._install_worker.finished.connect(self._on_install_finished)
        self._install_worker.start()

    def uninstall_extension(self, extension_id: str):
        """卸载扩展（异步）"""
        if self._install_worker and self._install_worker.isRunning():
            self.error_occurred.emit("有任务正在进行中")
            return

        self._install_worker = InstallWorker(extension_id, False, self._code_path, self)
        self._install_worker.finished.connect(self._on_install_finished)
        self._install_worker.start()

    def _on_install_finished(self, success: bool, message: str):
        """安装/卸载完成回调"""
        self.install_completed.emit(success, message)
        if success:
            self.refresh_installed()

    def open_in_vscode(self, path: str) -> bool:
        """在 VS Code 中打开文件或目录"""
        try:
            subprocess.Popen([self._code_path, path])
            return True
        except Exception as e:
            self.error_occurred.emit(f"打开 VS Code 失败: {e}")
            return False

    def format_install_count(self, count: int) -> str:
        """格式化安装数"""
        if count >= 1000000:
            return f"{count / 1000000:.1f}M"
        elif count >= 1000:
            return f"{count / 1000:.1f}K"
        return str(count)
