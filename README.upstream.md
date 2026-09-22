# Study-Pro-Bridge：本地学习库与网页工作流桥接

该项目连接数学、英语和 408 三套本地学习库与网页审阅流程。它负责按明确范围导出材料、构造网页交接包、检查回包和调用学科已有入口；正式学习事实仍由对应学科流程维护。

## 结构与运行逻辑

- `study_mcp.py`：向客户端提供学习资料读取与定位工具。
- `study_bridge.py`：交接包与回流流程的命令入口。
- `study_publication.py`：完整私库发布、校验和本地归档逻辑。公开仓库不含任何发布数据。
- `study_personalization.py`、`study_english_context.py`：学科上下文适配。
- `bridge_archives.py`、`lesson_export_408.py`：归档和 408 学习材料导出。
- `scripts/`：MCP 连通检查和显式隧道连接。
- `tests/`：源码测试；外部学科依赖需要另行配置。

## 开始使用

```sh
python3 -m venv .venv-study-mcp
.venv-study-mcp/bin/python -m pip install -r requirements-study-mcp.txt
.venv-study-mcp/bin/python study_bridge.py --help
.venv-study-mcp/bin/python study_publication.py --help
```

本地 stdio MCP 的服务入口为 `.venv-study-mcp/bin/python study_mcp.py`。客户端必须自行配置可访问的学科根目录。本仓库不分发官方隧道客户端和运行密钥；`scripts/connect_study_mcp.py` 是有此前提的连接工具。

公开副本默认将完整库发布目标限制为本地，并将远端账号设为占位符。不要把个人材料发布到公开源码仓库。

## 公开范围

这是从本机工作源码整理的公开副本。只包含代码、运行逻辑和必要结构，不包含真实学习记录、完整对话、学习画像、健康记录、原始教材、凭据、浏览器数据或运行数据库。路径中的 `YOUR_USER` 必须按本机环境配置。源码检查不代表已在另一台电脑部署成功；本次发布不启动服务、不写入真实学习库。
