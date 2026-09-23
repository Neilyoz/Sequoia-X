"""开发用启动脚本：python run_server.py。

等价于手动 `uvicorn sequoia_x.api.app:app --host ... --port ...`，
监听地址与端口读 .env 的 API_HOST / API_PORT。生产建议直接用 uvicorn 命令行，
本脚本的价值是让新同学不需要记 uvicorn 参数。
"""

from sequoia_x.core.bootstrap import bootstrap

# bootstrap() 必须先于一切 sequoia_x 业务子模块 import（约束 §1，与 main.py 同手法）：
# 这里也先于 uvicorn —— uvicorn 启动后导入 app 模块时 Settings 会被实例化读配置。
bootstrap()

import uvicorn  # noqa: E402

from sequoia_x.core.config import get_settings  # noqa: E402

if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "sequoia_x.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        # reload 写死 False：Windows 上热重载以新进程重启，正在跑的 baostock 任务
        # 会被硬切，留下脏 socket（约束 §4），不提供开关。
        reload=False,
    )
