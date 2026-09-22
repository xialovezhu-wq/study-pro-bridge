# Study MCP design attribution

The Study Library MCP integrates the read-only part of the Chat On Steroids approach with Study Bridge's existing publication reader. It is a Python adaptation of its bounded original-file reading, explicit partial-result reporting, direct MCP image content and capability separation. It does not import or run the upstream Electron app, terminal tools, browser recorder or agents.

Reference: https://github.com/totec448-spec/chat-on-steroids at b6cdb61224c6673c29c53d68d7d76f33abbc123f (2.0.7), particularly src/main/mcp/tools-core.ts, src/main/codex/read-backend.ts and src/main/codex/view-image.ts.

MIT License

Copyright (c) 2026 Chat On Steroids contributors

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
