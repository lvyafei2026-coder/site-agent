# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""Cloudflare REST client and the shared Radar D1 database."""

from .client import CloudflareClient
from .database import D1Database
from .client import CloudflareClient
from .database import D1Database

client = CloudflareClient()
database = D1Database(client)

__all__ = ["CloudflareClient", "D1Database", "client", "database"]
