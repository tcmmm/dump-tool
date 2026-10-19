# -*- coding: utf-8 -*-
"""
dump工具 MCP 服务器入口
========================
此文件仅用于 PyInstaller 打包「dump工具-mcp.exe」(console 版)。
双击直接运行会以 stdio 模式启动 MCP 服务器, 供 AI 客户端(WorkBuddy/Claude 等)拉起调用。

配置示例 (mcp.json):
{
  "mcpServers": {
    "dump-tool": {
      "command": "D:\\\\path\\\\dump工具-mcp.exe"
    }
  }
}
"""
from dump_gui import run_mcp_server

if __name__ == '__main__':
    run_mcp_server()
