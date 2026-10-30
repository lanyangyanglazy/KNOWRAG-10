"""
演示用用户目录与文档级访问控制。

真实政务系统里这一层应对接统一身份认证（OIDC / LDAP / CAS）与权限中心，
本文件用内存中的演示用户替代，重点是把「权限判定」写成可测试的纯函数，
并让它在检索**之前**生效（见 rag_service.RagService.retrieve）。
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from typing import Mapping

# 密级 → 数值等级。数值越大越敏感；未知密级一律拒绝（fail-closed）。
CLASSIFICATION_LEVELS = {"公开": 0, "内部": 1, "秘密": 2}

# 演示口令：仅用于本地演示，生产必须替换为统一身份认证（OIDC / CAS / LDAP）。
# 优先级：DEMO_PASSWORD_<USERNAME> > DEMO_PASSWORD > 内置默认值。
DEFAULT_DEMO_PASSWORD = "demo-2026"


@dataclass(frozen=True)
class User:
    """登录用户：部门 + 可访问的最高密级。"""

    username: str
    display_name: str
    dept: str
    max_level: int
    role: str
    roles: tuple[str, ...] = ()

    @property
    def max_classification(self) -> str:
        """最高可访问密级的中文名（用于前端展示）。"""
        for name, level in CLASSIFICATION_LEVELS.items():
            if level == self.max_level:
                return name
        return "未知"

    def to_dict(self) -> dict:
        return {
            "username": self.username,
            "display_name": self.display_name,
            "dept": self.dept,
            "max_level": self.max_level,
            "max_classification": self.max_classification,
            "role": self.role,
            "roles": list(self.roles),
        }

    def to_public_dict(self) -> dict:
        """未登录可见的字段：不含最高密级，避免泄露「谁有权看秘密」。"""
        return {
            "username": self.username,
            "display_name": self.display_name,
            "dept": self.dept,
            "role": self.role,
        }


# 演示账号：两个部门、两种密级权限，方便演示「同问题不同可见范围」。
DEMO_USERS: dict[str, User] = {
    "zhang": User("zhang", "张伟", "综合处", max_level=1, role="工作人员",
                  roles=("uploader", "reviewer")),
    "li": User("li", "李娜", "人事处", max_level=1, role="人事专员",
               roles=("uploader", "reviewer")),
    "wang": User("wang", "王强", "财务处", max_level=2, role="财务主管",
                 roles=("uploader", "reviewer", "admin")),
    "chen": User("chen", "陈静", "信息中心", max_level=2, role="安全管理员",
                 roles=("uploader", "reviewer", "admin")),
}


def get_user(username: str) -> User | None:
    return DEMO_USERS.get(username)


def demo_password(username: str | None = None) -> str:
    """演示口令（每次实时读取环境变量，便于部署时覆盖而不改代码）。"""
    if username:
        per_user = os.getenv(f"DEMO_PASSWORD_{username.upper()}")
        if per_user:
            return per_user
    return os.getenv("DEMO_PASSWORD", DEFAULT_DEMO_PASSWORD)


def authenticate(username: str, password: str | None) -> User | None:
    """
    校验账号口令。

    - 用户不存在时也执行一次等长比较，避免通过响应时间枚举账号；
    - 口令比较使用 hmac.compare_digest（常量时间），不落明文日志。
    """
    user = DEMO_USERS.get(username)
    expected = demo_password(username if user else None)
    supplied = password or ""
    matched = hmac.compare_digest(supplied, expected)
    return user if (user is not None and matched) else None


def can_view(user: User, metadata: Mapping) -> bool:
    """
    判断用户能否看到某个文档切片。

    规则（最小权限）：
    - 密级高于用户上限 → 拒绝
    - 公开（level 0）→ 全员可见
    - 内部/秘密 → 仅本部门可见

    文档缺少 classification_level 时按「最严」处理（视为不可读），
    避免元数据漏标导致越权检索。
    """
    level = metadata.get("classification_level")
    if not isinstance(level, int):
        return False
    if level > user.max_level:
        return False
    if level == 0:
        return True
    return metadata.get("dept") == user.dept
