"""节点层：一节点一文件，文件名带线前缀。

    normal_*_node.py   常规线主图（graph/normal_graph.py）：意图 / 取数 / 校验 / 报告 / 错误
    hotword_*_node.py  热词循环图（graph/hotword_graph.py）：取词 / 分析 / 汇总

节点只产出 state 增量；「下一步去哪」在 graph/edges.py，图装配在 graph/*_graph.py。
业务实现在 services 的同名实现类（对应表见 services/__init__.py）；纯渲染 / 纯聚合
节点（output / error / summary）没有 service。
依赖单向向下：nodes → services → tools → adapters。
"""