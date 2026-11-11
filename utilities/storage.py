# -*- coding: utf-8 -*-
"""附件相关的通用小工具。"""


def human_size(n):
    """把字节数格式化成可读大小，用于备份/上传文件的展示。"""
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
