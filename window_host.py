"""MainWindow 各 mixin 访问"宿主类"的唯一入口（打破循环 import）。

进程级共享状态（窗口计数、全局导航器、停靠方式、按屏分桶的侧栏宽度……）是
MainWindow 的**类属性**。mixin 方法里的 self 就是 MainWindow 实例，但不能写
`type(self)`：对子类会把 `+=` 写成子类上的影子属性，各窗口计数就分叉了。
这里沿 MRO 找到真正定义这些类属性的那个类（以 `_window_counter` 为标记），
单元测试里的假 self 找不到时回退到 type(self)。
"""

_MARKER = '_window_counter'


def host_class(obj):
    """返回定义进程级共享类属性的宿主类（真 MainWindow），供 mixin 读写类属性
    与构造新窗口：`host_class(self)._window_counter += 1`、
    `host_class(self)(initial_tab_data=...)`。"""
    for cls in type(obj).__mro__:
        if _MARKER in cls.__dict__:
            return cls
    return type(obj)
