"""调度任务装配：AsyncIOScheduler + cron 触发，交易日定时向 TaskManager 提交 DAILY 任务。

调度回调**只走 manager.submit**，绝不直调 runner.pipeline：baostock 是进程级全局
单连接（约束 §3.3），绕过单飞锁的第二条提交路径等于到点手动/自动双跑对撞。
cron 配置非法时 build_scheduler 返回 None 而不是抛异常——一个写错的 cron 串不该
连带把所有查询接口一起拖挂（任务书 T4 §4.1）。
"""

from datetime import timedelta, timezone, tzinfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.task.manager import TaskManager
from sequoia_x.task.models import TaskAlreadyRunning, TaskKind

logger = get_logger(__name__)

_UTC8 = timezone(timedelta(hours=8))


def _resolve_timezone(tz_name: str) -> tzinfo:
    """解析时区名为 tzinfo；不可用时回退固定 UTC+8（与 task/models.py:_get_tz 同策略）。

    Windows 精简 Python 发行版可能缺 IANA tz 数据库；中国无夏令时，固定 UTC+8 是
    安全的。调度器时区必须与 task_run.created_at（models._now_iso）同源，否则
    日志里会出现"19:15 的任务时间戳写着 11:15"这类对账灾难（任务书 T4 §4.3）。
    """
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(tz_name)
    except Exception:
        return _UTC8


def _daily_job(settings: Settings, manager: TaskManager) -> None:
    """调度回调：提交 DAILY 任务（triggered_by="scheduler"，推送语义与 CLI 一致）。

    本函数运行在 APScheduler 的线程池里，抛出去的任何异常都不该弄崩调度线程，
    所以逐类捕获并就地记日志（APScheduler 自己也会记 job 执行异常，但不依赖它）。
    """
    try:
        record = manager.submit(
            TaskKind.DAILY, params={"push": True}, triggered_by="scheduler"
        )
        logger.info(f"定时跑批已提交：{record.task_id}")
    except TaskAlreadyRunning as exc:
        # 单飞冲突是预期场景（如手动触发还没跑完就到点）：跳过本次，绝不排队补跑
        logger.warning(f"定时跑批被跳过，已有任务在跑：{exc.running_task_id}")
    except Exception:
        logger.exception("定时跑批提交失败")


def register_daily_job(
    scheduler: AsyncIOScheduler, settings: Settings, manager: TaskManager
) -> None:
    """向调度器注册每日跑批 job。cron 非法时 CronTrigger.from_crontab 抛 ValueError。

    - misfire_grace_time=300：服务重启错过整点，5 分钟内仍补跑；
    - coalesce=True：错过多次只补一次，堆积补跑等于自己打爆 baostock；
    - max_instances=1：APScheduler 层的第二道单飞保险（第一道在 TaskManager）。
    """
    scheduler.add_job(
        _daily_job,
        trigger=CronTrigger.from_crontab(settings.schedule_cron, timezone=scheduler.timezone),
        args=[settings, manager],
        id="daily_pipeline",
        name="每日定时跑批",
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True,
        max_instances=1,
    )


def build_scheduler(settings: Settings, manager: TaskManager) -> AsyncIOScheduler | None:
    """按配置装配调度器；未启用或 cron 非法时返回 None（服务照常启动）。

    返回的调度器**未启动**：start()/shutdown() 的生命周期由 app.py lifespan 管理，
    AsyncIOScheduler.start() 要求在运行中的事件循环里调用（uvicorn loop 内）。
    """
    if not settings.scheduler_enabled:
        logger.info("定时调度未启用（SCHEDULER_ENABLED=false）")
        return None
    scheduler = AsyncIOScheduler(timezone=_resolve_timezone(settings.timezone))
    try:
        register_daily_job(scheduler, settings, manager)
    except ValueError:
        # 只构造未 start 的调度器没有残留资源（无线程/无 socket），直接丢弃即可
        logger.error(
            f"SCHEDULE_CRON 配置非法：{settings.schedule_cron!r}，定时调度未启动"
            "（其余接口不受影响，请修正配置后重启服务）"
        )
        return None
    return scheduler
