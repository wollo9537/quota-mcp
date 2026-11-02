# quota-mcp

把各家 AI 平台的额度查询做成 MCP 工具，接进 Claude Code、Cursor 这类客户端后，在对话里问一句「我的 AI 额度还剩多少」就能拿到汇总表。

An MCP server that reports your AI provider quotas — balance and usage ratio — inside any MCP client. Providers are described in a plain JSON config; API tokens live in separate files and never enter the config or the repo.

## 工具

| 工具 | 干什么 |
| --- | --- |
| get_quota_summary | 全平台汇总表：占比型给已用 / 上限 / 剩余 / 占比，余额型给当前值；取数失败的平台标注原因，不影响其他平台 |
| get_platform_detail | 单平台明细，参数支持名字部分匹配（如 deepseek、claude） |

## 安装

要求 Python 3.10 及以上。推荐用 uv，不必自己建虚拟环境：

```bash
uv run server.py --once     # 跑一次看结果
uv run server.py            # 以 MCP 服务端身份启动
```

不用 uv 的话：

```bash
pip install -r requirements.txt
python server.py --once
```

## 接入客户端

在项目的 `.mcp.json` 里加一段（路径换成你自己的）：

```json
{
  "mcpServers": {
    "quota-mcp": {
      "command": "uv",
      "args": ["run", "D:/path/to/quota-mcp/server.py"]
    }
  }
}
```

接上后在对话里说「查一下我的 AI 额度」即可。

## 配置

配置文件查找顺序：环境变量 `QUOTA_MCP_CONFIG` > 本目录 `config.json` > 旁挂的 `../quota-panel/config.json`。样例见 `config.example.json`。

| 字段 | 描述 | 大白话 |
| --- | --- | --- |
| request.url / method / where | 请求地址、方法、参数放哪里 | where 填 query 走查询串，填 body 走 JSON 体 |
| request.headers / params | 每行一条「名字: 值」 | 值写 `@文件名` 时改读同目录的令牌文件，令牌不进配置 |
| extract.used / limit | 取值表达式 | 如 `data.limits.currentDailyCost`；支持 `a.b[0].c`、`sum:a[].x`，纯数字当固定上限 |
| display.kind / unit | 显示方式 | percent 给占比，amount 只给一个数（余额这种没有上限的用它） |
| baseline / period | 周期增量换算 | 接口只返回历史累计总额时用；周期起点读同目录 `cache.json`，只读不写 |

令牌单独放在和配置同目录的文件里，配置里用 `@文件名` 引用：

```text
quota-panel/
├── config.json
├── cache.json
└── deepseek.token      # 里面只放一串令牌，不要换行以外的其他内容
```

## 运行效果

示例输出（数值仅作演示）：

| 平台 | 类型 | 已用 / 余额 | 上限 | 剩余 | 占比 |
| --- | --- | --- | --- | --- | --- |
| 示例平台 A | 占比 | ✅ 12.40$ | 50.00$ | 37.60$ | 24.8% |
| 示例平台 B | 余额 | ¥ 88.20 | - | - | - |
| 示例平台 C | - | ❌ key 不对或已过期 | - | - | - |

## 边界

- 只读 `cache.json`（周期基线），不写 —— 记账交给宿主程序，两边各记一套会算出两个数
- 60 秒缓存：同一轮对话里连续追问共享一次取数，不会反复打平台接口
- 默认不走系统代理，需要时在平台配置里打开 `use_system_proxy`
- 单个平台失败只影响它自己那一行，不中断整批查询
- 报错信息里的完整地址会被隐去，避免把查询串里的令牌带进对话

## 文件结构

```text
quota-mcp/
├── server.py            # 全部实现，单文件无构建步骤
├── config.example.json  # 配置样例，令牌全部占位
├── requirements.txt     # 不用 uv 时的依赖清单
├── .gitignore           # 挡住 config.json 与 *.token
├── LICENSE
└── README.md
```

## 许可证

MIT
