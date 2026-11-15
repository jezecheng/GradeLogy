# -*- coding: utf-8 -*-
"""测试环境引导：在导入 app/config 之前把数据目录指向临时目录。

单独运行某个测试文件时，模块导入顺序不受 unittest discover 控制；
这个模块保证 config 第一次被导入前 DATA_DIR 已就绪。
"""
import atexit
import os
import shutil
import sys
import tempfile

if not ({"config", "db", "app"} & set(sys.modules)):
    _ROOT = tempfile.mkdtemp(prefix="gradelogy-tests-")
    os.environ.setdefault("DATA_DIR", _ROOT)
    os.environ.setdefault("AUTO_BACKUP", "0")
    os.environ.setdefault("AUTO_GRADE", "0")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    atexit.register(shutil.rmtree, _ROOT, ignore_errors=True)
