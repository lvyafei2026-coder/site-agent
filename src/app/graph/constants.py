"""图装配的节点名与路由标签。

节点名刻意用动词前缀：langgraph 0.3.x 不允许节点名与 WorkflowState 的字段名
同名，否则 add_node 抛 "'intent' is already being used as a state key"。

★2026-11-17：取值统一定义在 src.app.enums（GraphNode / RouteTag）；这里保留
Names / Tags 两个别名，既有引用（builder/edges/trace）不用改。
"""

from src.app.enums import GraphNode as Names
from src.app.enums import RouteTag as Tags

__all__ = ["Names", "Tags"]
