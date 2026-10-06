"""后台任务单执行者租约（server 集群，.docs/CLUSTER_DESIGN.md 阶段 2）。

实现约定（评审修正后）：**全部后端一律走 DB 表**（storage.lease_acquire/release）——
SQLite WAL 支持多进程共享文件，本机双进程即可完整验收；时间以 DB 侧时钟为准，
规避节点时钟漂移。单机单进程形态下 hold() 恒真、零额外开销，两种形态同一条代码路径。
"""
from __future__ import annotations


class DbLease:
    """一个具名租约的持有句柄。hold() 获取或续约；release() 主动让出。"""

    def __init__(self, storage, name: str, holder: str, ttl: int = 60):
        self.s = storage
        self.name = name
        self.holder = holder
        self.ttl = max(5, int(ttl))

    def hold(self) -> bool:
        """获取或续约租约。True=本进程持有（可干活）；False=他人持有且未过期。"""
        return self.s.lease_acquire(self.name, self.holder, self.ttl)

    def release(self) -> bool:
        """主动让出租约（优雅停机时调用；忘记调也安全——TTL 到期自动可被争夺）。"""
        return self.s.lease_release(self.name, self.holder)
