"""外部系统适配器：所有真实 IO 只在这里发生。

★包划分原则 = 「外部系统边界 + 业务域」两层，**不是按存储资源类型**（KV / D1 / git）分：
  cloudflare/   按**厂商**划分的基础设施层：一个 CloudflareClient + 三个端口实现
                （D1 数据库、agent 私有状态 KV、SQL 契约）。换厂商只动这个包。
  sources/      按**业务域**划分：热词业务的数据通道与读写（共享 KV 门面、候选快照、
                收藏整理口径、热词分析）+ 外部取数（GSC / Reddit / SERP / 趋势）。
  hosting/      按**业务域**划分：建站流水线（GitHub 仓库、页面渲染、构建验活）。
  publishing/   按**业务域**划分：对外分发（Dev.to 文章、Resend 邮件）。

关于 KV 的两个文件（常见疑问）：
  cloudflare/keyvalue.py = WorkersKV，agent 私有状态标记（键带 `agent:` 前缀），
      fail-fast（出错抛异常），复用共享 CloudflareClient。
  sources/kv.py = CloudflareKV，与 radar / console 共享的 RADAR_KV 业务表
      （saved / rejected / ai:v3: 等），fail-open（读失败返回默认值并留痕），
      带进程锁 + atomic_update。两个类指向**同一个 KV 命名空间**（KV_NAMESPACE_ID），
      差别是键前缀与错误语义；历史上分别是「厂商端口」与「共享表业务门面」。

子包按需 import，这里不做再导出——避免任一适配器被用到时把四组全拉起来。
"""
