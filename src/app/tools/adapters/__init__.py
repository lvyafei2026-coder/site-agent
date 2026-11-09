"""外部系统适配器：所有真实 IO 只在这里发生。

cloudflare/  D1 + Workers KV + SQL 契约
sources/     radar / GSC / Reddit 只读数据源
hosting/     GitHub 仓库 + 页面模板渲染 + 线上验活
publishing/  Dev.to / Resend

子包按需 import，这里不做再导出——避免任一适配器被用到时把四组全拉起来。
"""
