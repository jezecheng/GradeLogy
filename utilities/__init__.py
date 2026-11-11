# -*- coding: utf-8 -*-
"""utilities：化学作业批改流水线的可独立运行 POC 模块。

每个节点模块既能被 import 调用（纯函数），也能命令行直接跑：
    python -m utilities.detect --pdf uploads/test_stu.pdf
    python -m utilities.match --no 3.1 --pdf uploads/test_ans.pdf
    python -m utilities.grade  --image uploads/crop_x.jpg --pdf uploads/test_ans.pdf --no 3.1
"""
