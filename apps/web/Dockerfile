FROM node:24-alpine

RUN corepack enable && corepack prepare pnpm@11.7.0 --activate

WORKDIR /app
COPY package.json ./
RUN pnpm install --no-frozen-lockfile
COPY . .

EXPOSE 5173
CMD ["sh", "-c", "pnpm generate:api && pnpm dev --host 0.0.0.0 --port 5173"]
