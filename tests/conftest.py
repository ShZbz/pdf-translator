"""测试环境隔离（v0.5.0）：server.app 导入即建 JobManager 并触发持久化恢复。

不设此环境变量时，跑单测会读写项目根的真实 .ui_jobs.db——开发机上
若恰有未完成任务，测试进程会把它恢复重跑（意外 spawn 翻译子进程）。
这里把默认库指到系统临时目录，全部测试与真实数据互不干扰。
"""
import os
import tempfile

os.environ.setdefault(
    "PDF_TRANSLATOR_JOBS_DB",
    os.path.join(tempfile.gettempdir(), "pdf_translator_ui_jobs_test.db"))
