# MAIN-only image. An operator must select an exact, previously verified package version.
FROM node:20-alpine

ARG NOTION_MCP_VERSION
RUN printf '%s\n' "$NOTION_MCP_VERSION" \
      | grep -Eq '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' \
    && npm install -g "@notionhq/notion-mcp-server@${NOTION_MCP_VERSION}"

EXPOSE 3000

ENTRYPOINT ["npx", "--no-install", "@notionhq/notion-mcp-server"]
CMD ["--transport", "http", "--host", "0.0.0.0", "--port", "3000", "--enable-token-passthrough"]
