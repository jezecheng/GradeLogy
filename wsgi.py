"""生产入口：显式初始化数据和后台服务。"""
from app import create_app, initialize_app

app = initialize_app(create_app())
