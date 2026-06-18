# Notion MCP 서버 (Streamable HTTP 모드).
# ieum-agent가 stdio(npx) 대신 HTTP로 연결하기 위한 self-host 서비스다.
# Node 런타임을 포함하므로 ieum-agent(python:slim)에는 Node를 설치할 필요가 없다.
FROM node:20-alpine

# 버전 고정 권장: 운영 시 @latest 대신 검증된 버전으로 핀하라 (예: @1.x.x).
RUN npm install -g @notionhq/notion-mcp-server@latest

EXPOSE 3000

# --enable-token-passthrough: 클라이언트가 매 요청 Notion-Token 헤더로 유저별 토큰을 전달한다.
ENTRYPOINT ["npx", "-y", "@notionhq/notion-mcp-server"]
CMD ["--transport", "http", "--port", "3000", "--enable-token-passthrough"]
